"""The agent-facing block/warn text, per PLAN §8 and spec item 6.

``block_reason`` renders the Stop hook's ``reason`` field (what makes the agent's turn
continue); ``warn_message`` renders the ``mode: warn`` / consecutive-block-cap ``systemMessage``
variant, which is identical except for its header line. Both take a list of :class:`Entry`
(one per unsupported claim, in the order the claims appeared in the agent's message), the
configured skip token (scrubbed from the output everywhere it would otherwise appear), and an
optional list of plain-text tamper notes (each becomes exactly one extra line).

Layout, matching the spec's worked example::

    proof-of-done: 2 claims in your final message are not backed by this session's transcript.
    1. "All 42 tests pass" (tests_passed): the last test run `uv run pytest -q` at step 41 was
       before your edit to `src/app/models.py` at step 57.
       Run: uv run pytest -q
    2. "lint is clean" (lint_clean): no lint command ran in this session.
       Run: ruff check .
    Run these commands and report the real result, or restate your message without these claims.

Quotes are cut to 80 characters, commands to 120, paths to 80 (``_cut`` also collapses embedded
whitespace/newlines, so every rendered field stays on its own physical line). The whole message
stays within 20 lines and 1,800 characters; when it would not fit, entries are dropped from the
end and replaced with a single ``…and N more unsupported claims.`` line, computed by shrinking
the kept-entry count until the budget is met.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from proof_of_done import evidence

_QUOTE_LIMIT = 80
_COMMAND_LIMIT = 120
_PATH_LIMIT = 80
_MAX_LINES = 20
_MAX_CHARS = 1800

_CLOSING = (
    "Run these commands and report the real result, or restate your message without these claims."
)

# rule id -> the short noun used in reason phrases ("no lint command ran..."). Falls back to
# the rule id itself for a custom rule, or to the claim type when there is no rule at all.
_NOUN_BY_RULE_ID = {
    "tests": "test",
    "build": "build",
    "lint": "lint",
    "typecheck": "typecheck",
    "deploy": "deploy",
    "fixed": "fix",
    "verified": "verification",
}


@dataclass
class Entry:
    """One unsupported claim, ready to render: the quote and claim type the claim detector
    found, the `Verdict` `evidence.judge` returned for it, the suggested command (or `None`),
    and an optional authoritative override for "is it still running" that only the Stop
    payload's own `background_tasks` list can supply (see `VerdictDetails.background_running`'s
    docstring) -- `None` keeps the transcript-derived guess already on the verdict.
    """

    quote: str
    claim_type: str
    verdict: evidence.Verdict
    command: str | None
    background_active: bool | None = None


def _cut(text: str, limit: int) -> str:
    """Collapse `text` to one line (whitespace/newlines -> single spaces) and cut it to
    `limit` characters, appending an ellipsis when it was cut."""
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[: max(limit - 1, 0)].rstrip() + "…"


def _noun(verdict: evidence.Verdict, claim_type: str) -> str:
    if verdict.rule_id is not None:
        return _NOUN_BY_RULE_ID.get(verdict.rule_id, verdict.rule_id)
    return claim_type


def _step_no(step: int | None) -> str:
    """Steps are shown to the agent 1-based (`step.i + 1`); a missing step never appears in
    a rendered reason (every branch that reads this always has the matching detail set)."""
    return "?" if step is None else str(step + 1)


def _anchor_phrase(details: evidence.VerdictDetails) -> str:
    step = _step_no(details.anchor_step)
    if details.anchor_path is not None:
        path = _cut(details.anchor_path, _PATH_LIMIT)
        return f"your edit to `{path}` at step {step}"
    return f"a change to the project at step {step}"


def _reason_phrase(verdict: evidence.Verdict, noun: str, background_active: bool) -> str:
    d = verdict.details
    reason = verdict.reason
    command = _cut(d.candidate_display or "", _COMMAND_LIMIT)
    step = _step_no(d.candidate_step)

    if reason == "no_command":
        return f"no {noun} command ran in this session."
    if reason == "stale":
        return f"the last {noun} run `{command}` at step {step} was before {_anchor_phrase(d)}."
    if reason == "failed_exit":
        if d.interrupted:
            return (
                f"the last {noun} run `{command}` at step {step} was interrupted "
                "before it finished."
            )
        if d.timed_out:
            return f"the last {noun} run `{command}` at step {step} timed out."
        exit_bit = f" (exit code {d.exit_code})" if d.exit_code is not None else ""
        return f"the last {noun} run `{command}` at step {step} failed{exit_bit}."
    if reason == "failed_output":
        matched = _cut(d.matched_text or "", _QUOTE_LIMIT)
        return f'the last {noun} run `{command}` at step {step} reported failures ("{matched}").'
    if reason == "empty_run":
        matched = _cut(d.matched_text or "", _QUOTE_LIMIT)
        return f'the last {noun} run `{command}` at step {step} ran nothing ("{matched}").'
    if reason == "masked_inconclusive":
        return (
            f"the last {noun} run `{command}` at step {step} had its exit status masked "
            "(piped, ignored, or run under `set +e`) and its output does not confirm success."
        )
    if reason == "background_only":
        still_running = " and it is still running" if background_active else ""
        return (
            f"the last {noun} run `{command}` at step {step} is running in the "
            f"background{still_running}; re-run it in the foreground."
        )
    if reason == "superseded_by_failure":
        full_command = _cut(d.full_run_display or "", _COMMAND_LIMIT)
        full_step = _step_no(d.full_run_step)
        return (
            f"the last {noun} run `{command}` at step {step} only covered part of the suite; "
            f"the full run `{full_command}` at step {full_step} failed."
        )
    if reason == "no_result":
        return f"the last {noun} run `{command}` at step {step} has no recorded result yet."
    # Defensive fallback: every closed reason code above is handled; this only guards a future
    # reason code or a caller passing a "supported" verdict by mistake, so the hook never
    # raises while rendering a message (PLAN §9, fail-open).
    return f"the {noun} claim is not backed by this session's transcript."


def _render_entry(index: int, entry: Entry) -> list[str]:
    noun = _noun(entry.verdict, entry.claim_type)
    background_active = (
        entry.verdict.details.background_running
        if entry.background_active is None
        else entry.background_active
    )
    reason = _reason_phrase(entry.verdict, noun, background_active)
    quote = _cut(entry.quote, _QUOTE_LIMIT)
    if entry.command:
        run_line = f"   Run: {_cut(entry.command, _COMMAND_LIMIT)}"
    else:
        run_line = f"   Run: (no {noun} command found — run it in the foreground)"
    return [f'{index}. "{quote}" ({entry.claim_type}): {reason}', run_line]


def _header(prefix: str, n: int) -> str:
    claim_word = "claim" if n == 1 else "claims"
    be = "is" if n == 1 else "are"
    return (
        f"{prefix} {n} {claim_word} in your final message {be} not backed by "
        "this session's transcript."
    )


def _scrub(text: str, skip_token: str) -> str:
    if not skip_token:
        return text
    return text.replace(skip_token, "[skip token]")


def _assemble(
    header_prefix: str, rendered: Sequence[list[str]], note_lines: Sequence[str], keep: int
) -> str:
    lines: list[str] = [_header(header_prefix, len(rendered))]
    for block in rendered[:keep]:
        lines.extend(block)
    dropped = len(rendered) - keep
    if dropped > 0:
        claim_word = "claim" if dropped == 1 else "claims"
        lines.append(f"…and {dropped} more unsupported {claim_word}.")
    lines.extend(note_lines)
    lines.append(_CLOSING)
    return "\n".join(lines)


def _build(
    header_prefix: str, entries: Sequence[Entry], skip_token: str, notes: Sequence[str]
) -> str:
    if not entries:
        return ""
    rendered = [_render_entry(i + 1, entry) for i, entry in enumerate(entries)]
    note_lines = [_cut(note, _COMMAND_LIMIT) for note in notes]

    keep = len(rendered)
    text = _assemble(header_prefix, rendered, note_lines, keep)
    while keep > 0 and (text.count("\n") + 1 > _MAX_LINES or len(text) > _MAX_CHARS):
        keep -= 1
        text = _assemble(header_prefix, rendered, note_lines, keep)
    return _scrub(text, skip_token)


def block_reason(entries: Sequence[Entry], skip_token: str, notes: Sequence[str] = ()) -> str:
    """The Stop hook's ``reason`` for one or more unsupported ``block``-action claims."""
    return _build("proof-of-done:", entries, skip_token, notes)


def warn_message(entries: Sequence[Entry], skip_token: str, notes: Sequence[str] = ()) -> str:
    """The ``systemMessage`` variant for ``mode: warn`` or the consecutive-block cap."""
    return _build("proof-of-done (warn):", entries, skip_token, notes)


__all__ = ["Entry", "block_reason", "warn_message"]
