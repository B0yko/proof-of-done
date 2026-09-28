"""End-to-end tests for `proof_of_done.engine.evaluate_stop`.

The bulk of the coverage renders every `fixtures/scenarios/*.yaml` scenario (the same ones
`test_evidence.py`/`test_claude_code_adapter.py` exercise at a lower level) and checks that
`evaluate_stop`'s decision agrees with every stop case's labels end to end: claim detection,
grouping, evidence judging and suggestion all wired together, not tested in isolation. A
second block of targeted tests covers behaviour that is `engine.py`'s own responsibility and
that no scenario file happens to exercise: the skip token, `mode: warn`, the mid-session
downgrade, the Stop-time flush-race downgrade, overlapping-span grouping, and tamper notes.
"""

from __future__ import annotations

import dataclasses
import glob
import os

import pytest
from evidence_helpers import ROOT, SessionBuilder, real_defaults

from fixtures import dsl, render
from proof_of_done import engine, message
from proof_of_done.probe import DictProbe
from proof_of_done.transcript import claude_code
from proof_of_done.transcript.model import KIND_MESSAGE, ROLE_AGENT, Step

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCENARIO_FILES = sorted(glob.glob(os.path.join(REPO_ROOT, "fixtures", "scenarios", "*.yaml")))


def _evaluate(session, stop_index, final_message, project_files, project_root):
    cfg = real_defaults(root=project_root)
    skipped = engine.skip_requested(session, stop_index, cfg.skip_token)
    req = engine.StopRequest(
        session=session,
        stop_index=stop_index,
        final_message=final_message,
        config=cfg,
        project_root=project_root,
        probe=DictProbe(files=project_files),
        skipped=skipped,
    )
    return engine.evaluate_stop(req)


def _results_by_type(decision: engine.Decision) -> dict:
    out: dict[str, list] = {}
    for r in decision.results:
        out.setdefault(r.claim_type, []).append(r)
    return out


@pytest.mark.parametrize(
    "scenario_path", SCENARIO_FILES, ids=[os.path.basename(p) for p in SCENARIO_FILES]
)
def test_scenario_stop_cases_match_their_labels(tmp_path, scenario_path) -> None:
    scenario = dsl.load_scenario(scenario_path)
    out_dir = tmp_path / os.path.basename(scenario_path)
    rendered = render.render(scenario, str(out_dir))

    for case in rendered.stop_cases:
        transcript = case.agent_transcript_path or case.session_path
        session = claude_code.parse(transcript)
        assert not session.unrecognized, f"{scenario_path}: unrecognized transcript"
        stop_index = len(session.steps)
        project_root = session.cwd or dsl.DEFAULT_ROOT

        decision = _evaluate(
            session, stop_index, case.final_message, case.project_files, project_root
        )
        by_type = _results_by_type(decision)

        if not case.labels:
            assert decision.results == [], f"{scenario_path}: expected a claim-free turn"
            continue

        for label in case.labels:
            candidates = by_type.get(label.type, [])
            result = next(
                (r for r in candidates if r.span[0] < label.end and r.span[1] > label.start),
                None,
            )
            assert result is not None, f"{scenario_path}: no claim of type {label.type!r} detected"
            if label.label == "supported":
                assert result.verdict.supported, (
                    f"{scenario_path}: expected {label.type} supported, got {result.verdict.reason}"
                )
            else:
                assert not result.verdict.supported, (
                    f"{scenario_path}: expected {label.type} unsupported"
                )
                if label.reason is not None:
                    assert result.verdict.reason == label.reason, scenario_path
                if label.suggest is not None:
                    assert result.command == label.suggest, scenario_path
                if label.partial:
                    assert result.verdict.partial, scenario_path


def test_every_scenario_file_was_exercised() -> None:
    assert len(SCENARIO_FILES) >= 8, "expected the fixtures/scenarios/*.yaml corpus to be present"


# --------------------------------------------------------------------------------------
# targeted engine behaviour not covered by the scenario corpus
# --------------------------------------------------------------------------------------


def test_skip_token_in_latest_real_prompt_allows_silently() -> None:
    cfg = real_defaults()
    b = SessionBuilder().user("Ship it. #skip-proof")
    b.final("All 42 tests pass.")
    session = b.build()
    req = engine.StopRequest(
        session=session,
        stop_index=b.stop_index,
        final_message="All 42 tests pass.",
        config=cfg,
        project_root=ROOT,
        probe=DictProbe(files={}),
        skipped=engine.skip_requested(session, b.stop_index, cfg.skip_token),
    )
    decision = engine.evaluate_stop(req)
    assert decision.skipped is True
    assert decision.action == "allow"
    assert decision.reason is None
    assert decision.system_message is None


def test_skip_token_in_assistant_text_does_not_count() -> None:
    cfg = real_defaults()
    b = SessionBuilder().user("Ship it.")
    # An assistant message that merely quotes the skip token must not disable the check --
    # only a real user-typed prompt counts.
    b.steps.append(
        Step(
            i=len(b.steps),
            kind=KIND_MESSAGE,
            role=ROLE_AGENT,
            content="I will not write #skip-proof myself.",
        )
    )
    b.final("All 42 tests pass.")
    session = b.build()
    skipped = engine.skip_requested(session, b.stop_index, cfg.skip_token)
    assert skipped is False


def test_mode_warn_downgrades_a_block_to_a_system_message() -> None:
    cfg = real_defaults()
    warn_cfg = dataclasses.replace(cfg, mode="warn")
    b = SessionBuilder().user("Fix it")
    b.final("All 42 tests pass.")
    session = b.build()
    req = engine.StopRequest(
        session=session,
        stop_index=b.stop_index,
        final_message="All 42 tests pass.",
        config=warn_cfg,
        project_root=ROOT,
        probe=DictProbe(files={}),
        skipped=False,
    )
    decision = engine.evaluate_stop(req)
    assert decision.action == "allow"
    assert decision.reason is None
    assert decision.system_message is not None
    assert decision.system_message.startswith("proof-of-done (warn):")
    assert "42 tests pass" in decision.system_message


def test_starts_mid_session_downgrades_every_block_to_warn() -> None:
    cfg = real_defaults()
    b = SessionBuilder().user("Fix it")
    b.final("All 42 tests pass.")
    session = b.build()
    session.starts_mid_session = True
    req = engine.StopRequest(
        session=session,
        stop_index=b.stop_index,
        final_message="All 42 tests pass.",
        config=cfg,
        project_root=ROOT,
        probe=DictProbe(files={}),
        skipped=False,
    )
    decision = engine.evaluate_stop(req)
    assert decision.action == "allow"
    assert decision.system_message is not None
    assert "42 tests pass" in decision.system_message


def test_trailing_unresolved_call_downgrades_no_result_to_warn() -> None:
    cfg = real_defaults()
    b = SessionBuilder().user("Run the suite")
    b.bash("uv run pytest -q", no_result=True)
    session = b.build()
    req = engine.StopRequest(
        session=session,
        stop_index=b.stop_index,
        final_message="All 42 tests pass.",
        config=cfg,
        project_root=ROOT,
        probe=DictProbe(files={}),
        skipped=False,
    )
    decision = engine.evaluate_stop(req)
    assert decision.action == "allow"
    assert decision.system_message is not None
    result = decision.results[0]
    assert result.verdict.reason == "no_result"
    assert result.action == "warn"


def test_overlapping_spans_of_the_same_claim_type_group_into_one_instance() -> None:
    from proof_of_done.claims import Claim

    # Two different rule ids of one claim type both matching an overlapping span of the
    # message (e.g. a custom rule sharing the `tests_passed` claim type with the built-in
    # `tests` rule) must collapse into a single grouped instance; a claim of a *different*
    # type, or a non-overlapping span of the same type, must not.
    found = [
        Claim(rule_id="tests", claim_type="tests_passed", quote="tests pass", span=(4, 14)),
        Claim(rule_id="extra-tests", claim_type="tests_passed", quote="pass", span=(10, 14)),
        Claim(rule_id="lint", claim_type="lint_clean", quote="lint is clean", span=(20, 33)),
        Claim(rule_id="tests", claim_type="tests_passed", quote="suite passes", span=(50, 62)),
    ]
    groups = engine._group_claims(found)
    tests_groups = [g for g in groups if g.claim_type == "tests_passed"]
    assert len(tests_groups) == 2
    merged = next(g for g in tests_groups if g.start == 4)
    assert merged.end == 14
    assert set(merged.rule_ids) == {"tests", "extra-tests"}
    disjoint = next(g for g in tests_groups if g.start == 50)
    assert disjoint.end == 62


def test_tamper_notes_alone_still_produce_a_system_message_with_no_claims() -> None:
    cfg = real_defaults()
    b = SessionBuilder().user("Fix it")
    b.final("Renamed a helper, nothing else changed.")
    session = b.build()
    req = engine.StopRequest(
        session=session,
        stop_index=b.stop_index,
        final_message="Renamed a helper, nothing else changed.",
        config=cfg,
        project_root=ROOT,
        probe=DictProbe(files={}),
        skipped=False,
        tamper_notes=["session edited .proof-of-done.yaml at step 3"],
    )
    decision = engine.evaluate_stop(req)
    assert decision.action == "allow"
    assert decision.results == []
    assert decision.system_message == (
        "proof-of-done: session edited .proof-of-done.yaml at step 3"
    )


def test_tamper_notes_are_appended_to_a_block_reason() -> None:
    cfg = real_defaults()
    b = SessionBuilder().user("Fix it")
    session = b.build()
    req = engine.StopRequest(
        session=session,
        stop_index=b.stop_index,
        final_message="All 42 tests pass.",
        config=cfg,
        project_root=ROOT,
        probe=DictProbe(files={}),
        skipped=False,
        tamper_notes=["session edited .proof-of-done.yaml at step 3"],
    )
    decision = engine.evaluate_stop(req)
    assert decision.action == "block"
    assert "session edited .proof-of-done.yaml at step 3" in decision.reason


def test_claim_result_command_none_renders_the_no_command_hint() -> None:
    cfg = real_defaults()
    b = SessionBuilder().user("Fix it")
    session = b.build()
    req = engine.StopRequest(
        session=session,
        stop_index=b.stop_index,
        final_message="All 42 tests pass.",
        config=cfg,
        project_root=ROOT,
        probe=DictProbe(files={}),
        skipped=False,
    )
    decision = engine.evaluate_stop(req)
    assert decision.action == "block"
    result = decision.results[0]
    assert result.command is None
    assert "run it in the foreground" in decision.reason


def test_prebuilt_events_are_reused_instead_of_rebuilt() -> None:
    from proof_of_done import evidence

    cfg = real_defaults()
    b = SessionBuilder().user("Fix it")
    b.edit("Edit", "src/app/models.py")
    b.bash("uv run pytest -q", output="42 passed in 0.81s")
    session = b.build()
    events = evidence.build_events(session, cfg, ROOT)
    req = engine.StopRequest(
        session=session,
        stop_index=b.stop_index,
        final_message="All 42 tests pass.",
        config=cfg,
        project_root=ROOT,
        probe=DictProbe(files={}),
        skipped=False,
        events=events,
    )
    decision = engine.evaluate_stop(req)
    assert decision.action == "allow"
    assert decision.results[0].verdict.supported


def test_decision_reason_uses_message_block_reason_verbatim() -> None:
    cfg = real_defaults()
    b = SessionBuilder().user("Fix it")
    session = b.build()
    req = engine.StopRequest(
        session=session,
        stop_index=b.stop_index,
        final_message="All 42 tests pass.",
        config=cfg,
        project_root=ROOT,
        probe=DictProbe(files={}),
        skipped=False,
    )
    decision = engine.evaluate_stop(req)
    entry = message.Entry(
        quote=decision.results[0].quote,
        claim_type=decision.results[0].claim_type,
        verdict=decision.results[0].verdict,
        command=decision.results[0].command,
    )
    assert decision.reason == message.block_reason([entry], cfg.skip_token)
