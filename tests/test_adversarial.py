"""Tests for eval/adversarial/*.yaml: the labelled set of gaming attempts against the gate.

Only structural checks belong here: every file loads, its `id` matches its filename, it
renders, and every turn carries a labelled `expect`. Whether the (not yet built) hook actually
reaches each `expect` outcome is an `eval/run_eval.py` concern, not this test's.
"""

from __future__ import annotations

import glob
import os
import re

import pytest

from fixtures import dsl, render

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ADVERSARIAL_DIR = os.path.join(_REPO_ROOT, "eval", "adversarial")
_PATHS = sorted(glob.glob(os.path.join(_ADVERSARIAL_DIR, "*.yaml")))
_NAME_RE = re.compile(r"^adv-\d{2}-[a-z0-9-]+$")

_MIN_SESSIONS = 34


def test_at_least_the_minimum_session_count_is_present() -> None:
    assert len(_PATHS) >= _MIN_SESSIONS, len(_PATHS)


@pytest.mark.parametrize("path", _PATHS, ids=[os.path.basename(p) for p in _PATHS])
def test_file_name_follows_the_documented_pattern(path: str) -> None:
    base = os.path.basename(path)[: -len(".yaml")]
    assert _NAME_RE.match(base), base


@pytest.mark.parametrize("path", _PATHS, ids=[os.path.basename(p) for p in _PATHS])
def test_every_session_validates_and_id_matches_filename(path: str) -> None:
    scenario = dsl.load_scenario(path)
    assert scenario["id"] == os.path.basename(path)[: -len(".yaml")]


@pytest.mark.parametrize("path", _PATHS, ids=[os.path.basename(p) for p in _PATHS])
def test_every_session_renders(path: str, tmp_path: object) -> None:
    scenario = dsl.load_scenario(path)
    result = render.render(scenario, str(tmp_path))
    assert result.stop_cases


@pytest.mark.parametrize("path", _PATHS, ids=[os.path.basename(p) for p in _PATHS])
def test_every_turn_carries_a_labelled_expect(path: str) -> None:
    scenario = dsl.load_scenario(path)
    for turn in scenario["turns"]:
        assert "expect" in turn, f"{path}: turn has no `expect`"
        assert turn["expect"]["decision"] in ("block", "warn", "allow")
        reason = turn["expect"].get("reason")
        if reason is not None:
            assert reason in dsl.CLOSED_REASONS, reason


def test_expect_reasons_are_present_whenever_a_decision_is_not_allow() -> None:
    # `allow` sessions may omit `reason` (nothing was blocked); `block`/`warn` sessions should
    # usually name the reason so the known-miss cases (which intentionally omit it) stand out.
    missing_reason_on_block = []
    for path in _PATHS:
        scenario = dsl.load_scenario(path)
        for turn in scenario["turns"]:
            expect = turn["expect"]
            if expect["decision"] == "block" and expect.get("reason") is None:
                missing_reason_on_block.append(os.path.basename(path))
    # The two documented known misses (the Makefile and npm watch-script cases) are the only
    # sessions expected to omit a reason on a block decision.
    assert set(missing_reason_on_block) == {
        "adv-07-makefile-echo-only-known-miss.yaml",
        "adv-36-npm-run-test-watch-known-miss.yaml",
    }, missing_reason_on_block


def test_decisions_cover_block_and_allow() -> None:
    decisions = set()
    for path in _PATHS:
        scenario = dsl.load_scenario(path)
        for turn in scenario["turns"]:
            decisions.add(turn["expect"]["decision"])
    assert {"block", "allow"} <= decisions
