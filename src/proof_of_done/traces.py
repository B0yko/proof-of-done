"""agent-trace/v1 export and validation.

``export_session`` maps one parsed :class:`~proof_of_done.transcript.model.Session` plus the
verdicts already computed for it (by ``audit.py``, one :class:`~proof_of_done.engine.ClaimResult`
list per stop attempt) into one trace object, validated against ``schemas/agent-trace-v1.json``
before it is returned. ``validate_file`` backs the ``trace validate FILE`` CLI command.

Interpretation choices (see the schema's own docstring for the schema-shape ones):

- ``export_session`` takes ``(session, verdicts_by_turn, config, *, redact, salt, label=None)``;
  `config` is needed for ``meta.proof_of_done.config`` and to look up each step's tool_use_id
  independent of the session-derived `EventList`.
- ``verdicts_by_turn`` is keyed by **stop-attempt index** (`StopAttempt.index`, 0-based across
  the whole session), not by turn index: a turn can have several stop attempts (a block,
  feedback, then a retry), and the round-trip test needs every attempt's verdicts recoverable
  independently, not just the turn's last one. `meta.proof_of_done.turns` mirrors this keying
  (stringified, since JSON object keys are strings).
- `final_claim.claims` reflects only the **last** stop attempt in the session (the one whose
  agent-message run is the session's actual final message), matching `final_claim.text` =
  "the session's last assistant message".
- One agent-trace per :class:`Session` (ADR-7): a parent session with subagents produces one
  trace line per subagent too (the caller exports each `Session` — main and every subagent —
  separately); the parent's own trace only carries lightweight references to those subagents
  under `meta.proof_of_done.subagents` (`agent_id`, `agent_type`, `trace_id`), passed in via
  `subagent_refs`, so nothing is duplicated across lines.
- Redaction (`--redact`) covers step `content`, every `args` value, `output.text`, `error`,
  `task.instruction` and `final_claim.text` plus, so that no quote, command, path or session id
  survives in an exported trace, `trace_id`, `task.id`, every claim's `subject.quote`, and the
  free-text fields inside each verdict recorded under `meta.proof_of_done.turns` (`command`
  and the evidence-detail display strings, which can contain a real shell command or a real
  file path).
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from typing import Any

from proof_of_done import __version__
from proof_of_done import redact as redact_mod
from proof_of_done import turns as turns_mod
from proof_of_done.config import Config
from proof_of_done.engine import ClaimResult
from proof_of_done.transcript.model import (
    KIND_MESSAGE,
    KIND_TOOL_CALL,
    KIND_TOOL_RESULT,
    ROLE_AGENT,
    ROLE_ENVIRONMENT,
    ROLE_TOOL,
    ROLE_USER,
    Session,
    Step,
)

SCHEMA_NAME = "agent-trace/v1"

_TRUNC_HEAD = 1000
_TRUNC_TAIL = 1000
_TRUNC_THRESHOLD = 2000
_FALLBACK_TS = "1970-01-01T00:00:00Z"

_KIND_MAP = {KIND_MESSAGE: "message", KIND_TOOL_CALL: "tool_call", KIND_TOOL_RESULT: "tool_result"}
_ROLE_MAP = {
    ROLE_USER: "user",
    ROLE_AGENT: "agent",
    ROLE_TOOL: "tool",
    ROLE_ENVIRONMENT: "environment",
}


class TraceValidationError(ValueError):
    """A trace `export_session` built does not validate against the schema (a bug in this
    module, not a usage error -- callers should treat this as an internal error)."""


# ------------------------------------------------------------------------------------------
# schema loading + validation
# ------------------------------------------------------------------------------------------


def _repo_schema_path() -> str:
    # src/proof_of_done/traces.py -> src/proof_of_done -> src -> <repo root>/schemas/...
    src_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(os.path.dirname(src_dir))
    return os.path.join(repo_root, "schemas", "agent-trace-v1.json")


def load_schema() -> dict[str, Any]:
    """The agent-trace/v1 JSON Schema: the packaged copy
    (``proof_of_done/agent-trace-v1.json``, force-included in the wheel) when it is present,
    else the repository's own ``schemas/agent-trace-v1.json`` (source checkouts, editable
    installs)."""
    try:
        import importlib.resources as resources

        ref = resources.files("proof_of_done").joinpath("agent-trace-v1.json")
        if ref.is_file():
            loaded: Any = json.loads(ref.read_text(encoding="utf-8"))
            return loaded  # type: ignore[no-any-return]
    except (ImportError, OSError, ValueError):
        pass
    with open(_repo_schema_path(), encoding="utf-8") as fh:
        loaded = json.load(fh)
        return loaded  # type: ignore[no-any-return]


_validator_cache: Any = None


def get_validator() -> Any:
    """A cached ``jsonschema.Draft202012Validator`` for the agent-trace/v1 schema. Imports
    ``jsonschema`` lazily, only when trace code actually needs it."""
    global _validator_cache
    if _validator_cache is None:
        import jsonschema

        _validator_cache = jsonschema.Draft202012Validator(load_schema())
    return _validator_cache


def validate_trace(obj: Any) -> list[str]:
    """Every schema violation in `obj`, as human-readable ``<json-pointer>: <message>``
    strings (JSON Pointer using ``/``-joined path segments), sorted by path. Empty when
    valid."""
    validator = get_validator()
    out = []
    for err in sorted(validator.iter_errors(obj), key=lambda e: [str(p) for p in e.path]):
        pointer = "/" + "/".join(str(p) for p in err.path)
        out.append(f"{pointer}: {err.message}")
    return out


def validate_file(path: str) -> list[str]:
    """Validate every line of an agent-trace/v1 JSONL file. Returns one
    ``<path>:<line>: <json-pointer>: <message>`` string per violation (invalid JSON counts as
    one violation for that line); empty when the whole file is valid. Raises ``OSError`` if
    `path` cannot be read (the caller maps that to the usage-error exit code)."""
    errors: list[str] = []
    with open(path, encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, start=1):
            if not raw.strip():
                continue
            try:
                obj = json.loads(raw)
            except ValueError as exc:
                errors.append(f"{path}:{lineno}: /: invalid JSON: {exc}")
                continue
            for violation in validate_trace(obj):
                errors.append(f"{path}:{lineno}: {violation}")
    return errors


# ------------------------------------------------------------------------------------------
# export
# ------------------------------------------------------------------------------------------


def _truncate_output_text(text: str) -> str:
    if len(text) <= _TRUNC_THRESHOLD:
        return text
    omitted = len(text) - _TRUNC_HEAD - _TRUNC_TAIL
    return text[:_TRUNC_HEAD] + f"\n…[truncated {omitted} chars]…\n" + text[-_TRUNC_TAIL:]


def _step_flags(step: Step) -> dict[str, Any]:
    flags: dict[str, Any] = {}
    if step.background:
        flags["background"] = True
    if step.interrupted:
        flags["interrupted"] = True
    if step.timed_out:
        flags["timed_out"] = True
    if step.entry:
        flags["entry"] = step.entry
    if step.cwd is not None:
        flags["cwd"] = step.cwd
    return flags


def _r(text: str | None, redact: bool, salt: str) -> str | None:
    if text is None or not redact:
        return text
    return redact_mod.redact_text(text, salt)


def _trace_step(step: Step, redact: bool, salt: str) -> dict[str, Any]:
    output: dict[str, Any] | None = None
    if step.kind == KIND_TOOL_RESULT:
        text = step.output_text
        output = {
            "text": _r(_truncate_output_text(text), redact, salt) if text is not None else None,
            "exit_code": step.exit_code,
        }
    args = step.args
    if redact and isinstance(args, dict):
        args = {k: redact_mod.redact_value(v, salt) for k, v in args.items()}
    return {
        "i": step.i,
        "ts": step.ts or _FALLBACK_TS,
        "kind": _KIND_MAP.get(step.kind, step.kind),
        "role": _ROLE_MAP.get(step.role, step.role),
        "name": step.name,
        "content": _r(step.content, redact, salt),
        "args": args,
        "ok": step.ok,
        "output": output,
        "error": _r(step.error, redact, salt),
    }


def _serialize_verdict(cr: ClaimResult, redact: bool, salt: str) -> dict[str, Any]:
    d = cr.verdict.details
    return {
        "claim_type": cr.claim_type,
        "quote": _r(cr.quote, redact, salt),
        "span": list(cr.span),
        "rule_ids": list(cr.rule_ids),
        "supported": cr.verdict.supported,
        "reason": cr.verdict.reason,
        "partial": cr.verdict.partial,
        "exempt": cr.verdict.exempt,
        "action": cr.action,
        "command": _r(cr.command, redact, salt),
        "details": {
            "candidate_display": _r(d.candidate_display, redact, salt),
            "candidate_step": d.candidate_step,
            "exit_code": d.exit_code,
            "interrupted": d.interrupted,
            "timed_out": d.timed_out,
            "matched_text": _r(d.matched_text, redact, salt),
            "anchor_path": _r(d.anchor_path, redact, salt),
            "anchor_step": d.anchor_step,
            "background_running": d.background_running,
            "full_run_display": _r(d.full_run_display, redact, salt),
            "full_run_step": d.full_run_step,
        },
    }


def _base_session_id(session: Session) -> str:
    if session.session_id:
        return session.session_id
    if session.path:
        return os.path.basename(session.path)
    return "unknown-session"


def trace_id_for(session: Session, *, redact: bool, salt: str) -> str:
    """The `trace_id` `export_session` will give `session`, computable without exporting it
    first: a parent session's `meta.proof_of_done.subagents` references need each subagent's
    `trace_id` regardless of export order."""
    base_id = _base_session_id(session)
    raw = f"{base_id}:{session.agent_id}" if session.agent_id else base_id
    return _r(raw, redact, salt) or raw


def export_session(
    session: Session,
    verdicts_by_turn: Mapping[int, Sequence[ClaimResult]],
    config: Config,
    *,
    redact: bool,
    salt: str,
    label: Mapping[str, Any] | None = None,
    subagent_refs: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """One agent-trace/v1 trace for `session`. `verdicts_by_turn` is every
    stop attempt's `ClaimResult` list, keyed by `StopAttempt.index` (see the module
    docstring). `label` overrides `ground_truth` (default: unknown/none, for a real
    transcript); `subagent_refs` are `{"agent_id", "agent_type", "trace_id"}` mappings for
    this session's already- (or about to be) exported subagent traces. Raises
    :class:`TraceValidationError` if the built trace does not validate -- this module's own
    bug, never a caller error.
    """
    base_id = _base_session_id(session)
    trace_id_raw = f"{base_id}:{session.agent_id}" if session.agent_id else base_id

    first_user = next(
        (s for s in session.steps if s.kind == KIND_MESSAGE and s.role == ROLE_USER), None
    )
    instruction = first_user.content or "" if first_user is not None else ""

    attempts = turns_mod.stop_attempts(session.steps)
    if attempts:
        final_text = attempts[-1].final_message
        last_results = verdicts_by_turn.get(attempts[-1].index, ())
    else:
        last_agent = next(
            (s for s in reversed(session.steps) if s.kind == KIND_MESSAGE and s.role == ROLE_AGENT),
            None,
        )
        final_text = last_agent.content or "" if last_agent is not None else ""
        last_results = ()

    claims = [
        {
            "type": cr.claim_type,
            "subject": {
                "rule": ",".join(cr.rule_ids) if cr.rule_ids else cr.claim_type,
                "quote": _r(cr.quote, redact, salt),
            },
        }
        for cr in last_results
    ]

    ground_truth: dict[str, Any] = (
        dict(label)
        if label is not None
        else {
            "outcome": "unknown",
            "checked_by": "none",
        }
    )

    tool_use_ids = {str(s.i): s.tool_use_id for s in session.steps if s.tool_use_id}
    step_flags = {str(s.i): _step_flags(s) for s in session.steps if _step_flags(s)}
    turns_out = {
        str(idx): [_serialize_verdict(cr, redact, salt) for cr in results]
        for idx, results in verdicts_by_turn.items()
    }

    meta_pod: dict[str, Any] = {
        "version": __version__,
        "config": config.to_json(),
        "tool_use_ids": tool_use_ids,
        "step_flags": step_flags,
        "turns": turns_out,
    }
    if subagent_refs:
        meta_pod["subagents"] = list(subagent_refs)

    trace: dict[str, Any] = {
        "schema": SCHEMA_NAME,
        "trace_id": _r(trace_id_raw, redact, salt),
        "source": f"proof-of-done/{__version__}",
        "task": {
            "id": _r(base_id, redact, salt),
            "domain": "coding",
            "instruction": _r(instruction, redact, salt) or "",
        },
        "steps": [_trace_step(s, redact, salt) for s in session.steps],
        "final_claim": {"text": _r(final_text, redact, salt) or "", "claims": claims},
        "ground_truth": ground_truth,
        "meta": {"proof_of_done": meta_pod},
    }

    violations = validate_trace(trace)
    if violations:
        raise TraceValidationError(
            f"export_session produced an invalid agent-trace/v1 trace for session "
            f"{base_id!r}: {violations[0]}"
        )
    return trace


__all__ = [
    "SCHEMA_NAME",
    "TraceValidationError",
    "export_session",
    "get_validator",
    "load_schema",
    "trace_id_for",
    "validate_file",
    "validate_trace",
]
