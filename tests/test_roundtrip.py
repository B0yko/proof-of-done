"""Round-trip test (S9 spec item 7): render a fixture -> Claude Code verdicts -> export ->
re-import with the agent-trace adapter -> identical verdicts (type, span, supported, reason,
partial, suggested command) at every stop attempt, and the exported trace is schema-valid.

Covers parallel tool calls (one assistant message, two `tool_use` blocks) and >5 000-char
outputs whose pass/fail summary sits at the very tail -- both bounded again by this project's
own truncation rules (Claude Code's 64 KiB head+tail bound at parse time, this project's
1 000+1 000 bound at export time) -- plus retries, backgrounded/timed-out/interrupted runs.
"""

from __future__ import annotations

import json
import os
import sys

import pytest
from evidence_helpers import ROOT, real_defaults

from proof_of_done import engine, evidence
from proof_of_done import turns as turns_mod
from proof_of_done.engine import ClaimResult
from proof_of_done.probe import OsProbe
from proof_of_done.transcript import agent_trace, claude_code

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from fixtures import dsl  # noqa: E402
from fixtures import render as render_mod  # noqa: E402
from proof_of_done import traces  # noqa: E402

SCENARIOS_DIR = os.path.join(_REPO_ROOT, "fixtures", "scenarios")
CFG = real_defaults(ROOT)


def _verdict_key(
    cr: ClaimResult,
) -> tuple[str, tuple[int, int], bool, str, bool, str | None]:
    return (
        cr.claim_type,
        cr.span,
        cr.verdict.supported,
        cr.verdict.reason,
        cr.verdict.partial,
        cr.command,
    )


def _evaluate_every_attempt(session: object, config: object) -> dict[int, list[ClaimResult]]:
    project_root = session.cwd or ROOT  # type: ignore[attr-defined]
    probe = OsProbe(project_root)
    events = evidence.build_events(session, config, project_root)  # type: ignore[arg-type]
    attempts = turns_mod.stop_attempts(session.steps)  # type: ignore[attr-defined]
    out: dict[int, list[ClaimResult]] = {}
    for attempt in attempts:
        skipped = engine.skip_requested(session, attempt.stop_index, config.skip_token)  # type: ignore[arg-type]
        req = engine.StopRequest(
            session=session,  # type: ignore[arg-type]
            stop_index=attempt.stop_index,
            final_message=attempt.final_message,
            config=config,  # type: ignore[arg-type]
            project_root=project_root,
            probe=probe,
            skipped=skipped,
            events=events,
        )
        decision = engine.evaluate_stop(req)
        out[attempt.index] = decision.results
    return out


@pytest.mark.parametrize(
    "scenario_name",
    [
        "parallel-calls",
        "long-output-tail",
        "long-output-persisted",
        "timeout-and-moved",
        "interrupted-and-no-result",
        "attempts-retry",
        "background-notification",
    ],
)
def test_round_trip_preserves_verdicts(tmp_path: object, scenario_name: str) -> None:
    scenario = dsl.load_scenario(os.path.join(SCENARIOS_DIR, f"{scenario_name}.yaml"))
    out_dir = os.path.join(str(tmp_path), "render")
    rendered = render_mod.render(scenario, out_dir)

    session = claude_code.parse(rendered.session_path)
    assert session.unrecognized is False
    original_verdicts = _evaluate_every_attempt(session, CFG)
    assert original_verdicts, f"{scenario_name}: expected at least one stop attempt"

    trace = traces.export_session(session, original_verdicts, CFG, redact=False, salt="")
    assert traces.validate_trace(trace) == []

    trace_path = os.path.join(str(tmp_path), "trace.jsonl")
    with open(trace_path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(trace) + "\n")

    reimported = agent_trace.parse_traces(trace_path)
    assert len(reimported) == 1
    session2 = reimported[0]
    replayed_verdicts = _evaluate_every_attempt(session2, CFG)

    assert set(original_verdicts) == set(replayed_verdicts), scenario_name
    for idx in original_verdicts:
        orig_keys = [_verdict_key(cr) for cr in original_verdicts[idx]]
        new_keys = [_verdict_key(cr) for cr in replayed_verdicts[idx]]
        assert orig_keys == new_keys, f"{scenario_name} attempt {idx}"
