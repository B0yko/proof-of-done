"""Experimental Codex CLI rollout JSONL adapter.

Pinned to openai/codex commit ``44fe510ce3ee61c8ef623adcbf89b901c73ddd61`` (see
``_work/proof-of-done/research/codex-format.md`` for the source excerpts this module is built
from -- the research doc that commit's own field names against, fetched directly from
``codex-rs/history/src/rollout_payload.rs``, ``codex-rs/protocol/src/models.rs`` and
``codex-rs/protocol/src/protocol.rs``, ``codex-rs/core/src/tools/mod.rs`` and
``codex-rs/core/assets/tools/apply_patch.lark``).

Scope, confirmed against that source:

- One JSON object per line: ``{timestamp, ordinal?, type, payload, metadata?}``. ``type`` is
  the ``RolloutItem`` variant (``session_meta``, ``response_item``, plus ten others this
  adapter ignores: ``inter_agent_communication``, ``compacted``, ``turn_context``,
  ``token_usage_record``, ``world_state``, ``retained_context``, ``security_risk_score``,
  ``event_msg``, ``realtime_item``, ``inter_agent_communication_metadata``). ``payload`` holds
  that variant's own fields (``RolloutItemWire`` is ``#[serde(tag = "type")]`` with every
  variant carrying a ``payload`` field -- confirmed directly in
  ``codex-rs/history/src/rollout_payload.rs``, not merely inferred). A ``response_item``
  line's ``payload`` is itself a ``ResponseItem``, separately tagged by its own ``type``
  (``message``, ``function_call``, ``function_call_output``, ``local_shell_call``,
  ``custom_tool_call``, ``custom_tool_call_output``, and others this adapter ignores). As a
  defensive fallback for older/other encodings, a line whose top-level ``type`` is directly one
  of those `ResponseItem` type strings (no ``response_item``/``payload`` wrapping) is read the
  same way.
- ``session_meta``: ``session_id`` (or, absent that, ``id``) and ``cwd``.
- ``message``: ``role`` (``user``/``assistant``; anything else becomes an ignorable
  environment entry) and ``content`` (a list of ``{type, text}`` blocks -- ``input_text`` /
  ``output_text`` on the wire, ``text`` accepted defensively). A user message whose text starts
  with the harness's own injected-context tags (``<environment_context>``,
  ``<user_instructions>`` -- both confirmed string constants in
  ``codex-rs/protocol/src/protocol.rs``) is an environment entry, not a typed prompt.
- ``function_call`` named ``exec_command`` (current), or the legacy/alternate names ``shell`` /
  ``container.exec``: ``arguments`` is a JSON-encoded string (confirmed: `FunctionCall.arguments
  is a raw string, parsed by the harness, not already-structured JSON`), holding ``cmd`` (a
  single command string -- `exec_command`'s confirmed argument shape in
  ``codex-rs/core/src/tools/handlers/shell_spec.rs``) or a `command` list (older/alternate
  shape; joined with ``shlex.join``), plus optional ``workdir``.
- ``local_shell_call``: the Responses API's hosted shell tool. ``action.command`` is a string
  list (joined with ``shlex.join``), ``action.working_directory`` is the cwd.
- ``function_call_output`` pairs to a `function_call`/`local_shell_call` by `call_id`. ``output``
  is a plain string or a list of structured content items (`FunctionCallOutputPayload`'s
  confirmed untagged wire encoding) -- both are read. The exit code is not a structured field
  anywhere on the wire (confirmed: `format_exec_output_for_model` in
  ``codex-rs/core/src/tools/mod.rs`` only ever formats it into the text as ``Exit code: N``);
  this adapter regexes that out. A result is `ok` when the exit code is 0, unknown when no exit
  code line is found.
- ``custom_tool_call`` named ``apply_patch``: ``input`` is the raw V4A patch text (confirmed
  grammar: ``codex-rs/core/assets/tools/apply_patch.lark``), not JSON. Edit targets come from
  ``*** Add File: *``/``*** Update File: *``/``*** Delete File: *`` hunk headers, each becoming
  a synthetic ``Write`` (added file) or ``Edit`` (updated/deleted file) call step so the
  existing edit-detection tools/list picks them up unchanged; an ``*** Update File:`` hunk's
  optional ``*** Move to: *`` line adds the destination as a second `Edit` target (mirroring how
  a Bash `mv`'s source and destination both become edit targets elsewhere in this codebase).
  One `apply_patch` call can touch several files, so it expands to several synthetic
  call/result step pairs, all sharing the outcome of the one `custom_tool_call_output`.
- ``custom_tool_call_output``: no confirmed structured success/failure field exists on the wire
  for this item type (unlike `function_call_output`'s "Exit code: N" text convention, which is
  specific to the exec/shell formatting path). Its synthetic edit steps are recorded `ok=True`
  unconditionally -- edit-event detection never reads a result step's `ok` -- and its output
  text is attached only for trace completeness.
- Everything else (`AdditionalTools`, `AgentMessage`, `Reasoning`, `ToolSearchCall`,
  `WebSearchCall`, `ImageGenerationCall`, `Compaction`, and the ten non-`session_meta`/
  non-`response_item` `RolloutItem` variants) is ignored, mirroring the ``#[serde(other)]``
  catch-all the real Codex source itself uses for forward compatibility.

Not yet wired into anything: no `--source codex`, no `auto` detection, no audit registration.
This module only exposes :func:`parse` and :func:`looks_like_codex`; a later step does that
wiring. Never reads real Codex logs (``~/.codex/**``) -- every fixture this module is tested
against is hand-authored from the source definitions above, not captured from a real session.

Robustness: a bad line is skipped, never raised on. Unknown line types and unknown fields are
ignored.
"""

from __future__ import annotations

import json
import re
import shlex
from datetime import datetime, timezone
from typing import Any

from proof_of_done.transcript.model import (
    ENTRY_META,
    ENTRY_OTHER,
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

PINNED_COMMIT = "44fe510ce3ee61c8ef623adcbf89b901c73ddd61"

_SHELL_FUNCTION_NAMES = frozenset({"exec_command", "shell", "container.exec"})

_RESPONSE_ITEM_TYPES = frozenset(
    {
        "message",
        "function_call",
        "function_call_output",
        "local_shell_call",
        "custom_tool_call",
        "custom_tool_call_output",
    }
)

_HARNESS_CONTEXT_TAGS = ("<environment_context>", "<user_instructions>")
_MESSAGE_TEXT_TYPES = frozenset({"input_text", "output_text", "text"})

_EXIT_CODE_RE = re.compile(r"Exit code:\s*(-?\d+)")
_TIMED_OUT_MARK = "command timed out after"

_ADD_FILE_PREFIX = "*** Add File: "
_UPDATE_FILE_PREFIX = "*** Update File: "
_DELETE_FILE_PREFIX = "*** Delete File: "
_MOVE_TO_PREFIX = "*** Move to: "
_HUNK_PREFIX = "*** "

_UNRECOGNIZED_LINE_THRESHOLD = 50


def _default_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + (
        f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"
    )


def _read_lines(path: str) -> list[str]:
    with open(path, "rb") as fh:
        data = fh.read()
    return data.decode("utf-8", errors="replace").split("\n")


# --------------------------------------------------------------------------------------
# envelope / payload resolution
# --------------------------------------------------------------------------------------


def _line_kind(obj: dict[str, Any]) -> str | None:
    kind = obj.get("type")
    return kind if isinstance(kind, str) else None


def _line_payload(obj: dict[str, Any]) -> dict[str, Any]:
    payload = obj.get("payload")
    return payload if isinstance(payload, dict) else obj


def looks_like_codex(first_line: str) -> bool:
    """Best-effort sniff for auto-detection (not wired up anywhere yet): True when
    `first_line` decodes as a JSON object whose `RolloutItem` type is `session_meta` -- the
    first line of every real Codex rollout file, per the pinned commit's envelope described in
    this module's docstring."""
    line = first_line.strip()
    if not line:
        return False
    try:
        obj = json.loads(line)
    except ValueError:
        return False
    if not isinstance(obj, dict):
        return False
    return _line_kind(obj) == "session_meta"


# --------------------------------------------------------------------------------------
# message text / tool-output text
# --------------------------------------------------------------------------------------


def _joined_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") in _MESSAGE_TEXT_TYPES:
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return ""


def _parse_json_object(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _join_command(command: Any) -> str | None:
    if isinstance(command, str) and command:
        return command
    if isinstance(command, list) and command and all(isinstance(c, str) for c in command):
        return shlex.join(command)
    return None


def _shell_command_from_args(args: dict[str, Any]) -> str | None:
    cmd = args.get("cmd")
    if isinstance(cmd, str) and cmd:
        return cmd
    return _join_command(args.get("command"))


def _bash_result_fields(output_text: str) -> tuple[int | None, bool | None, bool]:
    match = _EXIT_CODE_RE.search(output_text)
    exit_code = int(match.group(1)) if match else None
    ok = (exit_code == 0) if exit_code is not None else None
    timed_out = _TIMED_OUT_MARK in output_text.lower()
    return exit_code, ok, timed_out


# --------------------------------------------------------------------------------------
# apply_patch (V4A) hunk-header parsing -- codex-rs/core/assets/tools/apply_patch.lark
# --------------------------------------------------------------------------------------


def _apply_patch_edit_targets(patch_text: str) -> list[tuple[str, str, str]]:
    """Parse `apply_patch`'s raw patch text into `(tool_name, file_path, hunk_body)` triples:
    `tool_name` is `Write` for an added file, `Edit` for an updated or deleted file (and again
    for a `*** Move to:` destination). `hunk_body` is kept only as `tamper_text`."""
    targets: list[tuple[str, str, str]] = []
    lines = patch_text.split("\n")
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if line.startswith(_ADD_FILE_PREFIX):
            path = line[len(_ADD_FILE_PREFIX) :].strip()
            i += 1
            body_lines: list[str] = []
            while i < n and not lines[i].startswith(_HUNK_PREFIX):
                body_lines.append(lines[i])
                i += 1
            if path:
                targets.append(("Write", path, "\n".join(body_lines)))
            continue
        if line.startswith(_UPDATE_FILE_PREFIX):
            path = line[len(_UPDATE_FILE_PREFIX) :].strip()
            i += 1
            move_to = None
            if i < n and lines[i].startswith(_MOVE_TO_PREFIX):
                move_to = lines[i][len(_MOVE_TO_PREFIX) :].strip()
                i += 1
            body_lines = []
            while i < n and not lines[i].startswith(_HUNK_PREFIX):
                body_lines.append(lines[i])
                i += 1
            body = "\n".join(body_lines)
            if path:
                targets.append(("Edit", path, body))
            if move_to:
                targets.append(("Edit", move_to, body))
            continue
        if line.startswith(_DELETE_FILE_PREFIX):
            path = line[len(_DELETE_FILE_PREFIX) :].strip()
            if path:
                targets.append(("Edit", path, ""))
            i += 1
            continue
        i += 1
    return targets


# --------------------------------------------------------------------------------------
# response_item dispatch
# --------------------------------------------------------------------------------------


def _handle_message(item: dict[str, Any], ts: str, steps: list[Step]) -> None:
    role = item.get("role")
    text = _joined_text(item.get("content"))
    if role == "assistant":
        if text:
            steps.append(Step(i=0, kind=KIND_MESSAGE, role=ROLE_AGENT, content=text, ts=ts))
        return
    if role == "user":
        if text.lstrip().startswith(_HARNESS_CONTEXT_TAGS):
            steps.append(
                Step(
                    i=0,
                    kind=KIND_MESSAGE,
                    role=ROLE_ENVIRONMENT,
                    content=text,
                    ts=ts,
                    entry=ENTRY_META,
                )
            )
        else:
            steps.append(Step(i=0, kind=KIND_MESSAGE, role=ROLE_USER, content=text, ts=ts))
        return
    # Any other role (system, developer, ...): not a typed prompt, kept only as an environment
    # entry for trace completeness.
    if text:
        steps.append(
            Step(
                i=0,
                kind=KIND_MESSAGE,
                role=ROLE_ENVIRONMENT,
                content=text,
                ts=ts,
                entry=ENTRY_OTHER,
            )
        )


def _handle_function_call(item: dict[str, Any], ts: str, steps: list[Step]) -> None:
    name = item.get("name")
    call_id = item.get("call_id")
    if not isinstance(name, str) or name not in _SHELL_FUNCTION_NAMES:
        return  # everything else is ignored
    if not isinstance(call_id, str) or not call_id:
        return
    args = _parse_json_object(item.get("arguments"))
    if args is None:
        return
    cmd = _shell_command_from_args(args)
    if not cmd:
        return
    workdir = args.get("workdir") if isinstance(args.get("workdir"), str) else None
    if workdir is None and isinstance(args.get("working_directory"), str):
        workdir = args["working_directory"]
    steps.append(
        Step(
            i=0,
            kind=KIND_TOOL_CALL,
            role=ROLE_AGENT,
            name="Bash",
            args={"command": cmd},
            tool_use_id=call_id,
            ts=ts,
            cwd=workdir,
        )
    )


def _handle_local_shell_call(item: dict[str, Any], ts: str, steps: list[Step]) -> None:
    call_id = item.get("call_id")
    action = item.get("action")
    if not isinstance(call_id, str) or not call_id or not isinstance(action, dict):
        return
    cmd = _join_command(action.get("command"))
    if not cmd:
        return
    workdir = action.get("working_directory")
    workdir = workdir if isinstance(workdir, str) else None
    steps.append(
        Step(
            i=0,
            kind=KIND_TOOL_CALL,
            role=ROLE_AGENT,
            name="Bash",
            args={"command": cmd},
            tool_use_id=call_id,
            ts=ts,
            cwd=workdir,
        )
    )


def _handle_function_call_output(
    item: dict[str, Any], ts: str, pending_bash_results: dict[str, Step]
) -> None:
    call_id = item.get("call_id")
    if not isinstance(call_id, str) or not call_id:
        return
    output_text = _joined_text(item.get("output"))
    exit_code, ok, timed_out = _bash_result_fields(output_text)
    pending_bash_results[call_id] = Step(
        i=0,
        kind=KIND_TOOL_RESULT,
        role=ROLE_TOOL,
        name="Bash",
        tool_use_id=call_id,
        ok=ok,
        exit_code=exit_code,
        output_text=output_text or None,
        ts=ts,
        timed_out=timed_out,
    )


def _handle_custom_tool_call(
    item: dict[str, Any],
    ts: str,
    steps: list[Step],
    pending_apply_patch_results: dict[str, list[Step]],
) -> None:
    name = item.get("name")
    call_id = item.get("call_id")
    if name != "apply_patch" or not isinstance(call_id, str) or not call_id:
        return  # everything else is ignored
    input_text = item.get("input")
    input_text = input_text if isinstance(input_text, str) else ""
    ops = _apply_patch_edit_targets(input_text)
    results: list[Step] = []
    for idx, (tool_name, path, body) in enumerate(ops):
        sub_id = f"{call_id}#{idx}"
        call_args: dict[str, Any] = (
            {"file_path": path, "content": body}
            if tool_name == "Write"
            else {"file_path": path, "old_string": "", "new_string": body}
        )
        steps.append(
            Step(
                i=0,
                kind=KIND_TOOL_CALL,
                role=ROLE_AGENT,
                name=tool_name,
                args=call_args,
                tool_use_id=sub_id,
                ts=ts,
            )
        )
        result_step = Step(
            i=0,
            kind=KIND_TOOL_RESULT,
            role=ROLE_TOOL,
            name=tool_name,
            tool_use_id=sub_id,
            ok=True,
            ts=ts,
        )
        steps.append(result_step)
        results.append(result_step)
    if results:
        pending_apply_patch_results[call_id] = results


def _handle_custom_tool_call_output(
    item: dict[str, Any], pending_apply_patch_results: dict[str, list[Step]]
) -> None:
    call_id = item.get("call_id")
    if not isinstance(call_id, str) or not call_id:
        return
    output_text = _joined_text(item.get("output"))
    results = pending_apply_patch_results.pop(call_id, None)
    if results and output_text:
        for result_step in results:
            result_step.output_text = output_text


def _dispatch_response_item(
    item_type: str,
    item: dict[str, Any],
    ts: str,
    steps: list[Step],
    pending_bash_results: dict[str, Step],
    pending_apply_patch_results: dict[str, list[Step]],
) -> None:
    if item_type == "message":
        _handle_message(item, ts, steps)
    elif item_type == "function_call":
        _handle_function_call(item, ts, steps)
    elif item_type == "function_call_output":
        _handle_function_call_output(item, ts, pending_bash_results)
    elif item_type == "local_shell_call":
        _handle_local_shell_call(item, ts, steps)
    elif item_type == "custom_tool_call":
        _handle_custom_tool_call(item, ts, steps, pending_apply_patch_results)
    elif item_type == "custom_tool_call_output":
        _handle_custom_tool_call_output(item, pending_apply_patch_results)
    # else: everything else is ignored (reasoning, web_search_call, ...).


# --------------------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------------------


def parse(path: str) -> Session:
    """Parse an experimental Codex CLI rollout JSONL transcript into a normalized `Session`.

    See the module docstring for exactly what is read and what is ignored, and the pinned
    source commit this shape is confirmed against. Never raises on a malformed line (it is
    skipped) and never reads anything other than `path`.
    """
    now = _default_now
    raw_lines = _read_lines(path)
    total_lines = sum(1 for line in raw_lines if line.strip())

    pre_steps: list[Step] = []
    pending_bash_results: dict[str, Step] = {}
    pending_apply_patch_results: dict[str, list[Step]] = {}
    session_id = ""
    session_cwd: str | None = None

    for raw in raw_lines:
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue

        ts_raw = obj.get("timestamp")
        ts = ts_raw if isinstance(ts_raw, str) and ts_raw else now()
        kind = _line_kind(obj)

        if kind == "session_meta":
            payload = _line_payload(obj)
            if not session_id:
                sid = payload.get("session_id")
                if not isinstance(sid, str) or not sid:
                    sid = payload.get("id")
                if isinstance(sid, str) and sid:
                    session_id = sid
            if session_cwd is None:
                cwd = payload.get("cwd")
                if isinstance(cwd, str) and cwd:
                    session_cwd = cwd
            continue

        if kind == "response_item":
            item = _line_payload(obj)
            item_type = item.get("type")
        elif kind in _RESPONSE_ITEM_TYPES:
            # Defensive fallback for an encoding that flattens a ResponseItem's own type
            # straight onto the line instead of nesting it under `type: response_item` +
            # `payload` (see the module docstring).
            item = obj
            item_type = kind
        else:
            continue  # every other RolloutItem variant is ignored

        if isinstance(item_type, str):
            _dispatch_response_item(
                item_type, item, ts, pre_steps, pending_bash_results, pending_apply_patch_results
            )

    final_steps: list[Step] = []
    for step in pre_steps:
        final_steps.append(step)
        if (
            step.kind == KIND_TOOL_CALL
            and step.tool_use_id
            and step.tool_use_id in pending_bash_results
        ):
            final_steps.append(pending_bash_results.pop(step.tool_use_id))
    for i, step in enumerate(final_steps):
        step.i = i

    unrecognized = not final_steps and total_lines > _UNRECOGNIZED_LINE_THRESHOLD

    return Session(
        session_id=session_id,
        source_format="codex",
        path=path,
        steps=final_steps,
        starts_mid_session=False,
        cwd=session_cwd,
        subagents=[],
        agent_type=None,
        agent_id=None,
        unrecognized=unrecognized,
    )


__all__ = ["PINNED_COMMIT", "looks_like_codex", "parse"]
