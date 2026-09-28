"""Size-capped, JSON-lines diagnostic log for the hook (PLAN §9, spec item 9).

Every record is rule ids, reason codes, timings and error class names -- never message text,
shell commands or filesystem paths (other than the log file's own path). Rotates the file to
``.1`` once it reaches the size cap; writing never raises (a log failure fails open, same as
everything else on the hook path).
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from typing import Any

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


__all__ = ["log_path", "write"]
