"""Tests for `proof_of_done.transcript.agent_trace`: reading agent-trace/v1 JSONL back into
`Session`s, including traces from a producer other than proof-of-done itself (no
`meta.proof_of_done` at all), `args.command` -> `Bash` recognition, alternate edit-tool-name
canonicalization, `apply_patch` expansion, and subagent grouping.
"""

from __future__ import annotations

import json

from proof_of_done.transcript import agent_trace
from proof_of_done.transcript.model import (
    KIND_MESSAGE,
    KIND_TOOL_CALL,
    KIND_TOOL_RESULT,
    ROLE_AGENT,
    ROLE_ENVIRONMENT,
    ROLE_USER,
)


def _trace(
    trace_id: str = "s1",
    *,
    steps: list[dict] | None = None,
    meta: dict | None = None,
    task_id: str | None = None,
) -> dict:
    return {
        "schema": "agent-trace/v1",
        "trace_id": trace_id,
        "source": "some-other-tool/1.0.0",
        "task": {"id": task_id or trace_id, "domain": "coding", "instruction": "do it"},
        "steps": steps or [],
        "final_claim": {"text": "done", "claims": []},
        "ground_truth": {"outcome": "unknown", "checked_by": "none"},
        "meta": meta or {},
    }


def _step(i: int, kind: str, role: str, **extra: object) -> dict:
    base = {
        "i": i,
        "ts": f"2026-01-05T10:00:{i:02d}.000Z",
        "kind": kind,
        "role": role,
        "name": None,
        "content": None,
        "args": None,
        "ok": None,
        "output": None,
        "error": None,
    }
    base.update(extra)
    return base


def _write(tmp_path: object, lines: list[dict]) -> str:
    path = str(tmp_path) + "/traces.jsonl"  # type: ignore[operator]
    with open(path, "w", encoding="utf-8") as fh:
        for line in lines:
            fh.write(json.dumps(line) + "\n")
    return path


# ------------------------------------------------------------------------------------------
# looks_like_agent_trace
# ------------------------------------------------------------------------------------------


def test_looks_like_agent_trace_accepts_a_real_trace_line() -> None:
    line = json.dumps(_trace())
    assert agent_trace.looks_like_agent_trace(line) is True


def test_looks_like_agent_trace_rejects_a_different_schema() -> None:
    line = json.dumps({"schema": "agent-trace/v2"})
    assert agent_trace.looks_like_agent_trace(line) is False


def test_looks_like_agent_trace_rejects_garbage() -> None:
    for bad in ["not json", "", "[]", '"str"']:
        assert agent_trace.looks_like_agent_trace(bad) is False


# ------------------------------------------------------------------------------------------
# basic parsing (no meta.proof_of_done -- a foreign producer)
# ------------------------------------------------------------------------------------------


def test_message_steps_map_to_user_agent_environment(tmp_path: object) -> None:
    steps = [
        _step(0, "message", "user", content="Fix it"),
        _step(1, "message", "agent", content="Done."),
        _step(2, "message", "environment", content="a note", name=None),
    ]
    path = _write(tmp_path, [_trace(steps=steps)])
    sessions = agent_trace.parse_traces(path)
    assert len(sessions) == 1
    s = sessions[0]
    assert [(st.kind, st.role, st.content) for st in s.steps] == [
        (KIND_MESSAGE, ROLE_USER, "Fix it"),
        (KIND_MESSAGE, ROLE_AGENT, "Done."),
        (KIND_MESSAGE, ROLE_ENVIRONMENT, "a note"),
    ]


def test_session_id_comes_from_task_id(tmp_path: object) -> None:
    path = _write(tmp_path, [_trace(trace_id="s1:a1", task_id="s1")])
    session = agent_trace.parse_traces(path)[0]
    assert session.session_id == "s1"


def test_args_command_is_recognized_as_bash(tmp_path: object) -> None:
    steps = [
        _step(0, "tool_call", "agent", name="exec", args={"command": "uv run pytest -q"}),
        _step(
            1,
            "tool_result",
            "tool",
            name="exec",
            ok=True,
            output={"text": "1 passed", "exit_code": 0},
        ),
    ]
    path = _write(tmp_path, [_trace(steps=steps)])
    session = agent_trace.parse_traces(path)[0]
    call = next(st for st in session.steps if st.kind == KIND_TOOL_CALL)
    assert call.name == "Bash"
    assert call.args == {"command": "uv run pytest -q"}
    result = session.steps[session.steps.index(call) + 1]
    assert result.kind == KIND_TOOL_RESULT
    assert result.name == "Bash"
    assert result.ok is True
    assert result.exit_code == 0
    assert result.output_text == "1 passed"


def test_write_file_and_edit_file_are_canonicalized(tmp_path: object) -> None:
    steps = [
        _step(0, "tool_call", "agent", name="write_file", args={"path": "a.py", "content": "x"}),
        _step(1, "tool_result", "tool", name="write_file", ok=True),
        _step(2, "tool_call", "agent", name="edit_file", args={"file_path": "b.py"}),
        _step(3, "tool_result", "tool", name="edit_file", ok=True),
    ]
    path = _write(tmp_path, [_trace(steps=steps)])
    session = agent_trace.parse_traces(path)[0]
    calls = [st for st in session.steps if st.kind == KIND_TOOL_CALL]
    assert [(c.name, c.args) for c in calls] == [
        ("Write", {"path": "a.py", "content": "x", "file_path": "a.py"}),
        ("Edit", {"file_path": "b.py"}),
    ]


def test_apply_patch_expands_into_write_edit_pairs(tmp_path: object) -> None:
    patch = (
        "*** Begin Patch\n"
        "*** Add File: new_module.py\n"
        "+def f():\n"
        "+    return 1\n"
        "*** Delete File: old_module.py\n"
        "*** End Patch"
    )
    steps = [
        _step(0, "tool_call", "agent", name="apply_patch", args={"input": patch}),
        _step(
            1,
            "tool_result",
            "tool",
            name="apply_patch",
            ok=True,
            output={"text": "Done", "exit_code": None},
        ),
    ]
    path = _write(tmp_path, [_trace(steps=steps)])
    session = agent_trace.parse_traces(path)[0]
    calls = [st for st in session.steps if st.kind == KIND_TOOL_CALL]
    assert [(c.name, c.args["file_path"]) for c in calls] == [
        ("Write", "new_module.py"),
        ("Edit", "old_module.py"),
    ]
    for call in calls:
        result = session.steps[session.steps.index(call) + 1]
        assert result.kind == KIND_TOOL_RESULT
        assert result.tool_use_id == call.tool_use_id


def test_state_probe_kind_does_not_crash_the_adapter(tmp_path: object) -> None:
    steps = [_step(0, "state_probe", "environment", content="probe result")]
    path = _write(tmp_path, [_trace(steps=steps)])
    session = agent_trace.parse_traces(path)[0]
    assert len(session.steps) == 1


# ------------------------------------------------------------------------------------------
# meta.proof_of_done: tool_use_ids, step_flags
# ------------------------------------------------------------------------------------------


def test_tool_use_ids_and_step_flags_are_restored(tmp_path: object) -> None:
    steps = [
        _step(0, "tool_call", "agent", name="Bash", args={"command": "sleep 999"}),
        _step(1, "tool_result", "tool", name="Bash", ok=None),
        _step(2, "message", "environment", content="Stop hook feedback: try again"),
    ]
    meta = {
        "proof_of_done": {
            "tool_use_ids": {"0": "toolu_abc", "1": "toolu_abc"},
            "step_flags": {
                "1": {"background": True, "cwd": "/work/other"},
                "2": {"entry": "hook_feedback"},
            },
        }
    }
    path = _write(tmp_path, [_trace(steps=steps, meta=meta)])
    session = agent_trace.parse_traces(path)[0]
    call, result, env_step = session.steps
    assert call.tool_use_id == "toolu_abc"
    assert result.tool_use_id == "toolu_abc"
    assert result.background is True
    assert result.cwd == "/work/other"
    assert env_step.entry == "hook_feedback"


# ------------------------------------------------------------------------------------------
# subagent grouping
# ------------------------------------------------------------------------------------------


def test_subagent_traces_are_grouped_under_the_parent(tmp_path: object) -> None:
    parent_meta = {
        "proof_of_done": {
            "subagents": [{"agent_id": "a1", "agent_type": "general-purpose", "trace_id": "s1:a1"}]
        }
    }
    parent = _trace(
        trace_id="s1",
        steps=[_step(0, "message", "user", content="Go")],
        meta=parent_meta,
    )
    child = _trace(
        trace_id="s1:a1",
        steps=[_step(0, "message", "user", content="Investigate")],
    )
    path = _write(tmp_path, [parent, child])
    sessions = agent_trace.parse_traces(path)
    assert len(sessions) == 1  # the child is grouped, not top-level
    top = sessions[0]
    assert len(top.subagents) == 1
    sub = top.subagents[0]
    assert sub.agent_id == "a1"
    assert sub.agent_type == "general-purpose"
    assert sub.steps[0].content == "Investigate"


# ------------------------------------------------------------------------------------------
# robustness
# ------------------------------------------------------------------------------------------


def test_invalid_trace_line_is_skipped(tmp_path: object) -> None:
    valid = _trace(trace_id="ok")
    invalid = _trace(trace_id="bad")
    del invalid["ground_truth"]["outcome"]
    path = _write(tmp_path, [valid, invalid])
    sessions = agent_trace.parse_traces(path)
    assert [s.session_id for s in sessions] == ["ok"]


def test_non_agent_trace_line_is_skipped(tmp_path: object) -> None:
    path = str(tmp_path) + "/traces.jsonl"  # type: ignore[operator]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"schema": "something-else"}) + "\n")
        fh.write(json.dumps(_trace(trace_id="ok")) + "\n")
    sessions = agent_trace.parse_traces(path)
    assert [s.session_id for s in sessions] == ["ok"]


def test_corrupt_json_line_is_skipped(tmp_path: object) -> None:
    path = str(tmp_path) + "/traces.jsonl"  # type: ignore[operator]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("not json {\n")
        fh.write(json.dumps(_trace(trace_id="ok")) + "\n")
    sessions = agent_trace.parse_traces(path)
    assert [s.session_id for s in sessions] == ["ok"]


def test_unrecognized_file_yields_one_marker_session(tmp_path: object) -> None:
    path = str(tmp_path) + "/not-a-trace.jsonl"  # type: ignore[operator]
    with open(path, "w", encoding="utf-8") as fh:
        for i in range(60):
            fh.write(json.dumps({"something": "else", "n": i}) + "\n")
    sessions = agent_trace.parse_traces(path)
    assert len(sessions) == 1
    assert sessions[0].unrecognized is True


def test_short_unrecognized_file_yields_no_sessions(tmp_path: object) -> None:
    path = str(tmp_path) + "/short.jsonl"  # type: ignore[operator]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"something": "else"}) + "\n")
    assert agent_trace.parse_traces(path) == []
