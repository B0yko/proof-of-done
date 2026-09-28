"""Format-independent session model and per-format adapters.

Every adapter (Claude Code today, others later) parses its own transcript format into the
:mod:`proof_of_done.transcript.model` types, so everything downstream of parsing is
format-independent.
"""

from __future__ import annotations
