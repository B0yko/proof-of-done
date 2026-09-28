# 7. One agent-trace/v1 trace per session, including per subagent

Status: accepted.

## Context

A Claude Code session can contain subagent calls, each with its own transcript file
(`<session>/subagents/agent-<id>.jsonl`) and its own final message and claims, checked at its
own `SubagentStop`. `audit --export-traces` has to decide whether a subagent's activity is
folded into its parent's single trace, or exported as its own trace line.

## Decision

Every `Session` — the main session and each subagent, recursively — is exported as its own
`agent-trace/v1` line by `traces.export_session`. A parent trace never inlines a subagent's
steps; instead it carries lightweight references under
`meta.proof_of_done.subagents: [{agent_id, agent_type, trace_id}, ...]`, and the subagent's own
trace is written as a separate line with its own `trace_id` (computed independently via
`traces.trace_id_for` so the reference can be built before or after that line is actually
written).

## Consequences

- `final_claim` for a subagent's trace is *its own* last message and claims, not folded into the
  parent's — a claim-checking consumer of agent-trace/v1 sees the subagent's claim as a
  first-class thing to judge, matching how the SubagentStop hook itself checks it independently.
- The schema's own step model (`kind`/`role`/`tool_call` + `tool_result` pairing) stays one flat,
  ordered list per trace; nesting a subagent's steps inside a parent step would have needed a
  schema extension this project's rules forbid (`schemas/agent-trace-v1.json`'s own docstring:
  "Do not extend or redefine the format").
- A consumer that wants "everything this session did, subagents included" has to follow the
  `meta.proof_of_done.subagents` references and load each trace_id's own line — acceptable
  because `meta` is explicitly the place for tool-specific structure the schema itself does not
  standardize.
- Audit's own aggregate counts still roll a subagent's claims into the same report as its
  parent's (a subagent does not get counted as an extra *session*, since it is not an
  independent top-level unit of work, but its claims and stop attempts count) — the trace-export
  granularity and the audit-report granularity are deliberately different.
