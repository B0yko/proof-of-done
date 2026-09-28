"""Tests for fixtures/expand.py: deterministic expansion of the scenario templates."""

from __future__ import annotations

import os

import pytest

from fixtures import dsl, expand, render

# Computed once per test session: expanding ~900 scenarios is cheap, but doing it in every
# test function would add up.
_SCENARIOS = expand.expand()

_TARGET_TURNS = 600
_TARGET_CLAIMS = 800

_BUILTIN_CLAIM_TYPES = {
    "tests_passed",
    "build_passed",
    "lint_clean",
    "typecheck_clean",
    "fixed",
    "deployed",
    "verified",
}


def test_expand_is_deterministic() -> None:
    assert expand.expand() == expand.expand()


def test_scenario_ids_follow_the_documented_pattern_and_are_unique() -> None:
    ids = [s["id"] for s in _SCENARIOS]
    assert len(ids) == len(set(ids))
    for scenario_id in ids:
        assert scenario_id.startswith("tpl-")
        # tpl-<template>-<ecosystem>-<k>: the ecosystem and the trailing variant index must be
        # recoverable from the id.
        eco = scenario_id.rsplit("-", 2)[-2]
        assert eco in expand.ECOSYSTEM_NAMES
        variant = scenario_id.rsplit("-", 1)[-1]
        assert variant.isdigit()


def test_every_expanded_scenario_validates() -> None:
    for scenario in _SCENARIOS:
        dsl.validate_scenario(scenario, scenario["id"] + ".yaml")


def test_every_expanded_scenario_renders(tmp_path: object) -> None:
    out_dir = str(tmp_path)
    for scenario in _SCENARIOS:
        validated = dsl.validate_scenario(scenario, scenario["id"] + ".yaml")
        result = render.render(validated, out_dir)
        assert os.path.exists(result.session_path)
        assert result.stop_cases


def test_expansion_meets_the_turn_and_claim_targets() -> None:
    stats = expand.compute_stats(_SCENARIOS)
    assert stats["turns"] >= _TARGET_TURNS, stats["turns"]
    assert stats["claims"] >= _TARGET_CLAIMS, stats["claims"]


def test_every_reason_code_occurs() -> None:
    stats = expand.compute_stats(_SCENARIOS)
    seen = set(stats["by_reason"])
    assert seen == dsl.CLOSED_REASONS, dsl.CLOSED_REASONS - seen


def test_every_builtin_claim_type_occurs() -> None:
    stats = expand.compute_stats(_SCENARIOS)
    seen = set(stats["by_claim_type"])
    assert seen == _BUILTIN_CLAIM_TYPES, _BUILTIN_CLAIM_TYPES - seen


def test_every_ecosystem_occurs() -> None:
    stats = expand.compute_stats(_SCENARIOS)
    seen = set(stats["by_ecosystem"])
    assert seen == set(expand.ECOSYSTEM_NAMES), set(expand.ECOSYSTEM_NAMES) - seen


def test_template_count_is_at_least_thirty() -> None:
    _ecosystems, _ctx, templates = expand.load_context()
    assert len(templates) >= 30, len(templates)


@pytest.mark.parametrize("claim_type", sorted(_BUILTIN_CLAIM_TYPES))
def test_every_claim_type_has_a_keyword_bearing_quote(claim_type: str) -> None:
    """Guards the fast-path prefilter invariant (PLAN.md §9/§15): every labelled claim quote
    must contain at least one of that type's detector keywords, so the prefilter never misses
    it. Approximated here as "the phrasing pool is non-empty and every phrasing is non-blank";
    the keyword-substring check itself lives in the claims.py test suite once it exists, but the
    phrasing pool is authored to satisfy it (see fixtures/templates/phrasings.yaml)."""
    _ecosystems, ctx, _templates = expand.load_context()
    phrasings = ctx["phrasings"][claim_type]
    assert len(phrasings) >= 10, len(phrasings)
    assert all(p.strip() for p in phrasings)


def test_cli_stats_runs(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = expand._cli(["--stats"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "turns:" in out
    assert "claim instances:" in out
    assert "by claim type:" in out
    assert "by reason:" in out
    assert "by ecosystem:" in out
