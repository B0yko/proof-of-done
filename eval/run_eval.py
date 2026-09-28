#!/usr/bin/env python3
"""Evaluation harness: the numbers the README shows.

Renders the templated set (`fixtures.expand.expand()`), the held-out set (`eval/heldout/*.yaml`)
and the adversarial set (`eval/adversarial/*.yaml`) into a temp dir, parses every rendered
transcript with the Claude Code adapter, evaluates every stop case with
`proof_of_done.engine.decide_stop` -- the same decision function the live hook calls, so the
tamper check, `enabled`, `mode` and the rule actions are applied in the hook's order -- under
the scenario's own config layer/env/project files, and computes the metrics in
`eval/metrics.py`: claim-instance detection P/R/F1, gate P/R/F1 (claim level and turn level,
Wilson 95% intervals, bootstrap F1), false-block rate, per-claim-type tables, a reason confusion
matrix, suggestion accuracy, and adversarial k/M. Every matching of detections to labels is
done per stop case (per message) and only the counts are summed.

A configuration file the session wrote with the `Write` tool is served to the decision function
as it would be on disk at the moment of the stop, so the tamper scenarios really exercise the
tamper protection instead of running on the untouched defaults.

Gate metrics for the templated and held-out sets are computed twice: once under the shipped
defaults ("shipped"), once with every built-in rule's `action` forced to `block`
("forced_block", the "before switch" variant). Held-out also gets a full failure list
(detection misses, wrong verdict, wrong reason, wrong suggestion, prefilter misses).

This step never tunes the detector or the evidence engine and never edits `eval/heldout/*`; a
bug this script surfaces belongs in the failure list, not in a fix made here.

Not part of the installed wheel: run directly as ``python eval/run_eval.py``.
"""

from __future__ import annotations

import argparse
import copy
import glob
import json
import os
import platform
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from eval import metrics  # noqa: E402
from fixtures import dsl, render  # noqa: E402
from fixtures import expand as expand_mod  # noqa: E402
from proof_of_done import claims as claims_mod  # noqa: E402
from proof_of_done import config as config_mod  # noqa: E402
from proof_of_done import engine, evidence  # noqa: E402
from proof_of_done.probe import DictProbe  # noqa: E402
from proof_of_done.transcript import claude_code  # noqa: E402
from proof_of_done.transcript.model import KIND_TOOL_CALL, Session  # noqa: E402

HELDOUT_DIR = os.path.join(_REPO_ROOT, "eval", "heldout")
ADVERSARIAL_DIR = os.path.join(_REPO_ROOT, "eval", "adversarial")
RESULTS_DIR = os.path.join(_REPO_ROOT, "eval", "results")
ALL_SETS = ("templated", "heldout", "adversarial")

# `generated_at`/`git_sha`/`python`/`platform` are the only fields that legitimately differ
# between two runs of the same code against the same fixtures; `--compare` ignores exactly
# these.
_VOLATILE_TOP_KEYS = ("generated_at", "git_sha", "python", "platform")


# --------------------------------------------------------------------------------------
# loading scenarios
# --------------------------------------------------------------------------------------


def _heldout_scenarios() -> list[dict[str, Any]]:
    paths = sorted(glob.glob(os.path.join(HELDOUT_DIR, "*.yaml")))
    return [dsl.load_scenario(p) for p in paths]


def _adversarial_scenarios() -> list[dict[str, Any]]:
    paths = sorted(glob.glob(os.path.join(ADVERSARIAL_DIR, "*.yaml")))
    return [dsl.load_scenario(p) for p in paths]


def _templated_scenarios() -> list[dict[str, Any]]:
    scenarios = expand_mod.expand()
    for scenario in scenarios:
        dsl.validate_scenario(scenario, scenario["id"] + ".yaml")
    return scenarios


_SCENARIO_LOADERS = {
    "templated": _templated_scenarios,
    "heldout": _heldout_scenarios,
    "adversarial": _adversarial_scenarios,
}


@dataclass
class EvalCase:
    set_name: str
    scenario_id: str
    case: Any  # fixtures.render.StopCase


def render_cases(set_names: list[str], out_dir: str) -> list[EvalCase]:
    """Render every scenario of every requested set into `out_dir`; return one `EvalCase` per
    rendered stop case, in a stable order (by set, then scenario id, then turn/attempt)."""
    cases: list[EvalCase] = []
    for set_name in set_names:
        scenarios = _SCENARIO_LOADERS[set_name]()
        for scenario in scenarios:
            scenario_out = os.path.join(out_dir, set_name, scenario["id"])
            rendered = render.render(scenario, scenario_out)
            for stop_case in rendered.stop_cases:
                cases.append(
                    EvalCase(set_name=set_name, scenario_id=scenario["id"], case=stop_case)
                )
    return cases


# --------------------------------------------------------------------------------------
# per-case evaluation: config layering, tamper scan, both action-policy variants
# --------------------------------------------------------------------------------------

# The machine-independent home directory a case is evaluated under: settings-file paths and the
# user config path are derived from it, so a run on another machine gives the same numbers.
EVAL_HOME = "/home/eval-harness"

_MERGED_CONFIGS: dict[Any, config_mod.Config] = {}


def _files_written_by(session: Session, root: str) -> dict[str, str]:
    """The last whole-file content the session wrote to each absolute path with the `Write`
    tool. Other edit forms (`Edit`, `MultiEdit`, shell writes) are not replayed: the harness has
    no base text to apply them to, so a file changed only that way keeps its scenario content.
    """
    written: dict[str, str] = {}
    for step in session.steps:
        if step.kind != KIND_TOOL_CALL or step.name != "Write" or not isinstance(step.args, dict):
            continue
        raw = step.args.get("file_path") or step.args.get("path")
        content = step.args.get("content")
        if not isinstance(raw, str) or not raw or not isinstance(content, str):
            continue
        path = raw if raw.startswith("/") else root.rstrip("/") + "/" + raw
        written[os.path.normpath(path)] = content
    return written


def _disk_reader(
    root: str, scenario_config: dict[str, Any] | None, written: dict[str, str]
) -> Callable[[str], str | None]:
    """A file reader for `config` layer loading over the state the stop would see: the packaged
    defaults, the scenario's project config (JSON is valid YAML), then whatever the session
    wrote on top. No user config, no `PROOF_OF_DONE_CONFIG` file."""
    project_path = os.path.normpath(config_mod.project_config_path(root))

    def read(path: str) -> str | None:
        norm = os.path.normpath(path)
        if norm in written:
            return written[norm]
        if path == config_mod.defaults_path():
            return config_mod.disk_reader(path)
        if norm == project_path and scenario_config is not None:
            return json.dumps(scenario_config)
        return None

    return read


def _pre_tamper_config(
    root: str, env: dict[str, str], read: Callable[[str], str | None]
) -> config_mod.Config:
    """The merged config with the environment overrides applied and no tamper adjustment: what
    the hook loads from its cache. Memoised on the layer texts, because parsing the packaged
    defaults for every stop case would dominate the run time."""
    paths = config_mod.layer_paths_for(root, env)
    key = (
        tuple(read(p) for p in paths[1:]),
        tuple(sorted((k, v) for k, v in env.items() if k.startswith("PROOF_OF_DONE"))),
    )
    cfg = _MERGED_CONFIGS.get(key)
    if cfg is None:
        layers = config_mod.load_layers(root, env, read)
        cfg, _notes = config_mod.effective(
            layers, env, tampered_paths=set(), settings_tampered=False
        )
        _MERGED_CONFIGS[key] = cfg
    return cfg


def _force_built_in_block(cfg: config_mod.Config) -> config_mod.Config:
    data = cfg.to_json()
    for rule in data["rules"]:
        if rule["id"] in config_mod.BUILTIN_RULE_IDS:
            rule["action"] = "block"
    return config_mod.build_config(data)


@dataclass
class CaseEval:
    eval_case: EvalCase
    decision_shipped: engine.Decision
    decision_forced_block: engine.Decision
    prefilter_hit: bool
    cfg_shipped: config_mod.Config  # the merged config before any tamper adjustment


def evaluate_case(eval_case: EvalCase) -> CaseEval:
    case = eval_case.case
    is_subagent = case.agent_transcript_path is not None
    active_transcript = case.agent_transcript_path if is_subagent else case.session_path
    session = claude_code.parse(active_transcript)
    root = case.payload.get("cwd") or session.cwd or dsl.DEFAULT_ROOT
    env = {"HOME": EVAL_HOME, **case.env}

    main_session = claude_code.parse(case.session_path) if is_subagent else session
    read = _disk_reader(root, case.config, _files_written_by(main_session, root))
    cfg = _pre_tamper_config(root, env, read)

    events = evidence.build_events(session, cfg, root, home=EVAL_HOME)
    main_events = (
        events
        if not is_subagent
        else evidence.build_events(main_session, cfg, root, home=EVAL_HOME)
    )
    agent_type = case.payload.get("agent_type") if is_subagent else None

    def _decide(
        adjust: Callable[[config_mod.Config], config_mod.Config] | None,
    ) -> engine.Decision:
        return engine.decide_stop(
            session=session,
            final_message=case.final_message,
            config=cfg,
            project_root=root,
            probe=DictProbe(files=case.project_files),
            env=env,
            read_file=read,
            main_session=main_session,
            events=events,
            main_events=main_events,
            background_tasks=case.payload.get("background_tasks", []),
            is_subagent=is_subagent,
            agent_type=agent_type,
            adjust_config=adjust,
        )

    return CaseEval(
        eval_case=eval_case,
        decision_shipped=_decide(None),
        decision_forced_block=_decide(_force_built_in_block),
        prefilter_hit=claims_mod.prefilter(case.final_message, cfg.keywords()),
        cfg_shipped=cfg,
    )


# --------------------------------------------------------------------------------------
# span/unit building
# --------------------------------------------------------------------------------------


def _decision_kind(decision: engine.Decision) -> str:
    if decision.action == "block":
        return "block"
    if decision.system_message and decision.system_message.startswith("proof-of-done (warn):"):
        return "warn"
    return "allow"


def _label_spans(case: Any) -> list[metrics.Span]:
    return [
        metrics.Span(claim_type=lbl.type, start=lbl.start, end=lbl.end, payload=lbl)
        for lbl in case.labels
    ]


def _detection_spans(decision: engine.Decision) -> list[metrics.Span]:
    return [
        metrics.Span(claim_type=r.claim_type, start=r.span[0], end=r.span[1], payload=r)
        for r in decision.results
    ]


def _claim_level_units(
    evaluations: list[CaseEval], *, forced_block: bool
) -> list[metrics.ConfusionUnit]:
    units: list[metrics.ConfusionUnit] = []
    for ce in evaluations:
        decision = ce.decision_forced_block if forced_block else ce.decision_shipped
        labels = _label_spans(ce.eval_case.case)
        detections = _detection_spans(decision)
        m = metrics.match_claims(labels, detections)
        for label, det in m.pairs:
            lbl = label.payload
            result = det.payload
            truth = lbl.label == "unsupported"
            predicted = not result.verdict.supported
            units.append(metrics.ConfusionUnit(truth, predicted, key=lbl.type))
        for label in m.unmatched_labels:
            lbl = label.payload
            units.append(metrics.ConfusionUnit(lbl.label == "unsupported", False, key=lbl.type))
        for det in m.unmatched_detections:
            result = det.payload
            if not result.verdict.supported:
                units.append(metrics.ConfusionUnit(False, True, key=det.claim_type))
    return units


def _turn_level_units(
    evaluations: list[CaseEval], *, forced_block: bool
) -> list[metrics.ConfusionUnit]:
    units: list[metrics.ConfusionUnit] = []
    for ce in evaluations:
        decision = ce.decision_forced_block if forced_block else ce.decision_shipped
        truth = any(lbl.label == "unsupported" for lbl in ce.eval_case.case.labels)
        predicted = _decision_kind(decision) == "block"
        units.append(metrics.ConfusionUnit(truth, predicted, key=ce.eval_case.scenario_id))
    return units


def _gate_report(evaluations: list[CaseEval], *, forced_block: bool) -> dict[str, Any]:
    claim_units = _claim_level_units(evaluations, forced_block=forced_block)
    turn_units = _turn_level_units(evaluations, forced_block=forced_block)
    by_type: dict[str, list[metrics.ConfusionUnit]] = {}
    for u in claim_units:
        by_type.setdefault(u.key, []).append(u)
    return {
        "claim_level": metrics.gate_summary(claim_units).to_json(),
        "turn_level": metrics.gate_summary(turn_units).to_json(),
        "false_block_rate": metrics.false_block_rate(turn_units),
        "per_claim_type": {
            claim_type: metrics.gate_summary(units).to_json()
            for claim_type, units in sorted(by_type.items())
        },
    }


def _detection_report(evaluations: list[CaseEval]) -> dict[str, Any]:
    labels: list[metrics.Span] = []
    detections: list[metrics.Span] = []
    for ce in evaluations:
        labels.extend(_label_spans(ce.eval_case.case))
        detections.extend(_detection_spans(ce.decision_shipped))
    return metrics.detection_metrics(labels, detections)


def _reason_confusion_matrix(evaluations: list[CaseEval]) -> list[dict[str, Any]]:
    counts: dict[tuple[str, str], int] = {}
    for ce in evaluations:
        labels = _label_spans(ce.eval_case.case)
        detections = _detection_spans(ce.decision_shipped)
        m = metrics.match_claims(labels, detections)
        for label, det in m.pairs:
            lbl = label.payload
            result = det.payload
            if lbl.label != "unsupported" or result.verdict.supported:
                continue
            key = (lbl.reason or "", result.verdict.reason)
            counts[key] = counts.get(key, 0) + 1
    return [
        {"expected": expected, "predicted": predicted, "count": count}
        for (expected, predicted), count in sorted(counts.items())
    ]


def _suggestion_accuracy(evaluations: list[CaseEval]) -> dict[str, Any]:
    correct = 0
    total = 0
    for ce in evaluations:
        labels = _label_spans(ce.eval_case.case)
        detections = _detection_spans(ce.decision_shipped)
        m = metrics.match_claims(labels, detections)
        for label, det in m.pairs:
            lbl = label.payload
            result = det.payload
            if lbl.label != "unsupported" or result.verdict.supported:
                continue
            if result.action != "block":
                continue
            total += 1
            if result.command == lbl.suggest:
                correct += 1
    rate = correct / total if total > 0 else None
    return {"n": total, "correct": correct, "rate": rate}


def _adversarial_report(evaluations: list[CaseEval]) -> dict[str, Any]:
    total = 0
    hits = 0
    misses: list[dict[str, Any]] = []
    for ce in evaluations:
        expect = ce.eval_case.case.expect
        if not expect:
            continue
        total += 1
        decision = ce.decision_shipped
        predicted_decision = _decision_kind(decision)
        ok = predicted_decision == expect.get("decision")
        predicted_reason = next(
            (r.verdict.reason for r in decision.results if not r.verdict.supported), None
        )
        expected_reason = expect.get("reason")
        if ok and expected_reason is not None:
            ok = predicted_reason == expected_reason
        if ok:
            hits += 1
        else:
            misses.append(
                {
                    "scenario_id": ce.eval_case.scenario_id,
                    "expected_decision": expect.get("decision"),
                    "expected_reason": expected_reason,
                    "predicted_decision": predicted_decision,
                    "predicted_reason": predicted_reason,
                }
            )
    misses.sort(key=lambda m: m["scenario_id"])
    return {"k": hits, "m": total, "misses": misses}


def _failure_list(evaluations: list[CaseEval]) -> list[dict[str, Any]]:
    """Held-out only: every detection FN/FP, wrong verdict, wrong reason, wrong
    suggestion and prefilter miss, with category, scenario id, quote and a short explanation."""
    failures: list[dict[str, Any]] = []
    for ce in evaluations:
        case = ce.eval_case.case
        scenario_id = ce.eval_case.scenario_id
        if case.labels and not ce.prefilter_hit:
            failures.append(
                {
                    "category": "prefilter_miss",
                    "scenario_id": scenario_id,
                    "claim_type": ",".join(sorted({lbl.type for lbl in case.labels})),
                    "quote": case.final_message[:200],
                    "explanation": (
                        "no configured keyword appears in the final message; the real hook's "
                        "fast path would have skipped this transcript"
                    ),
                }
            )
        labels = _label_spans(case)
        detections = _detection_spans(ce.decision_shipped)
        m = metrics.match_claims(labels, detections)
        for label in m.unmatched_labels:
            lbl = label.payload
            failures.append(
                {
                    "category": "detection_fn",
                    "scenario_id": scenario_id,
                    "claim_type": lbl.type,
                    "quote": case.final_message[label.start : label.end][:200],
                    "explanation": f"no {lbl.type} detection overlapped this labelled claim",
                }
            )
        for det in m.unmatched_detections:
            result = det.payload
            failures.append(
                {
                    "category": "detection_fp",
                    "scenario_id": scenario_id,
                    "claim_type": result.claim_type,
                    "quote": result.quote[:200],
                    "explanation": (
                        f"a spurious {result.claim_type} claim was detected with no matching label"
                    ),
                }
            )
        for label, det in m.pairs:
            lbl = label.payload
            result = det.payload
            quote = case.final_message[label.start : label.end][:200]
            expected_unsupported = lbl.label == "unsupported"
            predicted_unsupported = not result.verdict.supported
            if expected_unsupported != predicted_unsupported:
                failures.append(
                    {
                        "category": "wrong_verdict",
                        "scenario_id": scenario_id,
                        "claim_type": lbl.type,
                        "quote": quote,
                        "explanation": (
                            f"expected {lbl.label}, engine said "
                            f"{'unsupported' if predicted_unsupported else 'supported'} "
                            f"({result.verdict.reason})"
                        ),
                    }
                )
                continue
            if not expected_unsupported:
                continue
            if lbl.reason is not None and lbl.reason != result.verdict.reason:
                failures.append(
                    {
                        "category": "wrong_reason",
                        "scenario_id": scenario_id,
                        "claim_type": lbl.type,
                        "quote": quote,
                        "explanation": (
                            f"expected reason {lbl.reason!r}, got {result.verdict.reason!r}"
                        ),
                    }
                )
            if lbl.suggest != result.command:
                failures.append(
                    {
                        "category": "wrong_suggestion",
                        "scenario_id": scenario_id,
                        "claim_type": lbl.type,
                        "quote": quote,
                        "explanation": (
                            f"expected suggestion {lbl.suggest!r}, got {result.command!r}"
                        ),
                    }
                )
    failures.sort(key=lambda f: (f["category"], f["scenario_id"], f["quote"]))
    return failures


# --------------------------------------------------------------------------------------
# top-level report assembly
# --------------------------------------------------------------------------------------


def _git_sha() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        return out.stdout.strip()
    except Exception:
        return None


def build_report(set_names: list[str]) -> dict[str, Any]:
    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "git_sha": _git_sha(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "sets": {},
    }

    with tempfile.TemporaryDirectory(prefix="proof-of-done-eval-") as tmp_dir:
        cases = render_cases(set_names, tmp_dir)
        evaluations = [evaluate_case(c) for c in cases]

    by_set: dict[str, list[CaseEval]] = {name: [] for name in set_names}
    for ce in evaluations:
        by_set[ce.eval_case.set_name].append(ce)

    for set_name in set_names:
        set_evals = by_set[set_name]
        scenario_ids = sorted({ce.eval_case.scenario_id for ce in set_evals})
        set_report: dict[str, Any] = {
            "scenarios": len(scenario_ids),
            "stop_cases": len(set_evals),
            "claim_instances": sum(len(ce.eval_case.case.labels) for ce in set_evals),
        }
        if set_name in ("templated", "heldout"):
            set_report["detection"] = _detection_report(set_evals)
            set_report["gate"] = {
                "shipped": _gate_report(set_evals, forced_block=False),
                "forced_block": _gate_report(set_evals, forced_block=True),
            }
            set_report["reason_confusion_matrix"] = _reason_confusion_matrix(set_evals)
            set_report["suggestion_accuracy"] = _suggestion_accuracy(set_evals)
        if set_name == "heldout":
            set_report["failures"] = _failure_list(set_evals)
        if set_name == "adversarial":
            set_report["adversarial"] = _adversarial_report(set_evals)
        report["sets"][set_name] = set_report

    return report


# --------------------------------------------------------------------------------------
# --compare
# --------------------------------------------------------------------------------------


def _strip_volatile(report: dict[str, Any]) -> dict[str, Any]:
    stripped = copy.deepcopy(report)
    for key in _VOLATILE_TOP_KEYS:
        stripped.pop(key, None)
    return stripped


def _diff_keys(a: Any, b: Any, path: str = "") -> list[str]:
    diffs: list[str] = []
    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(set(a) | set(b)):
            diffs.extend(
                _diff_keys(a.get(key, "<missing>"), b.get(key, "<missing>"), f"{path}.{key}")
            )
    elif a != b:
        diffs.append(f"{path}: {a!r} != {b!r}")
    return diffs


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def _default_out_path() -> str:
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return os.path.join(RESULTS_DIR, f"{date}.json")


def _write_report(report: dict[str, Any], path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, sort_keys=True, ensure_ascii=False)
        fh.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="run_eval.py", description="Run the evaluation harness.")
    parser.add_argument(
        "--out", default=None, help="output path (default: eval/results/<date>.json)"
    )
    parser.add_argument(
        "--sets",
        default=",".join(ALL_SETS),
        help="comma-separated subset of templated,heldout,adversarial",
    )
    parser.add_argument(
        "--compare",
        default=None,
        metavar="FILE",
        help=(
            "re-run and exit 1 if any metric differs from FILE "
            "(ignoring generated_at/git_sha/python/platform)"
        ),
    )
    args = parser.parse_args(argv)

    set_names = [s.strip() for s in args.sets.split(",") if s.strip()]
    for name in set_names:
        if name not in ALL_SETS:
            parser.error(f"unknown set {name!r} (expected one of {ALL_SETS})")

    report = build_report(set_names)

    if args.compare:
        with open(args.compare, encoding="utf-8") as fh:
            baseline = json.load(fh)
        diffs = _diff_keys(_strip_volatile(baseline), _strip_volatile(report))
        if diffs:
            print(f"eval/run_eval.py --compare: {len(diffs)} metric(s) differ from {args.compare}:")
            for d in diffs[:50]:
                print(f"  {d}")
            return 1
        print(f"eval/run_eval.py --compare: matches {args.compare}")
        return 0

    out_path = args.out or _default_out_path()
    _write_report(report, out_path)
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
