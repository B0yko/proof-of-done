"""Tests for fixtures/dsl.py: scenario validation and marker/label parsing."""

from __future__ import annotations

import glob
import os

import pytest

from fixtures import dsl

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCENARIOS_DIR = os.path.join(_REPO_ROOT, "fixtures", "scenarios")
SCENARIO_PATHS = sorted(glob.glob(os.path.join(SCENARIOS_DIR, "*.yaml")))

MINIMAL = """
id: t
ecosystem: python
turns:
  - user: "hi"
    final: "done"
"""


@pytest.mark.parametrize("path", SCENARIO_PATHS, ids=[os.path.basename(p) for p in SCENARIO_PATHS])
def test_every_checked_in_scenario_is_valid(path: str) -> None:
    scenario = dsl.load_scenario(path)
    assert scenario["id"] == os.path.basename(path)[: -len(".yaml")]
    assert scenario["turns"]


def test_minimal_scenario_gets_documented_defaults() -> None:
    scenario = dsl.parse_scenario(MINIMAL, "t.yaml")
    assert scenario["root"] == dsl.DEFAULT_ROOT
    assert scenario["start"] == "fresh"
    assert scenario["project_files"] == {}
    assert scenario["env"] == {}


def test_unknown_top_level_key_is_rejected() -> None:
    text = MINIMAL + "bogus: 1\n"
    with pytest.raises(dsl.DslError) as exc:
        dsl.parse_scenario(text, "bad.yaml")
    assert str(exc.value) == "bad.yaml: bogus: unknown key"


def test_missing_required_key_names_the_file_and_path() -> None:
    text = "id: t\necosystem: python\n"
    with pytest.raises(dsl.DslError, match=r"^bad\.yaml: : missing required key 'turns'$"):
        dsl.parse_scenario(text, "bad.yaml")


def test_bad_ecosystem_enum_names_the_key_path() -> None:
    text = MINIMAL.replace("ecosystem: python", "ecosystem: cobol")
    with pytest.raises(dsl.DslError, match=r"^bad\.yaml: ecosystem: unknown value 'cobol'"):
        dsl.parse_scenario(text, "bad.yaml")


def test_step_with_zero_keys_is_rejected() -> None:
    text = MINIMAL.replace(
        'final: "done"',
        'steps:\n      - {}\n    final: "done"',
    )
    with pytest.raises(dsl.DslError, match=r"turns\[0\]\.steps\[0\]: expected exactly one of"):
        dsl.parse_scenario(text, "bad.yaml")


def test_step_with_two_keys_is_rejected() -> None:
    text = MINIMAL.replace(
        'final: "done"',
        'steps:\n      - {text: "a", user_bash: {cmd: "x"}}\n    final: "done"',
    )
    with pytest.raises(dsl.DslError, match=r"turns\[0\]\.steps\[0\]: expected exactly one of"):
        dsl.parse_scenario(text, "bad.yaml")


def test_marker_with_no_label_entry_is_rejected() -> None:
    text = MINIMAL.replace('final: "done"', 'final: "[[tests_passed|it passed]]"')
    with pytest.raises(dsl.DslError, match=r"missing label entry for marker 'tests_passed'"):
        dsl.parse_scenario(text, "bad.yaml")


def test_label_with_no_matching_marker_is_rejected() -> None:
    # `final` does have a marker (lint_clean), so this exercises the per-key mismatch check
    # rather than the "no markers at all" one below.
    text = MINIMAL.replace(
        'final: "done"',
        'final: "[[lint_clean|ok]]"\n'
        "    labels: {lint_clean: {label: supported}, tests_passed: {label: supported}}",
    )
    with pytest.raises(dsl.DslError, match=r"label 'tests_passed' has no matching \[\[marker\]\]"):
        dsl.parse_scenario(text, "bad.yaml")


def test_no_claim_turn_must_have_empty_labels() -> None:
    # a `final` with no marker but a non-empty `labels` is invalid even if every key would
    # otherwise be well-formed on its own.
    text = MINIMAL.replace(
        'final: "done"',
        'final: "done"\n    labels: {tests_passed: {label: supported}}',
    )
    with pytest.raises(dsl.DslError):
        dsl.parse_scenario(text, "bad.yaml")


def test_unsupported_label_requires_reason_and_suggest() -> None:
    text = MINIMAL.replace(
        'final: "done"',
        'final: "[[tests_passed|it passed]]"\n    labels: {tests_passed: {label: unsupported}}',
    )
    pattern = r"missing required key 'reason'|missing required key 'suggest'"
    with pytest.raises(dsl.DslError, match=pattern):
        dsl.parse_scenario(text, "bad.yaml")


def test_unsupported_label_rejects_reason_outside_the_closed_set() -> None:
    text = MINIMAL.replace(
        'final: "done"',
        'final: "[[tests_passed|it passed]]"\n'
        "    labels: {tests_passed: {label: unsupported, reason: made_up, suggest: null}}",
    )
    with pytest.raises(dsl.DslError, match=r"unknown value 'made_up'"):
        dsl.parse_scenario(text, "bad.yaml")


def test_unsupported_label_allows_null_suggest() -> None:
    text = MINIMAL.replace(
        'final: "done"',
        'final: "[[tests_passed|it passed]]"\n'
        "    labels: {tests_passed: {label: unsupported, reason: no_command, suggest: null}}",
    )
    scenario = dsl.parse_scenario(text, "ok.yaml")
    assert scenario["turns"][0]["labels"]["tests_passed"]["suggest"] is None


def test_second_claim_of_one_type_uses_hash_suffix() -> None:
    text = MINIMAL.replace(
        'final: "done"',
        'final: "[[tests_passed|first]] and [[tests_passed#2|second]]"\n'
        "    labels:\n"
        "      tests_passed: {label: supported}\n"
        "      tests_passed#2: {label: supported}",
    )
    scenario = dsl.parse_scenario(text, "ok.yaml")
    markers = dsl.parse_markers(scenario["turns"][0]["final"])
    assert [m[0] for m in markers] == ["tests_passed", "tests_passed#2"]


def test_uppercase_marker_syntax_is_treated_as_plain_text() -> None:
    # The type group in MARKER_RE only ever matches `^[a-z][a-z0-9_]*(#\d+)?$`, so an
    # uppercase-led `[[...]]` never becomes a marker at all -- it is just literal text, and the
    # turn is a no-claim turn (empty `labels` is required and accepted).
    text = MINIMAL.replace('final: "done"', 'final: "[[Tests_Passed|it passed]]"\n    labels: {}')
    scenario = dsl.parse_scenario(text, "ok.yaml")
    assert scenario["turns"][0].get("labels", {}) == {}
    assert dsl.parse_markers(scenario["turns"][0]["final"]) == []


def test_attempts_and_final_together_is_rejected() -> None:
    text = """
id: t
ecosystem: python
turns:
  - user: "hi"
    final: "done"
    attempts:
      - final: "done"
"""
    with pytest.raises(dsl.DslError, match=r"turn has both `attempts` and `final`"):
        dsl.parse_scenario(text, "bad.yaml")


def test_empty_attempts_list_is_rejected() -> None:
    text = """
id: t
ecosystem: python
turns:
  - user: "hi"
    attempts: []
"""
    with pytest.raises(dsl.DslError, match=r"attempts: must be non-empty"):
        dsl.parse_scenario(text, "bad.yaml")


def test_empty_turns_list_is_rejected() -> None:
    text = "id: t\necosystem: python\nturns: []\n"
    with pytest.raises(dsl.DslError, match=r"^bad\.yaml: turns: must be non-empty$"):
        dsl.parse_scenario(text, "bad.yaml")


def test_expect_decision_enum_is_validated() -> None:
    text = MINIMAL.replace(
        'final: "done"', 'final: "done"\n    expect: {decision: block, reason: stale}'
    )
    scenario = dsl.parse_scenario(text, "ok.yaml")
    assert scenario["turns"][0]["expect"] == {"decision": "block", "reason": "stale"}

    bad = MINIMAL.replace('final: "done"', 'final: "done"\n    expect: {decision: explode}')
    with pytest.raises(dsl.DslError, match=r"unknown value 'explode'"):
        dsl.parse_scenario(bad, "bad.yaml")


def test_parse_markers_returns_spans_in_the_original_text() -> None:
    text = "a [[x|one]] b [[y#2|two]] c"
    markers = dsl.parse_markers(text)
    assert [(t, inner) for t, inner, _s, _e in markers] == [("x", "one"), ("y#2", "two")]
    for _type, inner, start, end in markers:
        assert text[start:end] == f"[[{_type}|{inner}]]"


def test_scenario_id_from_filename() -> None:
    result = dsl.scenario_id_from_filename("fixtures/scenarios/parallel-calls.yaml")
    assert result == "parallel-calls"
    assert dsl.scenario_id_from_filename("no-extension") is None
