"""Tests for the experimental Codex CLI rollout adapter (`transcript/codex.py`): envelope
sniffing, message classification, call/result pairing, exit-code extraction and `apply_patch`
edit extraction against the hand-authored fixtures under `fixtures/codex/`, plus a few small
ad-hoc transcripts for edge cases those fixtures don't cover. The last section runs the real
`evidence.build_events` + `evidence.judge` pipeline (unmodified, exactly as the Claude Code
adapter's own output is judged) on each fixture to show the verdict it was authored to produce.

Never reads real Codex logs (`~/.codex/**`); every fixture here -- and every inline transcript
built in this file -- is hand-authored from the source definitions cited in
`proof_of_done.transcript.codex`'s module docstring, never captured from a real session. This
adapter is not registered anywhere yet (no `--source codex`, no `auto` detection, no audit
end-to-end test): a later step wires that up.
"""

from __future__ import annotations

import json
import os

import pytest
from evidence_helpers import real_defaults

from proof_of_done import evidence
from proof_of_done.transcript import codex
from proof_of_done.transcript.model import (
    ENTRY_META,
    KIND_MESSAGE,
    KIND_TOOL_CALL,
    KIND_TOOL_RESULT,
    ROLE_AGENT,
    ROLE_ENVIRONMENT,
    ROLE_USER,
)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES_DIR = os.path.join(_REPO_ROOT, "fixtures", "codex")
ROOT = "/work/demo-app"
CFG = real_defaults(ROOT)

FIXTURE_NAMES = [
    "passing-after-edit",
    "stale-after-apply-patch",
    "failing-exit-code",
    "claim-free",
]


def _path(name: str) -> str:
    return os.path.join(FIXTURES_DIR, f"{name}.jsonl")


def _first_line(name: str) -> str:
    with open(_path(name), encoding="utf-8") as fh:
        return fh.readline()


def _write(tmp_path: object, lines: list[dict]) -> str:
    path = os.path.join(str(tmp_path), "session.jsonl")
    with open(path, "w", encoding="utf-8") as fh:
        for line in lines:
            fh.write(json.dumps(line) + "\n")
    return path


def _session_meta(session_id: str = "s1", cwd: str = ROOT) -> dict:
    return {
        "timestamp": "2026-01-01T00:00:00.000Z",
        "type": "session_meta",
        "payload": {"session_id": session_id, "id": session_id, "cwd": cwd},
    }


# --------------------------------------------------------------------------------------
# looks_like_codex
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_looks_like_codex_accepts_every_fixture(name: str) -> None:
    assert codex.looks_like_codex(_first_line(name)) is True


def test_looks_like_codex_rejects_a_non_session_meta_first_line() -> None:
    line = json.dumps({"type": "response_item", "payload": {"type": "message"}})
    assert codex.looks_like_codex(line) is False


@pytest.mark.parametrize("line", ["not json", "", '"just a string"', "[]"])
def test_looks_like_codex_rejects_garbage(line: str) -> None:
    assert codex.looks_like_codex(line) is False


# --------------------------------------------------------------------------------------
# parsing: session_meta, message classification
# --------------------------------------------------------------------------------------


def test_session_meta_gives_session_id_cwd_and_source_format() -> None:
    session = codex.parse(_path("passing-after-edit"))
    assert session.session_id == "th_fixture_pass_0001"
    assert session.cwd == ROOT
    assert session.source_format == "codex"


def test_environment_context_tag_is_an_environment_entry_not_a_prompt() -> None:
    session = codex.parse(_path("passing-after-edit"))
    first = session.steps[0]
    assert first.kind == KIND_MESSAGE
    assert first.role == ROLE_ENVIRONMENT
    assert first.entry == ENTRY_META
    assert first.content is not None
    assert first.content.startswith("<environment_context>")

    prompt = session.steps[1]
    assert prompt.role == ROLE_USER
    assert prompt.entry == ""
    assert (
        prompt.content == "Fix the off-by-one bug in src/app/models.py and confirm the tests pass."
    )


def test_user_instructions_tag_is_also_an_environment_entry() -> None:
    session = codex.parse(_path("claim-free"))
    first = session.steps[0]
    assert first.role == ROLE_ENVIRONMENT
    assert first.entry == ENTRY_META
    assert first.content is not None
    assert first.content.startswith("<user_instructions>")


def test_assistant_text_becomes_an_agent_message() -> None:
    session = codex.parse(_path("passing-after-edit"))
    last = session.steps[-1]
    assert last.kind == KIND_MESSAGE
    assert last.role == ROLE_AGENT
    assert last.content == "All tests pass now."


def test_first_step_of_a_fresh_session_prompt_is_a_typed_prompt() -> None:
    session = codex.parse(_path("stale-after-apply-patch"))
    first = session.steps[0]
    assert first.kind == KIND_MESSAGE
    assert first.role == ROLE_USER
    assert first.entry == ""


# --------------------------------------------------------------------------------------
# pairing: a call is directly followed by its result
# --------------------------------------------------------------------------------------


def test_bash_call_is_directly_followed_by_its_result() -> None:
    session = codex.parse(_path("passing-after-edit"))
    call = next(s for s in session.steps if s.kind == KIND_TOOL_CALL and s.name == "Bash")
    idx = session.steps.index(call)
    result = session.steps[idx + 1]
    assert result.kind == KIND_TOOL_RESULT
    assert result.name == "Bash"
    assert result.tool_use_id == call.tool_use_id == "call_2"


def test_apply_patch_expands_to_one_call_result_pair_per_file() -> None:
    # failing-exit-code's apply_patch is one "Update File" hunk with a "Move to", so it
    # reports two edit targets from a single custom_tool_call.
    session = codex.parse(_path("failing-exit-code"))
    calls = [s for s in session.steps if s.kind == KIND_TOOL_CALL and s.name == "Edit"]
    assert [c.args["file_path"] for c in calls if c.args] == [
        "src/app/models.py",
        "src/app/arithmetic.py",
    ]
    for call in calls:
        idx = session.steps.index(call)
        result = session.steps[idx + 1]
        assert result.kind == KIND_TOOL_RESULT
        assert result.tool_use_id == call.tool_use_id
        assert result.ok is True


def test_local_shell_call_pairs_with_function_call_output_by_call_id() -> None:
    session = codex.parse(_path("failing-exit-code"))
    call = next(s for s in session.steps if s.kind == KIND_TOOL_CALL and s.name == "Bash")
    assert call.args == {"command": "bash -lc 'uv run pytest -q'"}
    idx = session.steps.index(call)
    result = session.steps[idx + 1]
    assert result.tool_use_id == call.tool_use_id == "call_2"


def test_parallel_style_ordering_survives_an_out_of_order_result(tmp_path: object) -> None:
    # The Bash call's output line is separated from its call by an unrelated apply_patch
    # call/result pair -- pairing must still land the Bash result directly after the Bash call.
    lines = [
        _session_meta(),
        {
            "timestamp": "t1",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "go"}],
            },
        },
        {
            "timestamp": "t2",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "call_id": "bash_1",
                "arguments": json.dumps({"cmd": "uv run pytest -q"}),
            },
        },
        {
            "timestamp": "t3",
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "call_id": "patch_1",
                "name": "apply_patch",
                "input": "*** Begin Patch\n*** Add File: notes.txt\n+hi\n*** End Patch",
            },
        },
        {
            "timestamp": "t4",
            "type": "response_item",
            "payload": {"type": "custom_tool_call_output", "call_id": "patch_1", "output": "Done"},
        },
        {
            "timestamp": "t5",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "bash_1",
                "output": "Exit code: 0\nOutput:\nok\n",
            },
        },
    ]
    session = codex.parse(_write(tmp_path, lines))
    kinds = [(s.kind, s.name) for s in session.steps]
    # The Bash result is moved to sit directly after the Bash call, ahead of the apply_patch
    # pair that appeared between the Bash call and its own output line in the raw file.
    assert kinds == [
        (KIND_MESSAGE, None),
        (KIND_TOOL_CALL, "Bash"),
        (KIND_TOOL_RESULT, "Bash"),
        (KIND_TOOL_CALL, "Write"),
        (KIND_TOOL_RESULT, "Write"),
    ]


# --------------------------------------------------------------------------------------
# exit codes
# --------------------------------------------------------------------------------------


def test_exit_code_zero_from_a_content_list_output() -> None:
    session = codex.parse(_path("passing-after-edit"))
    result = next(s for s in session.steps if s.kind == KIND_TOOL_RESULT and s.name == "Bash")
    assert result.exit_code == 0
    assert result.ok is True
    assert result.output_text is not None
    assert "42 passed" in result.output_text


def test_exit_code_one_from_a_plain_string_output() -> None:
    session = codex.parse(_path("failing-exit-code"))
    result = next(s for s in session.steps if s.kind == KIND_TOOL_RESULT and s.name == "Bash")
    assert result.exit_code == 1
    assert result.ok is False
    assert result.output_text is not None
    assert "1 failed, 41 passed" in result.output_text


def test_exit_code_is_unknown_without_an_exit_code_line(tmp_path: object) -> None:
    lines = [
        _session_meta(),
        {
            "timestamp": "t1",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "call_id": "c1",
                "arguments": json.dumps({"cmd": "uv run pytest -q"}),
            },
        },
        {
            "timestamp": "t2",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "c1",
                "output": "no exit marker here",
            },
        },
    ]
    session = codex.parse(_write(tmp_path, lines))
    result = next(s for s in session.steps if s.kind == KIND_TOOL_RESULT)
    assert result.exit_code is None
    assert result.ok is None


def test_timed_out_marker_sets_the_timed_out_flag(tmp_path: object) -> None:
    lines = [
        _session_meta(),
        {
            "timestamp": "t1",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "call_id": "c1",
                "arguments": json.dumps({"cmd": "sleep 999"}),
            },
        },
        {
            "timestamp": "t2",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "c1",
                "output": "command timed out after 60000 milliseconds\n",
            },
        },
    ]
    session = codex.parse(_write(tmp_path, lines))
    result = next(s for s in session.steps if s.kind == KIND_TOOL_RESULT)
    assert result.timed_out is True


# --------------------------------------------------------------------------------------
# shell function-name variants and workdir
# --------------------------------------------------------------------------------------


def test_legacy_shell_and_container_exec_names_are_recognized(tmp_path: object) -> None:
    lines = [
        _session_meta(),
        {
            "timestamp": "t1",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "shell",
                "call_id": "c1",
                "arguments": json.dumps({"command": ["bash", "-lc", "echo hi"], "workdir": ROOT}),
            },
        },
        {
            "timestamp": "t2",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "c1",
                "output": "Exit code: 0\nOutput:\nhi\n",
            },
        },
        {
            "timestamp": "t3",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "container.exec",
                "call_id": "c2",
                "arguments": json.dumps({"cmd": "echo bye"}),
            },
        },
        {
            "timestamp": "t4",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "c2",
                "output": "Exit code: 0\nOutput:\nbye\n",
            },
        },
    ]
    session = codex.parse(_write(tmp_path, lines))
    calls = [s for s in session.steps if s.kind == KIND_TOOL_CALL]
    assert [c.args["command"] for c in calls if c.args] == ["bash -lc 'echo hi'", "echo bye"]
    assert calls[0].cwd == ROOT


def test_unrecognized_function_call_name_is_ignored(tmp_path: object) -> None:
    lines = [
        _session_meta(),
        {
            "timestamp": "t1",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "web_search",
                "call_id": "c1",
                "arguments": "{}",
            },
        },
    ]
    session = codex.parse(_write(tmp_path, lines))
    assert session.steps == []


def test_other_rollout_item_and_response_item_types_are_ignored(tmp_path: object) -> None:
    lines = [
        _session_meta(),
        {"timestamp": "t1", "type": "event_msg", "payload": {"anything": "goes"}},
        {"timestamp": "t2", "type": "token_usage_record", "payload": {"total_tokens": 42}},
        {
            "timestamp": "t3",
            "type": "response_item",
            "payload": {"type": "reasoning", "id": None, "summary": []},
        },
    ]
    session = codex.parse(_write(tmp_path, lines))
    assert session.steps == []


def test_flattened_response_item_without_a_payload_wrapper_is_still_read(tmp_path: object) -> None:
    # Defensive fallback: a line whose top-level "type" is directly a ResponseItem type
    # (no "response_item" + "payload" wrapping) is read the same way.
    lines = [
        _session_meta(),
        {
            "timestamp": "t1",
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "hi"}],
        },
    ]
    session = codex.parse(_write(tmp_path, lines))
    assert len(session.steps) == 1
    assert session.steps[0].role == ROLE_USER
    assert session.steps[0].content == "hi"


# --------------------------------------------------------------------------------------
# apply_patch edit extraction
# --------------------------------------------------------------------------------------


def test_apply_patch_add_and_delete_file_targets(tmp_path: object) -> None:
    patch = (
        "*** Begin Patch\n"
        "*** Add File: src/app/new_module.py\n"
        "+def new():\n"
        "+    return 1\n"
        "*** Delete File: src/app/old_module.py\n"
        "*** End Patch"
    )
    lines = [
        _session_meta(),
        {
            "timestamp": "t1",
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "call_id": "c1",
                "name": "apply_patch",
                "input": patch,
            },
        },
        {
            "timestamp": "t2",
            "type": "response_item",
            "payload": {"type": "custom_tool_call_output", "call_id": "c1", "output": "Done"},
        },
    ]
    session = codex.parse(_write(tmp_path, lines))
    calls = [s for s in session.steps if s.kind == KIND_TOOL_CALL]
    assert [(c.name, c.args["file_path"]) for c in calls if c.args] == [
        ("Write", "src/app/new_module.py"),
        ("Edit", "src/app/old_module.py"),
    ]
    assert calls[0].args is not None
    assert calls[0].args["content"] == "+def new():\n+    return 1"


def test_apply_patch_move_to_adds_a_second_edit_target() -> None:
    session = codex.parse(_path("failing-exit-code"))
    calls = [s for s in session.steps if s.kind == KIND_TOOL_CALL and s.name == "Edit"]
    paths = [c.args["file_path"] for c in calls if c.args]
    assert paths == ["src/app/models.py", "src/app/arithmetic.py"]


def test_non_apply_patch_custom_tool_call_is_ignored(tmp_path: object) -> None:
    lines = [
        _session_meta(),
        {
            "timestamp": "t1",
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "call_id": "c1",
                "name": "some_other_tool",
                "input": "...",
            },
        },
    ]
    session = codex.parse(_write(tmp_path, lines))
    assert session.steps == []


# --------------------------------------------------------------------------------------
# robustness: bad lines, empty file, unrecognized format
# --------------------------------------------------------------------------------------


def test_corrupt_lines_are_skipped_not_raised(tmp_path: object) -> None:
    good_message = json.dumps(
        {
            "timestamp": "t2",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "ok"}],
            },
        }
    )
    raw_lines = [
        json.dumps(_session_meta()),
        "not json at all {",
        good_message[:10],  # truncated JSON
        good_message,
    ]
    path = os.path.join(str(tmp_path), "corrupt.jsonl")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(raw_lines) + "\n")

    session = codex.parse(path)  # must not raise
    assert len(session.steps) == 1
    assert session.steps[0].content == "ok"


def test_parse_never_raises_on_empty_file(tmp_path: object) -> None:
    path = os.path.join(str(tmp_path), "empty.jsonl")
    open(path, "w", encoding="utf-8").close()
    session = codex.parse(path)
    assert session.steps == []
    assert session.unrecognized is False


def test_unrecognized_format_is_flagged(tmp_path: object) -> None:
    path = os.path.join(str(tmp_path), "not-codex.jsonl")
    with open(path, "w", encoding="utf-8") as fh:
        for i in range(60):
            fh.write(json.dumps({"kind": "something-else", "n": i}) + "\n")
    session = codex.parse(path)
    assert session.unrecognized is True
    assert session.steps == []


def test_short_unrecognized_file_is_not_flagged(tmp_path: object) -> None:
    path = os.path.join(str(tmp_path), "short.jsonl")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"kind": "something-else"}) + "\n")
    session = codex.parse(path)
    assert session.unrecognized is False


# --------------------------------------------------------------------------------------
# end to end: evidence.build_events + evidence.judge on each fixture (expected verdict)
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name, expected_supported, expected_reason",
    [
        ("passing-after-edit", True, "supported"),
        ("stale-after-apply-patch", False, "stale"),
        ("failing-exit-code", False, "failed_exit"),
        ("claim-free", False, "no_command"),
    ],
    ids=FIXTURE_NAMES,
)
def test_fixture_verdict_via_build_events_and_judge(
    name: str, expected_supported: bool, expected_reason: str
) -> None:
    session = codex.parse(_path(name))
    events = evidence.build_events(session, CFG, ROOT)  # type: ignore[arg-type]
    verdict = evidence.judge("tests_passed", events, len(session.steps), CFG)  # type: ignore[arg-type]
    assert verdict.supported is expected_supported
    assert verdict.reason == expected_reason


def test_failing_exit_code_fixture_verdict_reports_exit_code_one() -> None:
    session = codex.parse(_path("failing-exit-code"))
    events = evidence.build_events(session, CFG, ROOT)  # type: ignore[arg-type]
    verdict = evidence.judge("tests_passed", events, len(session.steps), CFG)  # type: ignore[arg-type]
    assert verdict.details.exit_code == 1


def test_claim_free_fixture_has_no_edits_and_no_commands() -> None:
    # Nothing was changed and nothing was run: build_events must find no evidence either way,
    # matching the fixture's name (no claim was made, and none would have been supportable).
    session = codex.parse(_path("claim-free"))
    events = evidence.build_events(session, CFG, ROOT)  # type: ignore[arg-type]
    assert events.edits == []
    assert events.commands == []
