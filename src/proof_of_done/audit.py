"""``audit`` CLI logic: replay every stop attempt of every historic
session through the same `engine.evaluate_stop` the live hook uses, and aggregate the results
into one :class:`AuditReport`. `report.py` renders the result; `cli.py` wires this module to the
`audit` subcommand.

Interpretation choices:

- **Source registry.** `--source auto|claude-code|agent-trace|codex` dispatches through
  `ADAPTERS`, a name -> `(path) -> list[Session]` mapping, so a transcript format plugs in by
  adding one entry. `auto` sniffs a file's first
  non-blank line (`agent_trace.looks_like_agent_trace`, then `codex.looks_like_codex`) and
  falls back to `claude-code`. `codex` is experimental (see `transcript/codex.py`'s own
  docstring): `--help` says so, and so does this module's docstring.
- **File discovery.** A `PATH` argument is a glob (containing `* ? [`), a directory (walked
  recursively), or a file, used as-is. Every mode excludes `*.stop-*.jsonl` (the fixture
  renderer's per-attempt snapshot of a session, never a real transcript on its own -- the full
  session file already replays every stop attempt) and anything under a `subagents/` or
  `tool-results/` directory component (subagent transcripts are discovered relative to their
  parent instead, matching "grouped under the parent session").
- **Subagent skip-token check.** The live hook reads the skip token from the *main* session's
  latest real user prompt, at the point the SubagentStop actually fired (see
  `engine.skip_requested`'s docstring). Audit has no reliable way to align "the point a given
  historic subagent transcript ran" back to a specific step of its parent's already-replayed
  history without also parsing the subagent's own `.meta.json` linkage in every adapter, so it
  conservatively checks the *whole* main session's prompt history instead: a skip token
  anywhere in the main session's real user prompts skips every one of its subagents' stop
  attempts. This can only ever be *more* permissive than the live hook, never less.
- **Session count vs. detail counts.** "Subagent transcripts grouped under their parent
  session" is read as: a subagent does not add to the `sessions` count (it is part of its
  parent's unit, not an independent top-level item), but its turns, stop attempts and claims
  still roll into every other count -- a subagent's own claims are real evidence activity worth
  reporting.
- **Project root / suggestion probe.** A historic session's `project_root` is its own recorded
  `cwd` (never re-derived by walking this machine's filesystem for a `.git` marker, since the
  transcript may be from a different machine or a different point in time); the suggestion
  probe (`suggest.py`'s project-ecosystem detection) is an `OsProbe` rooted there, matching
  what the live hook does, on the (best-effort) chance the project still exists locally.
"""

from __future__ import annotations

import contextlib
import glob as glob_mod
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Callable

from proof_of_done import engine, evidence
from proof_of_done import redact as redact_mod
from proof_of_done import traces as traces_mod
from proof_of_done import turns as turns_mod
from proof_of_done.config import Config, Layer, load_for_audit
from proof_of_done.engine import ClaimResult
from proof_of_done.probe import OsProbe
from proof_of_done.transcript.model import Session

SOURCES = ("auto", "claude-code", "agent-trace", "codex")
EXPERIMENTAL_SOURCES = frozenset({"codex"})

_STOP_SNAPSHOT_MARKER = ".stop-"
_EXCLUDED_DIR_COMPONENTS = frozenset({"subagents", "tool-results"})


class AuditUsageError(Exception):
    """A usage error (maps to CLI exit code 2): not exactly one input mode, or an unknown
    `--source`."""


class AuditInputError(Exception):
    """No readable transcript was found for the given input (maps to CLI exit code 3)."""


# ------------------------------------------------------------------------------------------
# adapter registry
# ------------------------------------------------------------------------------------------


def _claude_code_sessions(path: str) -> list[Session]:
    from proof_of_done.transcript import claude_code

    session = claude_code.parse(path)
    subagent_paths = claude_code.find_subagent_transcripts(path)
    session.subagents = [claude_code.parse_subagent(p) for p in subagent_paths]
    return [session]


def _codex_sessions(path: str) -> list[Session]:
    from proof_of_done.transcript import codex

    return [codex.parse(path)]


def _agent_trace_sessions(path: str) -> list[Session]:
    from proof_of_done.transcript import agent_trace

    return agent_trace.parse_traces(path)


ADAPTERS: dict[str, Callable[[str], list[Session]]] = {
    "claude-code": _claude_code_sessions,
    "agent-trace": _agent_trace_sessions,
    "codex": _codex_sessions,
}


def _first_nonblank_line(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.strip():
                    return line
    except OSError:
        return ""
    return ""


def detect_source(path: str) -> str:
    """Best-effort `--source auto` sniff: agent-trace/v1, then the experimental Codex rollout
    format, else Claude Code (the required, most common format)."""
    from proof_of_done.transcript import agent_trace, codex

    line = _first_nonblank_line(path)
    if agent_trace.looks_like_agent_trace(line):
        return "agent-trace"
    if codex.looks_like_codex(line):
        return "codex"
    return "claude-code"


# ------------------------------------------------------------------------------------------
# file discovery
# ------------------------------------------------------------------------------------------


def _is_candidate_file(path: str) -> bool:
    name = os.path.basename(path)
    if not name.endswith(".jsonl") or _STOP_SNAPSHOT_MARKER in name:
        return False
    parts = path.replace(os.sep, "/").split("/")
    return not (_EXCLUDED_DIR_COMPONENTS & set(parts))


def _walk_dir(root: str) -> list[str]:
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDED_DIR_COMPONENTS]
        for name in filenames:
            full = os.path.join(dirpath, name)
            if _is_candidate_file(full):
                out.append(full)
    return sorted(out)


def _expand_path_arg(arg: str) -> list[str]:
    if any(ch in arg for ch in "*?["):
        return sorted(p for p in glob_mod.glob(arg, recursive=True) if _is_candidate_file(p))
    if os.path.isdir(arg):
        return _walk_dir(arg)
    if os.path.isfile(arg):
        return [arg]
    return []


def _claude_projects_dir(env: Mapping[str, str]) -> str:
    config_dir = env.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        return os.path.join(config_dir, "projects")
    home = env.get("HOME") or os.path.expanduser("~")
    return os.path.join(home, ".claude", "projects")


_demo_stack = contextlib.ExitStack()


def demo_files() -> list[str]:
    """Every packaged demo session file (`src/proof_of_done/demo/*.jsonl`), sorted, as real
    filesystem paths (extracted from a zipped install if necessary via
    `importlib.resources.as_file`)."""
    import importlib.resources as resources

    root = resources.files("proof_of_done").joinpath("demo")
    demo_dir = _demo_stack.enter_context(resources.as_file(root))
    return sorted(
        os.path.join(str(demo_dir), name)
        for name in os.listdir(demo_dir)
        if _is_candidate_file(name)
    )


def resolve_input_files(
    paths: Sequence[str], *, claude_projects: bool, demo: bool, env: Mapping[str, str]
) -> list[str]:
    """The list of transcript files for the given input mode. Raises :class:`AuditUsageError`
    unless exactly one of `paths`/`claude_projects`/`demo` is given, and
    :class:`AuditInputError` when that mode resolves to zero readable files."""
    modes = [bool(paths), claude_projects, demo]
    if sum(modes) != 1:
        raise AuditUsageError("exactly one of PATH..., --claude-projects or --demo is required")
    if demo:
        files = demo_files()
    elif claude_projects:
        base = _claude_projects_dir(env)
        files = _walk_dir(base) if os.path.isdir(base) else []
    else:
        collected: list[str] = []
        for p in paths:
            collected.extend(_expand_path_arg(p))
        seen: set[str] = set()
        files = []
        for f in collected:
            key = os.path.abspath(f)
            if key not in seen:
                seen.add(key)
                files.append(f)
    if not files:
        raise AuditInputError("no readable transcripts found for the given input")
    return files


# ------------------------------------------------------------------------------------------
# report accumulator
# ------------------------------------------------------------------------------------------


@dataclass
class TypeTally:
    claims: int = 0
    unsupported: int = 0


@dataclass
class Example:
    quote: str
    claim_type: str
    reason: str
    command: str | None
    session_id: str
    ts: str


@dataclass
class AuditReport:
    sessions: int = 0
    turns: int = 0
    stop_attempts: int = 0
    turns_with_claims: int = 0
    claims_total: int = 0
    supported_total: int = 0
    unsupported_total: int = 0
    partial_of_supported: int = 0
    by_type: dict[str, TypeTally] = field(default_factory=dict)
    by_reason: dict[str, int] = field(default_factory=dict)
    recent_unsupported: list[Example] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    files_processed: int = 0
    config_description: str = ""
    source_mode: str = "auto"
    redacted: bool = False
    salt_is_random: bool = False
    demo: bool = False

    def unsupported_rate(self) -> float:
        return self.unsupported_total / self.claims_total if self.claims_total else 0.0

    def type_rate(self, claim_type: str) -> float:
        t = self.by_type.get(claim_type)
        return (t.unsupported / t.claims) if t and t.claims else 0.0

    def partial_share(self) -> float:
        return self.partial_of_supported / self.supported_total if self.supported_total else 0.0


_MAX_EXAMPLES = 10


class _Accumulator:
    """Mutable running tallies -- kept separate from :class:`AuditReport` (its frozen-ish
    public shape) only to hold the unbounded example list until the final top-10-by-recency
    trim."""

    def __init__(self) -> None:
        self.report = AuditReport()
        self._all_examples: list[Example] = []

    def record_session(self) -> None:
        self.report.sessions += 1

    def record_turns(self, n: int) -> None:
        self.report.turns += n

    def record_attempt(self, has_claims: bool) -> None:
        self.report.stop_attempts += 1
        if has_claims:
            self.report.turns_with_claims += 1

    def record_claim(
        self, cr: ClaimResult, *, session_id: str, ts: str, redact: bool, salt: str
    ) -> None:
        r = self.report
        r.claims_total += 1
        tally = r.by_type.setdefault(cr.claim_type, TypeTally())
        tally.claims += 1
        if cr.verdict.supported:
            r.supported_total += 1
            if cr.verdict.partial:
                r.partial_of_supported += 1
        else:
            r.unsupported_total += 1
            tally.unsupported += 1
            r.by_reason[cr.verdict.reason] = r.by_reason.get(cr.verdict.reason, 0) + 1
            quote = redact_mod.redact_text(cr.quote, salt) if redact else cr.quote
            command = cr.command
            if redact and command:
                command = redact_mod.redact_text(command, salt)
            shown_session_id = redact_mod.redact_text(session_id, salt) if redact else session_id
            self._all_examples.append(
                Example(
                    quote=quote,
                    claim_type=cr.claim_type,
                    reason=cr.verdict.reason,
                    command=command,
                    session_id=shown_session_id,
                    ts=ts,
                )
            )

    def finalize(self) -> AuditReport:
        self._all_examples.sort(key=lambda e: e.ts, reverse=True)
        self.report.recent_unsupported = self._all_examples[:_MAX_EXAMPLES]
        return self.report


# ------------------------------------------------------------------------------------------
# trace export
# ------------------------------------------------------------------------------------------


class TraceWriter:
    def __init__(self, path: str) -> None:
        self._fh = open(path, "w", encoding="utf-8")  # noqa: SIM115 (closed explicitly)

    def write(self, trace: dict[str, Any]) -> None:
        self._fh.write(json.dumps(trace, ensure_ascii=False))
        self._fh.write("\n")

    def close(self) -> None:
        self._fh.close()


# ------------------------------------------------------------------------------------------
# per-session replay
# ------------------------------------------------------------------------------------------


def _attempt_ts(session: Session, attempt: turns_mod.StopAttempt) -> str:
    for i in range(attempt.stop_index - 1, attempt.start - 1, -1):
        if session.steps[i].ts:
            return session.steps[i].ts
    return ""


def session_verdicts(
    session: Session,
    *,
    main_session: Session,
    config: Config,
    on_attempt: Callable[[turns_mod.StopAttempt, engine.Decision], None] | None = None,
) -> dict[int, list[ClaimResult]]:
    """Simulate the Stop (or SubagentStop) hook at every stop attempt of `session` and return
    each attempt's claim results keyed by `StopAttempt.index`. `main_session` supplies the
    user prompts for the skip-token check when `session` is a subagent transcript."""
    project_root = session.cwd or "/"
    probe = OsProbe(project_root)
    events = evidence.build_events(session, config, project_root)
    skip_token = config.skip_token
    verdicts_by_attempt: dict[int, list[ClaimResult]] = {}
    for attempt in turns_mod.stop_attempts(session.steps):
        if session is main_session:
            skipped = engine.skip_requested(session, attempt.stop_index, skip_token)
        else:
            skipped = engine.skip_requested(main_session, len(main_session.steps), skip_token)
        req = engine.StopRequest(
            session=session,
            stop_index=attempt.stop_index,
            final_message=attempt.final_message,
            config=config,
            project_root=project_root,
            probe=probe,
            skipped=skipped,
            events=events,
        )
        decision = engine.evaluate_stop(req)
        verdicts_by_attempt[attempt.index] = decision.results
        if on_attempt is not None:
            on_attempt(attempt, decision)
    return verdicts_by_attempt


def _walk_session(
    session: Session,
    *,
    main_session: Session,
    config: Config,
    acc: _Accumulator,
    trace_writer: TraceWriter | None,
    redact: bool,
    salt: str,
    is_main: bool,
) -> None:
    def _record(attempt: turns_mod.StopAttempt, decision: engine.Decision) -> None:
        acc.record_attempt(bool(decision.results))
        ts = _attempt_ts(session, attempt)
        for cr in decision.results:
            acc.record_claim(cr, session_id=session.session_id, ts=ts, redact=redact, salt=salt)

    verdicts_by_attempt = session_verdicts(
        session, main_session=main_session, config=config, on_attempt=_record
    )

    if is_main:
        acc.record_session()
    acc.record_turns(len(turns_mod.turns(session.steps)))

    if trace_writer is not None:
        subagent_refs = [
            {
                "agent_id": sub.agent_id,
                "agent_type": sub.agent_type,
                "trace_id": traces_mod.trace_id_for(sub, redact=redact, salt=salt),
            }
            for sub in session.subagents
        ]
        trace = traces_mod.export_session(
            session,
            verdicts_by_attempt,
            config,
            redact=redact,
            salt=salt,
            subagent_refs=subagent_refs,
        )
        trace_writer.write(trace)

    for sub in session.subagents:
        _walk_session(
            sub,
            main_session=main_session,
            config=config,
            acc=acc,
            trace_writer=trace_writer,
            redact=redact,
            salt=salt,
            is_main=False,
        )


# ------------------------------------------------------------------------------------------
# top-level orchestration
# ------------------------------------------------------------------------------------------


@dataclass
class AuditResult:
    report: AuditReport
    config: Config
    layers: list[Layer]


def run_audit(
    *,
    paths: Sequence[str] = (),
    claude_projects: bool = False,
    demo: bool = False,
    source: str = "auto",
    config_path: str | None = None,
    export_traces_path: str | None = None,
    redact: bool = False,
    salt: str | None = None,
    env: Mapping[str, str] | None = None,
) -> AuditResult:
    """Run `audit` end to end: resolve input files, replay every stop attempt of every
    session (and its subagents) through `engine.evaluate_stop`, and return the aggregated
    :class:`AuditReport`. Raises :class:`AuditUsageError` / :class:`AuditInputError` (mapped by
    the CLI to exit codes 2 / 3).
    """
    env = dict(env) if env is not None else {}
    if source not in SOURCES:
        raise AuditUsageError(f"unknown --source {source!r} (choose from {', '.join(SOURCES)})")

    files = resolve_input_files(paths, claude_projects=claude_projects, demo=demo, env=env)

    config, layers = load_for_audit(config_path)
    config_description = "built-in defaults"
    if config_path:
        config_description += f" + {config_path}"

    resolved_salt = redact_mod.resolve_salt(salt, env)
    salt_is_random = not salt and not env.get("PROOF_OF_DONE_SALT")

    trace_writer = TraceWriter(export_traces_path) if export_traces_path else None
    acc = _Accumulator()
    files_processed = 0

    def _shown_path(p: str) -> str:
        return redact_mod.redact_text(p, resolved_salt) if redact else p

    try:
        for path in files:
            file_source = source if source != "auto" else detect_source(path)
            adapter = ADAPTERS.get(file_source)
            if adapter is None:
                acc.report.warnings.append(
                    f"{_shown_path(path)}: unknown source {file_source!r}, skipped"
                )
                continue
            try:
                sessions = adapter(path)
            except OSError as exc:
                acc.report.warnings.append(f"{_shown_path(path)}: could not read ({exc}), skipped")
                continue
            for session in sessions:
                if session.unrecognized:
                    acc.report.warnings.append(
                        f"{_shown_path(path)}: unrecognized transcript format, skipped"
                    )
                    continue
                files_processed += 1
                _walk_session(
                    session,
                    main_session=session,
                    config=config,
                    acc=acc,
                    trace_writer=trace_writer,
                    redact=redact,
                    salt=resolved_salt,
                    is_main=True,
                )
    finally:
        if trace_writer is not None:
            trace_writer.close()

    report = acc.finalize()
    report.files_processed = files_processed
    report.config_description = config_description
    report.source_mode = source
    report.redacted = redact
    report.salt_is_random = redact and salt_is_random
    report.demo = demo
    if files_processed == 0:
        raise AuditInputError("every input file failed to parse; nothing was audited")
    return AuditResult(report=report, config=config, layers=layers)


__all__ = [
    "ADAPTERS",
    "EXPERIMENTAL_SOURCES",
    "SOURCES",
    "AuditInputError",
    "AuditReport",
    "AuditResult",
    "AuditUsageError",
    "Example",
    "TraceWriter",
    "TypeTally",
    "demo_files",
    "detect_source",
    "resolve_input_files",
    "run_audit",
]
