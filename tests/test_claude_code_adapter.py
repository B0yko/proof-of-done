"""Round-trip tests: render every scenario in fixtures/scenarios/, parse it back with the
Claude Code adapter, and check the adapter's documented behavior.
"""

from __future__ import annotations

import glob
import json
import os

import pytest

from fixtures import dsl, render
from proof_of_done.transcript import claude_code
from proof_of_done.transcript.model import (
    ENTRY_BASH_MODE,
    ENTRY_COMPACTION,
    ENTRY_HOOK_FEEDBACK,
    ENTRY_LOCAL_COMMAND,
    ENTRY_META,
    ENTRY_TASK_NOTIFICATION,
    KIND_MESSAGE,
    KIND_TOOL_CALL,
    KIND_TOOL_RESULT,
    ROLE_AGENT,
    ROLE_ENVIRONMENT,
    ROLE_USER,
)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCENARIOS_DIR = os.path.join(_REPO_ROOT, "fixtures", "scenarios")
SCENARIO_PATHS = sorted(glob.glob(os.path.join(SCENARIOS_DIR, "*.yaml")))


def _render(scenario_id: str, out_dir: str) -> render.RenderedSession:
    scenario = dsl.load_scenario(os.path.join(SCENARIOS_DIR, f"{scenario_id}.yaml"))
    return render.render(scenario, out_dir)


@pytest.mark.parametrize("path", SCENARIO_PATHS, ids=[os.path.basename(p) for p in SCENARIO_PATHS])
def test_every_scenario_renders_and_parses(path: str, tmp_path: object) -> None:
    scenario = dsl.load_scenario(path)
    result = render.render(scenario, str(tmp_path))
    session = claude_code.parse(result.session_path)
    assert session.steps
    assert not session.unrecognized
    # every stop case's transcript prefix must itself parse cleanly
    for stop_case in result.stop_cases:
        claude_code.parse(stop_case.session_path)
        if stop_case.agent_transcript_path:
            sub = claude_code.parse_subagent(stop_case.agent_transcript_path)
            assert sub.agent_type


def test_first_step_of_every_scenario_is_a_typed_prompt(tmp_path: object) -> None:
    for path in SCENARIO_PATHS:
        scenario = dsl.load_scenario(path)
        result = render.render(scenario, str(tmp_path))
        session = claude_code.parse(result.session_path)
        first = session.steps[0]
        assert first.kind == KIND_MESSAGE
        assert first.role == ROLE_USER
        assert first.entry == ""  # a real typed prompt is not an environment entry
        assert first.content == scenario["turns"][0]["user"]


# --------------------------------------------------------------------------------------
# parallel calls: pairing order survives reverse-order result lines
# --------------------------------------------------------------------------------------


def test_parallel_calls_are_paired_in_call_order(tmp_path: str) -> None:
    result = _render("parallel-calls", tmp_path)
    session = claude_code.parse(result.session_path)
    kinds = [(s.kind, s.name) for s in session.steps]
    # user prompt, then Bash call/result, then Edit call/result, then the final message.
    assert kinds == [
        (KIND_MESSAGE, None),
        (KIND_TOOL_CALL, "Bash"),
        (KIND_TOOL_RESULT, "Bash"),
        (KIND_TOOL_CALL, "Edit"),
        (KIND_TOOL_RESULT, "Edit"),
        (KIND_MESSAGE, None),
    ]
    bash_call, bash_result, edit_call, edit_result = session.steps[1:5]
    assert bash_call.tool_use_id == bash_result.tool_use_id
    assert edit_call.tool_use_id == edit_result.tool_use_id
    assert bash_call.tool_use_id != edit_call.tool_use_id
    assert bash_result.ok is True
    assert bash_result.exit_code == 0
    assert edit_result.ok is True


# --------------------------------------------------------------------------------------
# background + notification
# --------------------------------------------------------------------------------------


def test_background_command_and_its_notification(tmp_path: str) -> None:
    result = _render("background-notification", tmp_path)
    session = claude_code.parse(result.session_path)
    bg_result = next(s for s in session.steps if s.kind == KIND_TOOL_RESULT and s.name == "Bash")
    assert bg_result.background is True
    assert bg_result.exit_code is None
    assert bg_result.ok is True

    notification = next(s for s in session.steps if s.entry == ENTRY_TASK_NOTIFICATION)
    assert notification.role == ROLE_ENVIRONMENT
    assert notification.tool_use_id == bg_result.tool_use_id
    assert notification.exit_code == 0


# --------------------------------------------------------------------------------------
# interrupted / no_result
# --------------------------------------------------------------------------------------


def test_interrupted_and_no_result(tmp_path: str) -> None:
    result = _render("interrupted-and-no-result", tmp_path)
    session = claude_code.parse(result.session_path)
    results = [s for s in session.steps if s.kind == KIND_TOOL_RESULT]
    assert len(results) == 1  # the second call never got a result line
    assert results[0].interrupted is True
    assert results[0].ok is False

    calls = [s for s in session.steps if s.kind == KIND_TOOL_CALL]
    assert len(calls) == 2
    # the second call's tool_use_id must not appear on any result step (no_result).
    assert calls[1].tool_use_id not in {r.tool_use_id for r in results}
    # and it must not be immediately followed by a tool_result step.
    idx = session.steps.index(calls[1])
    assert session.steps[idx + 1].kind != KIND_TOOL_RESULT


# --------------------------------------------------------------------------------------
# timeout / moved_to_background
# --------------------------------------------------------------------------------------


def test_timeout_and_moved_to_background(tmp_path: str) -> None:
    result = _render("timeout-and-moved", tmp_path)
    session = claude_code.parse(result.session_path)
    results = [s for s in session.steps if s.kind == KIND_TOOL_RESULT]
    assert len(results) == 2

    timed_out = results[0]
    assert timed_out.timed_out is True
    assert timed_out.exit_code == 143
    assert timed_out.ok is False

    moved = results[1]
    assert moved.timed_out is True
    assert moved.background is True
    assert moved.ok is True


# --------------------------------------------------------------------------------------
# subagent
# --------------------------------------------------------------------------------------


def test_subagent_transcript_and_meta(tmp_path: str) -> None:
    result = _render("subagent-investigation", tmp_path)
    subagent_stops = [sc for sc in result.stop_cases if sc.agent_transcript_path]
    assert len(subagent_stops) == 1
    stop = subagent_stops[0]

    paths = claude_code.find_subagent_transcripts(result.session_path)
    assert paths == [stop.agent_transcript_path]

    sub = claude_code.parse_subagent(paths[0])
    assert sub.agent_type == "general-purpose"
    assert sub.agent_id
    assert sub.steps[0].kind == KIND_MESSAGE
    assert sub.steps[0].role == ROLE_USER
    assert "flaky" in (sub.steps[0].content or "")
    assert sub.steps[-1].role == ROLE_AGENT
    assert "confirmed the fix" in (sub.steps[-1].content or "")

    # the parent's own transcript records the Agent call and the subagent's returned summary.
    session = claude_code.parse(result.session_path)
    agent_call = next(s for s in session.steps if s.kind == KIND_TOOL_CALL and s.name == "Agent")
    agent_result = next(
        s for s in session.steps if s.kind == KIND_TOOL_RESULT and s.name == "Agent"
    )
    assert agent_call.tool_use_id == agent_result.tool_use_id
    assert "race condition" in (agent_result.output_text or "")


# --------------------------------------------------------------------------------------
# attempts + hook feedback
# --------------------------------------------------------------------------------------


def test_attempts_each_get_their_own_truncated_stop_case(tmp_path: str) -> None:
    result = _render("attempts-retry", tmp_path)
    assert len(result.stop_cases) == 2
    first, second = result.stop_cases
    assert first.labels[0].label == "unsupported"
    assert second.labels[0].label == "supported"

    first_session = claude_code.parse(first.session_path)
    # attempt 1 has no hook-feedback entry yet, and no Bash call (only the Edit).
    assert not any(s.entry == ENTRY_HOOK_FEEDBACK for s in first_session.steps)
    assert not any(s.kind == KIND_TOOL_CALL and s.name == "Bash" for s in first_session.steps)

    second_session = claude_code.parse(second.session_path)
    assert any(s.entry == ENTRY_HOOK_FEEDBACK for s in second_session.steps)
    assert any(s.kind == KIND_TOOL_CALL and s.name == "Bash" for s in second_session.steps)
    # the second stop case's transcript is a strict continuation of the first's.
    assert len(second_session.steps) > len(first_session.steps)

    # the full (untruncated) transcript also ends with the second attempt's steps.
    full_session = claude_code.parse(result.session_path)
    assert len(full_session.steps) == len(second_session.steps)


# --------------------------------------------------------------------------------------
# mid-session start + every environment entry kind
# --------------------------------------------------------------------------------------


def test_mid_session_start_is_detected(tmp_path: str) -> None:
    result = _render("mid-session-env-entries", tmp_path)
    session = claude_code.parse(result.session_path)
    assert session.starts_mid_session is True

    entries = {s.entry for s in session.steps if s.role == ROLE_ENVIRONMENT}
    assert entries == {
        ENTRY_META,
        ENTRY_COMPACTION,
        ENTRY_LOCAL_COMMAND,
        ENTRY_HOOK_FEEDBACK,
        ENTRY_BASH_MODE,
    }

    # the queued prompt counts as a real typed prompt, not an environment entry.
    prompt_text = "Also bump the version number while you're at it"
    queued = next(s for s in session.steps if s.content == prompt_text)
    assert queued.kind == KIND_MESSAGE
    assert queued.role == ROLE_USER


def test_fresh_session_does_not_start_mid_session(tmp_path: str) -> None:
    result = _render("parallel-calls", tmp_path)
    session = claude_code.parse(result.session_path)
    assert session.starts_mid_session is False


# --------------------------------------------------------------------------------------
# long output: plain bound vs. persisted file
# --------------------------------------------------------------------------------------


def test_long_output_tail_is_not_lost_below_the_persist_threshold(tmp_path: str) -> None:
    result = _render("long-output-tail", tmp_path)
    session = claude_code.parse(result.session_path)
    bash_result = next(s for s in session.steps if s.kind == KIND_TOOL_RESULT)
    assert bash_result.ok is True
    assert (bash_result.output_text or "").rstrip().endswith("150 passed in 3.98s =====")


def test_persisted_output_tail_is_read_via_fallback_path(tmp_path: str) -> None:
    result = _render("long-output-persisted", tmp_path)
    session_dir = os.path.join(tmp_path, result.session_id)
    tool_results_dir = os.path.join(session_dir, "tool-results")
    assert os.path.isdir(tool_results_dir)
    assert os.listdir(tool_results_dir)  # the persisted file was actually written

    session = claude_code.parse(result.session_path)
    bash_result = next(s for s in session.steps if s.kind == KIND_TOOL_RESULT)
    assert bash_result.ok is True
    assert bash_result.exit_code == 0
    output = bash_result.output_text or ""
    assert output.startswith("tests/test_matrix.py::test_case PASSED")
    assert output.rstrip().endswith("900 passed in 41.17s =====")


# --------------------------------------------------------------------------------------
# _read_persisted_output containment: a persistedOutputPath from transcript JSON is
# untrusted and must never escape the session's own directory.
# --------------------------------------------------------------------------------------


def test_persisted_output_path_outside_the_session_dir_is_not_read(tmp_path) -> None:
    transcript_path = os.path.join(str(tmp_path), "transcript.jsonl")
    session_dir = os.path.join(str(tmp_path), "sess1")
    os.makedirs(os.path.join(session_dir, "tool-results"))

    outside = os.path.join(str(tmp_path), "secret.txt")
    with open(outside, "w", encoding="utf-8") as fh:
        fh.write("SECRET CONTENT")

    # No same-named file under the session's tool-results dir: falling back safely means
    # returning None, never the outside file's content.
    result = claude_code._read_persisted_output(transcript_path, "sess1", outside)
    assert result is None


def test_persisted_output_path_outside_prefers_the_safe_same_named_fallback(tmp_path) -> None:
    transcript_path = os.path.join(str(tmp_path), "transcript.jsonl")
    session_dir = os.path.join(str(tmp_path), "sess1")
    tool_results_dir = os.path.join(session_dir, "tool-results")
    os.makedirs(tool_results_dir)

    # A same-named, legitimate file inside the session's tool-results dir ...
    with open(os.path.join(tool_results_dir, "out.txt"), "w", encoding="utf-8") as fh:
        fh.write("FALLBACK CONTENT")

    # ... versus an attacker-shaped absolute path with the same basename, elsewhere on disk.
    outside_dir = os.path.join(str(tmp_path), "elsewhere")
    os.makedirs(outside_dir)
    with open(os.path.join(outside_dir, "out.txt"), "w", encoding="utf-8") as fh:
        fh.write("SECRET CONTENT")

    result = claude_code._read_persisted_output(
        transcript_path, "sess1", os.path.join(outside_dir, "out.txt")
    )
    assert result == "FALLBACK CONTENT"


def test_persisted_output_path_inside_the_session_dir_is_read_directly(tmp_path) -> None:
    transcript_path = os.path.join(str(tmp_path), "transcript.jsonl")
    session_dir = os.path.join(str(tmp_path), "sess1")
    os.makedirs(session_dir)
    direct_path = os.path.join(session_dir, "direct.txt")
    with open(direct_path, "w", encoding="utf-8") as fh:
        fh.write("DIRECT CONTENT")

    result = claude_code._read_persisted_output(transcript_path, "sess1", direct_path)
    assert result == "DIRECT CONTENT"


def test_persisted_output_path_absolute_with_no_session_id_is_never_read(tmp_path) -> None:
    transcript_path = os.path.join(str(tmp_path), "transcript.jsonl")
    outside = os.path.join(str(tmp_path), "secret.txt")
    with open(outside, "w", encoding="utf-8") as fh:
        fh.write("SECRET CONTENT")

    result = claude_code._read_persisted_output(transcript_path, None, outside)
    assert result is None


# --------------------------------------------------------------------------------------
# last_agent_text
# --------------------------------------------------------------------------------------


def test_last_agent_text_matches_the_stripped_final_message(tmp_path: str) -> None:
    result = _render("parallel-calls", tmp_path)
    text = claude_code.last_agent_text(result.session_path)
    assert text == result.stop_cases[-1].final_message


def test_last_agent_text_uses_the_last_attempt_not_the_first(tmp_path: str) -> None:
    result = _render("attempts-retry", tmp_path)
    text = claude_code.last_agent_text(result.session_path)
    assert text == result.stop_cases[-1].final_message
    assert text != result.stop_cases[0].final_message


def test_last_agent_text_joins_trailing_text_blocks_of_the_last_message_id(tmp_path: str) -> None:
    # A subagent transcript ends with a single assistant text message; last_agent_text should
    # return exactly that message even though earlier assistant turns exist in the same file.
    result = _render("subagent-investigation", tmp_path)
    sub_path = result.stop_cases[0].agent_transcript_path
    assert sub_path is not None
    text = claude_code.last_agent_text(sub_path)
    assert text == result.stop_cases[0].final_message


# --------------------------------------------------------------------------------------
# robustness: unrecognized format, corrupt lines
# --------------------------------------------------------------------------------------


def test_unrecognized_format_is_flagged(tmp_path: str) -> None:
    path = os.path.join(tmp_path, "not-a-transcript.jsonl")
    with open(path, "w", encoding="utf-8") as fh:
        for i in range(60):
            fh.write(json.dumps({"kind": "something-else", "n": i}) + "\n")
    session = claude_code.parse(path)
    assert session.unrecognized is True
    assert session.steps == []


def test_short_unrecognized_file_is_not_flagged(tmp_path: str) -> None:
    # Fewer than the 50-line threshold: empty, but not "unrecognized".
    path = os.path.join(tmp_path, "short.jsonl")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"kind": "something-else"}) + "\n")
    session = claude_code.parse(path)
    assert session.unrecognized is False
    assert session.steps == []


def test_corrupt_lines_are_skipped_not_raised(tmp_path: str) -> None:
    result = _render("parallel-calls", tmp_path)
    with open(result.session_path, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    good_session = claude_code.parse(result.session_path)

    # Corrupt one line (truncate its JSON) and one more (garbage), keep the rest intact.
    lines[2] = lines[2][: len(lines[2]) // 2]
    lines.insert(3, "not json at all {")
    corrupt_path = os.path.join(tmp_path, "corrupt.jsonl")
    with open(corrupt_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")

    session = claude_code.parse(corrupt_path)  # must not raise
    assert not session.unrecognized
    assert len(session.steps) < len(good_session.steps)


def test_parse_never_raises_on_empty_file(tmp_path: str) -> None:
    path = os.path.join(tmp_path, "empty.jsonl")
    open(path, "w", encoding="utf-8").close()
    session = claude_code.parse(path)
    assert session.steps == []
    assert session.unrecognized is False


def test_max_bytes_caps_reading(tmp_path: str) -> None:
    result = _render("mid-session-env-entries", tmp_path)
    full = claude_code.parse(result.session_path)
    capped = claude_code.parse(result.session_path, max_bytes=200)
    assert len(capped.steps) < len(full.steps)


def test_now_reader_fills_a_missing_timestamp(tmp_path: str) -> None:
    result = _render("parallel-calls", tmp_path)
    with open(result.session_path, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    obj = json.loads(lines[0])
    del obj["timestamp"]
    lines[0] = json.dumps(obj)
    path = os.path.join(tmp_path, "no-ts.jsonl")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")

    session = claude_code.parse(path, now_reader=lambda: "1999-01-01T00:00:00.000Z")
    assert session.steps[0].ts == "1999-01-01T00:00:00.000Z"
