"""Suggested ``Run:`` command for one unsupported claim.

Three sources, tried in order, the first hit wins:

1. the most recent command segment the agent itself ran in this session (any outcome --
   foreground or background, failed, interrupted, timed out, or backgrounded -- before the
   stop) that matches the deciding rule's evidence, is not disqualified, and is not a
   read-only command; a non-partial match beats a partial one regardless of recency;
2. the deciding rule's configured ``suggest`` string;
3. project-ecosystem detection through an injectable :class:`~proof_of_done.probe.FileProbe`.

``suggest`` takes a ``stop_index`` cutoff in addition to ``claim_type, verdict, events,
config`` and ``probe``. :func:`proof_of_done.evidence.build_events`
builds one :class:`~proof_of_done.evidence.EventList` per whole session, and
:func:`proof_of_done.evidence.judge` is called once per stop attempt against that same shared
event list plus an explicit ``stop_index`` (events are built once per session; judging N
claims does not rescan the transcript N times), precisely so a multi-attempt session's later
attempts never leak into an earlier attempt's verdict. Source 1 above re-scans
`events.commands` the same way `judge` does, so it needs the identical cutoff for the identical
reason; a session-wide `events` object with no cutoff would let a later retry's own commands
"suggest" a fix for an earlier attempt that could not have run them yet.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence

from proof_of_done import evidence, shell
from proof_of_done.config import Config, Rule
from proof_of_done.probe import FileProbe

# claim_type/rule_id -> the project-detection "action kind": `deploy` has
# no detection, and `fixed`/`verified` reuse the `tests` detection.
_ACTION_KIND_BY_RULE_ID: dict[str, str | None] = {
    "tests": "tests",
    "build": "build",
    "lint": "lint",
    "typecheck": "typecheck",
    "deploy": None,
    "fixed": "tests",
    "verified": "tests",
}
_ACTION_KIND_BY_CLAIM_TYPE: dict[str, str | None] = {
    "tests_passed": "tests",
    "build_passed": "build",
    "lint_clean": "lint",
    "typecheck_clean": "typecheck",
    "deployed": None,
    "fixed": "tests",
    "verified": "tests",
}

_PY_COMMANDS = {
    "tests": "pytest",
    "lint": "ruff check .",
    "typecheck": "mypy .",
    "build": "python -m build",
}
_GO_COMMANDS = {
    "tests": "go test ./...",
    "lint": "go vet ./...",
    "typecheck": "go vet ./...",
    "build": "go build ./...",
}
_RUST_COMMANDS = {
    "tests": "cargo test",
    "lint": "cargo clippy",
    "typecheck": "cargo check",
    "build": "cargo build",
}
# Node `package.json` script names to look for, per action kind; typecheck accepts aliases.
_NODE_SCRIPT_CANDIDATES: dict[str, tuple[str, ...]] = {
    "tests": ("test",),
    "lint": ("lint",),
    "build": ("build",),
    "typecheck": ("typecheck", "type-check", "tsc"),
}
_MAKEFILE_TARGETS = {"tests": "test", "lint": "lint", "typecheck": "typecheck", "build": "build"}


def _find_rule(config: Config, rule_id: str | None) -> Rule | None:
    if rule_id is None:
        return None
    for rule in config.rules:
        if rule.id == rule_id:
            return rule
    return None


def _own_command(
    rule: Rule,
    config: Config,
    events: evidence.EventList,
    stop_index: int,
    segments: Sequence[tuple[evidence.Pos, shell.Segment, evidence.CommandEvent]] | None = None,
) -> str | None:
    """Source 1: the agent's own most recent matching command. `segments` is
    `evidence.iter_segments` of `events.commands` filtered to `step < stop_index`, prebuilt by
    a caller that already has it (`engine.evaluate_stop`, which builds it once for
    `evidence.judge` too, so an unsupported claim's own suggestion does not re-walk and
    re-match every segment from scratch); left `None` to build it here."""
    if segments is None:
        commands = [c for c in events.commands if c.step < stop_index]
        segments = evidence.iter_segments(commands)
    prefixes = evidence.rule_commands(rule, config)
    cmd_index = shell.build_prefix_index(prefixes)
    read_only_index = shell.build_prefix_index(config.read_only_commands)
    ev = rule.evidence
    best_full: tuple[evidence.Pos, str] | None = None
    best_partial: tuple[evidence.Pos, str] | None = None
    for pos, seg, ce in segments:
        if not evidence.segment_qualifies_indexed(
            seg, ce.cmd, cmd_index, ev.command_regex, ev.exclude_args, read_only_index
        ):
            continue
        if shell.is_partial(seg, ev.partial_args):
            if best_partial is None or pos > best_partial[0]:
                best_partial = (pos, seg.display)
        elif best_full is None or pos > best_full[0]:
            best_full = (pos, seg.display)
    if best_full is not None:
        return best_full[1]
    if best_partial is not None:
        return best_partial[1]
    return None


def _action_kind(rule: Rule | None, claim_type: str) -> str | None:
    if rule is not None and rule.id in _ACTION_KIND_BY_RULE_ID:
        return _ACTION_KIND_BY_RULE_ID[rule.id]
    return _ACTION_KIND_BY_CLAIM_TYPE.get(claim_type)


def _detect_node(action_kind: str, probe: FileProbe) -> str | None:
    text = probe.read_text("package.json")
    if text is None:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    scripts = data.get("scripts") if isinstance(data, dict) else None
    if not isinstance(scripts, dict):
        return None
    script = next((name for name in _NODE_SCRIPT_CANDIDATES[action_kind] if name in scripts), None)
    if script is None:
        return None
    if probe.exists("pnpm-lock.yaml"):
        return "pnpm test" if action_kind == "tests" else f"pnpm run {script}"
    if probe.exists("yarn.lock"):
        return "yarn test" if action_kind == "tests" else f"yarn {script}"
    return "npm test" if action_kind == "tests" else f"npm run {script}"


def _detect_makefile(action_kind: str, probe: FileProbe) -> str | None:
    target = _MAKEFILE_TARGETS[action_kind]
    text = probe.read_text("Makefile")
    if text is None:
        text = probe.read_text("makefile")
    if text is None:
        return None
    if re.search(rf"(?m)^{re.escape(target)}\s*:", text) is None:
        return None
    return f"make {target}"


def detect_project_command(action_kind: str | None, probe: FileProbe) -> str | None:
    """Source 3: cheap, file-based project-ecosystem detection, first hit wins.

    `action_kind` is one of ``tests``/``lint``/``build``/``typecheck``; any other value
    (``None`` for ``deployed``, or an unrecognized custom claim type) has no detection.
    """
    if action_kind not in ("tests", "lint", "build", "typecheck"):
        return None
    if probe.exists("pyproject.toml") or probe.exists("pytest.ini"):
        command = _PY_COMMANDS[action_kind]
        if probe.exists("uv.lock"):
            return "uv build" if action_kind == "build" else f"uv run {command}"
        return command
    if probe.exists("package.json"):
        node_command = _detect_node(action_kind, probe)
        if node_command is not None:
            return node_command
    if probe.exists("go.mod"):
        return _GO_COMMANDS[action_kind]
    if probe.exists("Cargo.toml"):
        return _RUST_COMMANDS[action_kind]
    return _detect_makefile(action_kind, probe)


def suggest(
    claim_type: str,
    verdict: evidence.Verdict,
    events: evidence.EventList,
    config: Config,
    probe: FileProbe,
    stop_index: int,
    segments: Sequence[tuple[evidence.Pos, shell.Segment, evidence.CommandEvent]] | None = None,
) -> str | None:
    """The suggested ``Run:`` command for one unsupported claim. `stop_index`
    bounds "in this session" the same way it bounds `evidence.judge` -- see the module
    docstring. `segments` is passed straight through to `_own_command`."""
    rule = _find_rule(config, verdict.rule_id)
    if rule is not None:
        own = _own_command(rule, config, events, stop_index, segments)
        if own is not None:
            return own
        if rule.suggest is not None:
            return rule.suggest
    return detect_project_command(_action_kind(rule, claim_type), probe)


__all__ = ["detect_project_command", "suggest"]
