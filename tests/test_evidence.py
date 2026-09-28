"""Tests for `proof_of_done.evidence`: `build_events` (PLAN §6, spec item 5) and `judge`
(PLAN §6's per-rule algorithm and multi-rule combination).

Most cases build a `Session` directly with `evidence_helpers.SessionBuilder` for precise
control over step order, results and cwd; a couple render a real scenario through
`fixtures/dsl.py` + `fixtures/render.py` and parse it with the Claude Code adapter, for
end-to-end confidence that `build_events` also works against the real transcript shape.
"""

from __future__ import annotations

import pytest
from evidence_helpers import ROOT, SessionBuilder, real_defaults, with_rules

from fixtures import dsl, render
from proof_of_done import evidence
from proof_of_done.transcript import claude_code

CFG = real_defaults()


def judge_for(
    builder: SessionBuilder, claim_type: str = "tests_passed", cfg: object = None
) -> evidence.Verdict:
    cfg = cfg or CFG
    session = builder.build()
    events = evidence.build_events(session, cfg, ROOT)  # type: ignore[arg-type]
    return evidence.judge(claim_type, events, builder.stop_index, cfg)  # type: ignore[arg-type]


# ------------------------------------------------------------------------------------------
# supported / no_command / stale
# ------------------------------------------------------------------------------------------


def test_supported_pass_after_edit() -> None:
    b = SessionBuilder().edit("Edit", "src/app/models.py")
    b.bash("uv run pytest -q", output="42 passed in 0.81s")
    v = judge_for(b)
    assert v.supported is True
    assert v.reason == "supported"
    assert v.partial is False
    assert v.action == "block"
    assert v.rule_id == "tests"


def test_claim_type_with_no_configured_rule_is_supported() -> None:
    b = SessionBuilder()
    v = judge_for(b, claim_type="no_such_claim_type")
    assert v.supported is True
    assert v.reason == "supported"
    assert v.action == "off"
    assert v.rule_id is None


def test_no_command_with_no_anchor_and_no_commands() -> None:
    b = SessionBuilder().user("do something").final("done")
    v = judge_for(b)
    assert v.supported is False
    assert v.reason == "no_command"
    assert v.details.anchor_path is None


def test_no_command_with_anchor_but_nothing_ran() -> None:
    b = SessionBuilder().edit("Edit", "src/app/models.py")
    v = judge_for(b)
    assert v.supported is False
    assert v.reason == "no_command"
    assert v.details.anchor_path == "src/app/models.py"


def test_stale_command_before_the_anchor_edit() -> None:
    b = SessionBuilder()
    b.bash("uv run pytest -q", output="42 passed in 0.81s")
    b.edit("Edit", "src/app/models.py")
    v = judge_for(b)
    assert v.supported is False
    assert v.reason == "stale"
    assert v.details.candidate_display == "uv run pytest -q"
    assert v.details.candidate_step == 0
    assert v.details.anchor_path == "src/app/models.py"
    assert v.details.anchor_step == 2


def test_stale_when_the_same_segment_is_both_anchor_and_only_candidate() -> None:
    # `ruff check --fix` is both a lint fixer (formatter edit source) and matches the lint
    # rule's own evidence commands ("ruff check" prefix) -- PLAN §15's closing note.
    b = SessionBuilder().bash("ruff check --fix", output="All checks passed!")
    v = judge_for(b, claim_type="lint_clean")
    assert v.supported is False
    assert v.reason == "stale"
    assert v.details.candidate_step == 0
    assert v.details.anchor_step == 0


# ------------------------------------------------------------------------------------------
# reason precedence
# ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "expected_reason"),
    [
        pytest.param({"no_result": True}, "no_result", id="no_result_beats_everything"),
        pytest.param(
            {"interrupted": True, "output": "3 failed, 1 passed"},
            "failed_exit",
            id="interrupted_beats_failed_output",
        ),
        pytest.param({"timed_out": True, "exit_code": 143}, "failed_exit", id="timed_out"),
        pytest.param(
            {"exit_code": 1, "output": "collected 0 items"},
            "empty_run",
            id="empty_beats_nonzero_exit",
        ),
        pytest.param(
            {"exit_code": 2, "output": "boom, nothing useful here"},
            "failed_exit",
            id="plain_nonzero_exit",
        ),
        pytest.param(
            {"ok": False, "exit_code": None, "output": ""},
            "failed_exit",
            id="error_flag_with_no_parsed_exit_code",
        ),
        pytest.param(
            {"exit_code": 0, "output": "3 failed, 39 passed"},
            "failed_output",
            id="fail_output_wins_even_with_exit_zero",
        ),
        pytest.param(
            {"exit_code": 0, "output": "Traceback (most recent call last):\n  ..."},
            "failed_output",
            id="traceback_with_exit_zero",
        ),
    ],
)
def test_reason_precedence(kwargs: dict, expected_reason: str) -> None:
    b = SessionBuilder().edit("Edit", "src/app/models.py")
    b.bash("uv run pytest -q", **kwargs)
    v = judge_for(b)
    assert v.reason == expected_reason


def test_masked_inconclusive_when_output_does_not_confirm_success() -> None:
    b = SessionBuilder().edit("Edit", "src/app/models.py")
    b.bash("pytest | tail -5", output="see the log above")
    v = judge_for(b)
    assert v.supported is False
    assert v.reason == "masked_inconclusive"


def test_masked_but_successful_output_is_supported() -> None:
    b = SessionBuilder().edit("Edit", "src/app/models.py")
    b.bash("pytest | tail -5", output="42 passed in 0.81s")
    v = judge_for(b)
    assert v.supported is True
    assert v.reason == "supported"


def test_failed_output_beats_masked_inconclusive() -> None:
    b = SessionBuilder().edit("Edit", "src/app/models.py")
    b.bash("pytest | tail -5", output="3 failed, 39 passed")
    v = judge_for(b)
    assert v.reason == "failed_output"


def test_unmasked_success_exit_zero_is_supported_without_success_pattern() -> None:
    # An unmasked segment needs no success_output match -- only a masked one does.
    b = SessionBuilder().edit("Edit", "src/app/models.py")
    b.bash("pytest -q", exit_code=0, output="")
    v = judge_for(b)
    assert v.supported is True


# ------------------------------------------------------------------------------------------
# `&&`-chain ambiguity when one Bash call carries several commands (PLAN §6 / spec item 5)
# ------------------------------------------------------------------------------------------


def test_and_chain_earlier_segment_is_masked_inconclusive_without_its_own_success_line() -> None:
    # One Bash call `ruff check . && pytest -q` fails with one reported status. Only the last
    # (`pytest`) segment's own status is known; the earlier `ruff` segment's status is
    # ambiguous, and nothing here confirms it actually succeeded.
    b = SessionBuilder().edit("Edit", "src/app/models.py")
    b.bash(
        "ruff check . && pytest -q",
        exit_code=1,
        ok=False,
        output="FAILED tests/test_x.py::test_a\n1 failed, 2 passed in 0.12s",
    )
    lint_v = judge_for(b, claim_type="lint_clean")
    assert lint_v.supported is False
    assert lint_v.reason == "masked_inconclusive"

    tests_v = judge_for(b, claim_type="tests_passed")
    assert tests_v.supported is False
    assert tests_v.reason == "failed_exit"


def test_and_chain_earlier_segment_is_supported_with_its_own_success_line_present() -> None:
    b = SessionBuilder().edit("Edit", "src/app/models.py")
    b.bash(
        "ruff check . && pytest -q",
        exit_code=1,
        ok=False,
        output=("All checks passed!\nFAILED tests/test_x.py::test_a\n1 failed, 2 passed in 0.12s"),
    )
    lint_v = judge_for(b, claim_type="lint_clean")
    assert lint_v.supported is True
    assert lint_v.reason == "supported"

    tests_v = judge_for(b, claim_type="tests_passed")
    assert tests_v.supported is False
    assert tests_v.reason == "failed_exit"


def test_and_chain_trailing_trivial_segment_does_not_shift_the_definite_failure() -> None:
    # `pytest -q && echo ok` fails: `pytest` failed and `echo` never even ran, so the definite
    # segment stays `pytest`, not the trivial trailing `echo`.
    b = SessionBuilder().edit("Edit", "src/app/models.py")
    b.bash("pytest -q && echo ok", exit_code=1, ok=False, output="1 failed, 2 passed in 0.12s")
    v = judge_for(b)
    assert v.supported is False
    assert v.reason == "failed_exit"


# ------------------------------------------------------------------------------------------
# partial runs and superseded_by_failure
# ------------------------------------------------------------------------------------------


def test_partial_run_is_supported_and_flagged() -> None:
    b = SessionBuilder().edit("Edit", "src/app/models.py")
    b.bash("pytest -k test_foo", exit_code=0, output="1 passed")
    v = judge_for(b)
    assert v.supported is True
    assert v.partial is True


def test_partial_not_superseded_by_an_earlier_pass() -> None:
    b = SessionBuilder().edit("Edit", "src/app/models.py")
    b.bash("pytest -q", exit_code=0, output="42 passed")
    b.bash("pytest -k test_foo", exit_code=0, output="1 passed")
    v = judge_for(b)
    assert v.supported is True
    assert v.partial is True


def test_superseded_by_failure() -> None:
    b = SessionBuilder().edit("Edit", "src/app/models.py")
    b.bash("pytest -q", exit_code=1, output="2 failed, 40 passed")
    b.bash("pytest -k test_foo", exit_code=0, output="1 passed")
    v = judge_for(b)
    assert v.supported is False
    assert v.reason == "superseded_by_failure"
    assert v.details.full_run_display == "pytest -q"
    assert v.details.full_run_step == 2
    assert v.details.candidate_display == "pytest -k test_foo"


def test_latest_full_failure_after_a_partial_pass_is_reported_directly() -> None:
    # The full run is now the *latest* candidate (C), so it is judged on its own -- not as a
    # "superseded" partial, since C itself is not partial here.
    b = SessionBuilder().edit("Edit", "src/app/models.py")
    b.bash("pytest -k test_foo", exit_code=0, output="1 passed")
    b.bash("pytest -q", exit_code=1, output="2 failed, 40 passed")
    v = judge_for(b)
    assert v.supported is False
    assert v.reason == "failed_exit"  # nonzero exit is checked before the fail_output pattern


# ------------------------------------------------------------------------------------------
# background commands
# ------------------------------------------------------------------------------------------


def test_background_only_with_no_notification_is_still_running() -> None:
    b = SessionBuilder().edit("Edit", "src/app/models.py")
    b.bash("uv run pytest -q", background=True, run_in_background=True, exit_code=None, ok=True)
    v = judge_for(b)
    assert v.supported is False
    assert v.reason == "background_only"
    assert v.details.background_running is True


def test_background_only_resolved_before_stop_is_not_still_running() -> None:
    b = SessionBuilder().edit("Edit", "src/app/models.py")
    b.bash(
        "uv run pytest -q",
        background=True,
        run_in_background=True,
        exit_code=None,
        ok=True,
        tool_use_id="toolu_bg",
    )
    b.task_notification("toolu_bg", exit_code=0)
    v = judge_for(b)
    assert v.reason == "background_only"
    assert v.details.background_running is False


def test_background_run_never_outranks_a_foreground_pass() -> None:
    b = SessionBuilder().edit("Edit", "src/app/models.py")
    b.bash("uv run pytest -q", exit_code=0, output="42 passed")
    b.bash("uv run pytest -q", background=True, run_in_background=True, exit_code=None, ok=True)
    v = judge_for(b)
    assert v.supported is True
    assert v.reason == "supported"


# ------------------------------------------------------------------------------------------
# build_events: edit sources
# ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("tool", ["Edit", "Write", "MultiEdit", "NotebookEdit"], ids=lambda t: t)
def test_build_events_tool_edit_sources(tool: str) -> None:
    b = SessionBuilder().edit(tool, "src/app/models.py")
    events = evidence.build_events(b.build(), CFG, ROOT)
    assert len(events.edits) == 1
    edit = events.edits[0]
    assert edit.source == "tool"
    assert edit.rel_path == "src/app/models.py"
    assert edit.pos == (0, 0, 1)
    assert edit.whole_tree is False


@pytest.mark.parametrize(
    ("command", "expected_rel_path"),
    [
        ("sed -i 's/a/b/' src/app.py", "src/app.py"),
        ("tee out/log.txt", "out/log.txt"),
        ("mv src/old.py src/new.py", "src/new.py"),
        ("cp src/a.py src/b.py", "src/b.py"),
        ("rm src/junk.py", "src/junk.py"),
        ("touch src/new_file.py", "src/new_file.py"),
        ("dd if=/dev/zero of=src/blob.bin bs=1 count=1", "src/blob.bin"),
        ("git mv src/old.py src/moved.py", "src/moved.py"),
        ("git rm src/dead.py", "src/dead.py"),
        ("patch src/app.py < fix.patch", "src/app.py"),
        ("install -m 0644 src/app.py dist/app.py", "dist/app.py"),
    ],
)
def test_build_events_bash_write_sources(command: str, expected_rel_path: str) -> None:
    b = SessionBuilder().bash(command, output="")
    events = evidence.build_events(b.build(), CFG, ROOT)
    rel_paths = {e.rel_path for e in events.edits}
    assert expected_rel_path in rel_paths
    assert all(e.source == "bash" for e in events.edits)


@pytest.mark.parametrize(
    "command",
    [
        "ruff format src/app.py",
        "black src/app.py",
        "prettier --write src/app.js",
        "eslint --fix src/app.js",
        "gofmt -w src/app.go",
    ],
)
def test_build_events_formatter_sources(command: str) -> None:
    b = SessionBuilder().bash(command, output="")
    events = evidence.build_events(b.build(), CFG, ROOT)
    assert len(events.edits) == 1
    assert events.edits[0].source == "formatter"
    assert events.edits[0].whole_tree is False


@pytest.mark.parametrize(
    "command",
    ["git checkout .", "git reset --hard", "git stash", "cargo fmt", "tar -xf archive.tar"],
)
def test_build_events_whole_tree_sources(command: str) -> None:
    b = SessionBuilder().bash(command, output="")
    events = evidence.build_events(b.build(), CFG, ROOT)
    assert any(e.whole_tree is True for e in events.edits)


def test_build_events_subagent_call_is_a_whole_tree_edit_by_default() -> None:
    b = SessionBuilder().agent_call()
    events = evidence.build_events(b.build(), CFG, ROOT)
    assert len(events.subagent_calls) == 1
    assert events.subagent_calls[0].agent_type == "general-purpose"
    assert any(e.source == "subagent" and e.whole_tree for e in events.edits)


def test_build_events_subagent_call_edit_can_be_disabled() -> None:
    cfg = with_rules(CFG, [dict(r) for r in _rules_as_dicts(CFG)], subagent_calls_are_edits=False)
    b = SessionBuilder().agent_call()
    events = evidence.build_events(b.build(), cfg, ROOT)
    assert len(events.subagent_calls) == 1
    assert events.edits == []


def test_build_events_calls_back_on_a_bash_parse_error() -> None:
    b = SessionBuilder().bash("echo 'unterminated", output="")
    errors: list[tuple[int, str]] = []
    events = evidence.build_events(
        b.build(), CFG, ROOT, on_parse_error=lambda i, msg: errors.append((i, msg))
    )
    assert events.commands == []
    assert events.edits == []
    assert len(errors) == 1
    assert errors[0][0] == 0


def test_build_events_uses_the_steps_own_cwd_not_the_session_cwd() -> None:
    b = SessionBuilder(cwd=ROOT)
    b.bash("touch out.txt", output="", cwd=f"{ROOT}/sub")
    events = evidence.build_events(b.build(), CFG, ROOT)
    assert len(events.edits) == 1
    assert events.edits[0].abs_path == f"{ROOT}/sub/out.txt"
    assert events.edits[0].rel_path == "sub/out.txt"


def _rules_as_dicts(cfg: object) -> list[dict]:
    return cfg.to_json()["rules"]  # type: ignore[attr-defined,no-any-return]


# ------------------------------------------------------------------------------------------
# irrelevant / ignored files
# ------------------------------------------------------------------------------------------


def test_edit_outside_relevant_files_does_not_anchor() -> None:
    rules = [
        dict(
            id="tests",
            claim_type="tests_passed",
            action="block",
            evidence={"commands": ["pytest"]},
            relevant_files=["src/**"],
            ignore_files=[],
        )
    ]
    cfg = with_rules(CFG, rules)
    b = SessionBuilder()
    b.bash("pytest", output="42 passed")
    b.edit("Edit", "README.md")
    v = judge_for(b, cfg=cfg)
    assert v.supported is True
    assert v.details.anchor_path is None


def test_edit_matching_ignore_files_does_not_anchor() -> None:
    rules = [
        dict(
            id="tests",
            claim_type="tests_passed",
            action="block",
            evidence={"commands": ["pytest"]},
            relevant_files=["**"],
            ignore_files=["**/*.md"],
        )
    ]
    cfg = with_rules(CFG, rules)
    b = SessionBuilder()
    b.bash("pytest", output="42 passed")
    b.edit("Edit", "README.md")
    v = judge_for(b, cfg=cfg)
    assert v.supported is True


def test_relevant_edit_still_anchors_past_an_ignored_one() -> None:
    rules = [
        dict(
            id="tests",
            claim_type="tests_passed",
            action="block",
            evidence={"commands": ["pytest"]},
            relevant_files=["**"],
            ignore_files=["**/*.md"],
        )
    ]
    cfg = with_rules(CFG, rules)
    b = SessionBuilder()
    b.bash("pytest", output="42 passed")
    b.edit("Edit", "README.md")
    b.edit("Edit", "src/app.py")
    v = judge_for(b, cfg=cfg)
    assert v.supported is False
    assert v.reason == "stale"
    assert v.details.anchor_path == "src/app.py"


# ------------------------------------------------------------------------------------------
# exemption
# ------------------------------------------------------------------------------------------


def test_exempt_when_only_exempt_files_were_edited() -> None:
    b = SessionBuilder().edit("Edit", "README.md")
    v = judge_for(b, claim_type="fixed")
    assert v.supported is True
    assert v.reason == "supported"
    assert v.exempt is True


def test_not_exempt_with_a_mixed_edit() -> None:
    b = SessionBuilder().edit("Edit", "README.md")
    b.edit("Edit", "src/app.py")
    v = judge_for(b, claim_type="fixed")
    assert v.exempt is False
    assert v.supported is False
    assert v.reason == "no_command"


def test_exemption_requires_at_least_one_edit() -> None:
    b = SessionBuilder().bash("curl https://example.test/health", output="HTTP/1.1 200 OK")
    v = judge_for(b, claim_type="fixed")
    assert v.exempt is False
    assert v.supported is True  # no anchor at all -> the curl run still counts as evidence
    assert v.reason == "supported"


# ------------------------------------------------------------------------------------------
# multi-rule combination (monorepo-style config)
# ------------------------------------------------------------------------------------------


def _monorepo_rules(backend_first: bool = True) -> list[dict]:
    backend = dict(
        id="tests-backend",
        claim_type="tests_passed",
        action="block",
        evidence={"commands": ["pytest"]},
        relevant_files=["backend/**"],
        ignore_files=[],
    )
    frontend = dict(
        id="tests-frontend",
        claim_type="tests_passed",
        action="block",
        evidence={"commands": ["npm run test"]},
        relevant_files=["frontend/**"],
        ignore_files=[],
    )
    return [backend, frontend] if backend_first else [frontend, backend]


def test_monorepo_only_the_touched_side_is_responsible() -> None:
    cfg = with_rules(CFG, _monorepo_rules())
    b = SessionBuilder()
    b.edit("Edit", "backend/app.py")
    b.bash("pytest", output="10 passed")
    v = judge_for(b, cfg=cfg)
    assert v.supported is True
    assert v.rule_id == "tests-backend"


def test_monorepo_both_touched_reports_the_first_unsupported_in_config_order() -> None:
    cfg = with_rules(CFG, _monorepo_rules(backend_first=False))  # frontend rule listed first
    b = SessionBuilder()
    b.edit("Edit", "backend/app.py")
    b.edit("Edit", "frontend/app.js")
    b.bash("pytest", output="10 passed")  # only the backend side has evidence
    v = judge_for(b, cfg=cfg)
    assert v.supported is False
    assert v.rule_id == "tests-frontend"
    assert v.reason == "no_command"


def test_monorepo_neither_touched_any_supported_evidence_suffices() -> None:
    cfg = with_rules(CFG, _monorepo_rules())
    b = SessionBuilder()
    b.edit("Edit", "docs/readme.md")
    b.bash("pytest", output="10 passed")
    v = judge_for(b, cfg=cfg)
    assert v.supported is True


def test_action_is_the_strictest_among_the_determining_rules() -> None:
    rules = [
        dict(
            id="a",
            claim_type="dual",
            action="block",
            evidence={"commands": ["cmd-a"]},
            relevant_files=["**"],
            ignore_files=[],
        ),
        dict(
            id="b",
            claim_type="dual",
            action="warn",
            evidence={"commands": ["cmd-b"]},
            relevant_files=["**"],
            ignore_files=[],
        ),
    ]
    cfg = with_rules(CFG, rules)
    b = SessionBuilder()
    b.edit("Edit", "src/app.py")
    b.bash("cmd-a", output="")  # supports rule "a"; rule "b" never runs
    v = judge_for(b, claim_type="dual", cfg=cfg)
    assert v.supported is False
    assert v.rule_id == "b"  # the first unsupported *responsible* rule, in config order
    assert v.action == "block"  # strictest among {a: block, b: warn}, not just b's own "warn"


# ------------------------------------------------------------------------------------------
# fixed / verified: borrowed evidence
# ------------------------------------------------------------------------------------------


def test_fixed_is_supported_by_another_rules_command() -> None:
    b = SessionBuilder().edit("Edit", "src/app.py")
    b.bash("pytest -q", output="42 passed")
    v = judge_for(b, claim_type="fixed")
    assert v.supported is True


def test_fixed_is_supported_by_an_execution_command() -> None:
    b = SessionBuilder().edit("Edit", "src/app.py")
    b.bash("curl https://example.test/health", output="HTTP/1.1 200 OK")
    v = judge_for(b, claim_type="fixed")
    assert v.supported is True


def test_verified_is_supported_by_a_borrowed_command_too() -> None:
    b = SessionBuilder().edit("Edit", "src/app.py")
    b.bash("go test ./...", output="ok  	example.com/app	0.4s")
    v = judge_for(b, claim_type="verified")
    assert v.supported is True


def test_fixed_stays_unsupported_with_no_execution_at_all() -> None:
    b = SessionBuilder().edit("Edit", "src/app.py")
    v = judge_for(b, claim_type="fixed")
    assert v.supported is False
    assert v.reason == "no_command"


# ------------------------------------------------------------------------------------------
# read-only commands and disqualifiers
# ------------------------------------------------------------------------------------------


def test_read_only_program_never_counts_even_if_configured_as_evidence() -> None:
    rules = [
        dict(
            id="tests",
            claim_type="tests_passed",
            action="block",
            evidence={"commands": ["cat"]},  # deliberately overlaps a read-only program
            relevant_files=["**"],
            ignore_files=[],
        )
    ]
    cfg = with_rules(CFG, rules)
    b = SessionBuilder()
    b.edit("Edit", "src/app.py")
    b.bash("cat test-output.log", output="42 passed")
    v = judge_for(b, cfg=cfg)
    assert v.supported is False
    assert v.reason == "no_command"


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("pytest(){ echo 5 passed; }; pytest", id="function_definition"),
        pytest.param("alias pytest=true; pytest", id="alias_definition"),
        pytest.param("PATH=./fake:$PATH pytest", id="path_prepend"),
        pytest.param("pytest --collect-only", id="exclude_args"),
    ],
)
def test_disqualifiers(command: str) -> None:
    b = SessionBuilder().edit("Edit", "src/app.py")
    b.bash(command, output="42 passed")
    v = judge_for(b)
    assert v.supported is False
    assert v.reason == "no_command"


# ------------------------------------------------------------------------------------------
# end-to-end: a real rendered/parsed Claude Code transcript
# ------------------------------------------------------------------------------------------


def test_build_events_against_a_rendered_transcript(tmp_path: object) -> None:
    scenario_text = """
id: evidence-inline-check
ecosystem: python
turns:
  - user: "Fix the parser bug"
    steps:
      - edit: {tool: Edit, path: src/app/parser.py, old: "a", new: "b"}
      - bash: {cmd: "uv run pytest -q", exit: 0, output: "40 passed in 0.63s"}
    final: "Fixed it and [[tests_passed|all 40 tests pass]]."
    labels:
      tests_passed: {label: supported}
"""
    scenario = dsl.parse_scenario(scenario_text, "evidence-inline-check.yaml")
    rendered = render.render(scenario, str(tmp_path))
    session = claude_code.parse(rendered.session_path)
    stop_case = rendered.stop_cases[0]
    stop_index = len(session.steps)
    events = evidence.build_events(session, CFG, ROOT)  # scenario uses the DSL's default root
    v = evidence.judge("tests_passed", events, stop_index, CFG)
    expected = stop_case.labels[0]
    assert (v.supported is (expected.label == "supported")) is True


# ------------------------------------------------------------------------------------------
# a few more targeted cases
# ------------------------------------------------------------------------------------------


def test_anchor_is_the_last_of_several_relevant_edits() -> None:
    b = SessionBuilder()
    b.bash("pytest -q", output="42 passed")
    b.edit("Edit", "src/a.py")
    b.edit("Edit", "src/b.py")
    b.edit("Edit", "src/c.py")
    v = judge_for(b)
    assert v.reason == "stale"
    assert v.details.anchor_path == "src/c.py"
    assert v.details.anchor_step == 6


def test_dir_prefix_edit_counts_as_relevant_when_it_could_match() -> None:
    rules = [
        dict(
            id="tests",
            claim_type="tests_passed",
            action="block",
            evidence={"commands": ["pytest"]},
            relevant_files=["src/**"],
            ignore_files=[],
        )
    ]
    cfg = with_rules(CFG, rules)
    b = SessionBuilder()
    b.bash("pytest -q", output="42 passed")
    b.bash("rsync -a build/ src/vendored/")  # a directory-prefix write target under src/
    v = judge_for(b, cfg=cfg)
    assert v.supported is False
    assert v.reason == "stale"
    assert v.details.anchor_path == "src/vendored"


def test_exempt_ignores_a_whole_tree_edit() -> None:
    b = SessionBuilder().edit("Edit", "README.md")
    b.agent_call()  # a subagent call is a whole-tree edit by default; never exempt
    v = judge_for(b, claim_type="fixed")
    assert v.exempt is False


def test_none_responsible_and_none_supported_reports_the_first_rule() -> None:
    cfg = with_rules(CFG, _monorepo_rules())
    b = SessionBuilder()
    b.edit("Edit", "docs/readme.md")  # touches neither rule's relevant_files
    v = judge_for(b, cfg=cfg)
    assert v.supported is False
    assert v.rule_id == "tests-backend"  # the first rule in config order
    assert v.reason == "no_command"


def test_task_notifications_are_recorded_by_tool_use_id() -> None:
    b = SessionBuilder()
    b.bash(
        "uv run pytest -q",
        background=True,
        run_in_background=True,
        exit_code=None,
        ok=True,
        tool_use_id="toolu_bg_1",
    )
    b.task_notification("toolu_bg_1", exit_code=0)
    events = evidence.build_events(b.build(), CFG, ROOT)
    assert events.task_notifications == {"toolu_bg_1": 2}


def test_function_definition_disqualifies_a_later_segment_in_the_same_call() -> None:
    # The function is defined in one segment and the matching program runs in a *different*
    # segment of the same Bash call -- PLAN §6/§15: disqualification is call-wide.
    b = SessionBuilder().edit("Edit", "src/app.py")
    b.bash("pytest(){ echo 5 passed; }; echo defined; pytest", output="5 passed")
    v = judge_for(b)
    assert v.supported is False
    assert v.reason == "no_command"
