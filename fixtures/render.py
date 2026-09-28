#!/usr/bin/env python3
"""Render a validated scenario (see `fixtures/dsl.py`) into a Claude Code JSONL fixture tree,
in the pinned shape documented in `docs/transcript-format.md`.

Deterministic: two renders of the same scenario into two directories produce byte-identical
files. Session ids, line uuids and tool-use ids are `uuid5` under one fixed namespace (see
`fixtures/README.md`); timestamps start at a fixed base and advance one second per line.

Not part of the installed wheel: importable as `fixtures.render`, and runnable directly as
``python fixtures/render.py SCENARIO.yaml --out DIR``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from fixtures import dsl  # noqa: E402

NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://github.com/B0yko/proof-of-done/fixtures")
BASE_TIMESTAMP = datetime(2026, 1, 5, 10, 0, 0, tzinfo=timezone.utc)
MODEL_NAME = "synthetic-model"
VERSION = "2.1.281"
GIT_BRANCH = "main"
USER_TYPE = "external"
ENTRYPOINT = "cli"
FAIL_CONTENT_CAP = 30000
PERSIST_THRESHOLD = 30000


def _uuid5(scenario_id: str, kind: str, n: int) -> uuid.UUID:
    return uuid.uuid5(NAMESPACE, f"{scenario_id}/{kind}/{n}")


def _ts_for_line(n: int) -> str:
    t = BASE_TIMESTAMP + timedelta(seconds=n)
    return t.strftime("%Y-%m-%dT%H:%M:%S.000Z")


# --------------------------------------------------------------------------------------
# results returned to callers
# --------------------------------------------------------------------------------------


@dataclass
class LabelSpan:
    type: str
    start: int
    end: int
    label: str
    reason: str | None
    suggest: str | None
    partial: bool


@dataclass
class StopCase:
    scenario_id: str
    turn_index: int
    attempt_index: int
    session_path: str
    agent_transcript_path: str | None
    payload: dict[str, Any]
    final_message: str
    labels: list[LabelSpan]
    expect: dict[str, Any] | None
    project_files: dict[str, str | None]
    config: dict[str, Any] | None
    env: dict[str, str]


@dataclass
class RenderedSession:
    scenario_id: str
    session_id: str
    session_path: str
    stop_cases: list[StopCase]
    subagent_paths: list[str]


# --------------------------------------------------------------------------------------
# id/timestamp bookkeeping shared across the main file and every subagent file
# --------------------------------------------------------------------------------------


@dataclass
class RenderContext:
    scenario_id: str
    out_dir: str
    root: str
    session_id: str = ""
    project_files: dict[str, str | None] = field(default_factory=dict)
    config: dict[str, Any] | None = None
    env: dict[str, str] = field(default_factory=dict)
    stop_cases: list[StopCase] = field(default_factory=list)
    subagent_paths: list[str] = field(default_factory=list)
    current_turn_index: int = 0
    current_attempt_index: int = 0
    _tool_n: int = 0
    _agent_n: int = 0
    _msg_n: dict[str, int] = field(default_factory=dict)

    def line_uuid(self, file_key: str, n: int) -> uuid.UUID:
        kind = f"{file_key}/line" if file_key else "line"
        return _uuid5(self.scenario_id, kind, n)

    def next_tool_use_id(self) -> str:
        n = self._tool_n
        self._tool_n += 1
        return "toolu_" + _uuid5(self.scenario_id, "tool", n).hex

    def next_agent_index(self) -> int:
        n = self._agent_n
        self._agent_n += 1
        return n

    def agent_uuid(self, agent_index: int) -> uuid.UUID:
        return _uuid5(self.scenario_id, "agent", agent_index)

    def next_message_id(self, file_key: str) -> str:
        n = self._msg_n.get(file_key, 0)
        self._msg_n[file_key] = n + 1
        kind = f"{file_key}/msg" if file_key else "msg"
        return "msg_" + _uuid5(self.scenario_id, kind, n).hex[:24]

    def write_main_persisted_output(self, basename: str, content: str) -> None:
        path = os.path.join(self.out_dir, self.session_id, "tool-results", basename)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)


class _FileBuilder:
    """Accumulates JSONL line objects for one transcript file (main session or one subagent)."""

    def __init__(
        self,
        ctx: RenderContext,
        *,
        file_key: str,
        is_sidechain: bool,
        mid_session: bool = False,
    ):
        self.ctx = ctx
        self.file_key = file_key
        self.is_sidechain = is_sidechain
        self.mid_session = mid_session
        self.n = 0
        self.lines: list[dict[str, Any]] = []

    def add_line(self, fields: dict[str, Any]) -> str:
        n = self.n
        self.n += 1
        line_uuid = self.ctx.line_uuid(self.file_key, n)
        if n == 0:
            parent_uuid = self.ctx.line_uuid(self.file_key, -1) if self.mid_session else None
        else:
            parent_uuid = self.ctx.line_uuid(self.file_key, n - 1)
        obj: dict[str, Any] = {
            "type": fields.pop("type"),
            "uuid": str(line_uuid),
            "parentUuid": str(parent_uuid) if parent_uuid is not None else None,
            "sessionId": self.ctx.session_id,
            "timestamp": _ts_for_line(n),
            "cwd": self.ctx.root,
            "version": VERSION,
            "gitBranch": GIT_BRANCH,
            "isSidechain": self.is_sidechain,
            "userType": USER_TYPE,
            "entrypoint": ENTRYPOINT,
        }
        if self.is_sidechain:
            obj["agentId"] = str(self.ctx.agent_uuid(int(self.file_key.split("/")[1])))
        obj.update(fields)
        self.lines.append(obj)
        return str(line_uuid)

    def user_prompt(self, text: str) -> str:
        return self.add_line(
            {
                "type": "user",
                "message": {"role": "user", "content": text},
                "origin": {"kind": "human"},
                "turnOrigin": "human",
            }
        )

    def env_user_line(
        self, *, content: str, is_meta: bool = False, is_compact_summary: bool = False
    ) -> str:
        fields: dict[str, Any] = {"type": "user", "message": {"role": "user", "content": content}}
        if is_meta:
            fields["isMeta"] = True
        if is_compact_summary:
            fields["isCompactSummary"] = True
        return self.add_line(fields)

    def assistant_text(self, message_id: str, text: str) -> str:
        return self.add_line(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "id": message_id,
                    "model": MODEL_NAME,
                    "content": [{"type": "text", "text": text}],
                    "stop_reason": "end_turn",
                },
            }
        )

    def assistant_tool_use(
        self, message_id: str, tool_use_id: str, name: str, tool_input: dict[str, Any]
    ) -> str:
        return self.add_line(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "id": message_id,
                    "model": MODEL_NAME,
                    "content": [
                        {"type": "tool_use", "id": tool_use_id, "name": name, "input": tool_input}
                    ],
                    "stop_reason": "tool_use",
                },
            }
        )

    def tool_result(
        self,
        tool_use_id: str,
        *,
        content: str,
        is_error: bool,
        tool_use_result: Any,
    ) -> str:
        fields: dict[str, Any] = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": content,
                        "is_error": is_error,
                    }
                ],
            },
        }
        if tool_use_result is not None:
            fields["toolUseResult"] = tool_use_result
        return self.add_line(fields)

    def attachment_queued_command(self, prompt: str) -> str:
        return self.add_line(
            {"type": "attachment", "attachment": {"type": "queued_command", "prompt": prompt}}
        )


# --------------------------------------------------------------------------------------
# marker stripping
# --------------------------------------------------------------------------------------


def _strip_markers(final: str, labels_dsl: dict[str, Any]) -> tuple[str, list[LabelSpan]]:
    spans: list[LabelSpan] = []
    out: list[str] = []
    pos = 0
    length = 0
    for m in dsl.MARKER_RE.finditer(final):
        before = final[pos : m.start()]
        out.append(before)
        length += len(before)
        inner = m.group("text")
        start = length
        out.append(inner)
        length += len(inner)
        end = length
        type_token = m.group("type")
        base_type = type_token.split("#", 1)[0]
        entry = labels_dsl.get(type_token, {})
        spans.append(
            LabelSpan(
                type=base_type,
                start=start,
                end=end,
                label=entry.get("label", "supported"),
                reason=entry.get("reason"),
                suggest=entry.get("suggest"),
                partial=bool(entry.get("partial", False)),
            )
        )
        pos = m.end()
    out.append(final[pos:])
    return "".join(out), spans


# --------------------------------------------------------------------------------------
# step rendering
# --------------------------------------------------------------------------------------


def _step_kind(step: dict[str, Any]) -> str:
    return next(iter(set(step) & dsl.STEP_KEYS))


def _edit_payload(body: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    tool = body["tool"]
    path = body["path"]
    if tool == "Edit":
        args: dict[str, Any] = {
            "file_path": path,
            "old_string": body["old"],
            "new_string": body["new"],
        }
        tur: dict[str, Any] = {"filePath": path, "oldString": body["old"], "newString": body["new"]}
    elif tool == "Write":
        args = {"file_path": path, "content": body["content"]}
        tur = {"type": "create", "filePath": path, "content": body["content"]}
    elif tool == "MultiEdit":
        args = {"file_path": path, "edits": body["edits"]}
        tur = {"filePath": path}
    else:  # NotebookEdit
        args = {"notebook_path": path, "new_source": body["new_source"]}
        tur = {"filePath": path}
    return args, tur


def _bash_output(body: dict[str, Any]) -> str:
    output = body.get("output", "")
    rep = body.get("output_repeat")
    if rep:
        filler = "\n".join([rep["line"]] * rep["times"])
        return f"{filler}\n{output}" if output else filler
    return output


def _fail_content(exit_code: int, output: str) -> str:
    full = f"Exit code {exit_code}\n{output}"
    if len(full) > FAIL_CONTENT_CAP:
        omitted = len(full) - FAIL_CONTENT_CAP
        return full[:FAIL_CONTENT_CAP] + f"\n[{omitted} characters truncated]\n"
    return full


def _render_bash_result(
    fb: _FileBuilder, ctx: RenderContext, tool_use_id: str, body: dict[str, Any]
) -> None:
    exit_code = body.get("exit", 0)
    output = _bash_output(body)
    mode = body.get("mode", "foreground")

    if mode == "no_result":
        return

    if mode == "background":
        bg_id = "bg-" + tool_use_id[-8:]
        content = f"Command running in background with ID: {bg_id}"
        tur = {
            "stdout": "",
            "stderr": "",
            "interrupted": False,
            "isImage": False,
            "noOutputExpected": False,
            "backgroundTaskId": bg_id,
        }
        fb.tool_result(tool_use_id, content=content, is_error=False, tool_use_result=tur)
        notify = body.get("notify")
        if notify is not None:
            notify_exit = notify.get("exit", 0)
            status = "completed" if notify_exit == 0 else "failed"
            summary = (output[:200] or "background command finished") + f", exit code {notify_exit}"
            prompt = (
                "<task-notification>\n"
                f"<task-id>{bg_id}</task-id>\n"
                f"<tool-use-id>{tool_use_id}</tool-use-id>\n"
                f"<output-file>tool-results/{bg_id}.txt</output-file>\n"
                f"<status>{status}</status>\n"
                f"<summary>{summary}</summary>\n"
                "</task-notification>"
            )
            fb.attachment_queued_command(prompt)
        return

    if mode == "moved_to_background":
        bg_id = "bg-" + tool_use_id[-8:]
        content = f"Command timed out after 120s and was moved to background with ID: {bg_id}"
        tur = {"stdout": "", "stderr": "", "interrupted": False, "backgroundTaskId": bg_id}
        fb.tool_result(tool_use_id, content=content, is_error=False, tool_use_result=tur)
        return

    if mode == "interrupted":
        content = "[Request interrupted by user for tool use]"
        if output:
            content = content + "\n" + output
        fb.tool_result(tool_use_id, content=content, is_error=True, tool_use_result=content)
        return

    if mode == "timeout":
        content = f"Exit code 143\n{output}\nCommand timed out after 120s"
        fb.tool_result(tool_use_id, content=content, is_error=True, tool_use_result=content)
        return

    # foreground
    if exit_code == 0:
        if len(output) > PERSIST_THRESHOLD:
            basename = tool_use_id + ".txt"
            ctx.write_main_persisted_output(basename, output)
            preview = output[:2000]
            content = f"<persisted-output>\n{preview}\n</persisted-output>"
            tur = {
                "stdout": output[:PERSIST_THRESHOLD],
                "stderr": "",
                "interrupted": False,
                "isImage": False,
                "noOutputExpected": False,
                # Deliberately just the basename: `claude_code.parse` must fall back to
                # `<dir of transcript>/<sessionId>/tool-results/<basename>` to find it.
                "persistedOutputPath": basename,
                "persistedOutputSize": len(output),
            }
        else:
            content = output
            tur = {
                "stdout": output,
                "stderr": "",
                "interrupted": False,
                "isImage": False,
                "noOutputExpected": not bool(output),
            }
        fb.tool_result(tool_use_id, content=content, is_error=False, tool_use_result=tur)
    else:
        content = _fail_content(exit_code, output)
        fb.tool_result(tool_use_id, content=content, is_error=True, tool_use_result=content)


def _render_calls(
    fb: _FileBuilder, ctx: RenderContext, calls: list[tuple[str, dict[str, Any]]]
) -> None:
    message_id = ctx.next_message_id(fb.file_key)
    rendered: list[tuple[str, str, dict[str, Any]]] = []
    for kind, body in calls:
        tool_use_id = ctx.next_tool_use_id()
        if kind == "edit":
            args, _tur = _edit_payload(body)
            fb.assistant_tool_use(message_id, tool_use_id, body["tool"], args)
        else:
            bash_args: dict[str, Any] = {"command": body["cmd"]}
            if body.get("mode") == "background":
                bash_args["run_in_background"] = True
            fb.assistant_tool_use(message_id, tool_use_id, "Bash", bash_args)
        rendered.append((tool_use_id, kind, body))
    # Results are written in reverse call order to exercise id-based pairing on the read side.
    for tool_use_id, kind, body in reversed(rendered):
        if kind == "edit":
            _args, tur = _edit_payload(body)
            fb.tool_result(
                tool_use_id,
                content=f"Updated {body['path']}",
                is_error=False,
                tool_use_result=tur,
            )
        else:
            _render_bash_result(fb, ctx, tool_use_id, body)


def _render_env_entry(fb: _FileBuilder, ctx: RenderContext, body: dict[str, Any]) -> None:
    entry = body["entry"]
    text = body["text"]
    if entry == "hook_feedback":
        prefixes = ("Stop hook feedback", "SubagentStop hook feedback")
        content = text if text.startswith(prefixes) else f"Stop hook feedback: {text}"
        fb.env_user_line(content=content)
    elif entry == "task_notification":
        tool_use_id = ctx.next_tool_use_id()
        prompt = (
            "<task-notification>\n<task-id>manual</task-id>\n"
            f"<tool-use-id>{tool_use_id}</tool-use-id>\n"
            "<output-file>tool-results/manual.txt</output-file>\n"
            "<status>completed</status>\n"
            f"<summary>{text}</summary>\n</task-notification>"
        )
        fb.attachment_queued_command(prompt)
    elif entry == "meta":
        fb.env_user_line(content=text, is_meta=True)
    elif entry == "compaction":
        fb.env_user_line(content=text, is_compact_summary=True)
    elif entry == "local_command":
        fb.env_user_line(content=f"<local-command-stdout>{text}</local-command-stdout>")
    elif entry == "bash_mode":
        fb.env_user_line(content=f"<bash-input>{text}</bash-input>")


def _render_user_bash(fb: _FileBuilder, body: dict[str, Any]) -> None:
    fb.env_user_line(content=f"<bash-input>{body['cmd']}</bash-input>")
    output = body.get("output", "")
    if output:
        fb.env_user_line(content=f"<bash-stdout>{output}</bash-stdout>")


def _render_subagent(fb: _FileBuilder, ctx: RenderContext, body: dict[str, Any]) -> None:
    message_id = ctx.next_message_id(fb.file_key)
    tool_use_id = ctx.next_tool_use_id()
    fb.assistant_tool_use(
        message_id,
        tool_use_id,
        "Agent",
        {
            "subagent_type": body["type"],
            "description": body["prompt"][:60],
            "prompt": body["prompt"],
        },
    )
    parent_prefix_n = len(fb.lines)  # snapshot: after the call, before its result

    agent_index = ctx.next_agent_index()
    agent_id = str(ctx.agent_uuid(agent_index))
    sub_key = f"agent/{agent_index}"
    sub_fb = _FileBuilder(ctx, file_key=sub_key, is_sidechain=True)
    sub_fb.user_prompt(body["prompt"])
    _render_steps(sub_fb, ctx, body.get("steps", []))
    stripped, spans = _strip_markers(body["final"], body.get("labels", {}))
    sub_fb.assistant_text(ctx.next_message_id(sub_key), stripped)

    sub_dir = os.path.join(ctx.out_dir, ctx.session_id, "subagents")
    sub_path = os.path.join(sub_dir, f"agent-{agent_id}.jsonl")
    _write_jsonl(sub_path, sub_fb.lines)
    meta_path = os.path.join(sub_dir, f"agent-{agent_id}.meta.json")
    _write_json(
        meta_path,
        {
            "agentType": body["type"],
            "description": body["prompt"][:60],
            "toolUseId": tool_use_id,
            "spawnDepth": 1,
        },
    )
    ctx.subagent_paths.append(sub_path)

    parent_prefix_path = os.path.join(
        ctx.out_dir, f"{ctx.session_id}.stop-agent-{agent_index}.jsonl"
    )
    _write_jsonl(parent_prefix_path, fb.lines[:parent_prefix_n])

    payload = {
        "session_id": ctx.session_id,
        "transcript_path": parent_prefix_path,
        "cwd": ctx.root,
        "hook_event_name": "SubagentStop",
        "stop_hook_active": False,
        "last_assistant_message": stripped,
        "background_tasks": [],
        "session_crons": [],
        "agent_id": agent_id,
        "agent_type": body["type"],
        "agent_transcript_path": sub_path,
    }
    ctx.stop_cases.append(
        StopCase(
            scenario_id=ctx.scenario_id,
            turn_index=ctx.current_turn_index,
            attempt_index=ctx.current_attempt_index,
            session_path=parent_prefix_path,
            agent_transcript_path=sub_path,
            payload=payload,
            final_message=stripped,
            labels=spans,
            expect=None,
            project_files=ctx.project_files,
            config=ctx.config,
            env=ctx.env,
        )
    )

    fb.tool_result(tool_use_id, content=body["result"], is_error=False, tool_use_result=None)


def _render_steps(fb: _FileBuilder, ctx: RenderContext, steps_dsl: list[dict[str, Any]]) -> None:
    for step in steps_dsl:
        kind = _step_kind(step)
        body = step[kind]
        if kind in ("edit", "bash"):
            _render_calls(fb, ctx, [(kind, body)])
        elif kind == "parallel":
            calls = [(_step_kind(item), item[_step_kind(item)]) for item in body]
            _render_calls(fb, ctx, calls)
        elif kind == "subagent":
            _render_subagent(fb, ctx, body)
        elif kind == "text":
            fb.assistant_text(ctx.next_message_id(fb.file_key), body)
        elif kind == "env_entry":
            _render_env_entry(fb, ctx, body)
        elif kind == "user_bash":
            _render_user_bash(fb, body)
        elif kind == "queued_prompt":
            fb.attachment_queued_command(body)


# --------------------------------------------------------------------------------------
# file writing
# --------------------------------------------------------------------------------------


def _write_jsonl(path: str, lines: list[dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for obj in lines:
            fh.write(json.dumps(obj, separators=(",", ":"), ensure_ascii=False))
            fh.write("\n")


def _write_json(path: str, obj: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, sort_keys=True, ensure_ascii=False)
        fh.write("\n")


# --------------------------------------------------------------------------------------
# top level
# --------------------------------------------------------------------------------------


def render(scenario: dict[str, Any], out_dir: str) -> RenderedSession:
    """Render a validated scenario mapping (see `fixtures.dsl.load_scenario`) into `out_dir`."""
    scenario_id = scenario["id"]
    root = scenario.get("root", dsl.DEFAULT_ROOT)
    project_files: dict[str, str | None] = dict(scenario.get("project_files", {}))
    project_files.setdefault(".git", None)
    config = scenario.get("config")
    env = dict(scenario.get("env", {}))

    ctx = RenderContext(scenario_id=scenario_id, out_dir=out_dir, root=root)
    ctx.session_id = str(_uuid5(scenario_id, "session", 0))
    ctx.project_files = project_files
    ctx.config = config
    ctx.env = env

    mid_session = scenario.get("start", "fresh") == "mid_session"
    main_fb = _FileBuilder(ctx, file_key="", is_sidechain=False, mid_session=mid_session)

    for turn_index, turn in enumerate(scenario["turns"]):
        ctx.current_turn_index = turn_index
        main_fb.user_prompt(turn["user"])
        attempts = turn.get("attempts", [turn])
        for attempt_index, attempt in enumerate(attempts):
            ctx.current_attempt_index = attempt_index
            _render_steps(main_fb, ctx, attempt.get("steps", []))
            stripped, spans = _strip_markers(attempt["final"], attempt.get("labels", {}))
            main_fb.assistant_text(ctx.next_message_id(""), stripped)

            snapshot_n = len(main_fb.lines)
            stop_path = os.path.join(
                out_dir, f"{ctx.session_id}.stop-{turn_index}-{attempt_index}.jsonl"
            )
            _write_jsonl(stop_path, main_fb.lines[:snapshot_n])

            payload: dict[str, Any] = {
                "session_id": ctx.session_id,
                "transcript_path": stop_path,
                "cwd": root,
                "hook_event_name": "Stop",
                "stop_hook_active": attempt_index > 0,
                "last_assistant_message": stripped,
                "background_tasks": [],
                "session_crons": [],
            }
            payload.update(attempt.get("payload", {}))

            ctx.stop_cases.append(
                StopCase(
                    scenario_id=scenario_id,
                    turn_index=turn_index,
                    attempt_index=attempt_index,
                    session_path=stop_path,
                    agent_transcript_path=None,
                    payload=payload,
                    final_message=stripped,
                    labels=spans,
                    expect=attempt.get("expect"),
                    project_files=project_files,
                    config=config,
                    env=env,
                )
            )

            if attempt_index < len(attempts) - 1:
                feedback = (
                    f"Stop hook feedback: unsupported claim(s); "
                    f"attempt {attempt_index + 1} blocked."
                )
                main_fb.env_user_line(content=feedback)

    main_path = os.path.join(out_dir, f"{ctx.session_id}.jsonl")
    _write_jsonl(main_path, main_fb.lines)

    return RenderedSession(
        scenario_id=scenario_id,
        session_id=ctx.session_id,
        session_path=main_path,
        stop_cases=ctx.stop_cases,
        subagent_paths=ctx.subagent_paths,
    )


# --------------------------------------------------------------------------------------
# agent-trace/v1 export with synthetic-by-construction ground truth (ADR 9)
# --------------------------------------------------------------------------------------

_COMMAND_RULES = ("tests", "build", "lint", "typecheck", "deploy")
_FAILURE_REASONS = frozenset({"failed_exit", "failed_output", "empty_run", "masked_inconclusive"})


def ground_truth_for(session: Any, config: Any) -> dict[str, Any]:
    """ADR 9: `failure` if the last relevant command after the last edit failed, `success`
    if a full (non-partial) run after the last edit succeeded, `unknown` otherwise.

    "Relevant command" = a foreground segment that qualifies as evidence for one of the
    command-backed built-in rules (tests, build, lint, typecheck, deploy); "last edit" = the
    last edit event touching a file those rules treat as relevant."""
    from proof_of_done import evidence, shell

    root = session.cwd or "/"
    events = evidence.build_events(session, config, root)
    rules = [r for r in config.rules if r.id in _COMMAND_RULES and r.action != "off"]
    last_edit = None
    for edit in events.edits:
        touches = any(evidence._touches(edit, r.relevant_files, r.ignore_files) for r in rules)
        if touches and (last_edit is None or edit.pos > last_edit.pos):
            last_edit = edit
    latest = None
    for pos, seg, ce in evidence.iter_segments(events.commands):
        if ce.background or (last_edit is not None and pos <= last_edit.pos):
            continue
        for rule in rules:
            if evidence.segment_qualifies(
                seg,
                ce.cmd,
                rule.evidence.commands,
                rule.evidence.command_regex,
                rule.evidence.exclude_args,
                config.read_only_commands,
            ):
                latest = (seg, ce, rule)
                break
    outcome = "unknown"
    if latest is not None:
        seg, ce, rule = latest
        read_only_index = shell.build_prefix_index(config.read_only_commands)
        failure = evidence._segment_failure(seg, ce, rule.evidence, read_only_index)
        if failure is not None and failure[0] in _FAILURE_REASONS:
            outcome = "failure"
        elif failure is None and not shell.is_partial(seg, rule.evidence.partial_args):
            outcome = "success"
    return {
        "outcome": outcome,
        "checked_by": "none",
        "details": {"label_source": "synthetic-by-construction"},
    }


def export_agent_traces(scenario: dict[str, Any], out_dir: str) -> list[dict[str, Any]]:
    """Render `scenario`, then export the main session (and each subagent transcript) as
    agent-trace/v1 with the verdicts of every simulated stop attempt and the ADR 9 ground
    truth. Uses the built-in defaults, like `audit` without `--config`."""
    from proof_of_done import audit, traces
    from proof_of_done import config as config_mod
    from proof_of_done.transcript import claude_code

    rendered = render(scenario, out_dir)
    cfg, _layers = config_mod.load_for_audit(None)
    session = claude_code.parse(rendered.session_path)
    session.subagents = [
        claude_code.parse_subagent(p)
        for p in claude_code.find_subagent_transcripts(rendered.session_path)
    ]
    out: list[dict[str, Any]] = []
    for sess in [session, *session.subagents]:
        verdicts = audit.session_verdicts(sess, main_session=session, config=cfg)
        refs = (
            [
                {
                    "agent_id": sub.agent_id,
                    "agent_type": sub.agent_type,
                    "trace_id": traces.trace_id_for(sub, redact=False, salt=""),
                }
                for sub in session.subagents
            ]
            if sess is session
            else []
        )
        out.append(
            traces.export_session(
                sess,
                verdicts,
                cfg,
                redact=False,
                salt="",
                label=ground_truth_for(sess, cfg),
                subagent_refs=refs,
            )
        )
    return out


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="render.py",
        description="Render a scenario YAML into a Claude Code JSONL fixture tree "
        "(and optionally agent-trace/v1).",
    )
    parser.add_argument("scenario")
    parser.add_argument("--out", required=True)
    parser.add_argument(
        "--agent-trace",
        metavar="FILE",
        help="also write the session as agent-trace/v1 JSONL with synthetic ground truth",
    )
    args = parser.parse_args(argv)
    scenario = dsl.load_scenario(args.scenario)
    if args.agent_trace:
        traces_out = export_agent_traces(scenario, args.out)
        with open(args.agent_trace, "w", encoding="utf-8") as fh:
            for trace in traces_out:
                fh.write(json.dumps(trace, sort_keys=True) + "\n")
        print(f"wrote {len(traces_out)} trace(s) to {args.agent_trace}")
        return 0
    result = render(scenario, args.out)
    print(f"wrote {result.session_path} ({len(result.stop_cases)} stop case(s))")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
