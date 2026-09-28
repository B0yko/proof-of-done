"""Evidence engine: turns a parsed session into an ordered event list, then judges whether a
claim type is supported by that session, per PLAN §6.

``build_events`` walks a session's steps once, producing every edit, command and subagent-call
event in step order. ``judge`` is called once per claim instance (possibly several times per
session, one per stop attempt and claim type); it never re-parses a Bash command or re-walks the
step list, only the already-built :class:`EventList` and the config's already-compiled regexes.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from proof_of_done import paths, shell
from proof_of_done.config import Config, EvidenceSpec, Rule
from proof_of_done.transcript.model import (
    ENTRY_TASK_NOTIFICATION,
    KIND_MESSAGE,
    KIND_TOOL_CALL,
    KIND_TOOL_RESULT,
    ROLE_AGENT,
    ROLE_ENVIRONMENT,
    Session,
    Step,
)

# A position within a session: (step index, segment index, phase). Tool edits use (step, 0, 1);
# a Bash segment's own "did it match evidence" position also uses phase 1, so a segment that is
# both the anchor edit and the only matching command lands exactly on the anchor (not strictly
# after it) -- see the `stale` note at the end of PLAN §15.
Pos = tuple[int, int, int]

_CLOSED_REASONS = frozenset(
    {
        "supported",
        "no_command",
        "stale",
        "failed_exit",
        "failed_output",
        "empty_run",
        "masked_inconclusive",
        "background_only",
        "superseded_by_failure",
        "no_result",
    }
)


@dataclass
class EditEvent:
    """One file (or whole-tree, or directory-prefix) change, at a definite session position."""

    step: int
    pos: Pos
    abs_path: str | None  # absolute path; None for a whole-tree event
    rel_path: str | None  # project-relative POSIX path; None when outside the root or whole-tree
    is_dir: bool  # `abs_path`/`rel_path` are a directory prefix, not an exact file
    whole_tree: bool
    source: str  # tool | bash | formatter | tree | subagent
    # Kept only for the (future) settings-tamper scan: the text written by an Edit/Write/
    # MultiEdit call, or the raw command text of a Bash call. None for NotebookEdit, formatter
    # and tree-command edits (never plausible tamper sources).
    tamper_text: str | None = None
    tamper_abs_path: str | None = None


@dataclass
class CommandEvent:
    """One foreground-or-background Bash tool call and its (possibly missing) result."""

    step: int
    result_step: int | None
    tool_use_id: str | None
    cmd: shell.ParsedCommand
    background: bool
    interrupted: bool
    timed_out: bool
    no_result: bool
    ok: bool | None
    exit_code: int | None
    output_text: str
    error: str | None


@dataclass
class SubagentCall:
    step: int
    agent_type: str | None


@dataclass
class EventList:
    edits: list[EditEvent] = field(default_factory=list)
    commands: list[CommandEvent] = field(default_factory=list)
    subagent_calls: list[SubagentCall] = field(default_factory=list)
    # tool_use_id -> step index of the first `task_notification` environment entry resolving a
    # backgrounded call. Used only for the best-effort `background_running` detail below: a
    # caller that has the Stop payload's own `background_tasks` list has more authoritative
    # information than this transcript-derived guess and should prefer it.
    task_notifications: dict[str, int] = field(default_factory=dict)


@dataclass
class VerdictDetails:
    """Everything `message.py` needs to render one reason phrase, per PLAN §8."""

    candidate_display: str | None = None
    candidate_step: int | None = None
    exit_code: int | None = None
    interrupted: bool = False
    timed_out: bool = False
    matched_text: str | None = None  # the fail/empty output pattern's matched text
    anchor_path: str | None = None  # None for a whole-tree/subagent anchor, or no anchor at all
    anchor_step: int | None = None
    background_running: bool = False
    # `superseded_by_failure` only: the latest non-partial candidate after the anchor (and
    # before the reported partial candidate) whose own run failed.
    full_run_display: str | None = None
    full_run_step: int | None = None


@dataclass
class Verdict:
    claim_type: str
    supported: bool
    reason: str
    partial: bool
    exempt: bool
    action: str  # block | warn
    rule_id: str | None
    details: VerdictDetails = field(default_factory=VerdictDetails)


# --------------------------------------------------------------------------------------------
# build_events
# --------------------------------------------------------------------------------------------


def _arg_str(args: Any, *keys: str) -> str | None:
    if not isinstance(args, dict):
        return None
    for key in keys:
        value = args.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _resolve_edit_path(raw: str, project_root: str) -> str:
    if raw.startswith("/"):
        return raw
    return project_root.rstrip("/") + "/" + raw


def _multi_edit_tamper_text(args: dict[str, Any]) -> str | None:
    edits_list = args.get("edits")
    if not isinstance(edits_list, list):
        return None
    parts = [
        entry["new_string"]
        for entry in edits_list
        if isinstance(entry, dict) and isinstance(entry.get("new_string"), str)
    ]
    return "\n".join(parts) if parts else None


def _tool_edit_event(step: Step, project_root: str) -> EditEvent | None:
    args = step.args if isinstance(step.args, dict) else {}
    name = step.name
    if name == "NotebookEdit":
        raw_path = _arg_str(args, "notebook_path", "file_path")
        tamper_text = None
    else:
        raw_path = _arg_str(args, "file_path", "path")
        if name == "Edit":
            new_string = args.get("new_string")
            tamper_text = new_string if isinstance(new_string, str) else None
        elif name == "Write":
            content = args.get("content")
            tamper_text = content if isinstance(content, str) else None
        elif name == "MultiEdit":
            tamper_text = _multi_edit_tamper_text(args)
        else:
            tamper_text = None
    if not raw_path:
        return None
    abs_path = _resolve_edit_path(raw_path, project_root)
    rel_path = paths.to_rel(abs_path, project_root)
    return EditEvent(
        step=step.i,
        pos=(step.i, 0, 1),
        abs_path=abs_path,
        rel_path=rel_path,
        is_dir=False,
        whole_tree=False,
        source="tool",
        tamper_text=tamper_text,
        tamper_abs_path=abs_path,
    )


def _build_command_event(
    step: Step,
    result: Step | None,
    base_cwd: str | None,
    on_parse_error: Callable[[int, str], None] | None,
) -> CommandEvent | None:
    cmd_text = _arg_str(step.args, "command")
    if cmd_text is None:
        return None
    try:
        parsed = shell.parse_command(cmd_text, base_cwd)
    except shell.ParseError as exc:
        if on_parse_error is not None:
            on_parse_error(step.i, str(exc))
        return None
    explicit_background = bool(isinstance(step.args, dict) and step.args.get("run_in_background"))
    if result is None:
        return CommandEvent(
            step=step.i,
            result_step=None,
            tool_use_id=step.tool_use_id,
            cmd=parsed,
            background=explicit_background,
            interrupted=False,
            timed_out=False,
            no_result=True,
            ok=None,
            exit_code=None,
            output_text="",
            error=None,
        )
    return CommandEvent(
        step=step.i,
        result_step=result.i,
        tool_use_id=step.tool_use_id,
        cmd=parsed,
        background=result.background or explicit_background,
        interrupted=result.interrupted,
        timed_out=result.timed_out,
        no_result=False,
        ok=result.ok,
        exit_code=result.exit_code,
        output_text=result.output_text or "",
        error=result.error,
    )


def _bash_edit_events(
    ce: CommandEvent, project_root: str, config: Config, base_cwd: str | None
) -> list[EditEvent]:
    # `shell.bash_edits` always runs its own hardcoded per-program table first (`sed -i`,
    # `mv`, `git checkout`, `tar -x`, ...) *unconditionally*, then separately checks the
    # `tree_commands`/`bash_writes` prefix lists as a generic fallback for programs that
    # table does not know about. `config.edits.bash_writes`/`tree_commands` in defaults.yaml
    # document that same built-in table verbatim (see its comments), so passing them through
    # here would run the generic fallback *again* on top of the hardcoded result for every
    # built-in program -- duplicate `EditEvent`s at best, and a bogus extra target at worst
    # (the generic fallback treats every non-flag positional as a target, so `sed -i` would
    # also report its own script argument as an edited "path"). Only `formatters` is safe to
    # forward as-is: `shell._formatter_match` checks the built-in table first and only
    # consults its `extra` argument when nothing there matched, so it never double-fires.
    targets = shell.bash_edits(
        ce.cmd,
        base_cwd,
        formatters=config.edits.formatters,
    )
    out: list[EditEvent] = []
    for target in targets:
        if target.whole_tree:
            out.append(
                EditEvent(
                    step=ce.step,
                    pos=(ce.step, target.seg_index, target.phase),
                    abs_path=None,
                    rel_path=None,
                    is_dir=True,
                    whole_tree=True,
                    source=target.source,
                    tamper_text=ce.cmd.raw,
                    tamper_abs_path=None,
                )
            )
            continue
        abs_path = target.path
        rel_path = paths.to_rel(abs_path, project_root) if abs_path is not None else None
        out.append(
            EditEvent(
                step=ce.step,
                pos=(ce.step, target.seg_index, target.phase),
                abs_path=abs_path,
                rel_path=rel_path,
                is_dir=target.is_dir,
                whole_tree=False,
                source=target.source,
                tamper_text=ce.cmd.raw,
                tamper_abs_path=abs_path,
            )
        )
    return out


def build_events(
    session: Session,
    config: Config,
    project_root: str,
    *,
    on_parse_error: Callable[[int, str], None] | None = None,
) -> EventList:
    """Walk `session.steps` once, in order, producing every edit/command/subagent-call event.

    `on_parse_error(step_i, message)` is called for a Bash call `shell.parse_command` cannot
    tokenize; that call yields no candidate and no bash-derived edits, but parsing continues.
    """
    edits: list[EditEvent] = []
    commands: list[CommandEvent] = []
    subagent_calls: list[SubagentCall] = []
    task_notifications: dict[str, int] = {}

    session_cwd = session.cwd or project_root
    tool_names = frozenset(config.edits.tools)
    subagent_names = frozenset(config.edits.subagent_tools)

    steps = session.steps
    n = len(steps)
    i = 0
    while i < n:
        step = steps[i]
        if (
            step.kind == KIND_MESSAGE
            and step.role == ROLE_ENVIRONMENT
            and step.entry == ENTRY_TASK_NOTIFICATION
        ):
            if step.tool_use_id and step.tool_use_id not in task_notifications:
                task_notifications[step.tool_use_id] = step.i
        elif step.kind == KIND_TOOL_CALL and step.role == ROLE_AGENT:
            result = steps[i + 1] if i + 1 < n and steps[i + 1].kind == KIND_TOOL_RESULT else None
            name = step.name
            if name == "Bash":
                # Claude Code records a per-entry cwd; fall back to the session's first cwd
                # (or the project root) only when this particular step has none of its own.
                step_cwd = step.cwd if step.cwd is not None else session_cwd
                ce = _build_command_event(step, result, step_cwd, on_parse_error)
                if ce is not None:
                    commands.append(ce)
                    edits.extend(_bash_edit_events(ce, project_root, config, step_cwd))
            elif name in tool_names:
                edit_event = _tool_edit_event(step, project_root)
                if edit_event is not None:
                    edits.append(edit_event)
            elif name in subagent_names:
                agent_type = _arg_str(step.args, "subagent_type")
                subagent_calls.append(SubagentCall(step=step.i, agent_type=agent_type))
                if config.subagent_calls_are_edits:
                    edits.append(
                        EditEvent(
                            step=step.i,
                            pos=(step.i, 0, 1),
                            abs_path=None,
                            rel_path=None,
                            is_dir=True,
                            whole_tree=True,
                            source="subagent",
                        )
                    )
        i += 1

    return EventList(
        edits=edits,
        commands=commands,
        subagent_calls=subagent_calls,
        task_notifications=task_notifications,
    )


# --------------------------------------------------------------------------------------------
# matching helpers
# --------------------------------------------------------------------------------------------


def segment_qualifies(
    seg: shell.Segment,
    cmd: shell.ParsedCommand,
    commands: Sequence[Sequence[str]],
    regex: re.Pattern[str] | None,
    exclude_args: Sequence[str],
    read_only: Sequence[Sequence[str]],
) -> bool:
    """Whether `seg` counts as evidence for a `commands`/`regex` evidence spec: it matches,
    is not a read-only command, carries no excluded argument, and the call it belongs to
    defines no same-named function/alias and does not prepend to ``PATH``. Shared between
    `judge` and `suggest.suggest`, which both need the exact same qualification rule."""
    if not shell.matches_any(seg, commands, regex):
        return False
    if shell.is_read_only(seg, read_only):
        return False
    if shell.is_excluded(seg, exclude_args):
        return False
    return not shell.disqualified(cmd, seg)


def _segment_qualifies_indexed(
    seg: shell.Segment,
    cmd: shell.ParsedCommand,
    cmd_index: shell.PrefixIndex,
    regex: re.Pattern[str] | None,
    exclude_args: Sequence[str],
    read_only_index: shell.PrefixIndex,
) -> bool:
    """Same predicate as :func:`segment_qualifies`, taken pre-indexed (:func:`shell.
    build_prefix_index`) instead of raw prefix lists: `_judge_rule`'s hot loop builds each
    index once per rule (and once per config for `read_only_commands`) and reuses it across
    every segment, instead of `fnmatch`-ing every prefix against every segment (spec S11 item
    3)."""
    matched = shell.prefix_index_match(cmd_index, seg.argv)
    if not matched and regex is not None:
        matched = bool(regex.search(" ".join(seg.argv)))
    if not matched:
        return False
    if shell.prefix_index_match(read_only_index, seg.argv):
        return False
    if shell.is_excluded(seg, exclude_args):
        return False
    return not shell.disqualified(cmd, seg)


def iter_segments(
    commands: Sequence[CommandEvent],
) -> Sequence[tuple[Pos, shell.Segment, CommandEvent]]:
    """Every `(pos, segment, command_event)` triple across `commands`, in position order."""
    out: list[tuple[Pos, shell.Segment, CommandEvent]] = []
    for ce in commands:
        for seg in ce.cmd.segments:
            out.append(((ce.step, seg.index, 1), seg, ce))
    return out


def rule_commands(rule: Rule, config: Config) -> tuple[tuple[str, ...], ...]:
    """`rule.evidence.commands`, or -- for `fixed`/`verified` -- the union of every other
    enabled rule's `commands` plus `execution_commands` (PLAN §6, point 4). Shared between
    `judge` and `suggest.suggest`."""
    if rule.id not in ("fixed", "verified"):
        return rule.evidence.commands
    combined: list[tuple[str, ...]] = []
    for other in config.rules:
        if other.id == rule.id or other.action == "off":
            continue
        combined.extend(other.evidence.commands)
    combined.extend(config.execution_commands)
    return tuple(combined)


def _pattern_search(patterns: Sequence[re.Pattern[str]], text: str) -> str | None:
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            return match.group(0)[:120]
    return None


def _pattern_matches(patterns: Sequence[re.Pattern[str]], text: str) -> bool:
    return any(pattern.search(text) for pattern in patterns)


def _segment_failure(
    seg: shell.Segment, ce: CommandEvent, ev: EvidenceSpec
) -> tuple[str, str | None] | None:
    """The reason `seg`/`ce` fails to be usable evidence on its own, in PLAN §6's precedence
    order, or None if it would be accepted as supporting evidence."""
    if ce.no_result:
        return "no_result", None
    if ce.interrupted or ce.timed_out:
        return "failed_exit", None
    output = ce.output_text or ""
    matched = _pattern_search(ev.empty_output, output)
    if matched is not None:
        return "empty_run", matched
    if ce.ok is False or (ce.exit_code is not None and ce.exit_code != 0):
        return "failed_exit", None
    matched = _pattern_search(ev.fail_output, output)
    if matched is not None:
        return "failed_output", matched
    if seg.masked and not _pattern_matches(ev.success_output, output):
        return "masked_inconclusive", None
    return None


def _touches(edit: EditEvent, relevant: Sequence[str], ignore: Sequence[str]) -> bool:
    if edit.whole_tree:
        return True
    if edit.is_dir:
        if edit.rel_path is None:
            return False
        return any(paths.could_match_under(pattern, edit.rel_path) for pattern in relevant)
    if edit.rel_path is None:
        return False
    if not any(paths.glob_match(pattern, edit.rel_path) for pattern in relevant):
        return False
    return not any(paths.glob_match(pattern, edit.rel_path) for pattern in ignore)


def _is_exempt(rule: Rule, all_edits: Sequence[EditEvent]) -> bool:
    if not rule.exempt_if_only_edited or not all_edits:
        return False
    for edit in all_edits:
        if edit.whole_tree or edit.is_dir or edit.rel_path is None:
            return False
        if not any(paths.glob_match(p, edit.rel_path) for p in rule.exempt_if_only_edited):
            return False
    return True


def _edit_display_path(edit: EditEvent | None) -> str | None:
    if edit is None or edit.whole_tree:
        return None
    return edit.rel_path if edit.rel_path is not None else edit.abs_path


# --------------------------------------------------------------------------------------------
# per-rule judging
# --------------------------------------------------------------------------------------------


@dataclass
class _RuleResult:
    rule: Rule
    supported: bool
    reason: str
    partial: bool
    exempt: bool
    responsible: bool
    details: VerdictDetails


def _judge_rule(
    rule: Rule,
    config: Config,
    events: EventList,
    stop_index: int,
    segments: Sequence[tuple[Pos, shell.Segment, CommandEvent]],
    read_only_index: shell.PrefixIndex,
) -> _RuleResult:
    all_edits = [e for e in events.edits if e.step < stop_index]

    if _is_exempt(rule, all_edits):
        return _RuleResult(
            rule=rule,
            supported=True,
            reason="supported",
            partial=False,
            exempt=True,
            responsible=False,
            details=VerdictDetails(),
        )

    rule_edits = [e for e in all_edits if _touches(e, rule.relevant_files, rule.ignore_files)]
    responsible = bool(rule_edits)
    anchor = rule_edits[-1] if rule_edits else None
    anchor_pos = anchor.pos if anchor is not None else None
    anchor_path = _edit_display_path(anchor)
    anchor_step = anchor.step if anchor is not None else None

    ev = rule.evidence
    cmd_prefixes = rule_commands(rule, config)
    cmd_index = shell.build_prefix_index(cmd_prefixes)

    fg_after: list[tuple[Pos, shell.Segment, CommandEvent]] = []
    bg_after: list[tuple[Pos, shell.Segment, CommandEvent]] = []
    fg_at_or_before: list[tuple[Pos, shell.Segment, CommandEvent]] = []
    for pos, seg, ce in segments:
        if not _segment_qualifies_indexed(
            seg, ce.cmd, cmd_index, ev.command_regex, ev.exclude_args, read_only_index
        ):
            continue
        is_background = ce.background or seg.background
        if anchor_pos is None or pos > anchor_pos:
            (bg_after if is_background else fg_after).append((pos, seg, ce))
        elif not is_background:
            fg_at_or_before.append((pos, seg, ce))

    if not fg_after:
        if bg_after:
            _, seg, ce = max(bg_after, key=lambda t: t[0])
            resolved_step = events.task_notifications.get(ce.tool_use_id or "")
            background_running = resolved_step is None or resolved_step >= stop_index
            details = VerdictDetails(
                candidate_display=seg.display,
                candidate_step=ce.step,
                background_running=background_running,
                anchor_path=anchor_path,
                anchor_step=anchor_step,
            )
            reason = "background_only"
        elif fg_at_or_before:
            _, seg, ce = max(fg_at_or_before, key=lambda t: t[0])
            details = VerdictDetails(
                candidate_display=seg.display,
                candidate_step=ce.step,
                anchor_path=anchor_path,
                anchor_step=anchor_step,
            )
            reason = "stale"
        else:
            details = VerdictDetails(anchor_path=anchor_path, anchor_step=anchor_step)
            reason = "no_command"
        return _RuleResult(
            rule=rule,
            supported=False,
            reason=reason,
            partial=False,
            exempt=False,
            responsible=responsible,
            details=details,
        )

    pos_c, seg_c, ce_c = max(fg_after, key=lambda t: t[0])
    details = VerdictDetails(
        candidate_display=seg_c.display,
        candidate_step=ce_c.step,
        exit_code=ce_c.exit_code,
        interrupted=ce_c.interrupted,
        timed_out=ce_c.timed_out,
        anchor_path=anchor_path,
        anchor_step=anchor_step,
    )
    failure = _segment_failure(seg_c, ce_c, ev)
    if failure is not None:
        reason, matched = failure
        details.matched_text = matched
        return _RuleResult(
            rule=rule,
            supported=False,
            reason=reason,
            partial=False,
            exempt=False,
            responsible=responsible,
            details=details,
        )

    is_partial = shell.is_partial(seg_c, ev.partial_args)
    if is_partial:
        failing: list[tuple[Pos, shell.Segment, CommandEvent]] = []
        for pos_x, seg_x, ce_x in fg_after:
            if pos_x == pos_c or shell.is_partial(seg_x, ev.partial_args):
                continue
            if _segment_failure(seg_x, ce_x, ev) is not None:
                failing.append((pos_x, seg_x, ce_x))
        if failing:
            # All qualifying entries in `fg_after` other than `pos_c` are strictly earlier
            # than it (`pos_c` is the max of `fg_after`), so the latest of them is the full
            # run closest in time to the partial run being superseded.
            _, seg_f, ce_f = max(failing, key=lambda t: t[0])
            details.full_run_display = seg_f.display
            details.full_run_step = ce_f.step
            return _RuleResult(
                rule=rule,
                supported=False,
                reason="superseded_by_failure",
                partial=False,
                exempt=False,
                responsible=responsible,
                details=details,
            )

    return _RuleResult(
        rule=rule,
        supported=True,
        reason="supported",
        partial=is_partial,
        exempt=False,
        responsible=responsible,
        details=details,
    )


# --------------------------------------------------------------------------------------------
# judge
# --------------------------------------------------------------------------------------------

_ACTION_RANK = {"off": 0, "warn": 1, "block": 2}


def _strictest_action(actions: Sequence[str]) -> str:
    return max(actions, key=lambda a: _ACTION_RANK.get(a, 2))


def judge(
    claim_type: str,
    events: EventList,
    stop_index: int,
    config: Config,
    segments: Sequence[tuple[Pos, shell.Segment, CommandEvent]] | None = None,
) -> Verdict:
    """Judge whether `claim_type` is supported at the stop whose events end at `stop_index`
    (exclusive), combining every enabled rule of that claim type per PLAN §6, point 7.

    `segments` is `iter_segments` of `events.commands` filtered to `step < stop_index`,
    prebuilt once by a caller that judges several claim types against the same `(events,
    stop_index)` (`engine.evaluate_stop`, spec S11 item 3: a claim-type judged more than once
    in one Stop, or several claim types in one message, must not re-walk and re-match every
    segment from scratch each time); left `None` to build it here for a one-off call."""
    rules = config.rules_for(claim_type)
    if not rules:
        return Verdict(
            claim_type=claim_type,
            supported=True,
            reason="supported",
            partial=False,
            exempt=False,
            action="off",
            rule_id=None,
            details=VerdictDetails(),
        )

    if segments is None:
        commands = [c for c in events.commands if c.step < stop_index]
        segments = tuple(iter_segments(commands))
    read_only_index = shell.build_prefix_index(config.read_only_commands)

    results = [
        _judge_rule(rule, config, events, stop_index, segments, read_only_index) for rule in rules
    ]
    responsible = [r for r in results if r.responsible]
    determining = responsible if responsible else results

    if responsible:
        supported = all(r.supported for r in responsible)
        reported = responsible[0] if supported else next(r for r in responsible if not r.supported)
    else:
        supported = any(r.supported for r in results)
        reported = next(r for r in results if r.supported) if supported else results[0]

    action = _strictest_action([r.rule.action for r in determining])
    return Verdict(
        claim_type=claim_type,
        supported=supported,
        reason=reported.reason,
        partial=reported.partial,
        exempt=reported.exempt,
        action=action,
        rule_id=reported.rule.id,
        details=reported.details,
    )


__all__ = [
    "CommandEvent",
    "EditEvent",
    "EventList",
    "Pos",
    "SubagentCall",
    "Verdict",
    "VerdictDetails",
    "build_events",
    "iter_segments",
    "judge",
    "rule_commands",
    "segment_qualifies",
]
