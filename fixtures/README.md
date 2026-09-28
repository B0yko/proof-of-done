# fixtures/

Synthetic Claude Code transcripts, generated from a small scenario DSL. Everything under this
directory is original test data: no real transcript content, no real paths, no real names.
Licensed Apache-2.0, same as the rest of the repository.

Not part of the installed package. `tests/` and `eval/` import `fixtures.dsl` and
`fixtures.render` via a `sys.path` insertion of the repo root (see `tests/conftest.py`).

## Files

- `dsl.py` — loads and strictly validates a scenario YAML file (using the vendored,
  stdlib-only YAML loader), raising `DslError` with the file name and the offending key path.
- `render.py` — renders a validated scenario into a Claude Code JSONL transcript tree, in the
  shape pinned by `docs/transcript-format.md`. Also runnable directly:
  `python fixtures/render.py SCENARIO.yaml --out DIR`.
- `scenarios/*.yaml` — hand-written scenarios, one DSL feature (or a closely related group)
  per file.

## Scenario DSL

```yaml
id: my-scenario                 # unique; file name is <id>.yaml
description: free text
ecosystem: python | node | go | rust | mixed
root: /work/demo-app            # default; fixture paths always live under /work/
project_files:                  # files present at the project root; value = content or null
  pyproject.toml: "[project]\nname = \"demo-app\"\n"
  uv.lock: null                 # null means "exists, empty"
config: {mode: block, rules: []}  # optional; current content of .proof-of-done.yaml
env: {PROOF_OF_DONE_MODE: warn}   # optional
start: fresh | mid_session        # default fresh
turns:
  - user: "Fix the date parsing bug"   # a real typed prompt
    steps:                              # rendered in order; each item has exactly one key
      - edit: {tool: Edit, path: src/app/models.py, old: "a", new: "b"}
      - bash: {cmd: "uv run pytest -q", exit: 0, output: "42 passed in 0.81s", mode: foreground}
      - parallel: [ {bash: {...}}, {edit: {...}} ]
      - subagent: {type: general-purpose, prompt: "...", result: "...", steps: [...], final: "...", labels: {...}}
      - text: "interim assistant text"
      - env_entry: {entry: hook_feedback, text: "..."}
      - user_bash: {cmd: "git status", output: "..."}
      - queued_prompt: "a prompt typed while the agent is still working"
    final: "Fixed it. [[tests_passed|All 42 tests pass]]."
    labels:
      tests_passed: {label: supported}
```

Step kinds:

- `edit`: `tool` is `Edit` (`old`/`new`), `Write` (`content`), `MultiEdit` (`edits`), or
  `NotebookEdit` (`new_source`).
- `bash`: `mode` is `foreground` (default), `background` (optionally with
  `notify: {exit: N}` to render a later task-notification), `interrupted`, `timeout`,
  `moved_to_background`, or `no_result` (the call gets no result line at all). Long outputs are
  built with `output_repeat: {line, times}` plus a trailing `output` (so the pass/fail summary
  lands at the tail, the way a real test run's output does); output over 30 000 characters is
  rendered as Claude Code stores it — a preview inline, the full text persisted to a
  `tool-results/` file.
- `parallel`: a list of `edit`/`bash` steps issued as one assistant turn (one shared message
  id); the renderer writes every call first, then the result lines in **reverse** call order,
  to exercise id-based pairing on the read side.
- `subagent`: rendered as an `Agent` tool call plus the subagent's own transcript
  (`<session-id>/subagents/agent-<agent-id>.jsonl`) and `.meta.json` file. `result` is what
  comes back to the parent; `final`/`labels` are the subagent's own last message and claims,
  evaluated at its own (single) SubagentStop.
- `env_entry`: `entry` is one of `hook_feedback`, `task_notification`, `meta`, `compaction`,
  `local_command`, `bash_mode`.
- `user_bash`: a user-run `!` command; never agent evidence.
- `queued_prompt`: a real prompt typed by the user while the agent is still working (delivered
  as a `queued_command` attachment, not a task notification).

`attempts` replaces `steps`/`final`/`labels`/`payload`/`expect` on a turn with a list of the
same shape, one per stop attempt; the renderer inserts a `hook_feedback` environment entry
between attempts.

### Markers and labels

`[[type|text]]` inside `final` marks a labelled claim; the renderer strips the `[[...]]`
syntax and records the character span of `text` in the rendered (stripped) message. A claim
type matches `^[a-z][a-z0-9_]*(#\d+)?$` — use `tests_passed`, then `tests_passed#2` for a
second claim of the same type in one message. A `final` with no marker is a no-claim turn and
must have `labels: {}`.

`labels` is keyed by the exact marker token (including any `#N`). `label: supported` needs
nothing else; `label: unsupported` needs `reason` (one of `supported`, `no_command`, `stale`,
`failed_exit`, `failed_output`, `empty_run`, `masked_inconclusive`, `background_only`,
`superseded_by_failure`, `no_result`) and `suggest` (a command string, or `null` when none can
be suggested). `partial: true` is optional on either.

## Determinism

Rendering is fully deterministic: the same scenario renders to byte-identical files every
time. All ids are derived with `uuid5` under one fixed namespace:

```python
NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://github.com/B0yko/proof-of-done/fixtures")
# d986b061-b9aa-5b68-bc11-e2574d5004db
```

- Session id: `uuid5(NAMESPACE, f"{scenario_id}/session/0")`.
- Line uuid (main file, 0-based): `uuid5(NAMESPACE, f"{scenario_id}/line/{n}")`; a subagent's
  own lines use `uuid5(NAMESPACE, f"{scenario_id}/agent/{agent_index}/line/{n}")`. A line's
  `parentUuid` is the previous line's uuid (or, for a `start: mid_session` scenario, the first
  line's `parentUuid` is `uuid5(NAMESPACE, f"{scenario_id}/line/-1")` — deliberately not
  defined anywhere in the file).
- Tool-use id: `"toolu_" + uuid5(NAMESPACE, f"{scenario_id}/tool/{n}").hex` (`n` counts calls
  across the whole scenario, main file and subagents together).
- Agent id: `uuid5(NAMESPACE, f"{scenario_id}/agent/{n}")` (`n` counts subagents spawned).

Timestamps start at `2026-01-05T10:00:00.000Z` and advance one second per line, counted
separately within each file. The model name is `synthetic-model`; `version` is pinned to
`2.1.281`; `gitBranch` is `main`.

## Output layout

For a scenario rendered into `<out>`:

- `<out>/<session-id>.jsonl` — the full main transcript.
- `<out>/<session-id>/subagents/agent-<agent-id>.jsonl` (+ `.meta.json`) — one per subagent.
- `<out>/<session-id>/tool-results/<basename>` — persisted long outputs.
- `<out>/<session-id>.stop-<turn>-<attempt>.jsonl` — a truncated prefix of the main transcript
  at each stop attempt (and `...stop-agent-<n>.jsonl` for the parent's prefix at a subagent's
  stop), so a hook simulated at attempt *k* never sees a later attempt. `render()` returns one
  `StopCase` per stop attempt (including subagent stops) pointing at these paths, along with
  the Stop/SubagentStop payload, the final message with markers stripped, and the label spans.
