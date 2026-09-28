"""Tests for `proof_of_done.message`: the block/warn text builder (PLAN §8, spec item 6)."""

from __future__ import annotations

from proof_of_done import evidence, message


def mk_verdict(
    reason: str,
    *,
    rule_id: str | None = "tests",
    claim_type: str = "tests_passed",
    details: evidence.VerdictDetails | None = None,
) -> evidence.Verdict:
    return evidence.Verdict(
        claim_type=claim_type,
        supported=False,
        reason=reason,
        partial=False,
        exempt=False,
        action="block",
        rule_id=rule_id,
        details=details or evidence.VerdictDetails(),
    )


def mk_entry(
    reason: str,
    *,
    quote: str = "All tests pass",
    claim_type: str = "tests_passed",
    rule_id: str | None = "tests",
    command: str | None = "uv run pytest -q",
    details: evidence.VerdictDetails | None = None,
    background_active: bool | None = None,
) -> message.Entry:
    v = mk_verdict(reason, rule_id=rule_id, claim_type=claim_type, details=details)
    return message.Entry(
        quote=quote,
        claim_type=claim_type,
        verdict=v,
        command=command,
        background_active=background_active,
    )


# ------------------------------------------------------------------------------------------
# the exact worked example from PLAN §8
# ------------------------------------------------------------------------------------------


def test_worked_example_matches_plan_exactly() -> None:
    v1 = mk_verdict(
        "stale",
        rule_id="tests",
        details=evidence.VerdictDetails(
            candidate_display="uv run pytest -q",
            candidate_step=40,
            anchor_path="src/app/models.py",
            anchor_step=56,
        ),
    )
    v2 = mk_verdict("no_command", rule_id="lint", claim_type="lint_clean")
    entries = [
        message.Entry(
            quote="All 42 tests pass",
            claim_type="tests_passed",
            verdict=v1,
            command="uv run pytest -q",
        ),
        message.Entry(
            quote="lint is clean", claim_type="lint_clean", verdict=v2, command="ruff check ."
        ),
    ]
    text = message.block_reason(entries, "#skip-proof")
    expected = (
        "proof-of-done: 2 claims in your final message are not backed by "
        "this session's transcript.\n"
        '1. "All 42 tests pass" (tests_passed): the last test run `uv run pytest -q` at step 41 '
        "was before your edit to `src/app/models.py` at step 57.\n"
        "   Run: uv run pytest -q\n"
        '2. "lint is clean" (lint_clean): no lint command ran in this session.\n'
        "   Run: ruff check .\n"
        "Run these commands and report the real result, or restate your message without "
        "these claims."
    )
    assert text == expected


# ------------------------------------------------------------------------------------------
# every reason template
# ------------------------------------------------------------------------------------------


def test_no_command_template() -> None:
    e = mk_entry("no_command", details=evidence.VerdictDetails())
    text = message.block_reason([e], "#skip-proof")
    assert "no test command ran in this session." in text


def test_stale_template_with_a_whole_tree_anchor() -> None:
    details = evidence.VerdictDetails(
        candidate_display="pytest -q", candidate_step=2, anchor_path=None, anchor_step=5
    )
    e = mk_entry("stale", details=details)
    text = message.block_reason([e], "#skip-proof")
    assert "was before a change to the project at step 6." in text


def test_failed_exit_plain_nonzero() -> None:
    details = evidence.VerdictDetails(candidate_display="pytest -q", candidate_step=1, exit_code=2)
    e = mk_entry("failed_exit", details=details)
    text = message.block_reason([e], "#skip-proof")
    assert "at step 2 failed (exit code 2)." in text


def test_failed_exit_interrupted() -> None:
    details = evidence.VerdictDetails(
        candidate_display="pytest -q", candidate_step=1, interrupted=True
    )
    e = mk_entry("failed_exit", details=details)
    text = message.block_reason([e], "#skip-proof")
    assert "was interrupted before it finished." in text


def test_failed_exit_timed_out() -> None:
    details = evidence.VerdictDetails(
        candidate_display="pytest -q", candidate_step=1, timed_out=True
    )
    e = mk_entry("failed_exit", details=details)
    text = message.block_reason([e], "#skip-proof")
    assert "timed out." in text


def test_failed_output_template() -> None:
    details = evidence.VerdictDetails(
        candidate_display="pytest -q", candidate_step=1, matched_text="3 failed"
    )
    e = mk_entry("failed_output", details=details)
    text = message.block_reason([e], "#skip-proof")
    assert 'reported failures ("3 failed").' in text


def test_empty_run_template() -> None:
    details = evidence.VerdictDetails(
        candidate_display="pytest -q", candidate_step=1, matched_text="collected 0 items"
    )
    e = mk_entry("empty_run", details=details)
    text = message.block_reason([e], "#skip-proof")
    assert 'ran nothing ("collected 0 items").' in text


def test_masked_inconclusive_template() -> None:
    details = evidence.VerdictDetails(candidate_display="pytest | tail -5", candidate_step=1)
    e = mk_entry("masked_inconclusive", details=details)
    text = message.block_reason([e], "#skip-proof")
    assert "its output does not confirm success." in text


def test_background_only_still_running_from_verdict_details() -> None:
    details = evidence.VerdictDetails(
        candidate_display="pytest -q", candidate_step=1, background_running=True
    )
    e = mk_entry("background_only", details=details)
    text = message.block_reason([e], "#skip-proof")
    assert (
        "is running in the background and it is still running; re-run it in the foreground." in text
    )


def test_background_only_not_still_running() -> None:
    details = evidence.VerdictDetails(
        candidate_display="pytest -q", candidate_step=1, background_running=False
    )
    e = mk_entry("background_only", details=details)
    text = message.block_reason([e], "#skip-proof")
    assert "is running in the background; re-run it in the foreground." in text
    assert "still running" not in text


def test_background_active_entry_override_beats_verdict_detail() -> None:
    details = evidence.VerdictDetails(
        candidate_display="pytest -q", candidate_step=1, background_running=False
    )
    e = mk_entry("background_only", details=details, background_active=True)
    text = message.block_reason([e], "#skip-proof")
    assert "and it is still running" in text


def test_superseded_by_failure_template() -> None:
    details = evidence.VerdictDetails(
        candidate_display="pytest -k test_foo",
        candidate_step=3,
        full_run_display="pytest -q",
        full_run_step=1,
    )
    e = mk_entry("superseded_by_failure", details=details)
    text = message.block_reason([e], "#skip-proof")
    assert "only covered part of the suite; the full run `pytest -q` at step 2 failed." in text


def test_no_result_template() -> None:
    details = evidence.VerdictDetails(candidate_display="pytest -q", candidate_step=1)
    e = mk_entry("no_result", details=details)
    text = message.block_reason([e], "#skip-proof")
    assert "has no recorded result yet." in text


def test_unknown_reason_code_falls_back_without_raising() -> None:
    e = mk_entry("supported", details=evidence.VerdictDetails())
    text = message.block_reason([e], "#skip-proof")
    assert "not backed by this session's transcript." in text


# ------------------------------------------------------------------------------------------
# nouns
# ------------------------------------------------------------------------------------------


def test_noun_falls_back_to_custom_rule_id() -> None:
    e = mk_entry("no_command", rule_id="committed", claim_type="committed")
    text = message.block_reason([e], "#skip-proof")
    assert "no committed command ran in this session." in text


def test_noun_falls_back_to_claim_type_with_no_rule() -> None:
    e = mk_entry("no_command", rule_id=None, claim_type="committed")
    text = message.block_reason([e], "#skip-proof")
    assert "no committed command ran in this session." in text


def test_run_line_without_a_command() -> None:
    e = mk_entry("no_command", command=None)
    text = message.block_reason([e], "#skip-proof")
    assert "Run: (no test command found — run it in the foreground)" in text


# ------------------------------------------------------------------------------------------
# skip-token scrubbing
# ------------------------------------------------------------------------------------------


def test_skip_token_scrubbed_from_quote() -> None:
    e = mk_entry("no_command", quote="Done #skip-proof for real")
    text = message.block_reason([e], "#skip-proof")
    assert "#skip-proof" not in text
    assert "[skip token]" in text


def test_skip_token_scrubbed_from_command_and_notes() -> None:
    e = mk_entry("no_command", command="echo #skip-proof")
    notes = ["edited .proof-of-done.yaml #skip-proof"]
    text = message.block_reason([e], "#skip-proof", notes=notes)
    assert "#skip-proof" not in text


def test_empty_skip_token_is_a_no_op() -> None:
    e = mk_entry("no_command", quote="fine")
    text = message.block_reason([e], "")
    assert "fine" in text


# ------------------------------------------------------------------------------------------
# warn variant
# ------------------------------------------------------------------------------------------


def test_warn_message_header() -> None:
    e = mk_entry("no_command")
    text = message.warn_message([e], "#skip-proof")
    assert text.startswith("proof-of-done (warn): 1 claim in your final message is not backed")


def test_block_header_pluralization() -> None:
    text = message.block_reason([mk_entry("no_command")], "#skip-proof")
    assert text.startswith("proof-of-done: 1 claim in your final message is not backed")


def test_empty_entries_gives_empty_string() -> None:
    assert message.block_reason([], "#skip-proof") == ""
    assert message.warn_message([], "#skip-proof") == ""


# ------------------------------------------------------------------------------------------
# truncation of individual fields
# ------------------------------------------------------------------------------------------


def test_quote_cut_to_80_chars() -> None:
    e = mk_entry("no_command", quote="x" * 200)
    text = message.block_reason([e], "#skip-proof")
    line = next(line for line in text.splitlines() if line.startswith("1."))
    quoted = line.split('"')[1]
    assert len(quoted) <= 80
    assert quoted.endswith("…")


def test_command_cut_to_120_chars() -> None:
    e = mk_entry("no_command", command="x" * 300)
    text = message.block_reason([e], "#skip-proof")
    run_line = next(line for line in text.splitlines() if line.strip().startswith("Run:"))
    command_text = run_line.split("Run: ", 1)[1]
    assert len(command_text) <= 120
    assert command_text.endswith("…")


def test_anchor_path_cut_to_80_chars() -> None:
    details = evidence.VerdictDetails(
        candidate_display="pytest -q",
        candidate_step=1,
        anchor_path="src/" + ("a" * 100) + ".py",
        anchor_step=2,
    )
    e = mk_entry("stale", details=details)
    text = message.block_reason([e], "#skip-proof")
    # the backtick-quoted path segment must itself be at most 80 characters
    path_text = text.split("your edit to `", 1)[1].split("`", 1)[0]
    assert len(path_text) <= 80
    assert path_text.endswith("…")


def test_embedded_newlines_are_flattened_to_one_line_per_field() -> None:
    e = mk_entry("no_command", quote="line one\nline two\nline three")
    text = message.block_reason([e], "#skip-proof")
    lines = text.splitlines()
    # exactly 4 lines: header, claim line, Run line, closing sentence
    assert len(lines) == 4
    assert "line one line two line three" in lines[1]


# ------------------------------------------------------------------------------------------
# overall size limits: 30 claims and huge quotes
# ------------------------------------------------------------------------------------------


def test_thirty_huge_claims_stay_within_limits_and_report_the_drop_count() -> None:
    entries = [
        mk_entry("no_command", quote=f"claim number {i} " + "x" * 200, command="y" * 200)
        for i in range(30)
    ]
    text = message.block_reason(entries, "#skip-proof")
    lines = text.splitlines()
    assert len(lines) <= 20
    assert len(text) <= 1800
    assert text.startswith("proof-of-done: 30 claims in your final message are not backed")
    assert "more unsupported claim" in text
    assert text.endswith(
        "Run these commands and report the real result, or restate your message "
        "without these claims."
    )


def test_drop_count_is_exact() -> None:
    entries = [
        mk_entry("no_command", quote=f"claim number {i} " + "x" * 200, command="y" * 200)
        for i in range(30)
    ]
    text = message.block_reason(entries, "#skip-proof")
    lines = text.splitlines()
    drop_line = next(line for line in lines if line.startswith("…and"))
    kept = sum(1 for line in lines if line.startswith(tuple(f"{i}." for i in range(1, 31))))
    dropped_claimed = int(drop_line.split()[1])
    assert kept + dropped_claimed == 30


def test_singular_drop_count_wording() -> None:
    # 9 small entries plus 1 note is exactly the boundary where dropping a single entry (not
    # two) is enough to fit the 20-line budget -- verified against the real implementation.
    entries = [mk_entry("no_command", quote=f"claim {i}", command="pytest") for i in range(9)]
    text = message.block_reason(entries, "#skip-proof", notes=["one note"])
    lines = text.splitlines()
    assert len(lines) <= 20
    assert "…and 1 more unsupported claim." in text
    assert "…and 1 more unsupported claims." not in text


def test_notes_each_add_one_line() -> None:
    e = mk_entry("no_command")
    text = message.block_reason([e], "#skip-proof", notes=["note one", "note two"])
    lines = text.splitlines()
    assert "note one" in lines
    assert "note two" in lines


def test_notes_survive_when_entries_are_small() -> None:
    entries = [mk_entry("no_command", quote=f"claim {i}") for i in range(3)]
    text = message.block_reason(entries, "#skip-proof", notes=["ignored 'mode: warn': edited"])
    assert "ignored 'mode: warn': edited" in text
