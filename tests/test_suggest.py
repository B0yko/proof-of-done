"""Tests for `proof_of_done.suggest`: the three-source `Run:` command lookup."""

from __future__ import annotations

import pytest
from evidence_helpers import ROOT, SessionBuilder, real_defaults, with_rules

from proof_of_done import evidence, suggest
from proof_of_done.probe import DictProbe

CFG = real_defaults()


def verdict(rule_id: str | None, claim_type: str = "tests_passed") -> evidence.Verdict:
    return evidence.Verdict(
        claim_type=claim_type,
        supported=False,
        reason="no_command",
        partial=False,
        exempt=False,
        action="block",
        rule_id=rule_id,
        details=evidence.VerdictDetails(),
    )


def events_for(builder: SessionBuilder, cfg: object = None) -> evidence.EventList:
    cfg = cfg or CFG
    return evidence.build_events(builder.build(), cfg, ROOT)  # type: ignore[arg-type]


EMPTY_PROBE = DictProbe({})


# ------------------------------------------------------------------------------------------
# source 1: the agent's own command
# ------------------------------------------------------------------------------------------


def test_own_command_beats_rule_suggest_and_detection() -> None:
    rules = [
        dict(
            id="tests",
            claim_type="tests_passed",
            action="block",
            evidence={"commands": ["pytest"]},
            relevant_files=["**"],
            ignore_files=[],
            suggest="pytest --something-else",
        )
    ]
    cfg = with_rules(CFG, rules)
    b = SessionBuilder()
    b.bash("pytest -q", output="42 passed")
    events = events_for(b, cfg)
    probe = DictProbe({"pyproject.toml": ""})
    result = suggest.suggest("tests_passed", verdict("tests"), events, cfg, probe, b.stop_index)
    assert result == "pytest -q"


def test_own_command_display_drops_cd_pipes_and_redirects_but_keeps_wrappers() -> None:
    b = SessionBuilder()
    b.bash("cd app && uv run pytest -q 2>&1 | tail -5", output="42 passed", exit_code=0)
    events = events_for(b)
    result = suggest.suggest(
        "tests_passed", verdict("tests"), events, CFG, EMPTY_PROBE, b.stop_index
    )
    assert result == "uv run pytest -q"


def test_own_command_any_outcome_counts_including_failed_and_background() -> None:
    b = SessionBuilder()
    b.bash("pytest -q", exit_code=1, output="2 failed")
    events = events_for(b)
    result = suggest.suggest(
        "tests_passed", verdict("tests"), events, CFG, EMPTY_PROBE, b.stop_index
    )
    assert result == "pytest -q"


def test_own_command_prefers_latest_non_partial_over_a_later_partial() -> None:
    b = SessionBuilder()
    b.bash("pytest -q", output="42 passed")
    b.bash("pytest -k test_foo", output="1 passed")
    events = events_for(b)
    result = suggest.suggest(
        "tests_passed", verdict("tests"), events, CFG, EMPTY_PROBE, b.stop_index
    )
    assert result == "pytest -q"


def test_own_command_falls_back_to_the_latest_partial_when_no_full_run_exists() -> None:
    b = SessionBuilder()
    b.bash("pytest -k test_foo", output="1 passed")
    events = events_for(b)
    result = suggest.suggest(
        "tests_passed", verdict("tests"), events, CFG, EMPTY_PROBE, b.stop_index
    )
    assert result == "pytest -k test_foo"


def test_own_command_respects_the_stop_index_cutoff() -> None:
    b = SessionBuilder()
    cutoff = b.stop_index
    b.bash("pytest -q", output="42 passed")  # runs only in a later (retried) attempt
    events = events_for(b)
    result = suggest.suggest("tests_passed", verdict("tests"), events, CFG, EMPTY_PROBE, cutoff)
    # nothing of the agent's own qualifies before the cutoff, and no rule.suggest/probe hit
    assert result is None


def test_disqualified_own_command_is_never_suggested() -> None:
    b = SessionBuilder()
    b.bash("pytest(){ echo 5 passed; }; pytest", output="5 passed")
    events = events_for(b)
    result = suggest.suggest(
        "tests_passed", verdict("tests"), events, CFG, EMPTY_PROBE, b.stop_index
    )
    assert result is None


def test_read_only_own_command_is_never_suggested() -> None:
    rules = [
        dict(
            id="tests",
            claim_type="tests_passed",
            action="block",
            evidence={"commands": ["cat"]},
            relevant_files=["**"],
            ignore_files=[],
        )
    ]
    cfg = with_rules(CFG, rules)
    b = SessionBuilder()
    b.bash("cat results.log", output="42 passed")
    events = events_for(b, cfg)
    result = suggest.suggest(
        "tests_passed", verdict("tests"), events, cfg, EMPTY_PROBE, b.stop_index
    )
    assert result is None


# ------------------------------------------------------------------------------------------
# source 2: rule.suggest
# ------------------------------------------------------------------------------------------


def test_rule_suggest_used_when_no_own_command_ran() -> None:
    rules = [
        dict(
            id="tests",
            claim_type="tests_passed",
            action="block",
            evidence={"commands": ["pytest"]},
            relevant_files=["**"],
            ignore_files=[],
            suggest="make test",
        )
    ]
    cfg = with_rules(CFG, rules)
    events = events_for(SessionBuilder(), cfg)
    result = suggest.suggest("tests_passed", verdict("tests"), events, cfg, EMPTY_PROBE, 0)
    assert result == "make test"


# ------------------------------------------------------------------------------------------
# source 3: project-ecosystem detection
# ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("action_kind", "expected"),
    [
        ("tests", "pytest"),
        ("lint", "ruff check ."),
        ("typecheck", "mypy ."),
        ("build", "python -m build"),
    ],
)
def test_python_detection_by_pyproject(action_kind: str, expected: str) -> None:
    probe = DictProbe({"pyproject.toml": "[project]\nname='x'\n"})
    assert suggest.detect_project_command(action_kind, probe) == expected


def test_python_detection_by_pytest_ini_too() -> None:
    probe = DictProbe({"pytest.ini": ""})
    assert suggest.detect_project_command("tests", probe) == "pytest"


def test_python_detection_prefixes_uv_run_when_uv_lock_present() -> None:
    probe = DictProbe({"pyproject.toml": "", "uv.lock": None})
    assert suggest.detect_project_command("tests", probe) == "uv run pytest"
    assert suggest.detect_project_command("lint", probe) == "uv run ruff check ."


def test_python_build_with_uv_lock_is_uv_build_not_uv_run() -> None:
    probe = DictProbe({"pyproject.toml": "", "uv.lock": None})
    assert suggest.detect_project_command("build", probe) == "uv build"


@pytest.mark.parametrize(
    ("action_kind", "script", "expected"),
    [
        ("tests", "test", "npm test"),
        ("lint", "lint", "npm run lint"),
        ("build", "build", "npm run build"),
        ("typecheck", "typecheck", "npm run typecheck"),
        ("typecheck", "type-check", "npm run type-check"),
        ("typecheck", "tsc", "npm run tsc"),
    ],
)
def test_node_detection_script_names(action_kind: str, script: str, expected: str) -> None:
    probe = DictProbe({"package.json": f'{{"scripts": {{"{script}": "..."}}}}'})
    assert suggest.detect_project_command(action_kind, probe) == expected


def test_node_detection_prefers_pnpm_lockfile() -> None:
    probe = DictProbe({"package.json": '{"scripts": {"lint": "eslint ."}}', "pnpm-lock.yaml": None})
    assert suggest.detect_project_command("lint", probe) == "pnpm run lint"
    assert (
        suggest.detect_project_command(
            "tests",
            DictProbe({"package.json": '{"scripts": {"test": "vitest"}}', "pnpm-lock.yaml": None}),
        )
        == "pnpm test"
    )


def test_node_detection_prefers_yarn_lockfile() -> None:
    probe = DictProbe({"package.json": '{"scripts": {"build": "vite build"}}', "yarn.lock": None})
    assert suggest.detect_project_command("build", probe) == "yarn build"


def test_node_detection_falls_through_when_script_is_missing() -> None:
    probe = DictProbe({"package.json": '{"scripts": {"test": "vitest"}}', "go.mod": None})
    # no "lint" script in package.json -> Node is not a hit for lint -> falls through to Go
    assert suggest.detect_project_command("lint", probe) == "go vet ./..."


def test_node_detection_survives_malformed_json() -> None:
    probe = DictProbe({"package.json": "{not valid json", "Cargo.toml": None})
    assert suggest.detect_project_command("tests", probe) == "cargo test"


@pytest.mark.parametrize(
    ("action_kind", "expected"),
    [
        ("tests", "go test ./..."),
        ("lint", "go vet ./..."),
        ("typecheck", "go vet ./..."),
        ("build", "go build ./..."),
    ],
)
def test_go_detection(action_kind: str, expected: str) -> None:
    probe = DictProbe({"go.mod": "module example.com/app\n"})
    assert suggest.detect_project_command(action_kind, probe) == expected


@pytest.mark.parametrize(
    ("action_kind", "expected"),
    [
        ("tests", "cargo test"),
        ("lint", "cargo clippy"),
        ("typecheck", "cargo check"),
        ("build", "cargo build"),
    ],
)
def test_rust_detection(action_kind: str, expected: str) -> None:
    probe = DictProbe({"Cargo.toml": '[package]\nname = "x"\n'})
    assert suggest.detect_project_command(action_kind, probe) == expected


@pytest.mark.parametrize(
    ("action_kind", "target"), [("tests", "test"), ("lint", "lint"), ("build", "build")]
)
def test_makefile_detection(action_kind: str, target: str) -> None:
    probe = DictProbe({"Makefile": f"{target}:\n\techo running\n\nother: build\n"})
    assert suggest.detect_project_command(action_kind, probe) == f"make {target}"


def test_makefile_detection_requires_a_matching_target() -> None:
    probe = DictProbe({"Makefile": "build:\n\techo build\n"})
    assert suggest.detect_project_command("tests", probe) is None


def test_makefile_lowercase_variant_is_read_too() -> None:
    probe = DictProbe({"makefile": "test:\n\tpytest\n"})
    assert suggest.detect_project_command("tests", probe) == "make test"


def test_first_ecosystem_hit_wins_python_before_node() -> None:
    probe = DictProbe({"pyproject.toml": "", "package.json": '{"scripts": {"test": "vitest"}}'})
    assert suggest.detect_project_command("tests", probe) == "pytest"


def test_no_detection_for_deploy() -> None:
    probe = DictProbe({"pyproject.toml": "", "Makefile": "deploy:\n\techo ok\n"})
    assert suggest.detect_project_command("deploy", probe) is None
    assert suggest.detect_project_command(None, probe) is None


def test_no_detection_when_nothing_matches() -> None:
    assert suggest.detect_project_command("tests", DictProbe({})) is None


# ------------------------------------------------------------------------------------------
# end-to-end suggest(): action-kind mapping (fixed/verified reuse tests; deploy has none)
# ------------------------------------------------------------------------------------------


def test_suggest_end_to_end_falls_through_all_three_sources_to_detection() -> None:
    events = events_for(SessionBuilder())
    probe = DictProbe({"go.mod": "module x\n"})
    result = suggest.suggest("tests_passed", verdict("tests"), events, CFG, probe, 0)
    assert result == "go test ./..."


def test_suggest_fixed_reuses_tests_detection() -> None:
    events = events_for(SessionBuilder())
    probe = DictProbe({"Cargo.toml": ""})
    result = suggest.suggest("fixed", verdict("fixed", claim_type="fixed"), events, CFG, probe, 0)
    assert result == "cargo test"


def test_suggest_verified_reuses_tests_detection() -> None:
    events = events_for(SessionBuilder())
    probe = DictProbe({"go.mod": ""})
    result = suggest.suggest(
        "verified", verdict("verified", claim_type="verified"), events, CFG, probe, 0
    )
    assert result == "go test ./..."


def test_suggest_deploy_never_gets_a_detected_command() -> None:
    events = events_for(SessionBuilder())
    probe = DictProbe({"pyproject.toml": "", "Makefile": "deploy:\n\techo ok\n"})
    v = verdict("deploy", claim_type="deployed")
    result = suggest.suggest("deployed", v, events, CFG, probe, 0)
    assert result is None


def test_suggest_unknown_rule_id_falls_back_to_claim_type_mapping() -> None:
    # rule_id not found in config at all (e.g. it was removed since the verdict was built) --
    # suggest() still maps by claim_type for the four known action kinds.
    events = events_for(SessionBuilder())
    probe = DictProbe({"go.mod": ""})
    result = suggest.suggest(
        "lint_clean", verdict("no-such-rule-id", claim_type="lint_clean"), events, CFG, probe, 0
    )
    assert result == "go vet ./..."


def test_suggest_unrecognized_custom_claim_type_has_no_detection() -> None:
    events = events_for(SessionBuilder())
    probe = DictProbe({"pyproject.toml": ""})
    result = suggest.suggest(
        "committed", verdict(None, claim_type="committed"), events, CFG, probe, 0
    )
    assert result is None
