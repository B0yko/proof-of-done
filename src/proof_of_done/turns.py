"""Turns and stop attempts derived from a parsed session's step list, per PLAN §2.

A *turn* spans from one real user prompt up to (but not including) the next one, or the end of
the session. A *stop attempt* is a maximal run of consecutive `message`/`agent` steps (no
`tool_call` inside it) that is immediately followed by a real user prompt, a `hook_feedback`
environment step, or the end of the session -- i.e. exactly the text Claude Code would have
handed a Stop hook as `last_assistant_message` had one fired at that point. Interim assistant
text that precedes a further tool call is not a stop attempt.

Both are pure functions of `Session.steps`; the live hook does not need them (it always
evaluates the single stop attempt ending at the transcript's current end), but `audit` and
`eval` replay a whole transcript's history through them.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from proof_of_done.transcript.model import (
    ENTRY_HOOK_FEEDBACK,
    KIND_MESSAGE,
    ROLE_AGENT,
    ROLE_ENVIRONMENT,
    ROLE_USER,
    Step,
)


@dataclass(frozen=True)
class Turn:
    index: int
    user_step: int  # step index of the real user prompt starting the turn
    start: int  # == user_step
    end: int  # exclusive: the next turn's user_step, or len(steps)


@dataclass(frozen=True)
class StopAttempt:
    index: int  # 0-based across the whole session
    turn_index: int
    start: int  # step index (inclusive) where the agent-message run begins
    stop_index: int  # step index (exclusive) at which a Stop hook would have evaluated
    final_message: str  # the run's message steps, joined by "\n\n"


def _is_real_user_prompt(step: Step) -> bool:
    return step.kind == KIND_MESSAGE and step.role == ROLE_USER


def _is_hook_feedback(step: Step) -> bool:
    return (
        step.kind == KIND_MESSAGE
        and step.role == ROLE_ENVIRONMENT
        and step.entry == ENTRY_HOOK_FEEDBACK
    )


def _is_agent_message(step: Step) -> bool:
    return step.kind == KIND_MESSAGE and step.role == ROLE_AGENT


def turns(steps: Sequence[Step]) -> list[Turn]:
    """Every turn in `steps`: spans starting at each real user prompt."""
    starts = [i for i, s in enumerate(steps) if _is_real_user_prompt(s)]
    result: list[Turn] = []
    for idx, start in enumerate(starts):
        end = starts[idx + 1] if idx + 1 < len(starts) else len(steps)
        result.append(Turn(index=idx, user_step=start, start=start, end=end))
    return result


def _turn_index_for(trns: Sequence[Turn], step_i: int) -> int:
    for t in trns:
        if t.start <= step_i < t.end:
            return t.index
    return trns[-1].index if trns else 0


def stop_attempts(steps: Sequence[Step]) -> list[StopAttempt]:
    """Every stop attempt in `steps`, in order."""
    trns = turns(steps)
    attempts: list[StopAttempt] = []
    n = len(steps)
    i = 0
    run_start: int | None = None
    attempt_idx = 0
    while i <= n:
        if i < n and _is_agent_message(steps[i]):
            if run_start is None:
                run_start = i
            i += 1
            continue
        if run_start is not None:
            run_end = i
            is_boundary = i == n or _is_real_user_prompt(steps[i]) or _is_hook_feedback(steps[i])
            if is_boundary:
                final_message = "\n\n".join((s.content or "") for s in steps[run_start:run_end])
                attempts.append(
                    StopAttempt(
                        index=attempt_idx,
                        turn_index=_turn_index_for(trns, run_start),
                        start=run_start,
                        stop_index=run_end,
                        final_message=final_message,
                    )
                )
                attempt_idx += 1
            run_start = None
        i += 1
    return attempts


__all__ = ["StopAttempt", "Turn", "stop_attempts", "turns"]
