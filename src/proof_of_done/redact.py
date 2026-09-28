"""Redaction (spec item 11, PLAN §11): replace a free-text field with a salted SHA-256
prefix plus the original length, e.g. ``h:3f9a1c2b7d0e:len=42``. Same input + same salt always
hashes to the same digest (so repeated quotes/commands/paths are still visible as repeats in a
redacted report or export), but the salt is random per run by default, so digests do not link
across separate ``audit`` invocations unless the same ``--salt``/``PROOF_OF_DONE_SALT`` is
reused deliberately.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from typing import Any

_DIGEST_LEN = 12


def make_salt() -> str:
    """A fresh random salt, hex-encoded."""
    return os.urandom(16).hex()


def resolve_salt(cli_salt: str | None, env: Mapping[str, str]) -> str:
    """``--salt`` > ``PROOF_OF_DONE_SALT`` > a random salt (the report must say when this
    branch was taken, since the run cannot be reproduced without recording it)."""
    if cli_salt:
        return cli_salt
    env_salt = env.get("PROOF_OF_DONE_SALT")
    if env_salt:
        return env_salt
    return make_salt()


def redact_text(text: str, salt: str) -> str:
    """Redact one string: ``h:<12 hex chars>:len=<original length>``."""
    digest = hashlib.sha256((salt + text).encode("utf-8", errors="replace")).hexdigest()
    return f"h:{digest[:_DIGEST_LEN]}:len={len(text)}"


def redact_optional(text: str | None, salt: str) -> str | None:
    return redact_text(text, salt) if text is not None else None


def redact_value(value: Any, salt: str) -> str:
    """Redact an arbitrary JSON value (used for a tool call's ``args`` values, which are not
    always strings): a string is hashed directly, anything else is hashed via its canonical
    JSON serialization so structure never leaks either."""
    if isinstance(value, str):
        return redact_text(value, salt)
    return redact_text(json.dumps(value, sort_keys=True, default=str), salt)


__all__ = ["make_salt", "redact_optional", "redact_text", "redact_value", "resolve_salt"]
