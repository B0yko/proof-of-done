"""Stop-hook decision engine (PLAN §9, spec item 2): glues `claims`, `evidence`, `suggest` and
`message` into one `evaluate_stop` call. Shared by `hook.py`, `cli.py check`, and (later)
`audit.py`/`eval`.

Pure function: `evaluate_stop` performs no I/O of its own. Everything it needs -- the parsed
session, which stop attempt to evaluate, the already tamper-adjusted config, an already
computed skip decision, and a `FileProbe` for project-ecosystem detection -- comes in through
`StopRequest`. Callers that already parsed the transcript and need the event list for their own
purposes (tamper scanning, the flush-race re-check) may build it once and pass it in via
`StopRequest.events`; otherwise `evaluate_stop` builds it itself.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from proof_of_done import claims as claims_mod
from proof_of_done import evidence, message, suggest
from proof_of_done.config import Config
from proof_of_done.probe import FileProbe
from proof_of_done.transcript.model import KIND_MESSAGE, ROLE_USER, Session

# ------------------------------------------------------------------------------------------
# public data model
# ------------------------------------------------------------------------------------------


@dataclass
class StopRequest:
    session: Session
    stop_index: int  # step index (exclusive) the stop attempt ends at
    final_message: str  # the message the claim detector runs on
    config: Config  # already tamper-adjusted (see `tamper.py`)
    project_root: str
    probe: FileProbe
    skipped: bool = False  # skip token seen in the *main* session's latest real user prompt
    tamper_notes: Sequence[str] = ()
    background_tasks: Sequence[Mapping[str, Any]] = ()
    events: evidence.EventList | None = None  # prebuilt, or None to build from `session`


@dataclass
class ClaimResult:
    """One detected claim instance (claim spans of the same type that overlap are grouped into
    a single instance) with its evidence verdict and, when unsupported, a suggested command."""

    claim_type: str
    quote: str
    span: tuple[int, int]
    rule_ids: tuple[str, ...]
    verdict: evidence.Verdict
    command: str | None
    action: str  # "block" | "warn" (meaningless when the claim is supported)


@dataclass
class Decision:
    action: str  # "allow" | "block"
    reason: str | None  # the Stop hook's `reason` field text, only set when action == "block"
    system_message: str | None  # `systemMessage` text (warn entries and/or tamper notes)
    results: list[ClaimResult] = field(default_factory=list)
    skipped: bool = False


# ------------------------------------------------------------------------------------------
# skip token
# ------------------------------------------------------------------------------------------


def skip_requested(session: Session, stop_index: int, skip_token: str) -> bool:
    """Whether `skip_token` appears in the latest *real* user prompt before `stop_index`.

    Only `message`/`user` steps count (tool results, meta entries, hook feedback -- including
    this hook's own past block reasons -- task notifications, local-command output, compaction
    summaries and assistant text never do; the Claude Code adapter already classifies all of
    those as `message`/`environment` instead). No later user prompt or assistant text can
    retroactively un-skip a turn the way it is checked here: only the *latest* one before the
    stop is consulted, per turn.
    """
    if not skip_token:
        return False
    for step in reversed(session.steps[:stop_index]):
        if step.kind == KIND_MESSAGE and step.role == ROLE_USER:
            return skip_token in (step.content or "")
    return False


# ------------------------------------------------------------------------------------------
# claim grouping
# ------------------------------------------------------------------------------------------


@dataclass
class _Group:
    claim_type: str
    start: int
    end: int
    rule_ids: list[str]


def _group_claims(found: Sequence[claims_mod.Claim]) -> list[_Group]:
    """Merge overlapping spans of the same claim type into one instance each. `found` is
    already sorted by span (see `claims.detect`)."""
    groups: list[_Group] = []
    for c in found:
        merged = False
        for g in groups:
            if g.claim_type == c.claim_type and c.span[0] < g.end and c.span[1] > g.start:
                g.start = min(g.start, c.span[0])
                g.end = max(g.end, c.span[1])
                if c.rule_id not in g.rule_ids:
                    g.rule_ids.append(c.rule_id)
                merged = True
                break
        if not merged:
            groups.append(
                _Group(
                    claim_type=c.claim_type, start=c.span[0], end=c.span[1], rule_ids=[c.rule_id]
                )
            )
    groups.sort(key=lambda g: (g.start, g.end))
    return groups


def _claim_rules(config: Config) -> list[claims_mod.ClaimRule]:
    return [
        claims_mod.ClaimRule(
            id=r.id, claim_type=r.claim_type, patterns=r.claims, keywords=r.keywords
        )
        for r in config.rules
        if r.action != "off"
    ]


def _trailing_unresolved_step(events: evidence.EventList, stop_index: int) -> int | None:
    candidates = [c.step for c in events.commands if c.step < stop_index and c.no_result]
    return max(candidates) if candidates else None


def _notes_message(notes: Sequence[str]) -> str:
    return "\n".join(f"proof-of-done: {n}" for n in notes)


# ------------------------------------------------------------------------------------------
# evaluate_stop
# ------------------------------------------------------------------------------------------


def evaluate_stop(req: StopRequest) -> Decision:
    """The Stop/SubagentStop decision for one stop attempt, per PLAN §9 point 7."""
    if req.skipped:
        return Decision(action="allow", reason=None, system_message=None, results=[], skipped=True)

    config = req.config
    skip_token = config.skip_token
    tamper_notes = list(req.tamper_notes)

    rules = _claim_rules(config)
    found = claims_mod.detect(req.final_message, rules) if rules else []
    groups = _group_claims(found)

    if not groups:
        system_message = _notes_message(tamper_notes) if tamper_notes else None
        return Decision(action="allow", reason=None, system_message=system_message, results=[])

    events = (
        req.events
        if req.events is not None
        else evidence.build_events(req.session, config, req.project_root)
    )
    trailing_no_result_step = _trailing_unresolved_step(events, req.stop_index)
    mid_session = req.session.starts_mid_session

    results: list[ClaimResult] = []
    for g in groups:
        verdict = evidence.judge(g.claim_type, events, req.stop_index, config)
        quote = req.final_message[g.start : g.end]
        command: str | None = None
        action = verdict.action
        if not verdict.supported:
            command = suggest.suggest(
                g.claim_type, verdict, events, config, req.probe, req.stop_index
            )
            if (
                verdict.reason == "no_result"
                and trailing_no_result_step is not None
                and verdict.details.candidate_step == trailing_no_result_step
            ):
                action = "warn"
            if mid_session:
                action = "warn"
            if config.mode == "warn":
                action = "warn"
        results.append(
            ClaimResult(
                claim_type=g.claim_type,
                quote=quote,
                span=(g.start, g.end),
                rule_ids=tuple(g.rule_ids),
                verdict=verdict,
                command=command,
                action=action,
            )
        )

    block_entries = [
        message.Entry(quote=r.quote, claim_type=r.claim_type, verdict=r.verdict, command=r.command)
        for r in results
        if not r.verdict.supported and r.action == "block"
    ]
    warn_entries = [
        message.Entry(quote=r.quote, claim_type=r.claim_type, verdict=r.verdict, command=r.command)
        for r in results
        if not r.verdict.supported and r.action == "warn"
    ]

    if block_entries:
        reason = message.block_reason(block_entries, skip_token, notes=tamper_notes)
        return Decision(action="block", reason=reason, system_message=None, results=results)

    if warn_entries:
        system_message = message.warn_message(warn_entries, skip_token, notes=tamper_notes)
        return Decision(action="allow", reason=None, system_message=system_message, results=results)

    if tamper_notes:
        return Decision(
            action="allow",
            reason=None,
            system_message=_notes_message(tamper_notes),
            results=results,
        )

    return Decision(action="allow", reason=None, system_message=None, results=results)


__all__ = ["ClaimResult", "Decision", "StopRequest", "evaluate_stop", "skip_requested"]
