# Claude Code transcript format (as parsed by proof-of-done)

Claude Code writes each session as JSON Lines under
`~/.claude/projects/<project>/<session-id>.jsonl` (the official sessions docs state the location
and warn that the entry format is internal and changes between versions). proof-of-done pins the
shape below and ignores every line type and field it does not recognise.

**Verified against Claude Code 2.1.281** (macOS). "Observed" means the shape was seen in a real
transcript with `scripts/probe_transcript_shape.py`, which prints key paths, JSON types and
counts only. "Not observed" items are parsed defensively from the official docs; fixtures model
them as described here.

## Lines and common fields

Every line is one JSON object with a `type` discriminator. The types proof-of-done reads:

| `type` | Meaning |
|---|---|
| `user` | a user-role entry: typed prompt, tool results, meta entries, hook feedback |
| `assistant` | one content block of an assistant API message |
| `attachment` | harness attachments; `attachment.type = queued_command` carries queued prompts and task notifications |
| `system` | system events (for example `subtype = compact_boundary`) |

Other observed types (`file-history-snapshot`, `file-history-delta`, `queue-operation`,
`last-prompt`, `custom-title`, `agent-name`, …) are bookkeeping and are skipped. A substring
prefilter skips them before `json.loads`.

Fields common to `user`, `assistant` and `attachment` lines (observed): `uuid`, `parentUuid`
(`null` on the first entry of a file), `sessionId`, `timestamp` (ISO 8601 with milliseconds and
`Z`), `cwd` (the working directory at that moment), `version` (Claude Code version),
`gitBranch`, `isSidechain` (bool), `userType` (`external`), `entrypoint`.

## Assistant messages (observed)

`message.role = "assistant"`, `message.content` is a list of blocks with `type` `text`
(`text`), `thinking`, or `tool_use` (`id` starting `toolu_`, `name`, `input` object). One API
message can be spread over several lines, one block per line, sharing `message.id`.
`message.stop_reason` is `tool_use` or `end_turn` (or null on intermediate lines).

## User-role entries and how typed prompts are told apart

`message.role = "user"`. `message.content` is either a string or a list of blocks (`text`,
`tool_result`, `image`).

A line is a **user-typed prompt** only when all of these hold:

- `type = "user"`, `isSidechain` false (main transcript), `isMeta` absent or false,
  `isCompactSummary` absent or false;
- its content has no `tool_result` block;
- its text does not start with a harness marker: `<task-notification>`,
  `<local-command-stdout>`, `<local-command-stderr>`, `<local-command-caveat>`, `<bash-input>`,
  `<bash-stdout>`, `<bash-stderr>`, `Caveat:`, `Stop hook feedback`, `[Request interrupted`,
  `This session is being continued`;
- when present, `origin.kind` / `turnOrigin` is `human` (observed on typed prompts).

Slash commands typed by the user arrive as `<command-message>…</command-message>` /
`<command-name>…` text and count as typed prompts. A prompt typed while the agent is working is
delivered as an `attachment` with `attachment.type = "queued_command"` whose `prompt` is not a
task notification (observed); it also counts as a typed prompt.

Everything else is an **environment entry**, never a prompt:

| Entry | Recognised by |
|---|---|
| meta | `isMeta: true` (observed) |
| hook feedback | text starting `Stop hook feedback` / `SubagentStop hook feedback`, or containing the hook reason; `isMeta` (not observed) |
| task notification | `<task-notification>` text in a `queued_command` attachment (observed) or a user line |
| local command output | `<local-command-stdout>` / `<local-command-stderr>` (not observed) |
| `!` bash mode | `<bash-input>` for the command, `<bash-stdout>` / `<bash-stderr>` for its output (not observed). These are the user's commands, not the agent's, and never count as evidence |
| compaction summary | `isCompactSummary: true` user line; `system` line with `subtype = compact_boundary` (not observed) |

## Tool calls and results (observed)

A `tool_result` block (`tool_use_id`, `content` as a string or a list of text blocks,
`is_error`) answers the `tool_use` with the same id. Result lines also carry
`sourceToolAssistantUUID` and a top-level `toolUseResult`. Results of parallel calls arrive as
separate lines in completion order; proof-of-done pairs them by id.

**Bash**

- Success: `is_error` false; `toolUseResult` is an object with `stdout`, `stderr`,
  `interrupted`, `isImage`, `noOutputExpected`. Exit status 0 is implied; no exit code field.
- Non-zero exit: `is_error: true`; `content` starts with `Exit code N` on its own line, followed
  by the combined output; `toolUseResult` is a **string** (the error text), not an object.
- Killed by the tool timeout: `is_error: true`, `content` `Exit code 143` followed by
  `Command timed out after …`.
- Long output, success (more than about 30 000 characters): `content` is a
  `<persisted-output>` block with a 2 KB preview of the head; `toolUseResult.stdout` holds the
  first 30 000 characters; the full output is saved to the file named by
  `toolUseResult.persistedOutputPath` (size in `persistedOutputSize`) in the session's
  `tool-results/` directory. The tail of the output (where test summaries are) is only in that file.
- Long output, failure: `content` holds roughly the first 30 000 characters with a middle
  elision `[N characters truncated]`; the tail is not stored.
- `run_in_background: true` in the input: `content` says `Command running in background with ID: …`
  and `toolUseResult.backgroundTaskId` is set. The exit status arrives later as a task
  notification (below).
- Interrupted by the user (not observed): `toolUseResult.interrupted: true` or a
  `[Request interrupted by user…` result text.
- Moved to the background after a timeout (not observed on this version, where the timeout
  killed the command): treated as background when the result text says the command moved to or
  is running in the background, or `backgroundTaskId` is present.

proof-of-done reads the output text in this order: the persisted output file (first and last
64 KiB) when present, else `stdout` + `stderr`, else the `content` text. The exit code is 0 when
`is_error` is false, else the number after `Exit code`, else unknown.

**Edits**: `Edit` (`file_path`, `old_string`, `new_string`, `replace_all`), `Write`
(`file_path`, `content`), `NotebookEdit` (`notebook_path`). `MultiEdit` (`file_path`, `edits`)
is absent from the current tools reference and still accepted for older transcripts.
`toolUseResult` for `Write` holds `type = "create"`, `filePath`, `content`; for `Edit` it holds
`filePath`, `oldString`, `newString`, `originalFile`, `structuredPatch`, `userModified`,
`replaceAll`. proof-of-done reads edit targets from the tool input, not from these results.

**Subagents**: the `Agent` tool (legacy name `Task`) with `subagent_type`, `description`,
`prompt`.

## Task notifications (observed)

When a background command finishes, an `attachment` line with `attachment.type =
"queued_command"` carries a `prompt` of the form:

```
<task-notification>
<task-id>…</task-id>
<tool-use-id>…</tool-use-id>
<output-file>…</output-file>
<status>completed|failed|killed</status>
<summary>… exit code N</summary>
</task-notification>
```

`queue-operation` lines repeat the same content for queue bookkeeping and are skipped.
Notifications are linked to the background call through `<tool-use-id>` and never count as a
foreground run.

## Subagent transcripts (observed)

Each subagent writes `<session-id>/subagents/agent-<agent-id>.jsonl` next to the parent
`<session-id>.jsonl`, plus `agent-<agent-id>.meta.json` (`agentType`, `description`,
`toolUseId` of the parent `Agent` call, `spawnDepth`, …). Every line has `isSidechain: true`, an
`agentId`, and the parent's `sessionId`. The first line is the task prompt with `parentUuid`
null. The SubagentStop payload's `agent_transcript_path` points at this file.

## Timestamps, ordering, compaction

Lines are appended in order; `timestamp` is per line. proof-of-done orders events by line
position, placing each tool result directly after its call. Compaction keeps writing to the same
file: earlier history stays in the file, a `compact_boundary` system line and an
`isCompactSummary` user line mark the summary (not observed).

## Resumed and forked sessions

The sessions docs state that `--resume` / `--continue` append to the existing file, while
`--fork-session` and `/branch` create a new file that starts as a copy of the history. A file
whose first `user`/`assistant` line has a non-null `parentUuid` that no line in the same file
defines starts mid-session (its earlier history lives in another file); for such files the hook
downgrades blocks to warnings.

## Codex CLI rollout (experimental)

`src/proof_of_done/transcript/codex.py` reads OpenAI Codex CLI's `~/.codex/sessions/**` rollout
JSONL format (`parse(path) -> Session`, plus a `looks_like_codex(first_line)` sniff). It is
**experimental** and audit-only: `proof-of-done audit` reads it with `--source codex` or picks it
up under `--source auto` (which sniffs a file's first line), and there is no `Stop`-hook
equivalent. It is pinned to openai/codex commit
`44fe510ce3ee61c8ef623adcbf89b901c73ddd61`, confirmed directly against
`codex-rs/history/src/rollout_payload.rs`, `codex-rs/protocol/src/models.rs`,
`codex-rs/protocol/src/protocol.rs`, `codex-rs/core/src/tools/mod.rs` and
`codex-rs/core/assets/tools/apply_patch.lark` at that commit. Unlike the Claude Code
adapter above, no real Codex log was read to build this: every test fixture is hand-authored
from those source definitions.

Reasons to keep this narrow: Codex's own crate layout has already moved once (rollout
persistence used to live inside `core`; it is now a separate `rollout` crate, with `RolloutItem`
itself defined in yet another crate, `history`), the top-level `RolloutItem` enum carries twelve
variants and only two matter for an audit adapter, and the shell tool's function *name* has
already changed across releases (`exec_command` today, `shell`/`local_shell_call` earlier). A
version bump should be treated as a possible breaking change, not assumed forward-compatible.

**Envelope**: one JSON object per line, `{timestamp, ordinal?, type, payload, metadata?}`.
`type` is the `RolloutItem` variant; `payload` holds that variant's fields
(`#[serde(tag = "type")]`, confirmed directly in source, not merely inferred from a stale
description). Only two variants matter here:

- `session_meta`: `payload.session_id` (or `payload.id`) and `payload.cwd`.
- `response_item`: `payload` is itself a `ResponseItem`, separately tagged by its own `type`.
  Every other `RolloutItem` variant (`inter_agent_communication`, `compacted`, `turn_context`,
  `token_usage_record`, `world_state`, `retained_context`, `security_risk_score`, `event_msg`,
  `realtime_item`, `inter_agent_communication_metadata`) is ignored. As a defensive fallback for
  an older/other encoding, a line whose top-level `type` is directly one of the `ResponseItem`
  type strings below (no `response_item` + `payload` wrapping) is read the same way.

**`message`**: `role` (`user`/`assistant`; any other role becomes an ignorable environment
entry) and `content`, a list of `{type, text}` blocks (`input_text` / `output_text` on the wire,
`text` accepted defensively). A `user`-role message whose text starts with the harness's own
injected-context markers -- `<environment_context>` or `<user_instructions>` (confirmed string
constants in `codex-rs/protocol/src/protocol.rs`) -- is an environment entry, not a typed
prompt; every other `user`-role message is one. `assistant`-role text becomes an agent message.

**`function_call`** named `exec_command` (current), or the legacy/alternate names `shell` /
`container.exec`: `arguments` is a JSON-encoded string (confirmed: the field is a raw string,
not already-parsed JSON) holding `cmd` (a single command string -- `exec_command`'s confirmed
argument shape) or a `command` list (older/alternate shape, joined with `shlex.join`), plus an
optional `workdir`. Read as a `Bash` tool-call step so the existing evidence/edit-detection code
picks it up unchanged.

**`local_shell_call`**: the Responses API's hosted shell tool. `action.command` is a string
list (joined with `shlex.join`), `action.working_directory` is the cwd. Also read as `Bash`.

**`function_call_output`** pairs to a `function_call` / `local_shell_call` by `call_id`.
`output` is a plain string or a list of structured content items (confirmed untagged wire
encoding); both are read. The exit code is **not** a structured field anywhere on the wire --
confirmed: the harness only ever formats it into the text as `Exit code: N` -- so this adapter
regexes it out of the output text. A result is `ok` when the exit code is 0, unknown when no
`Exit code:` line is found.

**`custom_tool_call`** named `apply_patch`: `input` is the raw V4A patch text (confirmed
grammar, not JSON). Edit targets come from `*** Add File: *` / `*** Update File: *` /
`*** Delete File: *` hunk headers, each becoming a synthetic `Write` (added file) or `Edit`
(updated/deleted file) tool-call step -- reusing the same tool names the Claude Code adapter
uses, so the existing edit-detection tool list needs no change. An `*** Update File:` hunk's
optional `*** Move to: *` line adds the destination as a second `Edit` target (mirroring how a
Bash `mv`'s source and destination both become edit targets elsewhere in this codebase). One
`apply_patch` call can touch several files, so it expands to several synthetic call/result step
pairs, all sharing the outcome of the one `custom_tool_call_output`.

**`custom_tool_call_output`**: no confirmed structured success/failure field exists on the wire
for this item type (unlike `function_call_output`'s `Exit code: N` text convention, which is
specific to the exec/shell formatting path). Its synthetic edit steps are recorded `ok=True`
unconditionally -- edit-event detection never reads a result step's `ok` -- and the output text
is attached only for trace completeness.

Everything else (`AdditionalTools`, `AgentMessage`, `Reasoning`, `ToolSearchCall`,
`WebSearchCall`, `ImageGenerationCall`, `Compaction`, and the ten non-`session_meta`/
non-`response_item` `RolloutItem` variants) is ignored, mirroring the `#[serde(other)]`
catch-all the real Codex source itself uses for forward compatibility. Robustness: a bad line is
skipped, never raised on.
