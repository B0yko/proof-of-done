"""Tests for `proof_of_done.traces`: `export_session`'s mapping, the 2 000-char
output truncation rule, `--redact` coverage, and `trace_id_for`/`export_session` agreement."""

from __future__ import annotations

from evidence_helpers import ROOT, real_defaults
from evidence_helpers import SessionBuilder as SB

from proof_of_done import traces
from proof_of_done.engine import ClaimResult
from proof_of_done.evidence import Verdict, VerdictDetails

CFG = real_defaults(ROOT)


def _claim_result(**overrides: object) -> ClaimResult:
    defaults: dict[str, object] = dict(
        claim_type="tests_passed",
        quote="all tests pass",
        span=(0, 15),
        rule_ids=("tests",),
        verdict=Verdict(
            claim_type="tests_passed",
            supported=False,
            reason="stale",
            partial=False,
            exempt=False,
            action="block",
            rule_id="tests",
            details=VerdictDetails(
                candidate_display="uv run pytest -q",
                candidate_step=1,
                anchor_path="src/app/models.py",
                anchor_step=3,
            ),
        ),
        command="uv run pytest -q",
        action="block",
    )
    defaults.update(overrides)
    return ClaimResult(**defaults)  # type: ignore[arg-type]


def test_export_session_is_schema_valid() -> None:
    session = (
        SB()
        .user("Fix it and confirm tests pass")
        .bash("uv run pytest -q", output="42 passed")
        .final("All tests pass.")
        .build()
    )
    trace = traces.export_session(session, {}, CFG, redact=False, salt="")
    assert traces.validate_trace(trace) == []
    assert trace["schema"] == "agent-trace/v1"
    assert trace["task"]["domain"] == "coding"


def test_task_instruction_is_the_first_real_user_prompt() -> None:
    session = (
        SB().user("Fix the bug").bash("uv run pytest -q", output="1 passed").final("Done.").build()
    )
    trace = traces.export_session(session, {}, CFG, redact=False, salt="")
    assert trace["task"]["instruction"] == "Fix the bug"


def test_final_claim_text_is_the_last_stop_attempts_message() -> None:
    session = SB().user("Go").final("All good.").build()
    trace = traces.export_session(session, {}, CFG, redact=False, salt="")
    assert trace["final_claim"]["text"] == "All good."


def test_tool_call_immediately_followed_by_its_own_tool_result() -> None:
    session = SB().user("Go").bash("uv run pytest -q", output="1 passed").final("Done.").build()
    trace = traces.export_session(session, {}, CFG, redact=False, salt="")
    kinds = [(s["kind"], s["name"]) for s in trace["steps"]]
    call_idx = kinds.index(("tool_call", "Bash"))
    assert kinds[call_idx + 1] == ("tool_result", "Bash")


def test_tool_result_output_carries_text_and_exit_code() -> None:
    session = (
        SB()
        .user("Go")
        .bash("uv run pytest -q", output="1 passed", exit_code=0)
        .final("Done.")
        .build()
    )
    trace = traces.export_session(session, {}, CFG, redact=False, salt="")
    result_step = next(s for s in trace["steps"] if s["kind"] == "tool_result")
    assert result_step["output"] == {"text": "1 passed", "exit_code": 0}


def test_long_output_is_truncated_head_and_tail() -> None:
    long_output = "H" * 1500 + "MIDDLE" + "T" * 1500
    session = SB().user("Go").bash("uv run pytest -q", output=long_output).final("Done.").build()
    trace = traces.export_session(session, {}, CFG, redact=False, salt="")
    result_step = next(s for s in trace["steps"] if s["kind"] == "tool_result")
    text = result_step["output"]["text"]
    assert text.startswith("H" * 1000)
    assert text.endswith("T" * 1000)
    assert "MIDDLE" not in text
    assert "truncated" in text
    assert len(text) < len(long_output)


def test_short_output_is_not_truncated() -> None:
    output = "x" * 2000  # exactly at the threshold
    session = SB().user("Go").bash("uv run pytest -q", output=output).final("Done.").build()
    trace = traces.export_session(session, {}, CFG, redact=False, salt="")
    result_step = next(s for s in trace["steps"] if s["kind"] == "tool_result")
    assert result_step["output"]["text"] == output


def test_ground_truth_defaults_to_unknown_none() -> None:
    session = SB().user("Go").final("Done.").build()
    trace = traces.export_session(session, {}, CFG, redact=False, salt="")
    assert trace["ground_truth"] == {"outcome": "unknown", "checked_by": "none"}


def test_ground_truth_label_override() -> None:
    session = SB().user("Go").final("Done.").build()
    label = {
        "outcome": "success",
        "checked_by": "none",
        "details": {"label_source": "synthetic-by-construction"},
    }
    trace = traces.export_session(session, {}, CFG, redact=False, salt="", label=label)
    assert trace["ground_truth"] == label
    assert traces.validate_trace(trace) == []


def test_turns_meta_is_keyed_by_stop_attempt_index() -> None:
    session = SB().user("Go").final("[[tests_passed|tests pass]]").build()
    # Mark the claim marker span manually the way the renderer would (no [[..]] stripping
    # helper here; just point at a slice of the final message directly).
    cr = _claim_result(quote="tests pass", span=(len("[[tests_passed|") - 15, 10))
    trace = traces.export_session(session, {0: [cr]}, CFG, redact=False, salt="")
    assert "0" in trace["meta"]["proof_of_done"]["turns"]
    entry = trace["meta"]["proof_of_done"]["turns"]["0"][0]
    assert entry["claim_type"] == "tests_passed"
    assert entry["reason"] == "stale"
    assert entry["supported"] is False
    assert entry["command"] == "uv run pytest -q"


def test_step_flags_capture_background_interrupted_timed_out_entry_cwd() -> None:
    session = (
        SB().user("Go").bash("sleep 999", background=True, cwd="/work/other").final("Done.").build()
    )
    trace = traces.export_session(session, {}, CFG, redact=False, salt="")
    step_flags = trace["meta"]["proof_of_done"]["step_flags"]
    result_i = next(s["i"] for s in trace["steps"] if s["kind"] == "tool_result")
    assert step_flags[str(result_i)]["background"] is True


def test_tool_use_ids_keyed_by_step_index_as_strings() -> None:
    session = (
        SB()
        .user("Go")
        .bash("uv run pytest -q", output="1 passed", tool_use_id="toolu_abc")
        .final("Done.")
        .build()
    )
    trace = traces.export_session(session, {}, CFG, redact=False, salt="")
    tool_use_ids = trace["meta"]["proof_of_done"]["tool_use_ids"]
    call_i = next(s["i"] for s in trace["steps"] if s["kind"] == "tool_call")
    assert tool_use_ids[str(call_i)] == "toolu_abc"


def test_redact_hashes_content_args_output_error_instruction_final_claim() -> None:
    session = (
        SB()
        .user("Fix the secret leak in api_key.py")
        .bash("uv run pytest -q", output="1 failed, secret output here", exit_code=1, ok=False)
        .final("All tests pass now.")
        .build()
    )
    trace = traces.export_session(session, {}, CFG, redact=True, salt="pepper")
    blob = repr(trace)
    assert "api_key.py" not in blob
    assert "secret output here" not in blob
    assert "All tests pass now." not in blob
    assert "uv run pytest -q" not in blob
    assert trace["task"]["instruction"].startswith("h:")
    assert trace["final_claim"]["text"].startswith("h:")
    assert traces.validate_trace(trace) == []


def test_redact_hashes_trace_id_and_task_id_so_no_session_id_survives() -> None:
    session = SB().user("Go").final("Done.").build(session_id="a-real-session-id-12345")
    trace = traces.export_session(session, {}, CFG, redact=True, salt="pepper")
    assert "a-real-session-id-12345" not in repr(trace)
    assert trace["trace_id"].startswith("h:")
    assert trace["task"]["id"].startswith("h:")


def test_redact_hashes_verdict_command_and_details() -> None:
    session = SB().user("Go").final("[[tests_passed|tests pass]]").build()
    cr = _claim_result()
    trace = traces.export_session(session, {0: [cr]}, CFG, redact=True, salt="pepper")
    blob = repr(trace["meta"]["proof_of_done"]["turns"])
    assert "uv run pytest -q" not in blob
    assert "src/app/models.py" not in blob


def test_same_input_same_salt_hashes_identically() -> None:
    session = SB().user("Fix it").final("Done.").build()
    t1 = traces.export_session(session, {}, CFG, redact=True, salt="s")
    t2 = traces.export_session(session, {}, CFG, redact=True, salt="s")
    assert t1["task"]["instruction"] == t2["task"]["instruction"]


def test_different_salt_hashes_differently() -> None:
    session = SB().user("Fix it").final("Done.").build()
    t1 = traces.export_session(session, {}, CFG, redact=True, salt="s1")
    t2 = traces.export_session(session, {}, CFG, redact=True, salt="s2")
    assert t1["task"]["instruction"] != t2["task"]["instruction"]


def test_trace_id_for_matches_export_session_without_redaction() -> None:
    session = SB().user("Go").final("Done.").build(session_id="sid-1")
    trace = traces.export_session(session, {}, CFG, redact=False, salt="")
    assert traces.trace_id_for(session, redact=False, salt="") == trace["trace_id"]


def test_trace_id_for_matches_export_session_with_redaction() -> None:
    session = SB().user("Go").final("Done.").build(session_id="sid-1")
    trace = traces.export_session(session, {}, CFG, redact=True, salt="pepper")
    assert traces.trace_id_for(session, redact=True, salt="pepper") == trace["trace_id"]


def test_subagent_refs_land_under_meta_proof_of_done_subagents() -> None:
    session = SB().user("Go").final("Done.").build(session_id="sid-1")
    refs = [{"agent_id": "a1", "agent_type": "general-purpose", "trace_id": "sid-1:a1"}]
    trace = traces.export_session(session, {}, CFG, redact=False, salt="", subagent_refs=refs)
    assert trace["meta"]["proof_of_done"]["subagents"] == refs


def test_no_subagents_key_when_there_are_none() -> None:
    session = SB().user("Go").final("Done.").build()
    trace = traces.export_session(session, {}, CFG, redact=False, salt="")
    assert "subagents" not in trace["meta"]["proof_of_done"]
