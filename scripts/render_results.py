#!/usr/bin/env python3
"""Render Markdown result tables into README.md and docs/eval.md.

Reads the latest `eval/results/<date>.json` (from `eval/run_eval.py`) and
`eval/results/latency-<date>.json` (from `eval/latency.py`) and splices generated Markdown
between `<!-- results:NAME:start -->` / `<!-- results:NAME:end -->` marker pairs in both files.
`docs/eval.md` carries every results section's markers (it is the full reference); a section
whose markers are absent from a given file is skipped there rather than failing -- `--check`
only fails when a marker pair *is* present but its content is stale.

Sections: `detection`, `gate-claim`, `gate-turn`, `per-type`, `adversarial`, `suggestion`,
`latency`, `throughput`, `failure-modes`, `history` (README + docs/eval.md), plus `confusion` and
`failures` (docs/eval.md only) and `demo` (README only): the text report of an in-process
`proof-of-done audit --demo` run, so the demo output shown in the README cannot drift from what
the tool prints.

Run with no arguments to (re)write both files in place; `--check` exits 1 if either file's
existing markers are stale, without writing anything.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from typing import Any

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS_DIR = os.path.join(_REPO_ROOT, "eval", "results")
README_PATH = os.path.join(_REPO_ROOT, "README.md")
EVAL_DOC_PATH = os.path.join(_REPO_ROOT, "docs", "eval.md")

_MARKER_RE_TEMPLATE = r"<!-- results:{name}:start -->.*?<!-- results:{name}:end -->"

COMMON_SECTIONS = (
    "detection",
    "gate-claim",
    "gate-turn",
    "per-type",
    "adversarial",
    "suggestion",
    "latency",
    "throughput",
    "failure-modes",
    "history",
    "headline",
)
DOC_ONLY_SECTIONS = ("confusion", "failures")
README_ONLY_SECTIONS = ("demo",)
ALL_SECTIONS = COMMON_SECTIONS + DOC_ONLY_SECTIONS + README_ONLY_SECTIONS


# --------------------------------------------------------------------------------------
# loading the latest results
# --------------------------------------------------------------------------------------


def _latest(pattern: str) -> str | None:
    matches = sorted(glob.glob(os.path.join(RESULTS_DIR, pattern)))
    return matches[-1] if matches else None


def _latest_eval_results() -> dict[str, Any] | None:
    # `YYYY-MM-DD.json`, never the `latency-YYYY-MM-DD.json` files.
    path = _latest("[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].json")
    if path is None:
        return None
    with open(path, encoding="utf-8") as fh:
        data: dict[str, Any] = json.load(fh)
    data["_source_file"] = os.path.basename(path)
    return data


def _latest_latency_results() -> dict[str, Any] | None:
    path = _latest("latency-*.json")
    if path is None:
        return None
    with open(path, encoding="utf-8") as fh:
        data: dict[str, Any] = json.load(fh)
    data["_source_file"] = os.path.basename(path)
    return data


# --------------------------------------------------------------------------------------
# formatting helpers
# --------------------------------------------------------------------------------------


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:.1f}%"


def _ci(interval: list[float] | None) -> str:
    if not interval:
        return "n/a"
    return f"[{interval[0] * 100:.1f}%, {interval[1] * 100:.1f}%]"


def _f1_ci(interval: list[float] | None) -> str:
    if not interval:
        return "n/a"
    return f"[{interval[0]:.3f}, {interval[1]:.3f}]"


def _f1(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.3f}"


# --------------------------------------------------------------------------------------
# table builders
# --------------------------------------------------------------------------------------


def _table(header: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    for row in rows:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _detection_section(report: dict[str, Any]) -> str:
    rows = []
    for set_name in ("templated", "heldout"):
        s = report.get("sets", {}).get(set_name, {})
        d = s.get("detection")
        if not d:
            continue
        rows.append(
            [
                set_name,
                str(d["n_labels"]),
                str(d["n_detections"]),
                str(d["tp"]),
                str(d["fp"]),
                str(d["fn"]),
                _pct(d["precision"]),
                _pct(d["recall"]),
                _f1(d["f1"]),
            ]
        )
    if not rows:
        return "*(no detection results in the latest run)*"
    header = ["set", "N labels", "N detections", "TP", "FP", "FN", "precision", "recall", "F1"]
    return _table(header, rows)


def _gate_rows(report: dict[str, Any], level: str) -> list[list[str]]:
    rows = []
    for set_name in ("templated", "heldout"):
        s = report.get("sets", {}).get(set_name, {})
        gate = s.get("gate")
        if not gate:
            continue
        for variant in ("shipped", "forced_block"):
            g = gate.get(variant, {}).get(level)
            if not g:
                continue
            rows.append(
                [
                    set_name,
                    variant,
                    str(g["n"]),
                    str(g["tp"]),
                    str(g["fp"]),
                    str(g["fn"]),
                    f"{_pct(g['precision'])} {_ci(g.get('precision_wilson95'))}",
                    f"{_pct(g['recall'])} {_ci(g.get('recall_wilson95'))}",
                    f"{_f1(g['f1'])} {_f1_ci(g.get('f1_bootstrap95'))}",
                ]
            )
    return rows


def _gate_claim_section(report: dict[str, Any]) -> str:
    rows = _gate_rows(report, "claim_level")
    if not rows:
        return "*(no gate results in the latest run)*"
    header = [
        "set",
        "config",
        "N",
        "TP",
        "FP",
        "FN",
        "precision (95% CI)",
        "recall (95% CI)",
        "F1 (95% CI)",
    ]
    return _table(header, rows)


def _gate_turn_section(report: dict[str, Any]) -> str:
    rows = _gate_rows(report, "turn_level")
    fbr_rows = []
    for set_name in ("templated", "heldout"):
        s = report.get("sets", {}).get(set_name, {})
        gate = s.get("gate", {}).get("shipped", {})
        fbr = gate.get("false_block_rate")
        if fbr:
            fbr_rows.append(
                [
                    set_name,
                    str(fbr["n"]),
                    str(fbr["blocked"]),
                    _pct(fbr["rate"]),
                    _ci(fbr.get("wilson95")),
                ]
            )
    parts = []
    if rows:
        header = [
            "set",
            "config",
            "N",
            "TP",
            "FP",
            "FN",
            "precision (95% CI)",
            "recall (95% CI)",
            "F1 (95% CI)",
        ]
        parts.append(_table(header, rows))
    if fbr_rows:
        parts.append("")
        parts.append(
            "False-block rate (shipped config; share of no-unsupported-label turns blocked):"
        )
        parts.append(_table(["set", "N", "blocked", "rate", "95% CI"], fbr_rows))
    return "\n".join(parts) if parts else "*(no gate results in the latest run)*"


def _per_type_section(report: dict[str, Any]) -> str:
    rows = []
    heldout_blocks: list[tuple[int, str]] = []
    for set_name in ("templated", "heldout"):
        s = report.get("sets", {}).get(set_name, {})
        per_type = s.get("gate", {}).get("shipped", {}).get("per_claim_type", {})
        for claim_type, g in sorted(per_type.items()):
            predicted_blocks = g["tp"] + g["fp"]
            if set_name == "heldout":
                heldout_blocks.append((predicted_blocks, claim_type))
            rows.append(
                [
                    set_name,
                    claim_type,
                    str(g["n"]),
                    str(predicted_blocks),
                    _pct(g["precision"]),
                    _pct(g["recall"]),
                    _f1(g["f1"]),
                ]
            )
    if not rows:
        return "*(no per-type results in the latest run)*"
    table = _table(
        ["set", "claim type", "N", "predicted blocks", "precision", "recall", "F1"], rows
    )
    if not heldout_blocks:
        return table
    fewest = min(heldout_blocks)
    most = max(heldout_blocks, key=lambda item: (item[0], item[1]))
    note = (
        f"Held-out predicted blocks per claim type range from {fewest[0]} (`{fewest[1]}`) to "
        f"{most[0]} (`{most[1]}`); a precision figure that rests on only a few predicted "
        "blocks is a rough estimate."
    )
    return table + "\n\n" + note


def _adversarial_section(report: dict[str, Any]) -> str:
    adv = report.get("sets", {}).get("adversarial", {}).get("adversarial")
    if not adv:
        return "*(no adversarial results in the latest run)*"
    lines = [f"**{adv['k']} / {adv['m']}** adversarial cases met their labelled outcome.", ""]
    if adv["misses"]:
        lines.append("Known misses:")
        lines.append("")
        lines.append(
            _table(
                [
                    "scenario",
                    "expected decision",
                    "expected reason",
                    "predicted decision",
                    "predicted reason",
                ],
                [
                    [
                        m["scenario_id"],
                        str(m["expected_decision"]),
                        str(m["expected_reason"]),
                        str(m["predicted_decision"]),
                        str(m["predicted_reason"]),
                    ]
                    for m in adv["misses"]
                ],
            )
        )
    else:
        lines.append("No misses.")
    return "\n".join(lines)


def _suggestion_section(report: dict[str, Any]) -> str:
    rows = []
    for set_name in ("templated", "heldout"):
        s = report.get("sets", {}).get(set_name, {})
        acc = s.get("suggestion_accuracy")
        if acc:
            rows.append([set_name, str(acc["n"]), str(acc["correct"]), _pct(acc["rate"])])
    if not rows:
        return "*(no suggestion-accuracy results in the latest run)*"
    return _table(["set", "N correctly blocked", "matching suggestion", "accuracy"], rows)


def _confusion_section(report: dict[str, Any]) -> str:
    matrix = report.get("sets", {}).get("heldout", {}).get("reason_confusion_matrix")
    if not matrix:
        return "*(no held-out reason confusion matrix in the latest run)*"
    rows = [[row["expected"], row["predicted"], str(row["count"])] for row in matrix]
    return _table(["expected reason", "predicted reason", "count"], rows)


def _failures_section(report: dict[str, Any]) -> str:
    failures = report.get("sets", {}).get("heldout", {}).get("failures")
    if not failures:
        return "*(no held-out failures in the latest run)*"
    by_category: dict[str, list[dict[str, Any]]] = {}
    for f in failures:
        by_category.setdefault(f["category"], []).append(f)
    rows = []
    for category, items in sorted(by_category.items()):
        example = items[0]
        rows.append(
            [
                category,
                str(len(items)),
                example["scenario_id"],
                example["quote"],
                example["explanation"],
            ]
        )
    return _table(["category", "count", "example scenario", "example quote", "explanation"], rows)


def _failure_modes_section(report: dict[str, Any]) -> str:
    """Held-out errors grouped by (category, claim type), most frequent first, one example each."""
    failures = report.get("sets", {}).get("heldout", {}).get("failures")
    if not failures:
        return "*(no held-out failures in the latest run)*"
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for f in failures:
        groups.setdefault((f["category"], f.get("claim_type") or "-"), []).append(f)
    ordered = sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    rows = []
    for (category, claim_type), items in ordered:
        example = items[0]
        quote = example["quote"].replace("|", "\\|")
        rows.append(
            [category, claim_type, str(len(items)), f"`{example['scenario_id']}`", f'"{quote}"']
        )
    return _table(["failure mode", "claim type", "count", "example scenario", "example"], rows)


_HISTORY_LABELS = {
    "first-heldout-run": "first held-out run (detector tuned on the templated set only)",
    "after-dev-set-tuning": "after tuning on the development set (frozen labels)",
}


def _history_rows(report: dict[str, Any], label: str) -> list[str]:
    held = report.get("sets", {}).get("heldout", {})
    templ = report.get("sets", {}).get("templated", {})
    det = held.get("detection", {})
    gate = held.get("gate", {}).get("shipped", {})
    claim = gate.get("claim_level", {})
    turn = gate.get("turn_level", {})
    fbr = gate.get("false_block_rate", {})
    tclaim = templ.get("gate", {}).get("shipped", {}).get("claim_level", {})
    return [
        label,
        f"`{str(report.get('git_sha', ''))[:7]}`",
        f"{_pct(det.get('precision'))} / {_pct(det.get('recall'))}",
        f"{_pct(claim.get('precision'))} / {_pct(claim.get('recall'))}",
        f"{_pct(turn.get('precision'))} / {_pct(turn.get('recall'))}",
        _pct(fbr.get("rate")),
        f"{_pct(tclaim.get('precision'))} / {_pct(tclaim.get('recall'))}",
    ]


def _history_section(report: dict[str, Any]) -> str:
    rows = []
    for path in sorted(glob.glob(os.path.join(RESULTS_DIR, "history", "*.json"))):
        name = os.path.basename(path)[len("YYYY-MM-DD-N-") : -len(".json")]
        with open(path, encoding="utf-8") as fh:
            rows.append(_history_rows(json.load(fh), _HISTORY_LABELS.get(name, name)))
    rows.append(_history_rows(report, "current (held-out label corrections in CHANGES.md)"))
    return _table(
        [
            "run",
            "commit",
            "held-out detection P / R",
            "held-out gate claim P / R",
            "held-out gate turn P / R",
            "held-out false-block rate",
            "templated gate claim P / R",
        ],
        rows,
    )


def _demo_section() -> str:
    """The text report of an in-process `audit --demo` run, in a fenced block. Uses the
    packaged defaults and an empty environment, so nothing on the machine can change it."""
    src_dir = os.path.join(_REPO_ROOT, "src")
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)
    from proof_of_done import audit as audit_mod
    from proof_of_done import report as report_mod

    result = audit_mod.run_audit(demo=True, env={})
    text = report_mod.render(result.report, fmt="text").rstrip("\n")
    return "```\n" + text + "\n```"


def _headline_section(
    eval_report: dict[str, Any] | None, latency_report: dict[str, Any] | None
) -> str | None:
    """The README's at-a-glance table: detection and gate P/R on both labelled sets, the
    false-block rate, adversarial k/M, warm-cache hook p95 and audit throughput."""
    if not eval_report:
        return None
    sets = eval_report.get("sets", {})

    def pr(set_name: str, getter: Any) -> str:
        block = getter(sets.get(set_name, {})) or {}
        return f"{_pct(block.get('precision'))} / {_pct(block.get('recall'))}"

    def gate(s: dict[str, Any]) -> Any:
        return s.get("gate", {}).get("shipped", {}).get("claim_level")

    def fbr(set_name: str) -> str:
        rate = sets.get(set_name, {}).get("gate", {}).get("shipped", {}).get("false_block_rate")
        return _pct((rate or {}).get("rate"))

    rows = [
        ["claim detection — precision / recall"]
        + [pr(n, lambda s: s.get("detection")) for n in ("templated", "heldout")],
        ["gate on unsupported claims — precision / recall"]
        + [pr(n, gate) for n in ("templated", "heldout")],
        ["turns blocked without an unsupported claim"] + [fbr(n) for n in ("templated", "heldout")],
    ]
    lines = [_table(["", "templated", "held-out"], rows), ""]
    extras = []
    adv = sets.get("adversarial", {}).get("adversarial") or {}
    if adv.get("m"):
        extras.append(f"**{adv['k']} / {adv['m']}** gaming attempts met their labelled outcome")
    if latency_report:
        cases = latency_report.get("cases", {})
        ten = cases.get("10mb", {})
        sys_p95 = (ten.get("system_python3/warm") or {}).get("p95_ms")
        uv_p95 = (ten.get("uv_python_3_12/warm") or {}).get("p95_ms")
        if sys_p95 is not None and uv_p95 is not None:
            extras.append(
                f"hook p95 on a 10 MB transcript **{sys_p95:.0f} ms** (system Python 3.9) / "
                f"**{uv_p95:.0f} ms** (CPython 3.12)"
            )
        audit = latency_report.get("audit_throughput", {})
        if audit.get("available"):
            extras.append(f"audit **{audit['median_mb_per_s']:.0f} MB/s**")
    if extras:
        lines.append(" · ".join(extras))
    return "\n".join(lines)


_CASE_ORDER = ("fast_path", "1mb", "10mb", "50mb")
_CASE_LABELS = {
    "fast_path": "fast path (no claim)",
    "1mb": "1 MB",
    "10mb": "10 MB",
    "50mb": "50 MB",
}


def _macos_version(raw: str | None) -> str | None:
    if not raw:
        return None
    for line in raw.splitlines():
        if line.startswith("ProductVersion:"):
            return line.split(":", 1)[1].strip()
    return raw.strip()


def _load_range(latency_report: dict[str, Any]) -> str | None:
    values: list[float] = []
    for key in ("load_average_start", "load_average_end"):
        if latency_report.get(key):
            values.append(float(latency_report[key][0]))
    for case_report in latency_report.get("cases", {}).values():
        if isinstance(case_report, dict) and case_report.get("load_average_after"):
            values.append(float(case_report["load_average_after"][0]))
    if not values:
        return None
    return f"{min(values):.1f}–{max(values):.1f}"


def _latency_section(latency_report: dict[str, Any] | None) -> str:
    if not latency_report:
        return "*(no latency results yet -- run `python eval/latency.py --generate --run`)*"
    machine = latency_report.get("machine", {})
    mem = machine.get("hw_memsize")
    mem_gb = f"{int(mem) / 2**30:.0f} GB" if mem and str(mem).isdigit() else "n/a"
    versions = machine.get("python_versions") or {}
    parts = [
        f"Machine: {machine.get('hw_model')}, {machine.get('cpu_brand')}, {mem_gb} RAM, "
        f"macOS {_macos_version(machine.get('macos_version'))}; interpreters: system `python3` "
        f"{versions.get('system_python3')}, uv-managed CPython {versions.get('uv_python_3_12')}; "
        f"measured {latency_report.get('generated_at')}."
    ]
    load = _load_range(latency_report)
    if load:
        parts.append(
            f"1-minute load average during the run: {load} on {machine.get('cpu_count')} cores "
            "(other processes were running)."
        )
    lines = [" ".join(parts), ""]
    rows = []
    cases = latency_report.get("cases", {})
    for case_name in sorted(cases, key=lambda c: (*_CASE_ORDER, c).index(c)):
        case_report = cases[case_name]
        for key in sorted(k for k, v in case_report.items() if isinstance(v, dict)):
            stats = case_report[key]
            rows.append(
                [
                    _CASE_LABELS.get(case_name, case_name),
                    key.replace("system_python3", "system python3")
                    .replace("uv_python_3_12", "uv CPython 3.12")
                    .replace("/", ", ")
                    + " config cache",
                    str(stats["invocations"]),
                    f"{stats['p50_ms']:.1f}",
                    f"{stats['p95_ms']:.1f}",
                    f"{stats['max_ms']:.1f}",
                ]
            )
    if rows:
        lines.append(
            _table(["case", "interpreter, cache", "N", "p50 (ms)", "p95 (ms)", "max (ms)"], rows)
        )
    return "\n".join(lines)


def _throughput_section(latency_report: dict[str, Any] | None) -> str:
    if not latency_report:
        return "*(no throughput results yet -- run `python eval/latency.py --generate --run`)*"
    audit = latency_report.get("audit_throughput", {})
    if not audit.get("available"):
        return f"*(audit throughput unavailable: {audit.get('error')})*"
    return (
        f"`proof-of-done audit` on a {audit['file_mb']:.1f} MB transcript: "
        f"median **{audit['median_mb_per_s']:.1f} MB/s** over {audit['runs']} run(s)."
    )


# Each builder takes `(eval_report, latency_report)` and returns rendered Markdown, or `None`
# when its source report is missing -- `render_sections` then leaves that section's markers
# (and whatever placeholder text they already wrap) untouched, exactly like a marker pair
# `splice()` never found, rather than overwriting a hand-written stub with a "no results"
# placeholder of its own that would make `--check` permanently fail on a fresh checkout.
_SECTION_BUILDERS = {
    "detection": lambda ev, _lat: _detection_section(ev) if ev else None,
    "gate-claim": lambda ev, _lat: _gate_claim_section(ev) if ev else None,
    "gate-turn": lambda ev, _lat: _gate_turn_section(ev) if ev else None,
    "per-type": lambda ev, _lat: _per_type_section(ev) if ev else None,
    "adversarial": lambda ev, _lat: _adversarial_section(ev) if ev else None,
    "suggestion": lambda ev, _lat: _suggestion_section(ev) if ev else None,
    "latency": lambda _ev, lat: _latency_section(lat) if lat else None,
    "throughput": lambda _ev, lat: _throughput_section(lat) if lat else None,
    "confusion": lambda ev, _lat: _confusion_section(ev) if ev else None,
    "failures": lambda ev, _lat: _failures_section(ev) if ev else None,
    "failure-modes": lambda ev, _lat: _failure_modes_section(ev) if ev else None,
    "history": lambda ev, _lat: _history_section(ev) if ev else None,
    "demo": lambda _ev, _lat: _demo_section(),
    "headline": lambda ev, lat: _headline_section(ev, lat),
}


def render_sections(
    eval_report: dict[str, Any] | None, latency_report: dict[str, Any] | None
) -> dict[str, str]:
    """One entry per section whose source report is available; a section with no data source
    yet is omitted entirely, so `splice()` leaves its markers (and whatever placeholder text
    a document already has between them) untouched."""
    rendered = {
        name: builder(eval_report, latency_report) for name, builder in _SECTION_BUILDERS.items()
    }
    return {name: content for name, content in rendered.items() if content is not None}


# --------------------------------------------------------------------------------------
# splicing markers into a document
# --------------------------------------------------------------------------------------


def splice(doc: str, sections: dict[str, str]) -> tuple[str, list[str]]:
    """Replace the content between every `<!-- results:NAME:start -->` /
    `<!-- results:NAME:end -->` pair this `doc` actually contains. A section name with no
    marker pair in `doc` is left untouched. Returns `(new_doc, names_replaced)`."""
    replaced: list[str] = []
    for name, content in sections.items():
        pattern = re.compile(_MARKER_RE_TEMPLATE.format(name=re.escape(name)), re.DOTALL)
        if not pattern.search(doc):
            continue
        start = f"<!-- results:{name}:start -->"
        end = f"<!-- results:{name}:end -->"
        doc = pattern.sub(lambda _m, s=start, e=end, c=content: f"{s}\n{c}\n{e}", doc, count=1)
        replaced.append(name)
    return doc, replaced


def _process_file(path: str, sections: dict[str, str], *, check: bool) -> bool:
    """Returns True if `path` is up to date (or was just rewritten); False if `--check`
    found it stale."""
    if not os.path.exists(path):
        return True
    with open(path, encoding="utf-8") as fh:
        original = fh.read()
    new_doc, _replaced = splice(original, sections)
    if new_doc == original:
        return True
    if check:
        print(f"{path}: results tables are stale; run `python scripts/render_results.py`")
        return False
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(new_doc)
    return True


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if either file's existing markers are stale, instead of rewriting them",
    )
    args = parser.parse_args(argv)

    eval_report = _latest_eval_results()
    latency_report = _latest_latency_results()
    sections = render_sections(eval_report, latency_report)

    ok = True
    for path in (README_PATH, EVAL_DOC_PATH):
        ok = _process_file(path, sections, check=args.check) and ok

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
