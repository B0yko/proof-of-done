# 4. Fail open, always

Status: accepted.

## Context

The hook runs on the critical path of every agent turn. If it crashes, hangs, or blocks
incorrectly because of a bug, a corrupt transcript, an oversized file, or a config error, the
user is stuck: the agent cannot end its turn and there is no interactive way to dismiss a Stop
hook's decision mid-session.

## Decision

Every failure mode exits 0 with, at most, a short `systemMessage`, never a `decision: block`:

- Any uncaught exception in `hook.py`'s `_run` (`main`'s `try/except Exception`) — logs the
  exception's class name only, never its message or a traceback, to the size-capped log.
- A transcript over `max_transcript_mb` (default 200 MB) — `"transcript too large, check
  skipped"`.
- A transcript with more than 50 lines and zero recognizable steps — `"unrecognized transcript
  format, check skipped"`. (A file that starts mid-history is not a fail-open: the engine
  downgrades every block to `warn`.)
- Missing or unreadable stdin JSON, a missing `transcript_path`, or a transcript file that
  cannot be stat'd — the hook returns no output at all (exit 0, silently).
- The launcher itself (`bin/proof-of-done-hook`, POSIX sh): no `python3` on `PATH` — prints a
  fail-open message. On macOS, the resolved interpreter is the system `/usr/bin/python3` and
  `xcode-select -p` fails (Command Line Tools not installed) — fails open instead of ever
  triggering the install dialog, caching the successful check as a marker file so this test
  only costs one `xcode-select` call per data directory.
- `hooks/entry.py`, execed directly by the launcher, is syntactically valid on Python 2.7 so an
  interpreter that old prints `"Python 3.9+ required, check skipped"` (via `sys.version_info`
  checked before anything else runs) instead of a `SyntaxError` with no visible message.
- A hook that hits its 10-second timeout is killed by Claude Code and its output discarded
  (confirmed against the Hooks reference and covered by a contract test): no decision reaches
  the model, so the stop proceeds exactly as if the hook were absent.

A tampered config is a related but distinct case: it never *blocks* a stop by
itself, it only stops a tampered config file's own downgrades from taking effect for the rest of
that session — see the tamper check in `docs/how-it-works.md`.

## Consequences

- The worst a bug in this product can do to a user's session is let a false claim through
  unchecked, or occasionally block a claim it shouldn't — never freeze the agent.
- Diagnosing a fail-open requires the log (`${CLAUDE_PLUGIN_DATA}/proof-of-done.log`), since
  nothing reaches the user by default beyond a one-line `systemMessage`.
- Fail-open error messages must stay generic (no message text, no stack trace) because the log
  itself is privacy-constrained the same way the block reason is.
