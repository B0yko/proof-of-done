# 1. Deterministic detection instead of an LLM judge

Status: accepted.

## Context

A Stop hook has to decide, on every turn, whether the agent's final message contains a
completion claim ("tests pass", "fixed", "deployed") and whether that claim is backed by the
session so far. One design sends the message (and maybe the transcript) to a second model and
asks it to judge. Claude Code's own hook-development documentation even ships this pattern as a
worked example for hook authors.

## Decision

`claims.py` and `evidence.py` use stdlib `re` and plain data-structure walks only. No network
call, no model call, anywhere in the product. The same input always produces the same output.

## Consequences

- Zero marginal cost per stop, and a latency budget dominated by interpreter startup and JSON
  parsing rather than a round trip to a model provider — see `docs/eval.md` for the measured
  numbers.
- No dependency on an API key or a specific model's availability; the hook still works offline.
- Precision and recall are bounded by what regex-based clause analysis can express. Claims
  outside the seven built-in types, non-English messages, and phrasing an author did not
  anticipate are out of scope (see the README's Limitations section) — the trade a judge model
  would (probabilistically) cover.
- Every claim type, negation and hedge rule is testable in isolation and its behaviour is
  auditable by reading `claims.py`, not by re-running a model against a fixed prompt.
