"""agent-trace/v1 transcript adapter (spec item 12): reads a JSONL file of agent-trace/v1
traces (one trace per line, validated on read against ``schemas/agent-trace-v1.json``) and
builds one normalized :class:`~proof_of_done.transcript.model.Session` per trace, so
`audit`/`trace validate`/eval can treat a trace exported by this project (or by a sibling
project such as agent-claimcheck or booking-truth, for its own coding-domain traces) the same
way as a native Claude Code transcript.

Robustness, matching the other adapters: an unparseable line, a line whose `schema` is not
`agent-trace/v1`, or a line that fails schema validation is skipped, never raised on. A file
that looks like it should be agent-trace/v1 (more than a handful of lines) but yields zero
valid traces is reported the same way the other adapters report an unrecognized transcript: one
synthetic session with `unrecognized=True`.

Two kinds of `tool_call` step are recognized as evidence sources, per spec item 12: a step
whose `args.command` holds a shell command is treated as a `Bash` call; a step whose `name` is
in `edits.agent_trace_tools` (default `Edit`, `Write`, `MultiEdit`, `NotebookEdit`,
`apply_patch`, `write_file`, `edit_file`) and whose `args` hold a file path
(`file_path`/`path`/`notebook_path`/`filename`; `apply_patch`'s `*** Update|Add|Delete File:`
patch text is parsed the same way `transcript/codex.py` parses it) is treated as an edit. Both
are normalized in place, at read time, so `evidence.build_events` needs no special case for
them: a `Bash`-shaped call is renamed to `Bash` with `args={"command": ...}`; a file-edit call
keeps its own tool name but gains a `file_path` arg if it only had one of the alternate keys,
and an `apply_patch` call/result pair is expanded into one synthetic `Write`/`Edit` pair per
touched file, exactly like `transcript/codex.py` does for its own `apply_patch` calls.

`meta.proof_of_done` (present only on a trace this project itself exported) restores exactly
what plain schema fields cannot carry: each step's original `tool_use_id`
(`tool_use_ids`, keyed by step `i`) and the flags `evidence.py`/`turns.py` need
(`step_flags`: `background`/`interrupted`/`timed_out`/`entry`/`cwd`). A trace from another
producer simply has no `meta.proof_of_done`, so every step gets the defaults (no flags, no
`entry` -- every `environment`-role step reads as a generic entry, so it can never look like
this project's own `hook_feedback` and will not accidentally close a stop attempt the way one
would; see `turns.py`). Subagent grouping (`meta.proof_of_done.subagents`, `{agent_id,
agent_type, trace_id}` per entry) is this project's own extension for round-tripping its own
`audit --export-traces` output; a trace without it is simply never grouped as anyone's
subagent.
"""

from __future__ import annotations

import json
from typing import Any

from proof_of_done import traces
from proof_of_done.transcript.codex import apply_patch_edit_targets
from proof_of_done.transcript.model import (
    KIND_MESSAGE,
    KIND_TOOL_CALL,
    KIND_TOOL_RESULT,
    ROLE_AGENT,
    ROLE_ENVIRONMENT,
    ROLE_TOOL,
    ROLE_USER,
    Session,
    Step,
)

_DEFAULT_AGENT_TRACE_TOOLS = (
    "Edit",
    "Write",
    "MultiEdit",
    "NotebookEdit",
    "apply_patch",
    "write_file",
    "edit_file",
)
_PATH_ARG_KEYS = ("file_path", "path", "notebook_path", "filename")
# `evidence.build_events` only ever recognizes an edit tool_call by `config.edits.tools`
# (the canonical `Edit`/`Write`/`MultiEdit`/`NotebookEdit` names every adapter's output is
# judged against, format-independently); an alternate-tool producer's own name is rewritten to
# one of those at read time, exactly like `transcript/codex.py` rewrites `apply_patch`.
_CANONICAL_EDIT_NAME = {"write_file": "Write", "edit_file": "Edit"}

_KIND_IN = {
    "message": KIND_MESSAGE,
    "tool_call": KIND_TOOL_CALL,
    "tool_result": KIND_TOOL_RESULT,
    "state_probe": KIND_MESSAGE,  # best-effort: not part of this project's own vocabulary
}
_ROLE_IN = {
    "user": ROLE_USER,
    "agent": ROLE_AGENT,
    "tool": ROLE_TOOL,
    "environment": ROLE_ENVIRONMENT,
}

_UNRECOGNIZED_LINE_THRESHOLD = 50


def looks_like_agent_trace(first_line: str) -> bool:
    """Best-effort sniff for ``--source auto``: True when `first_line` decodes as a JSON
    object whose `schema` is exactly ``agent-trace/v1``."""
    line = first_line.strip()
    if not line:
        return False
    try:
        obj = json.loads(line)
    except ValueError:
        return False
    return isinstance(obj, dict) and obj.get("schema") == traces.SCHEMA_NAME


# --------------------------------------------------------------------------------------
# one trace -> one Session
# --------------------------------------------------------------------------------------


def _path_arg(args: dict[str, Any]) -> str | None:
    for key in _PATH_ARG_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _expand_apply_patch(step: Step, result: Step | None) -> list[Step]:
    """One synthetic `Write`/`Edit` call/result pair per file touched by an `apply_patch`
    call, mirroring `transcript.codex`'s own handling of the same tool."""
    patch_text = step.args.get("input") if isinstance(step.args, dict) else None
    patch_text = patch_text if isinstance(patch_text, str) else (step.content or "")
    targets = apply_patch_edit_targets(patch_text)
    if not targets:
        return [step] if result is None else [step, result]
    out: list[Step] = []
    for idx, (tool_name, path, _body) in enumerate(targets):
        sub_id = f"{step.tool_use_id or 'apply_patch'}#{idx}"
        call_args: dict[str, Any] = {"file_path": path}
        out.append(
            Step(
                i=0,
                kind=KIND_TOOL_CALL,
                role=step.role,
                name=tool_name,
                args=call_args,
                tool_use_id=sub_id,
                ts=step.ts,
            )
        )
        out.append(
            Step(
                i=0,
                kind=KIND_TOOL_RESULT,
                role=ROLE_TOOL,
                name=tool_name,
                tool_use_id=sub_id,
                ok=result.ok if result is not None else True,
                ts=result.ts if result is not None else step.ts,
            )
        )
    return out


def _normalize_call(step: Step, agent_trace_tools: tuple[str, ...]) -> Step:
    """Recognize the two evidence-relevant `tool_call` shapes spec item 12 names, in place."""
    args = step.args if isinstance(step.args, dict) else None
    if args is None:
        return step
    if isinstance(args.get("command"), str) and args["command"]:
        step.name = "Bash"
        step.args = {"command": args["command"]}
        return step
    if step.name in agent_trace_tools:
        path = _path_arg(args)
        if path is not None and "file_path" not in args:
            new_args = dict(args)
            new_args["file_path"] = path
            args = new_args
            step.args = new_args
        step.name = _CANONICAL_EDIT_NAME.get(step.name, step.name)
    return step


def _step_from_trace_step(
    raw: dict[str, Any], tool_use_ids: dict[str, Any], step_flags: dict[str, Any]
) -> Step | None:
    i = raw.get("i")
    if not isinstance(i, int):
        return None
    kind_raw, role_raw = raw.get("kind"), raw.get("role")
    kind = _KIND_IN.get(kind_raw) if isinstance(kind_raw, str) else None
    role = _ROLE_IN.get(role_raw) if isinstance(role_raw, str) else None
    if kind is None or role is None:
        return None
    output = raw.get("output")
    output_text: str | None = None
    exit_code: int | None = None
    if isinstance(output, dict):
        text = output.get("text")
        output_text = text if isinstance(text, str) else None
        ec = output.get("exit_code")
        exit_code = ec if isinstance(ec, int) and not isinstance(ec, bool) else None
    flags = step_flags.get(str(i))
    flags = flags if isinstance(flags, dict) else {}
    name = raw.get("name")
    content = raw.get("content")
    args = raw.get("args")
    ok = raw.get("ok")
    error = raw.get("error")
    ts = raw.get("ts")
    cwd = flags.get("cwd")
    entry_raw = flags.get("entry")
    entry = entry_raw if isinstance(entry_raw, str) else ""
    return Step(
        i=i,
        kind=kind,
        role=role,
        name=name if isinstance(name, str) else None,
        content=content if isinstance(content, str) else None,
        args=args if isinstance(args, dict) else None,
        ok=ok if isinstance(ok, bool) else None,
        output_text=output_text,
        exit_code=exit_code,
        error=error if isinstance(error, str) else None,
        ts=ts if isinstance(ts, str) else "",
        tool_use_id=tool_use_ids.get(str(i)) if isinstance(tool_use_ids.get(str(i)), str) else None,
        background=bool(flags.get("background", False)),
        interrupted=bool(flags.get("interrupted", False)),
        timed_out=bool(flags.get("timed_out", False)),
        entry=entry,
        cwd=cwd if isinstance(cwd, str) else None,
    )


def parse_trace_obj(
    obj: dict[str, Any],
    path: str,
    *,
    agent_trace_tools: tuple[str, ...] = _DEFAULT_AGENT_TRACE_TOOLS,
) -> Session:
    """One already-decoded, already schema-valid agent-trace/v1 trace -> one `Session`."""
    meta = obj.get("meta")
    pod = meta.get("proof_of_done") if isinstance(meta, dict) else None
    pod = pod if isinstance(pod, dict) else {}
    tool_use_ids = pod.get("tool_use_ids")
    tool_use_ids = tool_use_ids if isinstance(tool_use_ids, dict) else {}
    step_flags = pod.get("step_flags")
    step_flags = step_flags if isinstance(step_flags, dict) else {}

    raw_steps = obj.get("steps")
    raw_steps = raw_steps if isinstance(raw_steps, list) else []

    pre_steps: list[Step] = []
    for raw in raw_steps:
        if not isinstance(raw, dict):
            continue
        step = _step_from_trace_step(raw, tool_use_ids, step_flags)
        if step is not None:
            pre_steps.append(step)
    pre_steps.sort(key=lambda s: s.i)

    expanded: list[Step] = []
    i = 0
    n = len(pre_steps)
    while i < n:
        step = pre_steps[i]
        if step.kind == KIND_TOOL_CALL:
            original_name = step.name
            step = _normalize_call(step, agent_trace_tools)
            result = (
                pre_steps[i + 1]
                if i + 1 < n and pre_steps[i + 1].kind == KIND_TOOL_RESULT
                else None
            )
            if step.name == "apply_patch":
                expanded.extend(_expand_apply_patch(step, result))
                i += 2 if result is not None else 1
                continue
            if result is not None and step.name != original_name and result.name == original_name:
                # Keep the schema's "a tool_result refers to the preceding tool_call with the
                # same name" rule true of this adapter's own (renamed/canonicalized) output too.
                result.name = step.name
        expanded.append(step)
        i += 1

    for idx, step in enumerate(expanded):
        step.i = idx

    task = obj.get("task")
    task = task if isinstance(task, dict) else {}
    session_id = task.get("id")
    session_id = session_id if isinstance(session_id, str) else ""

    cwd = next((s.cwd for s in expanded if s.cwd is not None), None)

    return Session(
        session_id=session_id,
        source_format="agent-trace",
        path=path,
        steps=expanded,
        starts_mid_session=False,
        cwd=cwd,
        subagents=[],
        agent_type=None,
        agent_id=None,
        unrecognized=False,
    )


# --------------------------------------------------------------------------------------
# a whole JSONL file -> every Session, subagents grouped under their parent
# --------------------------------------------------------------------------------------


def parse_traces(path: str) -> list[Session]:
    """Every trace in `path`, one `Session` each. A trace listed as a subagent by one of its
    siblings' `meta.proof_of_done.subagents` is grouped under that parent (`Session.subagents`,
    with `agent_id`/`agent_type` restored from the reference) instead of appearing at the top
    level."""
    entries: list[tuple[str | None, Session, list[dict[str, Any]]]] = []
    total_lines = 0
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            total_lines += 1
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if not isinstance(obj, dict) or obj.get("schema") != traces.SCHEMA_NAME:
                continue
            if traces.validate_trace(obj):
                continue  # invalid trace: skip, never raise (robustness, like every adapter)
            session = parse_trace_obj(obj, path)
            trace_id = obj.get("trace_id")
            meta = obj.get("meta")
            pod = meta.get("proof_of_done") if isinstance(meta, dict) else None
            refs = pod.get("subagents") if isinstance(pod, dict) else None
            refs = [r for r in refs if isinstance(r, dict)] if isinstance(refs, list) else []
            entries.append((trace_id if isinstance(trace_id, str) else None, session, refs))

    by_trace_id = {tid: session for tid, session, _refs in entries if tid is not None}
    grouped: set[str] = set()
    for _tid, session, refs in entries:
        for ref in refs:
            child_id = ref.get("trace_id")
            if not isinstance(child_id, str) or child_id in grouped:
                continue
            child = by_trace_id.get(child_id)
            if child is None:
                continue
            agent_id = ref.get("agent_id")
            agent_type = ref.get("agent_type")
            child.agent_id = agent_id if isinstance(agent_id, str) else None
            child.agent_type = agent_type if isinstance(agent_type, str) else None
            session.subagents.append(child)
            grouped.add(child_id)

    result = [session for tid, session, _refs in entries if tid not in grouped]
    if not result and total_lines > _UNRECOGNIZED_LINE_THRESHOLD:
        return [
            Session(
                session_id="",
                source_format="agent-trace",
                path=path,
                steps=[],
                starts_mid_session=False,
                cwd=None,
                subagents=[],
                agent_type=None,
                agent_id=None,
                unrecognized=True,
            )
        ]
    return result


__all__ = ["looks_like_agent_trace", "parse_trace_obj", "parse_traces"]
