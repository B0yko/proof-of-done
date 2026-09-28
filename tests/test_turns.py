"""Tests for `proof_of_done.turns`: turn spans and stop-attempt reconstruction from a plain
step list."""

from __future__ import annotations

from proof_of_done import turns
from proof_of_done.transcript.model import (
    ENTRY_HOOK_FEEDBACK,
    KIND_MESSAGE,
    KIND_TOOL_CALL,
    KIND_TOOL_RESULT,
    ROLE_AGENT,
    ROLE_ENVIRONMENT,
    ROLE_TOOL,
    ROLE_USER,
    Step,
)


def _user(i: int, text: str) -> Step:
    return Step(i=i, kind=KIND_MESSAGE, role=ROLE_USER, content=text)


def _agent_text(i: int, text: str) -> Step:
    return Step(i=i, kind=KIND_MESSAGE, role=ROLE_AGENT, content=text)


def _tool_call(i: int, name: str = "Bash") -> Step:
    return Step(i=i, kind=KIND_TOOL_CALL, role=ROLE_AGENT, name=name, tool_use_id=f"t{i}")


def _tool_result(i: int, name: str = "Bash") -> Step:
    return Step(
        i=i, kind=KIND_TOOL_RESULT, role=ROLE_TOOL, name=name, ok=True, tool_use_id=f"t{i - 1}"
    )


def _hook_feedback(i: int) -> Step:
    return Step(
        i=i, kind=KIND_MESSAGE, role=ROLE_ENVIRONMENT, entry=ENTRY_HOOK_FEEDBACK, content="blocked"
    )


def test_single_turn_single_stop_attempt() -> None:
    steps = [_user(0, "fix it"), _agent_text(1, "Done.")]
    t = turns.turns(steps)
    assert len(t) == 1
    assert t[0].start == 0
    assert t[0].end == 2

    attempts = turns.stop_attempts(steps)
    assert len(attempts) == 1
    assert attempts[0].turn_index == 0
    assert attempts[0].start == 1
    assert attempts[0].stop_index == 2
    assert attempts[0].final_message == "Done."


def test_interim_text_before_a_tool_call_is_not_a_stop_attempt() -> None:
    steps = [
        _user(0, "fix it"),
        _agent_text(1, "Let me check."),
        _tool_call(2),
        _tool_result(3),
        _agent_text(4, "Fixed."),
    ]
    attempts = turns.stop_attempts(steps)
    assert len(attempts) == 1
    assert attempts[0].start == 4
    assert attempts[0].final_message == "Fixed."


def test_two_turns_each_get_their_own_stop_attempt() -> None:
    steps = [
        _user(0, "fix it"),
        _agent_text(1, "Fixed."),
        _user(2, "now the other bug"),
        _agent_text(3, "Fixed that too."),
    ]
    t = turns.turns(steps)
    assert [x.start for x in t] == [0, 2]
    assert [x.end for x in t] == [2, 4]

    attempts = turns.stop_attempts(steps)
    assert len(attempts) == 2
    assert attempts[0].turn_index == 0
    assert attempts[0].final_message == "Fixed."
    assert attempts[1].turn_index == 1
    assert attempts[1].final_message == "Fixed that too."


def test_hook_feedback_boundary_creates_a_second_stop_attempt_in_one_turn() -> None:
    steps = [
        _user(0, "fix it"),
        _agent_text(1, "Tests pass."),
        _hook_feedback(2),
        _agent_text(3, "Ran it for real; tests pass."),
    ]
    attempts = turns.stop_attempts(steps)
    assert len(attempts) == 2
    assert attempts[0].turn_index == attempts[1].turn_index == 0
    assert attempts[0].final_message == "Tests pass."
    assert attempts[0].stop_index == 2
    assert attempts[1].start == 3
    assert attempts[1].final_message == "Ran it for real; tests pass."


def test_consecutive_agent_text_steps_join_with_blank_line() -> None:
    steps = [
        _user(0, "fix it"),
        _agent_text(1, "First paragraph."),
        _agent_text(2, "Second paragraph."),
    ]
    attempts = turns.stop_attempts(steps)
    assert len(attempts) == 1
    assert attempts[0].final_message == "First paragraph.\n\nSecond paragraph."


def test_no_stop_attempt_when_the_turn_ends_mid_tool_call() -> None:
    steps = [_user(0, "fix it"), _agent_text(1, "Checking."), _tool_call(2)]
    assert turns.stop_attempts(steps) == []


def test_empty_step_list_yields_no_turns_or_attempts() -> None:
    assert turns.turns([]) == []
    assert turns.stop_attempts([]) == []
