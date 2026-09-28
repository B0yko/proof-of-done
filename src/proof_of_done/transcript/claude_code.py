"""Claude Code JSONL transcript adapter, pinned to the shape documented in
`docs/transcript-format.md` (verified against Claude Code 2.1.281).

Robustness: a bad line is counted and skipped, never raised on. Unknown line types and unknown
fields are ignored. Performance: `parse()` avoids `json.loads` on lines that cannot possibly be
one of the four types it reads, via a cheap substring prefilter on the raw line.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from proof_of_done.transcript.model import (
    ENTRY_BASH_MODE,
    ENTRY_COMPACTION,
    ENTRY_HOOK_FEEDBACK,
    ENTRY_LOCAL_COMMAND,
    ENTRY_META,
    ENTRY_OTHER,
    ENTRY_TASK_NOTIFICATION,
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

# --------------------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------------------

_RECOGNIZED_TYPES = ("user", "assistant", "attachment", "system")
_PREFILTER_TOKENS = ('"user"', '"assistant"', '"attachment"', '"system"')

_HOOK_FEEDBACK_PREFIXES = ("Stop hook feedback", "SubagentStop hook feedback")
_LOCAL_COMMAND_PREFIXES = (
    "<local-command-stdout>",
    "<local-command-stderr>",
    "<local-command-caveat>",
)
_BASH_MODE_PREFIXES = ("<bash-input>", "<bash-stdout>", "<bash-stderr>")

_BASH_EXIT_RE = re.compile(r"^Exit code (\d+)")
_EXIT_CODE_IN_TEXT_RE = re.compile(r"exit code (\d+)", re.IGNORECASE)
_TASK_NOTIFICATION_RE = re.compile(
    r"<task-notification>.*?"
    r"<task-id>(?P<task_id>.*?)</task-id>.*?"
    r"<tool-use-id>(?P<tool_use_id>.*?)</tool-use-id>.*?"
    r"<output-file>(?P<output_file>.*?)</output-file>.*?"
    r"<status>(?P<status>.*?)</status>.*?"
    r"<summary>(?P<summary>.*?)</summary>",
    re.DOTALL,
)

_TIMEOUT_MARK = "Command timed out after"
_BACKGROUND_RUNNING_MARK = "Command running in background"
_MOVED_BACKGROUND_MARK = "moved to background"
_INTERRUPTED_MARK = "[Request interrupted by user"

_HEAD_TAIL_BYTES = 64 * 1024
_TRUNCATION_MARKER = "\n...[proof-of-done: output truncated]...\n"

_UNRECOGNIZED_LINE_THRESHOLD = 50


def _default_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + (
        f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"
    )


def _maybe_relevant(raw_line: str) -> bool:
    """Cheap prefilter: True unless `raw_line` cannot possibly be a type we read."""
    if '"type"' not in raw_line:
        return False
    return any(token in raw_line for token in _PREFILTER_TOKENS)


def _bound_text(text: str) -> str:
    if len(text) <= _HEAD_TAIL_BYTES * 2:
        return text
    return text[:_HEAD_TAIL_BYTES] + _TRUNCATION_MARKER + text[-_HEAD_TAIL_BYTES:]


def _read_bounded_file(path: str) -> str | None:
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size <= _HEAD_TAIL_BYTES * 2:
                data = fh.read()
                return data.decode("utf-8", errors="replace")
            head = fh.read(_HEAD_TAIL_BYTES)
            fh.seek(size - _HEAD_TAIL_BYTES)
            tail = fh.read(_HEAD_TAIL_BYTES)
        return (
            head.decode("utf-8", errors="replace")
            + _TRUNCATION_MARKER
            + tail.decode("utf-8", errors="replace")
        )
    except OSError:
        return None


def _read_persisted_output(
    transcript_path: str, session_id: str | None, persisted_output_path: str
) -> str | None:
    """Read the file a `toolUseResult.persistedOutputPath` points at, never outside the
    session's own directory. `persisted_output_path` comes straight from transcript JSON, so
    it is untrusted: an absolute path (or a relative one laced with ``..``) could otherwise
    point anywhere on disk with no containment check at all.

    The direct candidate (the path as given, absolute as-is or joined onto the transcript's
    own directory) is read only when its realpath resolves inside
    ``<dirname(transcript)>/<sessionId>/`` -- the session directory Claude Code itself writes
    persisted output under. Otherwise this falls back to
    ``<dirname(transcript)>/<sessionId>/tool-results/<basename>``, using only
    `os.path.basename` of the given path (so a `..`-laced or absolute value cannot escape
    that directory either). With no `session_id` at all there is no session directory to
    contain anything, so an absolute path is rejected outright and only a plain
    transcript-relative read is attempted, exactly as before this containment check existed.
    """
    transcript_dir = os.path.dirname(transcript_path)
    is_abs = os.path.isabs(persisted_output_path)
    candidate = (
        persisted_output_path if is_abs else os.path.join(transcript_dir, persisted_output_path)
    )

    if session_id:
        session_dir = os.path.join(transcript_dir, session_id)
        real_candidate = os.path.realpath(candidate)
        real_session_dir = os.path.realpath(session_dir)
        contained = real_candidate == real_session_dir or real_candidate.startswith(
            real_session_dir + os.sep
        )
        if contained:
            text = _read_bounded_file(candidate)
            if text is not None:
                return text
        basename = os.path.basename(persisted_output_path)
        fallback = os.path.join(session_dir, "tool-results", basename)
        text = _read_bounded_file(fallback)
        if text is not None:
            return text
    elif not is_abs:
        text = _read_bounded_file(candidate)
        if text is not None:
            return text
    return None


# --------------------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------------------


def _read_lines(path: str, max_bytes: int | None) -> list[str]:
    with open(path, "rb") as fh:
        data = fh.read(max_bytes) if max_bytes is not None else fh.read()
    text = data.decode("utf-8", errors="replace")
    return text.split("\n")


# --------------------------------------------------------------------------------------
# user-line classification
# --------------------------------------------------------------------------------------


def _user_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        ]
        return "\n".join(p for p in parts if isinstance(p, str))
    return ""


def _classify_user_entry(obj: dict[str, Any], text: str) -> str | None:
    """Return the environment `entry` kind for a non-typed-prompt user line, or None for a real
    typed prompt."""
    if obj.get("isMeta"):
        return ENTRY_META
    if text.startswith(_HOOK_FEEDBACK_PREFIXES):
        return ENTRY_HOOK_FEEDBACK
    if text.startswith("<task-notification>"):
        return ENTRY_TASK_NOTIFICATION
    if text.startswith(_LOCAL_COMMAND_PREFIXES):
        return ENTRY_LOCAL_COMMAND
    if text.startswith(_BASH_MODE_PREFIXES):
        return ENTRY_BASH_MODE
    if obj.get("isCompactSummary"):
        return ENTRY_COMPACTION
    if text.startswith("This session is being continued"):
        return ENTRY_COMPACTION
    if text.startswith("Caveat:"):
        return ENTRY_OTHER
    if text.startswith("[Request interrupted"):
        return ENTRY_OTHER
    # Note: `isSidechain` is deliberately not checked here. It distinguishes a subagent's own
    # (sidechain) lines from the main transcript when both are viewed together, but within a
    # subagent's own file (parsed by `parse_subagent`) every line has it set, including the
    # subagent's real task prompt, which must still classify as a typed prompt.
    origin = obj.get("origin")
    origin_kind = origin.get("kind") if isinstance(origin, dict) else None
    if isinstance(origin_kind, str) and origin_kind != "human":
        return ENTRY_OTHER
    turn_origin = obj.get("turnOrigin")
    if isinstance(turn_origin, str) and turn_origin != "human":
        return ENTRY_OTHER
    return None


# --------------------------------------------------------------------------------------
# tool_result decoding
# --------------------------------------------------------------------------------------


def _tool_result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        ]
        return "\n".join(p for p in parts if isinstance(p, str))
    return ""


def _decode_bash_result(
    *,
    is_error: bool,
    tool_use_id: str | None,
    content_text: str,
    tur: Any,
    ts: str,
    transcript_path: str,
    session_id: str | None,
) -> Step:
    ok = not is_error
    interrupted = False
    background = False
    exit_code: int | None = None
    error: str | None = None
    output_text = content_text

    if isinstance(tur, dict):
        stdout = tur.get("stdout") or ""
        stderr = tur.get("stderr") or ""
        interrupted = bool(tur.get("interrupted"))
        background_task_id = tur.get("backgroundTaskId")
        if isinstance(background_task_id, str) and background_task_id:
            background = True
        persisted_path = tur.get("persistedOutputPath")
        persisted_text = None
        if isinstance(persisted_path, str) and persisted_path:
            persisted_text = _read_persisted_output(transcript_path, session_id, persisted_path)
        if persisted_text is not None:
            output_text = persisted_text
        elif stdout or stderr:
            output_text = stdout + stderr
        else:
            output_text = content_text
        if ok and not interrupted and not background:
            exit_code = 0
    else:
        match = _BASH_EXIT_RE.match(content_text)
        if match:
            exit_code = int(match.group(1))
        error = content_text[:2000] if content_text else None

    if _MOVED_BACKGROUND_MARK in content_text or _BACKGROUND_RUNNING_MARK in content_text:
        background = True
    timed_out = _TIMEOUT_MARK in content_text
    if _INTERRUPTED_MARK in content_text:
        interrupted = True

    return Step(
        i=0,
        kind=KIND_TOOL_RESULT,
        role=ROLE_TOOL,
        name="Bash",
        tool_use_id=tool_use_id,
        ok=ok,
        exit_code=exit_code,
        output_text=_bound_text(output_text) if output_text else None,
        error=error,
        ts=ts,
        background=background,
        interrupted=interrupted,
        timed_out=timed_out,
    )


def _decode_tool_result(
    obj: dict[str, Any],
    block: dict[str, Any],
    ts: str,
    call_names: dict[str, str],
    transcript_path: str,
) -> Step:
    tool_use_id = block.get("tool_use_id")
    tool_use_id = tool_use_id if isinstance(tool_use_id, str) else None
    name = call_names.get(tool_use_id) if tool_use_id else None
    is_error = bool(block.get("is_error"))
    content_text = _tool_result_text(block.get("content"))
    session_id = obj.get("sessionId")
    session_id = session_id if isinstance(session_id, str) else None

    if name == "Bash":
        return _decode_bash_result(
            is_error=is_error,
            tool_use_id=tool_use_id,
            content_text=content_text,
            tur=obj.get("toolUseResult"),
            ts=ts,
            transcript_path=transcript_path,
            session_id=session_id,
        )

    step = Step(
        i=0,
        kind=KIND_TOOL_RESULT,
        role=ROLE_TOOL,
        name=name,
        tool_use_id=tool_use_id,
        ok=not is_error,
        ts=ts,
        output_text=_bound_text(content_text) if content_text else None,
    )
    if is_error:
        step.error = content_text[:2000] if content_text else None
    return step


# --------------------------------------------------------------------------------------
# line handlers
# --------------------------------------------------------------------------------------


def _handle_assistant(
    obj: dict[str, Any], ts: str, call_names: dict[str, str], steps: list[Step]
) -> None:
    message = obj.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text = block.get("text")
            if isinstance(text, str):
                steps.append(Step(i=0, kind=KIND_MESSAGE, role=ROLE_AGENT, content=text, ts=ts))
        elif btype == "tool_use":
            name = block.get("name")
            tool_use_id = block.get("id")
            raw_args = block.get("input")
            args = raw_args if isinstance(raw_args, dict) else None
            name = name if isinstance(name, str) else None
            tool_use_id = tool_use_id if isinstance(tool_use_id, str) else None
            if name and tool_use_id:
                call_names[tool_use_id] = name
            steps.append(
                Step(
                    i=0,
                    kind=KIND_TOOL_CALL,
                    role=ROLE_AGENT,
                    name=name,
                    args=args,
                    tool_use_id=tool_use_id,
                    ts=ts,
                )
            )


def _handle_user(
    obj: dict[str, Any],
    ts: str,
    call_names: dict[str, str],
    steps: list[Step],
    pending_results: dict[str, Step],
    transcript_path: str,
) -> None:
    message = obj.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    cwd = obj.get("cwd")
    cwd = cwd if isinstance(cwd, str) else None

    if isinstance(content, list):
        result_blocks = [
            b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"
        ]
        if result_blocks:
            for block in result_blocks:
                step = _decode_tool_result(obj, block, ts, call_names, transcript_path)
                if step.tool_use_id:
                    pending_results[step.tool_use_id] = step
            return

    text = _user_text(content)
    entry = _classify_user_entry(obj, text)
    if entry is None:
        steps.append(Step(i=0, kind=KIND_MESSAGE, role=ROLE_USER, content=text, ts=ts))
    else:
        steps.append(
            Step(
                i=0,
                kind=KIND_MESSAGE,
                role=ROLE_ENVIRONMENT,
                content=text,
                ts=ts,
                entry=entry,
                cwd=cwd,
            )
        )


def _handle_attachment(obj: dict[str, Any], ts: str, steps: list[Step]) -> None:
    attachment = obj.get("attachment")
    if not isinstance(attachment, dict) or attachment.get("type") != "queued_command":
        return
    prompt = attachment.get("prompt")
    if not isinstance(prompt, str):
        return
    cwd = obj.get("cwd")
    cwd = cwd if isinstance(cwd, str) else None

    if prompt.lstrip().startswith("<task-notification>"):
        match = _TASK_NOTIFICATION_RE.search(prompt)
        tool_use_id = match.group("tool_use_id").strip() if match else None
        summary = match.group("summary").strip() if match else prompt
        status = match.group("status").strip() if match else None
        exit_code = None
        exit_match = _EXIT_CODE_IN_TEXT_RE.search(summary)
        if exit_match:
            exit_code = int(exit_match.group(1))
        step = Step(
            i=0,
            kind=KIND_MESSAGE,
            role=ROLE_ENVIRONMENT,
            content=summary,
            ts=ts,
            entry=ENTRY_TASK_NOTIFICATION,
            tool_use_id=tool_use_id,
            exit_code=exit_code,
            cwd=cwd,
        )
        if status == "failed":
            step.error = summary
        elif status == "killed":
            step.timed_out = True
        steps.append(step)
    else:
        steps.append(Step(i=0, kind=KIND_MESSAGE, role=ROLE_USER, content=prompt, ts=ts))


def _handle_system(obj: dict[str, Any], ts: str, steps: list[Step]) -> None:
    if obj.get("subtype") == "compact_boundary":
        steps.append(
            Step(i=0, kind=KIND_MESSAGE, role=ROLE_ENVIRONMENT, entry=ENTRY_COMPACTION, ts=ts)
        )


# --------------------------------------------------------------------------------------
# step assembly (call/result pairing)
# --------------------------------------------------------------------------------------


def _build_steps(
    objs: list[dict[str, Any]], transcript_path: str, now: Callable[[], str]
) -> list[Step]:
    call_names: dict[str, str] = {}
    pending_results: dict[str, Step] = {}
    pre_steps: list[Step] = []

    for obj in objs:
        obj_type = obj.get("type")
        ts_raw = obj.get("timestamp")
        ts = ts_raw if isinstance(ts_raw, str) and ts_raw else now()
        if obj_type == "assistant":
            _handle_assistant(obj, ts, call_names, pre_steps)
        elif obj_type == "user":
            _handle_user(obj, ts, call_names, pre_steps, pending_results, transcript_path)
        elif obj_type == "attachment":
            _handle_attachment(obj, ts, pre_steps)
        elif obj_type == "system":
            _handle_system(obj, ts, pre_steps)

    final_steps: list[Step] = []
    for step in pre_steps:
        final_steps.append(step)
        if step.kind == KIND_TOOL_CALL and step.tool_use_id and step.tool_use_id in pending_results:
            final_steps.append(pending_results.pop(step.tool_use_id))
    for i, step in enumerate(final_steps):
        step.i = i
    return final_steps


# --------------------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------------------


def parse(
    path: str,
    *,
    max_bytes: int | None = None,
    now_reader: Callable[[], str] | None = None,
) -> Session:
    """Parse a Claude Code JSONL transcript into a normalized `Session`.

    Never raises on a malformed line (it is counted and skipped) and ignores unknown line
    types/fields. `max_bytes` caps how much of the file is read. `now_reader` supplies a
    fallback timestamp for lines missing one (defaults to the wall clock).
    """
    now = now_reader or _default_now
    raw_lines = _read_lines(path, max_bytes)
    total_lines = sum(1 for line in raw_lines if line.strip())

    objs: list[dict[str, Any]] = []
    session_id = ""
    session_cwd: str | None = None
    for raw in raw_lines:
        if not raw.strip() or not _maybe_relevant(raw):
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(obj, dict) or obj.get("type") not in _RECOGNIZED_TYPES:
            continue
        if not session_id:
            sid = obj.get("sessionId")
            if isinstance(sid, str) and sid:
                session_id = sid
        if session_cwd is None:
            cwd = obj.get("cwd")
            if isinstance(cwd, str) and cwd:
                session_cwd = cwd
        objs.append(obj)

    starts_mid_session = False
    all_uuids = {
        o["uuid"]
        for o in objs
        if o.get("type") in ("user", "assistant") and isinstance(o.get("uuid"), str)
    }
    for o in objs:
        if o.get("type") in ("user", "assistant"):
            parent = o.get("parentUuid")
            starts_mid_session = bool(parent) and parent not in all_uuids
            break

    steps = _build_steps(objs, path, now)
    unrecognized = not steps and total_lines > _UNRECOGNIZED_LINE_THRESHOLD

    return Session(
        session_id=session_id,
        source_format="claude-code",
        path=path,
        steps=steps,
        starts_mid_session=starts_mid_session,
        cwd=session_cwd,
        subagents=[],
        agent_type=None,
        agent_id=None,
        unrecognized=unrecognized,
    )


def last_agent_text(path: str, tail_bytes: int = 524288) -> str | None:
    """The final assistant text: the trailing text blocks of the last assistant message id found
    in the last `tail_bytes` of the file, joined with a blank line."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            seeked = size > tail_bytes
            if seeked:
                fh.seek(size - tail_bytes)
            data = fh.read()
    except OSError:
        return None

    text = data.decode("utf-8", errors="replace")
    lines = text.split("\n")
    if seeked and len(lines) > 1:
        lines = lines[1:]

    current_id: Any = object()
    blocks: list[str] = []
    for raw in lines:
        if not raw.strip() or not _maybe_relevant(raw):
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(obj, dict) or obj.get("type") != "assistant":
            continue
        message = obj.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        message_id = message.get("id") if isinstance(message, dict) else None
        if message_id != current_id:
            current_id = message_id
            blocks = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                block_text = block.get("text")
                if isinstance(block_text, str):
                    blocks.append(block_text)

    if not blocks:
        return None
    return "\n\n".join(blocks)


def find_subagent_transcripts(session_path: str) -> list[str]:
    """Paths of `<session-id>/subagents/agent-*.jsonl` files next to `session_path`."""
    base_dir = os.path.dirname(session_path)
    base_name = os.path.basename(session_path)
    session_id = base_name[: -len(".jsonl")] if base_name.endswith(".jsonl") else base_name
    subagents_dir = os.path.join(base_dir, session_id, "subagents")
    try:
        names = os.listdir(subagents_dir)
    except OSError:
        return []
    return sorted(
        os.path.join(subagents_dir, name)
        for name in names
        if name.startswith("agent-") and name.endswith(".jsonl")
    )


def _first_agent_id(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                if not raw.strip() or not _maybe_relevant(raw):
                    continue
                try:
                    obj = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    agent_id = obj.get("agentId")
                    if isinstance(agent_id, str) and agent_id:
                        return agent_id
    except OSError:
        return None
    return None


def _read_agent_type(meta_path: str) -> str | None:
    try:
        with open(meta_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if isinstance(data, dict):
        agent_type = data.get("agentType")
        if isinstance(agent_type, str):
            return agent_type
    return None


def parse_subagent(
    path: str,
    *,
    max_bytes: int | None = None,
    now_reader: Callable[[], str] | None = None,
) -> Session:
    """Parse a subagent transcript (`agent-<id>.jsonl`). `agent_type` comes from the sibling
    `.meta.json` when present."""
    session = parse(path, max_bytes=max_bytes, now_reader=now_reader)
    session.agent_id = _first_agent_id(path)
    meta_path = (
        path[: -len(".jsonl")] + ".meta.json" if path.endswith(".jsonl") else path + ".meta.json"
    )
    session.agent_type = _read_agent_type(meta_path)
    return session
