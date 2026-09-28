"""Tests for `proof_of_done.claims`: masking, unit/clause splitting, the built-in patterns in
`defaults.yaml`, every rejection filter, spans, Markdown forms, multi-claim messages and
custom rules. Table-driven throughout; see PLAN.md §10 and spec item 4 for the behaviour
these encode.

The built-in rule table comes straight from `defaults.yaml` through `proof_of_done.config`,
so these tests exercise the real shipped patterns, not a hand-copied subset. A second table
is driven by `fixtures/templates/phrasings.yaml` (the claim wording and non-claim look-alikes
`fixtures/expand.py` uses to build the templated evaluation set), so the same wording this
module must recognise or reject is checked here directly, one phrasing per test case.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any

import pytest

from proof_of_done import config
from proof_of_done.claims import (
    Claim,
    ClaimRule,
    Rejection,
    builtin_patterns,
    detect,
    detect_with_rejections,
    prefilter,
)
from proof_of_done.yamlload import safe_load

# ---------------------------------------------------------------------------------------
# Built-in rules, loaded from the real defaults.yaml (no user/project/env layers)
# ---------------------------------------------------------------------------------------


def _load_builtin_rules() -> tuple[ClaimRule, ...]:
    def reader(path: str) -> str | None:
        if path == config.defaults_path():
            with open(path, encoding="utf-8") as handle:
                return handle.read()
        return None

    layers = config.load_layers("/work/demo-app", {}, reader)
    cfg, _notes = config.effective(layers, {}, tampered_paths=set(), settings_tampered=False)
    return tuple(
        ClaimRule(id=r.id, claim_type=r.claim_type, patterns=r.claims, keywords=r.keywords)
        for r in cfg.rules
    )


RULES = _load_builtin_rules()
RULES_BY_TYPE = {rule.claim_type: rule for rule in RULES}
BUILTIN_CLAIM_TYPES = tuple(RULES_BY_TYPE)


def claim_types_of(message: str) -> list[str]:
    return [c.claim_type for c in detect(message, RULES)]


# ---------------------------------------------------------------------------------------
# Phrasing/lookalike fixtures, loaded from fixtures/templates/phrasings.yaml
# ---------------------------------------------------------------------------------------

_TEMPLATES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures", "templates"
)


def _load_phrasings() -> dict[str, Any]:
    path = os.path.join(_TEMPLATES_DIR, "phrasings.yaml")
    with open(path, encoding="utf-8") as handle:
        return safe_load(handle.read())  # type: ignore[no-any-return]


_PHRASINGS = _load_phrasings()

_CMD_BY_TYPE = {
    "tests_passed": "uv run pytest -q",
    "build_passed": "uv build",
    "lint_clean": "ruff check .",
    "typecheck_clean": "mypy .",
    "deployed": "vercel deploy --prod",
    "fixed": "uv run pytest -q",
    "verified": "uv run pytest -q",
}


def _fill(text: str, claim_type: str) -> str:
    filled = text.replace("{n}", "42").replace("{cmd}", _CMD_BY_TYPE[claim_type])
    return filled if filled.endswith((".", "!", "?")) else filled + "."


PHRASING_CASES = [
    (claim_type, _fill(text, claim_type))
    for claim_type, texts in _PHRASINGS["claim_phrasings"].items()
    for text in texts
]

LOOKALIKE_ITEMS = _PHRASINGS["lookalikes"]


def _lookalike_message(item: dict[str, str]) -> str:
    text = item["text"].replace("{n}", "42").replace("{cmd}", "uv run pytest -q")
    category = item["category"]
    if category == "code_block":
        return f"```\n{text}\n```"
    if category == "inline_code":
        return f"`{text}`"
    return text


LOOKALIKE_CASES = [(item["category"], _lookalike_message(item)) for item in LOOKALIKE_ITEMS]


# ---------------------------------------------------------------------------------------
# Positive coverage: every phrasing in fixtures/templates/phrasings.yaml is a claim of its
# claim type (>= 78 cases: one row per phrasing variant across the 7 built-in claim types).
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("claim_type,message", PHRASING_CASES, ids=[m for _t, m in PHRASING_CASES])
def test_every_phrasing_is_detected_as_its_claim_type(claim_type: str, message: str) -> None:
    assert claim_type in claim_types_of(message), message


def test_phrasing_table_covers_every_builtin_claim_type() -> None:
    covered = {t for t, _m in PHRASING_CASES}
    assert covered == set(BUILTIN_CLAIM_TYPES)


# ---------------------------------------------------------------------------------------
# Negative coverage: every look-alike in fixtures/templates/phrasings.yaml produces no claim
# (>= 36 cases: negated, hedged, conditional, future, question, instruction, code_block,
# inline_code, quoted_user_text).
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("category,message", LOOKALIKE_CASES, ids=[m for _c, m in LOOKALIKE_CASES])
def test_every_lookalike_produces_no_claim(category: str, message: str) -> None:
    assert detect(message, RULES) == [], (category, message)


def test_lookalike_table_covers_every_filter_category() -> None:
    covered = {c for c, _m in LOOKALIKE_CASES}
    assert covered == {
        "negated",
        "hedged",
        "conditional",
        "future",
        "question",
        "instruction",
        "code_block",
        "inline_code",
        "quoted_user_text",
    }


# ---------------------------------------------------------------------------------------
# Spec item 4's own worked examples
# ---------------------------------------------------------------------------------------

NEGATION_STRENGTHENER_CLAIMS = [
    "Build succeeds without errors.",
    "All 42 tests pass with no warnings.",
    "No errors, tests pass.",
    "No lint errors remain.",
]


@pytest.mark.parametrize("message", NEGATION_STRENGTHENER_CLAIMS)
def test_no_without_before_a_strengthener_noun_is_not_a_negation(message: str) -> None:
    assert claim_types_of(message) != []


NOT_CLAIMS = [
    "Done.",
    "Implemented the feature.",
    "The email was sent.",
    "Not verified.",
    "Untested.",
    "I did not run the tests.",
    "I have not run pytest.",
    "Tests do not pass yet.",
    "The build is not passing.",
    "Lint is not clean.",
    "Nothing is fixed yet.",
]


@pytest.mark.parametrize("message", NOT_CLAIMS)
def test_generic_and_negated_text_is_never_a_claim(message: str) -> None:
    assert detect(message, RULES) == []


FIXED_LOOKALIKES = [
    "Fixed width column.",
    "The panel has a fixed-size layout.",
    "This is a fixed set of values.",
    "The variable is prefixed with an underscore.",
]


@pytest.mark.parametrize("message", FIXED_LOOKALIKES)
def test_fixed_word_forms_that_are_not_the_fixed_claim(message: str) -> None:
    assert "fixed" not in claim_types_of(message)


def test_this_fixes_hash_number_is_a_fixed_claim() -> None:
    claims = detect("This fixes #12.", RULES)
    assert [c.claim_type for c in claims] == ["fixed"]
    assert claims[0].quote == "This fixes #12"


# ---------------------------------------------------------------------------------------
# Markdown forms
# ---------------------------------------------------------------------------------------


def test_bullet_list_claim_is_detected() -> None:
    message = "Summary:\n\n- Ran the full suite\n- All 42 tests pass\n- Cleaned up temp files\n"
    assert "tests_passed" in claim_types_of(message)


def test_table_row_claim_is_detected() -> None:
    message = "| Check | Result |\n| --- | --- |\n| Tests | ✅ passing |\n"
    claims = detect(message, RULES)
    assert any(c.claim_type == "tests_passed" for c in claims)


def test_checkmark_before_label_is_detected() -> None:
    assert claim_types_of("✅ Tests") == ["tests_passed"]
    assert claim_types_of("✅ Build") == ["build_passed"]
    assert claim_types_of("✅ Lint") == ["lint_clean"]
    assert claim_types_of("✅ Deployed") == ["deployed"]


def test_bold_label_claim_is_detected() -> None:
    assert "tests_passed" in claim_types_of("**Tests:** 42 passed.")
    assert "build_passed" in claim_types_of("**Build:** passing.")


def test_go_test_backtick_subject_is_green_is_detected() -> None:
    message = "`go test ./...` is green."
    claims = detect(message, RULES)
    assert any(c.claim_type == "tests_passed" for c in claims)
    # the quote comes from the ORIGINAL message, so it still shows the backticked command
    assert any("go test" in c.quote for c in claims)


# ---------------------------------------------------------------------------------------
# Multi-claim messages
# ---------------------------------------------------------------------------------------


def test_tests_lint_and_types_all_pass_yields_three_claims() -> None:
    claims = detect("Tests, lint and types all pass.", RULES)
    assert {c.claim_type for c in claims} == {"tests_passed", "lint_clean", "typecheck_clean"}


def test_two_independent_claims_in_one_message() -> None:
    message = "Fixed the bug, confirmed tests pass, and made sure lint is clean."
    types = set(claim_types_of(message))
    assert "tests_passed" in types
    assert "lint_clean" in types


def test_claim_inside_parenthetical_aside_is_still_detected() -> None:
    message = "Deployed the change (the release is live) and updated the changelog."
    assert "deployed" in claim_types_of(message)


# ---------------------------------------------------------------------------------------
# Masking: fenced code, inline code, block quotes, quoted text, emphasis markers
# ---------------------------------------------------------------------------------------


def test_fenced_code_block_claim_is_masked() -> None:
    message = "Summary:\n\n```\nAll 42 tests pass\n```\n"
    assert detect(message, RULES) == []


def test_inline_code_claim_is_masked() -> None:
    message = "Status: `All tests pass` (see the notes above)."
    assert detect(message, RULES) == []


def test_blockquote_claim_is_masked() -> None:
    message = "> All 42 tests pass\n\nI have not re-run them since."
    assert detect(message, RULES) == []


def test_quoted_user_text_claim_is_masked() -> None:
    message = 'You said "tests pass" earlier, but I have not re-run them.'
    assert detect(message, RULES) == []


def test_fenced_code_close_does_not_swallow_trailing_real_claim() -> None:
    # A closing ``` fence followed by more text on the *same physical line* (as the
    # adversarial/decoy fixtures produce by joining snippets with a plain space) still
    # closes the block; the trailing prose after it is not masked away with it.
    message = "```\nAll 42 tests pass\n``` `build succeeded` The suite is green."
    types = claim_types_of(message)
    assert types == ["tests_passed"]


def test_unclosed_fenced_code_masks_through_end_of_message() -> None:
    message = "```\nAll 42 tests pass\n"
    assert detect(message, RULES) == []


def test_emphasis_markers_removed_but_surrounding_text_kept() -> None:
    claims = detect("**Tests:** 42 passed.", RULES)
    assert any(c.claim_type == "tests_passed" for c in claims)


# ---------------------------------------------------------------------------------------
# Spans: the quote is sliced from the ORIGINAL message and matches the reported span
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "All 42 tests pass.",
        "The build succeeds.",
        "Lint is clean.",
        "Typecheck is clean.",
        "Deployed to production.",
        "Fixed the bug.",
        "Verified the fix works.",
        "**Tests:** 42 passed.",
        "✅ Tests",
    ],
)
def test_span_matches_quote_in_original_message(message: str) -> None:
    for claim in detect(message, RULES):
        start, end = claim.span
        assert message[start:end] == claim.quote


def test_span_is_reported_in_message_order() -> None:
    message = "Fixed the bug. Confirmed that it works. Deployed to production."
    claims = detect(message, RULES)
    starts = [c.span[0] for c in claims]
    assert starts == sorted(starts)


def test_claims_are_returned_in_message_order_across_types() -> None:
    message = "The build succeeds. Lint is clean. Typecheck is clean."
    claims = detect(message, RULES)
    # de-duplicate consecutive same-type entries: more than one built-in pattern may match
    # the same sentence (e.g. both a specific and a generic "build succeeds" pattern), which
    # is expected -- the engine merges overlapping same-type spans into one claim instance.
    seen_in_order: list[str] = []
    for c in claims:
        if not seen_in_order or seen_in_order[-1] != c.claim_type:
            seen_in_order.append(c.claim_type)
    assert seen_in_order == ["build_passed", "lint_clean", "typecheck_clean"]


# ---------------------------------------------------------------------------------------
# Every built-in claim type: at least one direct positive and one direct negative beyond
# the phrasing table, covering the exact wording spec item 4 / PLAN.md §15 call out.
# ---------------------------------------------------------------------------------------

DIRECT_POSITIVES = [
    ("tests_passed", "All tests pass"),
    ("tests_passed", "Tests are green"),
    ("tests_passed", "The suite passes"),
    ("tests_passed", "42 passed"),
    ("tests_passed", "pytest passes"),
    ("build_passed", "Build succeeds"),
    ("build_passed", "Compiles cleanly"),
    ("build_passed", "Builds without errors"),
    ("lint_clean", "Lint is clean"),
    ("lint_clean", "ruff reports no issues"),
    ("lint_clean", "No lint errors remain"),
    ("lint_clean", "eslint passes"),
    ("lint_clean", "Clippy is happy"),
    ("typecheck_clean", "mypy passes with zero errors"),
    ("typecheck_clean", "Type checking succeeds"),
    ("typecheck_clean", "No type errors remain"),
    ("typecheck_clean", "tsc reports zero problems"),
    ("fixed", "Fixed the crash"),
    ("fixed", "The bug is fixed"),
    ("fixed", "Resolved the issue"),
    ("deployed", "Deployed to production"),
    ("deployed", "Shipped v1.2"),
    ("deployed", "Rolled out to production"),
    ("deployed", "Pushed the image"),
    ("verified", "I verified the endpoint responds"),
    ("verified", "Confirmed that it works"),
    ("verified", "Tested manually and it works"),
    ("verified", "Verified with curl"),
]


@pytest.mark.parametrize(
    "claim_type,text", DIRECT_POSITIVES, ids=[f"{t}:{m}" for t, m in DIRECT_POSITIVES]
)
def test_direct_positive_wording(claim_type: str, text: str) -> None:
    message = text if text.endswith((".", "!", "?")) else text + "."
    assert claim_type in claim_types_of(message)


# ---------------------------------------------------------------------------------------
# Custom rules go through the same filters as built-in ones
# ---------------------------------------------------------------------------------------


def _custom_rule(
    pattern: str, claim_type: str = "committed", keywords: tuple[str, ...] = ("commit",)
) -> ClaimRule:
    return ClaimRule(
        id="committed",
        claim_type=claim_type,
        patterns=(re.compile(pattern),),
        keywords=keywords,
    )


def test_custom_rule_matches_its_own_pattern() -> None:
    rule = _custom_rule(r"\b(?P<pred>committed)\s+the\s+changes\b")
    claims = detect("I committed the changes.", [rule])
    assert [c.claim_type for c in claims] == ["committed"]
    assert claims[0].rule_id == "committed"


def test_custom_rule_without_a_pred_group_uses_whole_match() -> None:
    rule = _custom_rule(r"\bcommitted\b", keywords=("commit",))
    claims = detect("I committed.", [rule])
    assert claims and claims[0].quote == "committed"


def test_custom_rule_is_negated_like_a_builtin_one() -> None:
    rule = _custom_rule(r"\b(?P<pred>committed)\s+the\s+changes\b")
    assert detect("Never committed the changes.", [rule]) == []
    assert detect("I have not committed the changes.", [rule]) == []
    assert detect("I haven't committed the changes.", [rule]) == []


def test_custom_rule_is_hedged_like_a_builtin_one() -> None:
    rule = _custom_rule(r"\b(?P<pred>committed)\s+the\s+changes\b")
    assert detect("I should have committed the changes.", [rule]) == []
    assert detect("I probably committed the changes.", [rule]) == []


def test_custom_rule_future_and_question_and_instruction() -> None:
    rule = _custom_rule(r"\b(?P<pred>committed)\s+the\s+changes\b")
    assert detect("I will have committed the changes.", [rule]) == []
    assert detect("Committed the changes?", [rule]) == []
    assert detect("Please committed the changes.", [rule]) == []


def test_multiple_rules_of_the_same_claim_type() -> None:
    rule_a = ClaimRule(
        id="a",
        claim_type="shipped",
        patterns=(re.compile(r"\b(?P<pred>a_ok)\b"),),
        keywords=("a_ok",),
    )
    rule_b = ClaimRule(
        id="b",
        claim_type="shipped",
        patterns=(re.compile(r"\b(?P<pred>b_ok)\b"),),
        keywords=("b_ok",),
    )
    claims = detect("a_ok b_ok", [rule_a, rule_b])
    assert {c.rule_id for c in claims} == {"a", "b"}
    assert all(c.claim_type == "shipped" for c in claims)


# ---------------------------------------------------------------------------------------
# Filter unit tests: exercised directly against a loose synthetic rule so the filter runs
# independent of a tight built-in pattern's own word adjacency.
# ---------------------------------------------------------------------------------------

_LOOSE_WORKS_RULE = ClaimRule(
    id="works",
    claim_type="works",
    patterns=(re.compile(r"\b(?P<pred>works)\b"),),
    keywords=("work",),
)

NEGATION_REJECTED = [
    "It never works.",
    "It does not works.",
    "It cannot works.",
    "It doesn't works.",
    "This is unable to works.",
]

NEGATION_ACCEPTED = [
    "It works.",
    "No bugs are left, it works.",  # negator is more than 3 tokens before the predicate
    "Without a doubt, it eventually works.",
]


@pytest.mark.parametrize("message", NEGATION_REJECTED)
def test_negation_window_rejects(message: str) -> None:
    assert detect(message, [_LOOSE_WORKS_RULE]) == []


@pytest.mark.parametrize("message", NEGATION_ACCEPTED)
def test_negation_window_accepts_when_negator_out_of_range_or_absent(message: str) -> None:
    assert detect(message, [_LOOSE_WORKS_RULE]) != []


def test_no_immediately_before_a_strengthener_noun_does_not_negate() -> None:
    rule = ClaimRule(
        id="works",
        claim_type="works",
        patterns=(re.compile(r"\b(?P<pred>works)\b"),),
        keywords=("work",),
    )
    assert detect("no errors, it works.", [rule]) != []
    assert detect("no bugs, it works.", [rule]) != []


HEDGE_REJECTED = [
    "It should works.",
    "It might works.",
    "It probably works.",
    "I think it works.",
    "It works once you deploy it.",
    "It works if the flag is set.",
]


@pytest.mark.parametrize("message", HEDGE_REJECTED)
def test_hedge_and_conditional_rejected(message: str) -> None:
    assert detect(message, [_LOOSE_WORKS_RULE]) == []


FUTURE_REJECTED = [
    "It will works.",
    "It'll works.",
    "Going to check that it works.",
    "Next step: it works.",
    "About to confirm it works.",
]


@pytest.mark.parametrize("message", FUTURE_REJECTED)
def test_future_rejected(message: str) -> None:
    assert detect(message, [_LOOSE_WORKS_RULE]) == []


def test_question_rejected() -> None:
    assert detect("Does it works?", [_LOOSE_WORKS_RULE]) == []


INSTRUCTION_REJECTED = [
    "Run it and confirm it works.",
    "Verify it works.",
    "Check that it works.",
    "Please confirm it works.",
    "You should confirm it works.",
    "Go ahead and confirm it works.",
    "Feel free to confirm it works.",
]


@pytest.mark.parametrize("message", INSTRUCTION_REJECTED)
def test_instruction_rejected(message: str) -> None:
    assert detect(message, [_LOOSE_WORKS_RULE]) == []


def test_non_finite_predicate_rejected() -> None:
    to_pass_rule = ClaimRule(
        id="tests",
        claim_type="tests_passed",
        patterns=(re.compile(r"tests?\s+(?P<pred>\S+\s+to\s+pass)\b"),),
        keywords=("test",),
    )
    assert detect("Waiting for the tests to pass.", [to_pass_rule]) == []
    assert detect("We need to get the tests to pass.", [RULES_BY_TYPE["tests_passed"]]) == []
    assert detect("Let's make the build pass.", [RULES_BY_TYPE["build_passed"]]) == []


def test_non_claim_instruction_examples_from_spec() -> None:
    assert detect("Run `pytest` to confirm the tests pass.", RULES) == []
    assert detect("Re-run mypy to confirm typecheck is clean.", RULES) == []


# ---------------------------------------------------------------------------------------
# prefilter()
# ---------------------------------------------------------------------------------------


def test_prefilter_true_when_a_keyword_is_present() -> None:
    assert prefilter("All tests pass.", ["test", "build"]) is True


def test_prefilter_false_when_no_keyword_is_present() -> None:
    assert prefilter("Updated the README.", ["test", "build"]) is False


def test_prefilter_is_case_insensitive() -> None:
    assert prefilter("ALL TESTS PASS", ["test"]) is True


def test_prefilter_matches_as_a_substring() -> None:
    # "spec" is a tests_passed keyword; "specification" contains it as a substring, matching
    # the documented "any keyword in message.lower()" semantics (a coarse, cheap gate).
    assert prefilter("Updated the specification document.", ["spec"]) is True


@pytest.mark.parametrize("claim_type,message", PHRASING_CASES)
def test_every_phrasing_passes_its_own_rules_prefilter(claim_type: str, message: str) -> None:
    rule = RULES_BY_TYPE[claim_type]
    assert prefilter(message, rule.keywords) is True


# ---------------------------------------------------------------------------------------
# Misc API surface
# ---------------------------------------------------------------------------------------


def test_builtin_patterns_is_empty_because_patterns_live_in_defaults_yaml() -> None:
    assert builtin_patterns() == {}


def test_claim_and_claimrule_are_frozen_dataclasses() -> None:
    claim = Claim(rule_id="tests", claim_type="tests_passed", quote="All tests pass", span=(0, 15))
    with pytest.raises(AttributeError):
        claim.quote = "changed"  # type: ignore[misc]
    rule = ClaimRule(id="tests", claim_type="tests_passed", patterns=(), keywords=())
    with pytest.raises(AttributeError):
        rule.id = "changed"  # type: ignore[misc]


def test_detect_with_no_rules_returns_no_claims() -> None:
    assert detect("All tests pass.", []) == []


def test_detect_is_deterministic() -> None:
    message = "Fixed the bug. Confirmed tests pass, lint is clean, and it's deployed."
    assert detect(message, RULES) == detect(message, RULES)


def test_negator_separated_by_punctuation_does_not_govern() -> None:
    from proof_of_done.claims import _is_negated

    clause = "No, the tests pass"
    assert not _is_negated(clause, clause.index("pass"))
    clause = "No bugs, tests pass"
    assert not _is_negated(clause, clause.index("pass"))
    clause = "No regressions and the tests pass"
    assert not _is_negated(clause, clause.index("pass"))
    clause = "The tests do not pass"
    assert _is_negated(clause, clause.index("pass"))


# ---------------------------------------------------------------------------------------
# A neutral adverb between subject and predicate ("tests still pass", "the bug is now fixed")
# is still the same claim, and does not weaken negation, hedge or other filters
# ---------------------------------------------------------------------------------------

ADVERB_POSITIVES = [
    ("tests_passed", "Simplified the function; tests still pass"),
    ("tests_passed", "The tests already pass"),
    ("tests_passed", "All 42 tests still pass"),
    ("tests_passed", "The previously failing test now passes"),
    ("tests_passed", "The suite finally passes"),
    ("tests_passed", "Tests consistently pass"),
    ("tests_passed", "The full test suite reliably passes"),
    ("tests_passed", "Tests are still green"),
    ("tests_passed", "The integration tests also pass"),
    ("tests_passed", "pytest now passes"),
    ("build_passed", "The build now succeeds"),
    ("build_passed", "Build is still green"),
    ("build_passed", "The build still passes"),
    ("lint_clean", "Lint is still clean"),
    ("lint_clean", "Ruff is now clean"),
    ("lint_clean", "eslint now passes"),
    ("typecheck_clean", "Typecheck is still clean"),
    ("typecheck_clean", "Type checking now succeeds"),
    ("typecheck_clean", "All type errors are now resolved"),
    ("deployed", "The release is now live"),
    ("deployed", "Deployment finally succeeded"),
    ("fixed", "The bug is now fixed"),
    ("fixed", "The root cause is already fixed"),
    ("fixed", "The problem is finally solved"),
    ("fixed", "This also fixes #12"),
    ("verified", "I also verified the output"),
    ("verified", "The fix is now verified"),
]


@pytest.mark.parametrize(
    "claim_type,text", ADVERB_POSITIVES, ids=[f"{t}:{m}" for t, m in ADVERB_POSITIVES]
)
def test_neutral_adverb_before_predicate_is_still_a_claim(claim_type: str, text: str) -> None:
    assert claim_type in claim_types_of(text + ".")


def test_compound_subject_list_tolerates_a_neutral_adverb() -> None:
    assert claim_types_of("Tests, lint and types all still pass.") == [
        "lint_clean",
        "tests_passed",
        "typecheck_clean",
    ]
    assert sorted(claim_types_of("Tests and lint now pass.")) == ["lint_clean", "tests_passed"]


ADVERB_NOT_CLAIMS = [
    "Tests still don't pass.",
    "Tests still fail.",
    "The tests no longer pass.",
    "Not all tests still pass.",
    "The build is still not green.",
    "The bug is still not fixed.",
    "Tests should still pass.",
    "The tests probably still pass.",
    "Tests still pass if the cache is warm.",
    "Tests will still pass.",
    "Do the tests still pass?",
    "Make sure the tests still pass.",
    "Check that the tests now pass.",
]


@pytest.mark.parametrize("message", ADVERB_NOT_CLAIMS)
def test_neutral_adverb_does_not_turn_a_non_claim_into_a_claim(message: str) -> None:
    assert detect(message, RULES) == []


def test_negation_window_skips_neutral_adverbs() -> None:
    # "Not" is 4 tokens before "works" but only 3 once "still" is skipped, the same distance
    # it has in "Not every part works".
    assert detect("Not every part works.", [_LOOSE_WORKS_RULE]) == []
    assert detect("Not every part still works.", [_LOOSE_WORKS_RULE]) == []


def test_builtin_adverb_slots_match_the_negation_window_skip_list() -> None:
    from proof_of_done.claims import _NEUTRAL_ADVERBS

    slot_re = re.compile(r"\(\?:\(\?:([a-z|]+)\)\\s\+\)")
    slots = [
        set(words.split("|")) - {"all"}
        for rule in RULES
        for pattern in rule.patterns
        for words in slot_re.findall(pattern.pattern)
    ]
    assert slots, "no adverb slot found in the built-in patterns"
    assert all(slot == _NEUTRAL_ADVERBS for slot in slots)


# ---------------------------------------------------------------------------------------
# S6c generalisation mechanisms (PLAN.md §10, spec item 4, tuned on eval/dev/messages.yaml
# train errors): clause-initial bare predicates, zero-count and tool-vocabulary forms, the
# "no longer"/"no failing X" negation exceptions, and the narrowed "expected" hedge.
# ---------------------------------------------------------------------------------------

BARE_CLAUSE_INITIAL_CLAIMS = [
    ("fixed", "Fixed by adding a mutex around the critical section."),
    ("fixed", "Resolved the deadlock between the two background workers."),
    ("fixed", "Patched the SQL injection vulnerability in the search endpoint."),
    ("fixed", "Squashed the memory leak in the worker process."),
    ("deployed", "Deployed the schema migration ahead of the app release."),
    ("deployed", "Shipped the hotfix straight to production."),
    ("deployed", "Rolled out the config change cluster-wide."),
    ("verified", "Checked the output manually against the expected fixture."),
    ("verified", "Verified the webhook fires by triggering it manually."),
    ("verified", "Confirmed the background job actually ran."),
    ("verified", "Tested it by hand in three browsers."),
]


@pytest.mark.parametrize(
    "claim_type,text",
    BARE_CLAUSE_INITIAL_CLAIMS,
    ids=[f"{t}:{m}" for t, m in BARE_CLAUSE_INITIAL_CLAIMS],
)
def test_bare_clause_initial_predicate_is_a_claim(claim_type: str, text: str) -> None:
    assert claim_type in claim_types_of(text)


FIXED_IDIOM_EXCLUSIONS = [
    "Fixed-width columns are used for the report.",
    "The fixed income desk asked about the report format.",
    "Set a fixed rate for the retry backoff.",
    "The value is prefixed with an underscore.",
]


@pytest.mark.parametrize("message", FIXED_IDIOM_EXCLUSIONS)
def test_fixed_idiom_is_not_a_claim(message: str) -> None:
    assert "fixed" not in claim_types_of(message)


ZERO_COUNT_AND_TOOL_VOCAB_CLAIMS = [
    ("lint_clean", "Ktlint run: 0 issues found in 214 files."),
    ("lint_clean", "yamllint over the CI configs: no issues."),
    ("lint_clean", "markdownlint over docs/: no violations."),
    ("lint_clean", "Lint summary: clean -- ran ruff, mypy config aside."),
    ("typecheck_clean", "pyright over src/: 0 errors, 0 warnings."),
    ("typecheck_clean", "clang -fsyntax-only over the whole project: no diagnostics."),
    ("typecheck_clean", "tsc reports zero errors after the migration."),
    ("build_passed", "npm run build finished: no errors, bundle written to dist/."),
    ("build_passed", "go build ./... finished with no errors."),
]


@pytest.mark.parametrize(
    "claim_type,text",
    ZERO_COUNT_AND_TOOL_VOCAB_CLAIMS,
    ids=[f"{t}:{m}" for t, m in ZERO_COUNT_AND_TOOL_VOCAB_CLAIMS],
)
def test_zero_count_and_tool_vocabulary_forms_are_claims(claim_type: str, text: str) -> None:
    assert claim_type in claim_types_of(text)


def test_no_failing_tests_is_a_claim_not_a_negation() -> None:
    assert "tests_passed" in claim_types_of("No failing tests left after the cleanup.")


def test_no_longer_complaint_verb_is_a_claim_not_a_negation() -> None:
    assert "lint_clean" in claim_types_of("The linter no longer flags anything.")
    assert "typecheck_clean" in claim_types_of("The type checker no longer complains.")


def test_no_longer_good_outcome_verb_still_negates() -> None:
    # Unlike a complaint verb ("flags", "complains"), "no longer" over a good-outcome verb
    # ("pass") stays a real negation: the tests used to pass and now do not.
    assert detect("The tests no longer pass.", RULES) == []


def test_nothing_is_fixed_yet_is_negated() -> None:
    assert detect("Nothing is fixed yet.", RULES) == []


EXPECTED_ADJECTIVE_CLAIMS = [
    ("verified", "Status: manually verified, works as expected."),
]


@pytest.mark.parametrize(
    "claim_type,text",
    EXPECTED_ADJECTIVE_CLAIMS,
    ids=[f"{t}:{m}" for t, m in EXPECTED_ADJECTIVE_CLAIMS],
)
def test_bare_adjective_expected_does_not_hedge(claim_type: str, text: str) -> None:
    assert claim_type in claim_types_of(text)


def test_expected_to_verb_still_hedges() -> None:
    assert detect("The build is expected to pass once you re-run it.", RULES) == []


def test_only_checked_or_tested_is_not_a_verified_claim() -> None:
    message = "Not tested against staging -- only checked the query syntax by eye."
    assert detect(message, RULES) == []
    assert "verified" not in claim_types_of(
        "The endpoint isn't verified yet; I only checked the route."
    )


def test_checked_in_into_out_is_not_a_verified_claim() -> None:
    assert "verified" not in claim_types_of("Checked in the fix for the flaky test.")
    assert "verified" not in claim_types_of("Checked out the release branch.")


def test_confirmed_for_weekday_is_a_scheduling_idiom_not_a_claim() -> None:
    assert "verified" not in claim_types_of(
        "Let the team know the maintenance window is confirmed for Saturday."
    )


# ---------------------------------------------------------------------------------------
# S6d round 2 (generalisation, round 2): new mechanisms, tested with fresh sentences, not
# copies of eval/dev/messages.yaml entries. See specs/S6d-tune2.md items A-C.
# ---------------------------------------------------------------------------------------


def test_dash_delimited_count_aside_lets_subject_and_predicate_meet() -> None:
    # A short digit-bearing aside between two em dashes is blanked like a parenthetical, so
    # "the suite" and "passes" can still be matched as one claim across it (item B).
    assert "tests_passed" in claim_types_of("The whole suite — 214 cases — passes cleanly.")


def test_dash_delimited_non_digit_aside_still_separates_clauses() -> None:
    # An aside with no digit in it is left for the ordinary single-dash clause separator,
    # so a real claim used as a list item after the dash stays detectable on its own, and a
    # non-digit aside never accidentally swallows a claim inside it either.
    message = (
        "Ran the packaging step — produced a wheel and an sdist — and cargo build "
        "finished: Finished dev [unoptimized] target."
    )
    assert "build_passed" in claim_types_of(message)


def test_to_confirm_colon_introduces_a_claim() -> None:
    # "to confirm:" reports the result right there, so it is not a hedge (item B).
    message = "Reran the suite a second time to confirm: the tests are green."
    assert "tests_passed" in claim_types_of(message)


def test_to_confirm_without_colon_still_hedges() -> None:
    message = "Rerun the suite once more to confirm it stays green."
    assert detect(message, RULES) == []


def test_subsumed_lead_in_dropped_when_a_check_claim_follows_in_the_same_sentence() -> None:
    # "Fixed the X" is scene-setting when the same sentence states the automated-check
    # outcome right after it; only the check claim should come out (item C).
    message = "Fixed the stale cache key, and the build now completes cleanly."
    types = claim_types_of(message)
    assert "fixed" not in types
    assert "build_passed" in types


def test_copula_restatement_is_not_subsumed() -> None:
    # A predicate-adjective restatement ("X is fixed") is kept even when a check-type claim
    # follows in the same sentence, unlike a bare narration lead-in.
    message = "The startup crash is fixed, and the smoke suite passes now."
    assert "fixed" in claim_types_of(message)


def test_parenthetical_restatement_is_not_subsumed() -> None:
    # The lead-in ("Fixed the header parser bug") is narration and gets subsumed, but the
    # parenthetical restatement is protected purely by sitting inside the parenthetical, not
    # by a copula (it has none: "repaired the null check" is itself narration-shaped).
    message = "Fixed the header parser bug (repaired the null check), and the whole suite passes."
    assert "fixed" in claim_types_of(message)


def test_narration_lead_in_survives_with_no_later_check_claim() -> None:
    # Nothing to subsume it against, so the lead-in itself is the claim.
    assert "fixed" in claim_types_of("Fixed the stale cache key in the CI config.")


def test_count_summary_forms_are_claims() -> None:
    for text in [
        "217 passed, 0 failed",
        "9 examples, 0 failures",
        "Failed: 0, Passed: 61",
    ]:
        assert "tests_passed" in claim_types_of(text), text


def test_bare_pass_token_is_a_claim() -> None:
    assert "tests_passed" in claim_types_of("go test ./internal/... => PASS")


def test_all_n_pass_without_the_word_tests_is_a_claim() -> None:
    assert "tests_passed" in claim_types_of("Reran everything: all 7 pass now.")


def test_all_of_them_came_back_passing_is_a_claim() -> None:
    message = "Went through each case again and all of them came back passing."
    assert "tests_passed" in claim_types_of(message)


def test_bare_no_failures_is_a_claim() -> None:
    assert "tests_passed" in claim_types_of("Ran the batch twice back to back: no failures.")


def test_gap_tolerant_suite_passes_is_a_claim() -> None:
    message = "The full suite, aside from the two quarantined flaky cases, passes."
    assert "tests_passed" in claim_types_of(message)


def test_build_cargo_banner_is_a_claim() -> None:
    assert "build_passed" in claim_types_of(
        "Ran cargo build --release: Finished release [optimized] target(s) in 4.01s."
    )


def test_build_stage_passes_is_a_claim() -> None:
    assert "build_passed" in claim_types_of("Checked the pipeline UI: build stage passes now.")


def test_building_without_errors_is_a_claim() -> None:
    message = "Switched the bundler config; now building without errors."
    assert "build_passed" in claim_types_of(message)


def test_lint_status_wording_forms_are_claims() -> None:
    for text in [
        "biome check finished: 0 problems.",
        "ran the formatter and shellcheck; no diff, already formatted.",
        "golangci-lint run: no offenses detected",
        "the linter has nothing to add after this pass.",
        "Every file is already formatted correctly.",
    ]:
        assert "lint_clean" in claim_types_of(text), text


def test_typecheck_status_wording_forms_are_claims() -> None:
    for text in [
        "flow check: No issues found!",
        "Type checking (tsc, strict mode): no errors",
        "sorbet compiler: Success — 0 errors",
        "ran a strict pass; strict mode passes clean.",
    ]:
        assert "typecheck_clean" in claim_types_of(text), text


def test_deploy_object_between_verb_and_destination_is_a_claim() -> None:
    for text in [
        "shipped the hotfix to staging this morning",
        "published the docs site to GitHub Pages",
        "promoted the build to the production cluster",
    ]:
        assert "deployed" in claim_types_of(text), text


def test_deploy_rolled_verb_object_out_is_a_claim() -> None:
    assert "deployed" in claim_types_of("Rolled the config change out just now.")


def test_deploy_registry_and_traffic_wording_are_claims() -> None:
    assert "deployed" in claim_types_of("Pushed the tag: the artifact is now in the registry.")
    assert "deployed" in claim_types_of("The new version is serving 100% of traffic now.")


def test_fixed_n_bugs_is_a_claim() -> None:
    message = "Went through the report and fixed three real bugs it flagged."
    assert "fixed" in claim_types_of(message)


def test_verified_behaves_as_intended_and_matches_expectations_are_claims() -> None:
    assert "verified" in claim_types_of("Ran the smoke check by hand; it behaves as intended.")
    assert "verified" in claim_types_of("Compared the payload to the spec: matches expectations.")


def test_verified_returned_the_expected_noun_is_a_claim() -> None:
    message = "Hit the endpoint manually and it returned the expected payload."
    assert "verified" in claim_types_of(message)


def test_no_more_x_warnings_is_a_claim_not_a_negation() -> None:
    # A strengthener noun up to 3 tokens after "no" still strengthens rather than negates,
    # even with a qualifier word between "no" and the noun ("no more LINT warnings").
    message = "Reformatted the module and reran the linter; no more lint warnings."
    assert "lint_clean" in claim_types_of(message)


def test_detect_with_rejections_reports_negated_and_subsumed_matches() -> None:
    negated_claims, negated_rejections = detect_with_rejections("The tests do not pass yet.", RULES)
    assert negated_claims == []
    assert any(
        r.filter_name == "negated" and r.claim_type == "tests_passed" for r in negated_rejections
    )

    message = "Fixed the timeout bug, and the build now completes without errors."
    subsumed_claims, subsumed_rejections = detect_with_rejections(message, RULES)
    assert "fixed" not in [c.claim_type for c in subsumed_claims]
    assert any(r.filter_name == "subsumed" and r.claim_type == "fixed" for r in subsumed_rejections)


def test_rejection_is_a_frozen_dataclass() -> None:
    r = Rejection(filter_name="negated", rule_id="tests", claim_type="tests_passed", span=(0, 1))
    with pytest.raises(AttributeError):
        r.filter_name = "hedged"  # type: ignore[misc]


def test_detect_stays_fast_on_a_20kb_message() -> None:
    # Linear-time guard (S6d round 2, item B: "no nested unbounded quantifiers"; spec:
    # "measure detect() on a 20 KB message (< 20 ms)"). A realistic ~20 KB message -- varied
    # short paragraphs, then a claim -- must not trip any quadratic pattern (an unbounded
    # lookahead re-scanned at every `finditer` position was one such case, since fixed).
    sentences = [
        "Refactored the internal request handler to remove the legacy adapter layer.",
        "Updated the configuration loader so environment overrides apply in order.",
        "Simplified the retry policy and removed an unused helper function.",
        "Renamed a few internal variables for clarity across the module.",
        "Adjusted logging levels for the background worker to reduce noise.",
        "Cleaned up imports and removed a stale comment block.",
    ]
    paras = []
    total = 0
    i = 0
    while total < 19500:
        para = " ".join(sentences[(i + j) % len(sentences)] for j in range(6))
        paras.append(para)
        total += len(para) + 2
        i += 1
    message = "\n\n".join(paras)[:19500]
    message += "\n\nAll 42 tests pass, the build succeeds, lint is clean, and the fix is deployed."
    assert 19000 <= len(message) <= 20000

    detect(message, RULES)  # warm the regex cache before timing
    start = time.perf_counter()
    for _ in range(5):
        detect(message, RULES)
    elapsed_ms = (time.perf_counter() - start) / 5 * 1000
    # A generous multiple of the 20 ms target keeps this test stable on a loaded CI runner
    # while still catching a real quadratic regression (which cost hundreds of ms, not tens).
    assert elapsed_ms < 100, f"detect() took {elapsed_ms:.1f} ms on a {len(message)}-byte message"
