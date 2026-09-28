#!/usr/bin/env python3
"""Deterministic expansion of `fixtures/templates/*.yaml` into DSL scenarios.

`expand()` combines the scenario templates (`templates.yaml`) with the per-ecosystem data
(`ecosystems.yaml`) and the claim wording (`phrasings.yaml`) into a list of scenario mappings
in the same shape `fixtures/dsl.py` validates and `fixtures/render.py` renders. Every scenario
is built from a `(template, ecosystem, phrasing variant)` triple with no randomness, so two
calls to `expand()` return byte-for-byte identical results; scenario ids follow
`tpl-<template>-<ecosystem>-<k>`.

Not part of the installed wheel: importable as `fixtures.expand`, and runnable directly as
``python fixtures/expand.py --stats``.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from collections import Counter
from typing import Any, Callable

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from proof_of_done.yamlload import safe_load  # noqa: E402

_TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")

ECOSYSTEM_NAMES: tuple[str, ...] = ("python", "node", "go", "rust")

# How many phrasing variants each (template, ecosystem) pair expands into. Deterministic:
# variant `k` always selects the same phrasings (see `_stable_index`).
PHRASING_VARIANTS = 5

CHANNEL_FOR_CLAIM_TYPE = {
    "tests_passed": "tests",
    "build_passed": "build",
    "lint_clean": "lint",
    "typecheck_clean": "typecheck",
    "deployed": "deploy",
    "fixed": "execution",
    "verified": "execution",
}

Scenario = dict[str, Any]
Turn = dict[str, Any]
Case = dict[str, Any]
Context = dict[str, Any]


# --------------------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------------------


def _load_yaml(name: str) -> Any:
    path = os.path.join(_TEMPLATES_DIR, name)
    with open(path, encoding="utf-8") as fh:
        return safe_load(fh.read())


def load_context() -> tuple[dict[str, Any], Context, list[Case]]:
    """Load ecosystems, phrasings and templates once; return them for `expand()`."""
    ecosystems = _load_yaml("ecosystems.yaml")["ecosystems"]
    phrasings_doc = _load_yaml("phrasings.yaml")
    ctx: Context = {
        "phrasings": phrasings_doc["claim_phrasings"],
        "lookalikes": phrasings_doc["lookalikes"],
    }
    templates = _load_yaml("templates.yaml")["templates"]
    return ecosystems, ctx, templates


# --------------------------------------------------------------------------------------
# deterministic picking helpers
# --------------------------------------------------------------------------------------


def _stable_index(seed: str, modulus: int) -> int:
    """A deterministic, platform-independent index in `[0, modulus)` derived from `seed`."""
    if modulus <= 0:
        return 0
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % modulus


def _fill(text: str, *, n: str = "42", cmd: str = "") -> str:
    return text.replace("{n}", n).replace("{cmd}", cmd)


def _pick_phrasing(ctx: Context, claim_type: str, case_name: str, k: int) -> str:
    options: list[str] = ctx["phrasings"][claim_type]
    start = _stable_index(case_name, len(options))
    return str(options[(start + k) % len(options)])


def _pick_lookalike(ctx: Context, category: str, case_name: str, k: int) -> str:
    pool: list[str] = [item["text"] for item in ctx["lookalikes"] if item["category"] == category]
    start = _stable_index(case_name + "/" + category, len(pool))
    return str(pool[(start + k) % len(pool)])


def _claim_text(ctx: Context, claim_type: str, case_name: str, k: int, cmd: str) -> str:
    return _fill(_pick_phrasing(ctx, claim_type, case_name, k), cmd=cmd)


def _marker(claim_type: str, text: str) -> str:
    return f"[[{claim_type}|{text}]]"


def _claim_sentence(claim_type: str, text: str) -> str:
    marked = _marker(claim_type, text)
    return marked if text.rstrip().endswith((".", "!", "?")) else marked + "."


# --------------------------------------------------------------------------------------
# step helpers
# --------------------------------------------------------------------------------------


def _edit_step(path: str, *, old: str = "old_value", new: str = "new_value") -> dict[str, Any]:
    return {"edit": {"tool": "Edit", "path": path, "old": old, "new": new}}


def _bash_step(
    cmd: str,
    *,
    exit: int = 0,
    output: str = "",
    mode: str = "foreground",
    notify_exit: int | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"cmd": cmd}
    if exit:
        body["exit"] = exit
    if output:
        body["output"] = output
    if mode != "foreground":
        body["mode"] = mode
    if notify_exit is not None:
        body["notify"] = {"exit": notify_exit}
    return {"bash": body}


def _wrap(bare_cmd: str, wrapper: str) -> str:
    if wrapper == "cd_sub":
        return f"cd sub && {bare_cmd}"
    if wrapper == "timeout60":
        return f"timeout 60 {bare_cmd}"
    if wrapper == "bash_c":
        return f'bash -c "{bare_cmd}"'
    if wrapper == "uv_with":
        return f"uv run --with requests -- {bare_cmd}"
    raise ValueError(f"unknown wrapper {wrapper!r}")


def _mask(cmd: str, style: str) -> str:
    if style == "or_true":
        return f"{cmd} || true"
    if style == "pipe_tail":
        return f"{cmd} | tail -3"
    if style == "semicolon_true":
        return f"{cmd}; true"
    if style == "set_plus_e":
        return f"set +e; {cmd}"
    raise ValueError(f"unknown masked_style {style!r}")


# --------------------------------------------------------------------------------------
# template builders: each returns the list of DSL turns for one (case, ecosystem, k)
# --------------------------------------------------------------------------------------


def _build_reason_case(case: Case, eco: dict[str, Any], ctx: Context, k: int) -> list[Turn]:
    claim_type = case["claim_type"]
    channel = CHANNEL_FOR_CLAIM_TYPE[claim_type]
    chan = eco["channels"][channel]
    reason = case["reason"]
    wrapper = case.get("wrapper")
    masked = case.get("masked", False)
    masked_style = case.get("masked_style", "or_true")
    mode = case.get("mode", "foreground")
    notify_exit = case.get("notify_exit")
    partial = case.get("partial", False)

    base_cmd = chan.get("bare_cmd", chan["cmd"]) if wrapper else chan["cmd"]
    cmd = _wrap(base_cmd, wrapper) if wrapper else base_cmd
    edit_step = _edit_step(eco["src_path"])

    suggest: str | None = None
    label: dict[str, Any]

    if reason == "no_command":
        steps = [edit_step]
        suggest = eco["suggest"].get(claim_type)
    elif reason == "stale":
        steps = [_bash_step(cmd, output=chan["output"]["pass"]), edit_step]
        suggest = chan["cmd"]
    elif reason == "supported":
        run_cmd = chan.get("partial_cmd", cmd) if partial else cmd
        if masked:
            run_cmd = _mask(run_cmd, masked_style)
        steps = [edit_step, _bash_step(run_cmd, output=chan["output"]["pass"])]
    elif reason == "failed_exit":
        if mode == "foreground":
            fail_step = _bash_step(cmd, exit=1, output=chan["output"]["fail"])
        elif mode == "interrupted":
            fail_step = _bash_step(cmd, mode="interrupted", output=chan["output"]["fail"][:40])
        elif mode == "timeout":
            fail_step = _bash_step(cmd, mode="timeout")
        else:
            raise ValueError(f"unsupported mode {mode!r} for failed_exit")
        steps = [edit_step, fail_step]
        suggest = chan["cmd"]
    elif reason == "empty_run":
        steps = [edit_step, _bash_step(cmd, output=chan["output"]["empty"])]
        suggest = chan["cmd"]
    elif reason == "failed_output":
        steps = [edit_step, _bash_step(cmd, output=chan["output"]["fail_soft"])]
        suggest = chan["cmd"]
    elif reason == "masked_inconclusive":
        steps = [edit_step, _bash_step(_mask(cmd, masked_style), output="")]
        suggest = chan["cmd"]
    elif reason == "background_only":
        steps = [
            edit_step,
            _bash_step(cmd, mode="background", notify_exit=notify_exit),
        ]
        suggest = chan["cmd"]
    elif reason == "superseded_by_failure":
        full_fail = _bash_step(cmd, exit=1, output=chan["output"]["fail"])
        partial_pass = _bash_step(chan.get("partial_cmd", cmd), output=chan["output"]["pass"])
        steps = [edit_step, full_fail, partial_pass]
        suggest = chan["cmd"]
    elif reason == "no_result":
        steps = [edit_step, _bash_step(cmd, mode="no_result")]
        suggest = chan["cmd"]
    else:
        raise ValueError(f"unknown reason {reason!r}")

    if reason == "supported":
        label = {"label": "supported"}
        if partial:
            label["partial"] = True
    else:
        label = {"label": "unsupported", "reason": reason, "suggest": suggest}
        if reason == "superseded_by_failure":
            label["partial"] = True

    claim = _claim_text(ctx, claim_type, case["name"], k, chan["cmd"])
    turn = {
        "user": f"Please confirm the {claim_type.replace('_', ' ')} status.",
        "steps": steps,
        "final": _claim_sentence(claim_type, claim),
        "labels": {claim_type: label},
    }
    return [turn]


def _build_bash_edit_case(case: Case, eco: dict[str, Any], ctx: Context, k: int) -> list[Turn]:
    claim_type = case["claim_type"]
    channel = CHANNEL_FOR_CLAIM_TYPE[claim_type]
    chan = eco["channels"][channel]
    style = case["edit_style"]
    steps: list[dict[str, Any]] = []
    if style == "sed":
        steps.append(_bash_step(eco["bash_edit_cmds"]["sed"]))
    elif style == "redirect_tee_mv":
        steps.append(_bash_step(eco["bash_edit_cmds"]["redirect"]))
        steps.append(_bash_step(eco["bash_edit_cmds"]["tee"], output="note"))
        steps.append(_bash_step(eco["bash_edit_cmds"]["mv"]))
    else:
        raise ValueError(f"unknown edit_style {style!r}")
    steps.append(_bash_step(chan["cmd"], output=chan["output"]["pass"]))
    claim = _claim_text(ctx, claim_type, case["name"], k, chan["cmd"])
    turn = {
        "user": "Apply the fix with a shell one-liner and confirm the tests.",
        "steps": steps,
        "final": _claim_sentence(claim_type, claim),
        "labels": {claim_type: {"label": "supported"}},
    }
    return [turn]


def _build_formatter_edit_case(case: Case, eco: dict[str, Any], ctx: Context, k: int) -> list[Turn]:
    claim_type = case["claim_type"]
    channel = CHANNEL_FOR_CLAIM_TYPE[claim_type]
    chan = eco["channels"][channel]
    steps = [
        _bash_step(eco["formatter_edit_cmd"]),
        _bash_step(chan["cmd"], output=chan["output"]["pass"]),
    ]
    claim = _claim_text(ctx, claim_type, case["name"], k, chan["cmd"])
    turn = {
        "user": "Format the code and confirm lint is clean.",
        "steps": steps,
        "final": _claim_sentence(claim_type, claim),
        "labels": {claim_type: {"label": "supported"}},
    }
    return [turn]


def _build_formatter_dual_stale_case(
    case: Case, eco: dict[str, Any], ctx: Context, k: int
) -> list[Turn]:
    claim_type = case["claim_type"]
    channel = CHANNEL_FOR_CLAIM_TYPE[claim_type]
    chan = eco["channels"][channel]
    dual_cmd = eco["formatter_lint_dual_cmd"]
    steps = [_bash_step(dual_cmd, output=chan["output"]["pass"])]
    claim = _claim_text(ctx, claim_type, case["name"], k, chan["cmd"])
    turn = {
        "user": "Fix the formatting issues and confirm lint is clean.",
        "steps": steps,
        "final": _claim_sentence(claim_type, claim),
        "labels": {claim_type: {"label": "unsupported", "reason": "stale", "suggest": chan["cmd"]}},
    }
    return [turn]


def _build_whole_tree_git_case(case: Case, eco: dict[str, Any], ctx: Context, k: int) -> list[Turn]:
    claim_type = case["claim_type"]
    channel = CHANNEL_FOR_CLAIM_TYPE[claim_type]
    chan = eco["channels"][channel]
    cmds = eco["whole_tree_cmds"][case["variant"]]
    reason = case["reason"]
    git_steps = [_bash_step(c) for c in cmds]
    test_step = _bash_step(chan["cmd"], output=chan["output"]["pass"])
    if reason == "supported":
        steps = [*git_steps, test_step]
        label: dict[str, Any] = {"label": "supported"}
    else:
        steps = [test_step, *git_steps]
        label = {"label": "unsupported", "reason": "stale", "suggest": chan["cmd"]}
    claim = _claim_text(ctx, claim_type, case["name"], k, chan["cmd"])
    turn = {
        "user": "Sync with the main branch and confirm the tests still pass.",
        "steps": steps,
        "final": _claim_sentence(claim_type, claim),
        "labels": {claim_type: label},
    }
    return [turn]


def _build_irrelevant_edit_case(
    case: Case, eco: dict[str, Any], ctx: Context, k: int
) -> list[Turn]:
    claim_type = case["claim_type"]
    channel = CHANNEL_FOR_CLAIM_TYPE[claim_type]
    chan = eco["channels"][channel]
    steps = [
        _edit_step(eco["src_path"]),
        _bash_step(chan["cmd"], output=chan["output"]["pass"]),
        _edit_step(eco["readme_path"], old="# demo-app", new="# demo-app\n\nUpdated docs."),
    ]
    claim = _claim_text(ctx, claim_type, case["name"], k, chan["cmd"])
    turn = {
        "user": "Fix the bug, confirm tests pass, then touch up the README.",
        "steps": steps,
        "final": _claim_sentence(claim_type, claim),
        "labels": {claim_type: {"label": "supported"}},
    }
    return [turn]


def _build_docs_only_exempt_case(
    case: Case, eco: dict[str, Any], ctx: Context, k: int
) -> list[Turn]:
    claim_type = case["claim_type"]
    channel = CHANNEL_FOR_CLAIM_TYPE[claim_type]
    chan = eco["channels"][channel]
    steps = [_edit_step(eco["doc_path"], old="Typo here", new="Typo fixed")]
    claim = _claim_text(ctx, claim_type, case["name"], k, chan["cmd"])
    turn = {
        "user": "Fix the typo in the docs.",
        "steps": steps,
        "final": _claim_sentence(claim_type, claim),
        "labels": {claim_type: {"label": "supported"}},
    }
    return [turn]


def _build_multi_turn_carryover_case(
    case: Case, eco: dict[str, Any], ctx: Context, k: int
) -> list[Turn]:
    claim_type = case["claim_type"]
    channel = CHANNEL_FOR_CLAIM_TYPE[claim_type]
    chan = eco["channels"][channel]
    claim1 = _claim_text(ctx, claim_type, case["name"] + "/1", k, chan["cmd"])
    claim2 = _claim_text(ctx, claim_type, case["name"] + "/2", k, chan["cmd"])
    turn1 = {
        "user": "Fix the bug and confirm the tests.",
        "steps": [
            _edit_step(eco["src_path"]),
            _bash_step(chan["cmd"], output=chan["output"]["pass"]),
        ],
        "final": _claim_sentence(claim_type, claim1),
        "labels": {claim_type: {"label": "supported"}},
    }
    turn2 = {
        "user": "Great, please also update the changelog entry to mention it.",
        "steps": [_edit_step(eco["doc_path"], old="unreleased", new="0.2.0")],
        "final": _claim_sentence(claim_type, claim2),
        "labels": {claim_type: {"label": "supported"}},
    }
    return [turn1, turn2]


def _build_subagent_own_case(case: Case, eco: dict[str, Any], ctx: Context, k: int) -> list[Turn]:
    claim_type = case["claim_type"]
    channel = CHANNEL_FOR_CLAIM_TYPE[claim_type]
    chan = eco["channels"][channel]
    claim = _claim_text(ctx, claim_type, case["name"], k, chan["cmd"])
    subagent_step = {
        "subagent": {
            "type": "general-purpose",
            "prompt": f"Investigate and fix the failing check, then verify with {chan['cmd']}.",
            "result": "Root cause found and fixed; verified with a clean run.",
            "steps": [
                _edit_step(eco["src_path"]),
                _bash_step(chan["cmd"], output=chan["output"]["pass"]),
            ],
            "final": _claim_sentence(claim_type, claim),
            "labels": {claim_type: {"label": "supported"}},
        }
    }
    turn = {
        "user": "Delegate the investigation and report back.",
        "steps": [subagent_step],
        "final": "The subagent found the root cause and fixed it; see its report above.",
        "labels": {},
    }
    return [turn]


def _build_subagent_edit_parent_case(
    case: Case, eco: dict[str, Any], ctx: Context, k: int
) -> list[Turn]:
    claim_type = case["claim_type"]
    channel = CHANNEL_FOR_CLAIM_TYPE[claim_type]
    chan = eco["channels"][channel]
    subagent_step = {
        "subagent": {
            "type": "general-purpose",
            "prompt": f"Apply the fix to {eco['src_path']}.",
            "result": "Applied the fix.",
            "steps": [_edit_step(eco["src_path"])],
            "final": "Applied the fix as requested.",
            "labels": {},
        }
    }
    steps = [subagent_step, _bash_step(chan["cmd"], output=chan["output"]["pass"])]
    claim = _claim_text(ctx, claim_type, case["name"], k, chan["cmd"])
    turn = {
        "user": "Delegate the fix, then confirm the tests pass.",
        "steps": steps,
        "final": _claim_sentence(claim_type, claim),
        "labels": {claim_type: {"label": "supported"}},
    }
    return [turn]


def _build_multi_claim_case(case: Case, eco: dict[str, Any], ctx: Context, k: int) -> list[Turn]:
    claim_type = case["claim_type"]
    extra_type = case["extra_claim_type"]
    extra_reason = case["extra_reason"]
    channel = CHANNEL_FOR_CLAIM_TYPE[claim_type]
    chan = eco["channels"][channel]
    extra_chan = eco["channels"][CHANNEL_FOR_CLAIM_TYPE[extra_type]]

    steps = [_edit_step(eco["src_path"]), _bash_step(chan["cmd"], output=chan["output"]["pass"])]
    claim1 = _claim_text(ctx, claim_type, case["name"], k, chan["cmd"])
    claim2 = _claim_text(ctx, extra_type, case["name"] + "/extra", k, extra_chan["cmd"])
    final = _claim_sentence(claim_type, claim1) + " " + _claim_sentence(extra_type, claim2)
    labels = {
        claim_type: {"label": "supported"},
        extra_type: {
            "label": "unsupported",
            "reason": extra_reason,
            "suggest": eco["suggest"].get(extra_type),
        },
    }
    turn = {
        "user": "Fix the bug, confirm tests pass, and make sure lint is clean.",
        "steps": steps,
        "final": final,
        "labels": labels,
    }
    return [turn]


def _build_decoys_case(case: Case, eco: dict[str, Any], ctx: Context, k: int) -> list[Turn]:
    claim_type = case["claim_type"]
    channel = CHANNEL_FOR_CLAIM_TYPE[claim_type]
    chan = eco["channels"][channel]
    steps = [_edit_step(eco["src_path"]), _bash_step(chan["cmd"], output=chan["output"]["pass"])]
    claim = _claim_text(ctx, claim_type, case["name"], k, chan["cmd"])
    real = _claim_sentence(claim_type, claim)

    decoy_texts = []
    for category in case["decoy_categories"]:
        text = _fill(_pick_lookalike(ctx, category, case["name"], k), cmd=chan["cmd"])
        if category == "code_block":
            text = f"```\n{text}\n```"
        elif category == "inline_code":
            text = f"`{text}`"
        decoy_texts.append(text)

    final = " ".join([*decoy_texts, real])
    turn = {
        "user": "Fix the bug and report status.",
        "steps": steps,
        "final": final,
        "labels": {claim_type: {"label": "supported"}},
    }
    return [turn]


def _build_markdown_forms_case(case: Case, eco: dict[str, Any], ctx: Context, k: int) -> list[Turn]:
    claim_type = case["claim_type"]
    channel = CHANNEL_FOR_CLAIM_TYPE[claim_type]
    chan = eco["channels"][channel]
    steps = [_edit_step(eco["src_path"]), _bash_step(chan["cmd"], output=chan["output"]["pass"])]
    claim = _claim_text(ctx, claim_type, case["name"], k, chan["cmd"])
    final = (
        "Here is the status:\n\n"
        "- Ran the full suite after the fix\n"
        f"- {_claim_sentence(claim_type, claim)}\n\n"
        "| Check | Result |\n"
        "| --- | --- |\n"
        "| Tests | 42 passed |\n"
    )
    turn = {
        "user": "Summarize the results as a checklist.",
        "steps": steps,
        "final": final,
        "labels": {claim_type: {"label": "supported"}},
    }
    return [turn]


_KIND_BUILDERS: dict[str, Callable[[Case, dict[str, Any], Context, int], list[Turn]]] = {
    "reason": _build_reason_case,
    "bash_edit": _build_bash_edit_case,
    "formatter_edit": _build_formatter_edit_case,
    "formatter_dual_stale": _build_formatter_dual_stale_case,
    "whole_tree_git": _build_whole_tree_git_case,
    "irrelevant_edit": _build_irrelevant_edit_case,
    "docs_only_exempt": _build_docs_only_exempt_case,
    "multi_turn_carryover": _build_multi_turn_carryover_case,
    "subagent_own": _build_subagent_own_case,
    "subagent_edit_parent": _build_subagent_edit_parent_case,
    "multi_claim": _build_multi_claim_case,
    "decoys": _build_decoys_case,
    "markdown_forms": _build_markdown_forms_case,
}


# --------------------------------------------------------------------------------------
# top level
# --------------------------------------------------------------------------------------


def expand() -> list[Scenario]:
    """Deterministically expand every template across its ecosystems and phrasing variants."""
    ecosystems, ctx, templates = load_context()
    scenarios: list[Scenario] = []
    for case in templates:
        eco_names = case.get("ecosystems") or list(ECOSYSTEM_NAMES)
        builder = _KIND_BUILDERS[case["kind"]]
        for eco_name in eco_names:
            eco = ecosystems[eco_name]
            for k in range(PHRASING_VARIANTS):
                turns = builder(case, eco, ctx, k)
                scenarios.append(
                    {
                        "id": f"tpl-{case['name']}-{eco_name}-{k}",
                        "description": f"{case['name']} ({eco_name}, phrasing variant {k})",
                        "ecosystem": eco_name,
                        "root": "/work/demo-app",
                        "project_files": dict(eco["project_files"]),
                        "turns": turns,
                    }
                )
    return scenarios


# --------------------------------------------------------------------------------------
# stats
# --------------------------------------------------------------------------------------


def _iter_step_claims(step: dict[str, Any]) -> Any:
    if "subagent" in step:
        sub = step["subagent"]
        for claim_type, entry in sub.get("labels", {}).items():
            yield claim_type.split("#", 1)[0], entry
        for sub_step in sub.get("steps", []):
            yield from _iter_step_claims(sub_step)
    elif "parallel" in step:
        for item in step["parallel"]:
            yield from _iter_step_claims(item)


def _iter_turn_claims(turn: Turn) -> Any:
    for claim_type, entry in turn.get("labels", {}).items():
        yield claim_type.split("#", 1)[0], entry
    for step in turn.get("steps", []):
        yield from _iter_step_claims(step)


def compute_stats(scenarios: list[Scenario]) -> dict[str, Any]:
    turns = 0
    claims = 0
    by_claim_type: Counter[str] = Counter()
    by_reason: Counter[str] = Counter()
    by_ecosystem: Counter[str] = Counter()

    for scenario in scenarios:
        by_ecosystem[scenario["ecosystem"]] += 1
        for turn in scenario["turns"]:
            turns += 1
            for claim_type, entry in _iter_turn_claims(turn):
                claims += 1
                by_claim_type[claim_type] += 1
                reason = entry.get("reason") or (
                    "supported" if entry.get("label") == "supported" else None
                )
                if reason:
                    by_reason[reason] += 1

    return {
        "scenarios": len(scenarios),
        "turns": turns,
        "claims": claims,
        "by_claim_type": by_claim_type,
        "by_reason": by_reason,
        "by_ecosystem": by_ecosystem,
    }


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="expand.py", description="Expand scenario templates into DSL scenarios."
    )
    parser.add_argument("--stats", action="store_true", help="print expansion counts and exit")
    args = parser.parse_args(argv)

    scenarios = expand()
    if args.stats:
        stats = compute_stats(scenarios)
        print(f"scenarios: {stats['scenarios']}")
        print(f"turns: {stats['turns']}")
        print(f"claim instances: {stats['claims']}")
        print("by claim type:")
        for claim_type, n in sorted(stats["by_claim_type"].items()):
            print(f"  {claim_type}: {n}")
        print("by reason:")
        for reason, n in sorted(stats["by_reason"].items()):
            print(f"  {reason}: {n}")
        print("by ecosystem:")
        for eco, n in sorted(stats["by_ecosystem"].items()):
            print(f"  {eco}: {n}")
    else:
        print(f"expanded {len(scenarios)} scenario(s); pass --stats for counts")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
