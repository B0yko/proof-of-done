"""Size-capped, JSON-lines diagnostic log for the hook.

Every record is rule ids, claim types, reason codes, timings and error class names -- never
message text, quotes, shell commands or filesystem paths (other than the log file's own path).
Rotates the file to ``.1`` once it reaches the size cap; writing never raises (a log failure
fails open, same as everything else on the hook path).

A decision record (:func:`write_decision`) looks like::

    {"ts": ..., "event": "decision", "hook_event": "stop", "decision": "block",
     "claims": 1, "unsupported": 1, "skipped": false, "disabled": false, "tampered": false,
     "capped": false,
     "results": [{"claim_type": "tests_passed", "rule_ids": ["tests"], "supported": false,
                  "reason": "stale", "action": "block"}],
     "timings_ms": {"fast_path": 0.4, "parse": 12.1, "detect_judge": 3.2, "total": 18.0}}

``decision`` is the final one, written after the block-counter step: ``block``, or ``allow``
with ``"capped": true`` when the per-turn block cap turned a block into an allow.
``detect_judge`` covers the tamper scan and effective-config step as well as claim detection
and evidence judging.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from collections.abc import Mapping, Sequence
from typing import Any, NamedTuple

_MAX_BYTES = 256 * 1024
_FILENAME = "proof-of-done.log"


def log_path(data_dir: str) -> str:
    return os.path.join(data_dir, _FILENAME)


def _rotate_if_needed(path: str) -> None:
    try:
        if os.path.getsize(path) >= _MAX_BYTES:
            os.replace(path, path + ".1")
    except OSError:
        pass


def write(data_dir: str, event: str, **fields: Any) -> None:
    """Append one JSON-line record: `{"ts": ..., "event": event, **fields}`. Never raises."""
    with contextlib.suppress(OSError, TypeError, ValueError):
        os.makedirs(data_dir, exist_ok=True)
        path = log_path(data_dir)
        _rotate_if_needed(path)
        record: dict[str, Any] = {"ts": time.time(), "event": event}
        record.update(fields)
        line = json.dumps(record, sort_keys=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


class ClaimLog(NamedTuple):
    """The only per-claim facts a decision record keeps: no quote, no message text."""

    claim_type: str
    rule_ids: Sequence[str]
    supported: bool
    reason: str
    action: str


_MAX_LOGGED_CLAIMS = 20


def write_decision(
    data_dir: str,
    *,
    hook_event: str,
    decision: str,
    results: Sequence[ClaimLog],
    skipped: bool,
    disabled: bool,
    tampered: bool,
    capped: bool = False,
    timings_ms: Mapping[str, float],
) -> None:
    """Append one ``decision`` record (see the module docstring). Never raises."""
    write(
        data_dir,
        "decision",
        hook_event=hook_event,
        decision=decision,
        claims=len(results),
        unsupported=sum(1 for r in results if not r.supported),
        skipped=skipped,
        disabled=disabled,
        tampered=tampered,
        capped=capped,
        results=[
            {
                "claim_type": r.claim_type,
                "rule_ids": list(r.rule_ids),
                "supported": r.supported,
                "reason": r.reason,
                "action": r.action,
            }
            for r in results[:_MAX_LOGGED_CLAIMS]
        ],
        timings_ms=dict(timings_ms),
    )


__all__ = ["ClaimLog", "log_path", "write", "write_decision"]
