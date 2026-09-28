# 8. How `schemas/agent-trace-v1.json` interprets the shared trace format

Status: accepted.

## Context

The shared `agent-trace/v1` format is defined as one example
JSON document plus prose rules, not a JSON Schema. Three sibling portfolio projects
(`booking-truth`, `agent-claimcheck`, `proof-of-done`) all read and write it, so every field's
type, nullability and required/optional status has to be pinned exactly once, in one schema file
(`schemas/agent-trace-v1.json`, JSON Schema draft 2020-12), not re-interpreted per project.

## Decision

Every interpretation choice below is encoded directly in the schema and exercised by
`tests/test_schema.py`:

- **Every field the example document lists is `required`**, at every level (`trace` itself:
  `schema`, `trace_id`, `source`, `task`, `steps`, `final_claim`, `ground_truth`, `meta`; `task`:
  `id`, `domain`, `instruction`; each `step`: `i`, `ts`, `kind`, `role`, `name`, `content`,
  `args`, `ok`, `output`, `error`; `final_claim`: `text`, `claims`; each `claim`: `type`,
  `subject`) — **except `ground_truth.details`**, per the format's own explicit carve-out ("`
  ground_truth` may be `{"outcome": "unknown", "checked_by": "none"}` for unlabelled traces").
- **Nullable-but-present, not optional**: a message step has no tool name or tool arguments, but
  the schema still requires the keys `name`/`args`/`ok`/`output`/`error` to be present, typed
  `["string", "null"]` / `["object", "null"]` / `["boolean", "null"]` — so a consumer can always
  do `step["args"]`, never `step.get("args")`, regardless of `kind`. This is the schema's own
  reading of "unknown extra fields go in `meta`," applied in the other direction: a field the
  schema *does* define is never simply absent.
- **`additionalProperties: false` everywhere except `meta` and the four explicitly free-form
  objects** (`args`, `output`, `subject`, `details`) — a producer cannot smuggle a new top-level
  or step-level field past the schema; it has to go in `meta.<tool-name>` instead, which is
  exactly the format's own escape hatch ("unknown extra fields go in `meta`").
- **`schema` is a `const`** (`"agent-trace/v1"`), not a free string with an enum of one, so a
  `v2` trace is rejected outright by this schema rather than silently accepted with an
  unrecognized version string.
- **`i` is `minimum: 0`** (0-based, matching the format's own step example starting at `i: 0`);
  contiguity within a trace is a prose rule the schema does not itself enforce (JSON Schema has
  no natural way to express "this array's `i` values are exactly `0..n-1`" without a much more
  invasive check), left to `traces.py`'s exporter and to tests instead.
- **`ts` is pattern-matched as RFC 3339**, requiring an explicit offset or `Z` — accepting the
  common but non-compliant "space instead of `T`" or offset-less forms was considered and
  rejected, since every producer in this ecosystem controls its own timestamp formatting.

## Consequences

- A trace that validates against this schema is guaranteed shape-complete: every consumer can
  destructure every field without a `KeyError`, at the cost of every producer having to emit
  `null` placeholders it might otherwise have omitted.
- Extending the format for a new tool-specific need never requires touching this schema — it
  goes in that tool's own key under `meta`, by construction.
- A genuinely breaking change (a new required top-level field, a changed type, a tightened
  enum) is `agent-trace/v2`, a new file, never a change to this one in place.
