"""Per-turn consecutive-block counters, keyed by
``sha256("<session_id>:<agent_id>")`` under ``<data_dir>/counters/``.

Writes are atomic (temp file + ``os.replace``); every operation fails open (never raises) so a
counter-directory problem never breaks the hook. Counter files older than 7 days are pruned.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
import time
from typing import Any

_PRUNE_AGE_SECONDS = 7 * 24 * 60 * 60
_DIRNAME = "counters"


def _key(session_id: str, agent_id: str | None) -> str:
    raw = f"{session_id}:{agent_id or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _counter_path(data_dir: str, session_id: str, agent_id: str | None) -> str:
    return os.path.join(data_dir, _DIRNAME, _key(session_id, agent_id) + ".json")


def read(data_dir: str, session_id: str, agent_id: str | None) -> int:
    """The current consecutive-block count, or 0 if there is none yet or it cannot be read."""
    path = _counter_path(data_dir, session_id, agent_id)
    try:
        with open(path, encoding="utf-8") as fh:
            data: Any = json.load(fh)
    except (OSError, ValueError):
        return 0
    if isinstance(data, dict) and isinstance(data.get("count"), int):
        count = data["count"]
        return count if count >= 0 else 0
    return 0


def write(data_dir: str, session_id: str, agent_id: str | None, count: int) -> None:
    """Atomically store `count`. Never raises: a write failure just loses this update."""
    directory = os.path.join(data_dir, _DIRNAME)
    with contextlib.suppress(OSError):
        os.makedirs(directory, mode=0o700, exist_ok=True)
        path = _counter_path(data_dir, session_id, agent_id)
        fd, tmp_path = tempfile.mkstemp(prefix=".counter-", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"count": count, "ts": time.time()}, fh)
            os.replace(tmp_path, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise


def prune(data_dir: str, *, now: float | None = None) -> None:
    """Delete counter files last modified more than 7 days before `now` (default: wall clock).
    Best-effort: any error for any single file is silently skipped."""
    directory = os.path.join(data_dir, _DIRNAME)
    cutoff = (now if now is not None else time.time()) - _PRUNE_AGE_SECONDS
    with contextlib.suppress(OSError):
        for name in os.listdir(directory):
            path = os.path.join(directory, name)
            with contextlib.suppress(OSError):
                if os.path.getmtime(path) < cutoff:
                    os.unlink(path)


__all__ = ["prune", "read", "write"]
