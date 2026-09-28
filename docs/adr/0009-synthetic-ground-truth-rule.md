# 9. Ground truth for synthetic fixtures

Status: accepted.

## Context

`agent-trace/v1`'s `ground_truth` field records whether a task actually succeeded, by whom, and
how. For a real transcript `proof-of-done audit` reads from disk, there is no way to know that —
the tool has no state probe into the user's actual project — so the spec fixes `ground_truth` at
`{"outcome": "unknown", "checked_by": "none"}` for every real, unlabelled trace. `traces.py`
implements exactly this as `export_session`'s default when its caller passes no `label` override.

Fixture-derived traces are different: every fixture scenario's expected outcome is authored by
hand as part of its label (`supported`/`unsupported` + reason), so a fixture-derived trace
*could* carry a real `ground_truth.outcome` instead of `unknown`.

## Decision

The rule the fixture DSL and its labels are written against, for a scenario turn: `outcome` is
`"failure"` if the last relevant command run after the last edit failed (any of the closed
reason codes that mean the command itself failed or ran nothing: `failed_exit`, `failed_output`,
`empty_run`, or `masked_inconclusive` with no success match), `"success"` if a full (non-partial)
run after the last edit succeeded, and `"unknown"` otherwise (`no_command`, `stale`,
`background_only`, `superseded_by_failure`, `no_result`, or no relevant command at all — none of
these tell you what a fresh run would actually do). A labelled fixture's `ground_truth` uses
`checked_by: "none"` (no state probe ran; the label is authored, not measured) with
`details.label_source: "synthetic-by-construction"` recorded, so a consumer can tell a
fixture-labelled trace apart from a genuinely unlabelled one at a glance.

`export_session`'s `label` parameter is where this plugs in. `audit --export-traces` never
passes one, so every trace `proof-of-done audit` produces carries the spec's default
`{"outcome": "unknown", "checked_by": "none"}`, for real transcripts and the demo corpus
alike. Labelled fixtures are exported by `python fixtures/render.py SCENARIO.yaml --out DIR
--agent-trace FILE`, which computes the outcome with `fixtures.render.ground_truth_for`: the
last foreground segment after the last relevant edit that qualifies as evidence for the tests,
build, lint, typecheck or deploy rule decides — `failure` when it fails for one of the reasons
above, `success` when it is accepted and not a partial run, `unknown` otherwise.
`tests/test_fixture_traces.py` covers each outcome and schema validity.

## Consequences

- `audit --export-traces`'s own output makes no ground-truth claim about any session, real or
  synthetic — consistent with the product's own honesty rule (never invent a measurement this
  tool did not make) and with the spec's instruction that the demo corpus be labelled as a demo,
  not evaluated as ground truth.
- A future addition (exporting the demo/fixture corpus *with* its authored labels attached, for
  someone else's evaluation harness to consume) is a small, additive change — call
  `export_session(..., label=<computed above>)` from a new script — not a schema or interpretation
  change, because the rule and the schema slot for it already exist and are tested.
- The rule itself only classifies what the fixture author already encoded as a label; it is not
  a new source of truth independent of the evidence engine it mirrors.
