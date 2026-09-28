# Configuration reference

proof-of-done reads YAML configuration from up to four layers, merges them, and caches the
merged result. This page documents every key, the merge rules, the environment variables,
and the evidence commands each built-in rule looks for.

## Layers

Lowest to highest precedence:

1. **Built-in defaults** — `defaults.yaml`, shipped inside the package. Always present.
2. **User config** — `${XDG_CONFIG_HOME:-~/.config}/proof-of-done/config.yaml`. Optional.
3. **Project config** — `<project root>/.proof-of-done.yaml`. Optional. The project root is
   the nearest ancestor of the current directory that contains `.proof-of-done.yaml` or
   `.git`, not walking above `$HOME`.
4. **`PROOF_OF_DONE_CONFIG`** — the file named by this environment variable, if set.
   Optional.

After the four file layers are merged, two environment variables can still adjust the
result (see [Environment variables](#environment-variables) below).

Run `proof-of-done config show` to print the effective configuration and which files it
came from.

## Merge semantics

- **Scalars** (`enabled`, `mode`, `max_blocks_per_turn`, ...) from a higher layer replace
  the lower layer's value.
- **Lists** (`subagent_skip_types`, `execution_commands`, a rule's `relevant_files`, ...)
  from a higher layer replace the lower layer's list wholesale — they are never
  concatenated.
- **`rules`** merge by `id`: a rule in a higher layer with the same `id` as a built-in or
  already-defined rule overrides it field by field (untouched fields keep the lower
  layer's value); a rule whose `evidence` mapping is given merges *that* mapping by key
  the same way (an override that only sets `evidence.command_regex` leaves
  `evidence.commands` untouched); a new `id` is appended as a new rule, after the existing
  ones, in the order it appears.

Because rules merge field by field, a project config can turn off one built-in rule
(`action: "off"`) and add unrelated custom rules in the same file without repeating
anything from `defaults.yaml`. See `examples/python.yaml` and `examples/monorepo.yaml`.

> **YAML gotcha:** prefer writing `action: "off"` in quotes. YAML 1.1 parses an unquoted
> `off` as the boolean `false`, not the string `"off"`; the config loader accepts this as a
> nicety and treats it exactly like the string `"off"`, but quoting stays the clearer way to
> write it.

## Environment variables

| Variable | Effect |
|---|---|
| `PROOF_OF_DONE=off` | Disables the hook for this shell/session (`enabled: false`). Ignored if the session itself edited a Claude Code settings file to set it — see [Tamper protection](#tamper-protection). |
| `PROOF_OF_DONE_MODE=block\|warn` | Overrides the merged `mode`. `warn` is ignored under the same tamper condition as `PROOF_OF_DONE=off`; `block` always applies. |
| `PROOF_OF_DONE_CONFIG=<path>` | An extra config file layered above the project config. |
| `PROOF_OF_DONE_PYTHON=<path>` | The `python3` interpreter the hook launcher uses. |
| `PROOF_OF_DONE_SALT=<string>` | Seeds `audit --redact`'s hashing (also settable with `--salt`). |

## Tamper protection

If the session being checked itself edited `.proof-of-done.yaml`, the user config, the
`PROOF_OF_DONE_CONFIG` file, or a `.claude/settings*.json` file that touches
`PROOF_OF_DONE`/`enabledPlugins`, the hook runs a tamper scan before applying the merged
config. For every edited config file, only the *downgrades* it is responsible for are
undone — an `enabled: false`, a `mode: warn`, a rule `action` lowered towards `warn`/`off`,
or a raised `max_blocks_per_turn` — using the value the merge would have had without that
file. Anything else the same file changed (a lowered cap, a new custom rule, a tightened
`action`) still applies. Tampering alone never blocks a stop; a warning names the edited
file.

## Top-level keys

| Key | Type | Default | Meaning |
|---|---|---|---|
| `version` | int | `1` | Config schema version. Must be `1` when present. |
| `enabled` | bool | `true` | Whether the hook checks anything at all. |
| `mode` | `block` \| `warn` | `block` | Downgrades every rule's `block` action to a user-visible warning when set to `warn`. |
| `max_blocks_per_turn` | int ≥ 0 | `2` | Consecutive blocks allowed per turn (per `session_id` + `agent_id`) before the hook allows the stop and just warns. Must stay below Claude Code's own stop-hook block cap. |
| `check_subagents` | bool | `true` | Whether the `SubagentStop` hook checks subagent transcripts at all. |
| `subagent_skip_types` | list of str | `[Explore, Plan]` | Subagent `agent_type` values that are never checked (the built-in read-only subagent types). |
| `subagent_calls_are_edits` | bool | `true` | Whether an `Agent`/`Task` tool call counts as an edit event (conservative: a subagent may have changed files the parent transcript cannot see). |
| `skip_token` | str | `"#skip-proof"` | Token that, in the user's latest real prompt, disables checks for that turn. |
| `max_transcript_mb` | number > 0 | `200` | Transcripts larger than this fail open with a `systemMessage` instead of being parsed. |
| `edits` | mapping | see below | Tool names and command prefixes that count as edit events. |
| `execution_commands` | list of str (prefixes) | see [generated table](#evidence-commands) | Extra commands accepted as evidence for `fixed`/`verified` claims, beyond every other enabled rule's own commands. |
| `read_only_commands` | list of str (prefixes) | see [generated table](#evidence-commands) | Commands that never count as evidence for any rule, however they match a rule's `evidence.commands`. |
| `rules` | list of rule mappings | the 7 built-in rules | See [Rule keys](#rule-keys). |

### `edits`

| Key | Type | Meaning |
|---|---|---|
| `tools` | list of str | Tool names whose calls are edit events (`Edit`, `Write`, `MultiEdit`, `NotebookEdit`). |
| `subagent_tools` | list of str | Tool names that invoke a subagent (`Agent`, legacy `Task`). |
| `bash_writes` | list of str (prefixes) | Bash programs that write to a file the shell segment identifies (`sed -i`, `mv`, `rm`, `tee`, `git apply`, ...). |
| `formatters` | list of str (prefixes) | Formatters/fixers whose path arguments (or the whole project, with none) count as edits (`ruff format`, `prettier --write`, `cargo fmt`, ...). |
| `tree_commands` | list of str (prefixes) | Commands that touch every file in the project (`git checkout`, `git reset --hard`, `tar -x`, ...). |
| `agent_trace_tools` | list of str | Tool names the `agent-trace/v1` adapter treats as edits (`Edit`, `Write`, `MultiEdit`, `NotebookEdit`, `apply_patch`, `write_file`, `edit_file`). |

## Rule keys

Every entry in `rules` is a mapping:

| Key | Type | Meaning |
|---|---|---|
| `id` | str, required | Unique rule identifier. Rules merge by `id`. |
| `claim_type` | str matching `^[a-z][a-z0-9_]*$` | The claim type this rule judges (`tests_passed`, `lint_clean`, or a custom type for a custom rule). |
| `action` | `block` \| `warn` \| `"off"` | What an unsupported claim of this rule does. Write `"off"` quoted (see the YAML gotcha above); an unquoted `off` is also accepted. |
| `claims` | list of regex str | Patterns matched against a message clause to detect this rule's claim. Built-in rules get these from the claim detector; custom rules must set their own. |
| `keywords` | list of lowercase str | Every match of this rule's `claims` must contain one of these; they feed the fast-path prefilter. Required (non-empty) for any rule whose `id` is not one of the 7 built-ins. |
| `evidence` | mapping | See below. |
| `relevant_files` | list of glob str | Files whose edits anchor this rule's evidence search. `**` matches any depth. |
| `ignore_files` | list of glob str | Files excluded from `relevant_files` (docs, logs, the config file itself, ...). |
| `exempt_if_only_edited` | list of glob str | If every edit in the session matches one of these globs, the claim needs no evidence at all (for example, a doc-only fix). |
| `suggest` | str or null | A fixed `Run:` suggestion for this rule, used when the agent never ran a matching command itself. Project-type detection is the next fallback. |

### `evidence`

| Key | Type | Meaning |
|---|---|---|
| `commands` | list of str (prefixes) | Shell command prefixes that count as evidence. Empty for `fixed`/`verified`, which instead accept any other enabled rule's commands plus `execution_commands`. |
| `command_regex` | regex str or null | An additional pattern matched against the full command text. |
| `exclude_args` | list of str | Any of these tokens present in the command disqualifies it (`--dry-run`, `--watch`, ...). |
| `partial_args` | list of str | Any of these tokens marks a matching command as a *partial* run (a subset of tests, for example). |
| `fail_output` | list of regex str | Any match in the command's output marks it failed. |
| `success_output` | list of regex str | Required to match when the command's segment is masked (piped, `\|\| true`, ...); otherwise informational. |
| `empty_output` | list of regex str | A match means the command ran but exercised nothing (`collected 0 items`, ...); never counts as evidence. |

## Several rules, one claim type

More than one rule can share a `claim_type` (see `examples/monorepo.yaml`, which splits
`tests_passed` into `tests-python` and `tests-node`). The claim is supported only if every
rule that has a relevant edit since session start is itself supported; with no relevant
edit under any of them, any one rule's evidence is enough.

## Evidence commands

The table below is generated from `defaults.yaml` by `scripts/render_config_doc.py`; run
`python scripts/render_config_doc.py --check` to verify it is current, or without `--check`
to regenerate it after editing `defaults.yaml`.

<!-- evidence-table:start -->
| rule id | claim type | default action | evidence commands (prefixes) |
|---|---|---|---|
| `tests` | `tests_passed` | block | `pytest`, `python -m pytest`, `python -m unittest`, `tox`, `nox`, `hatch test`, `npm test`, `npm t`, `npm run test`, `npm run test:*`, `pnpm test`, `pnpm run test`, `pnpm run test:*`, `yarn test`, `yarn run test`, `bun test`, `jest`, `vitest run`, `vitest --run`, `mocha`, `playwright test`, `go test`, `cargo test`, `cargo nextest run`, `make test`, `make check`, `just test` |
| `build` | `build_passed` | block | `npm run build`, `pnpm build`, `pnpm run build`, `yarn build`, `yarn run build`, `bun run build`, `vite build`, `next build`, `tsc --build`, `tsc -b`, `webpack`, `go build`, `cargo build`, `make build`, `make all`, `python -m build`, `uv build`, `hatch build`, `poetry build`, `docker build`, `docker buildx build` |
| `lint` | `lint_clean` | block | `ruff check`, `ruff format --check`, `flake8`, `pylint`, `black --check`, `eslint`, `npm run lint`, `pnpm lint`, `pnpm run lint`, `yarn lint`, `yarn run lint`, `biome check`, `biome lint`, `prettier --check`, `golangci-lint run`, `go vet`, `staticcheck`, `cargo clippy`, `cargo fmt --check`, `make lint`, `pre-commit run`, `shellcheck` |
| `typecheck` | `typecheck_clean` | block | `mypy`, `dmypy run`, `pyright`, `basedpyright`, `tsc`, `vue-tsc`, `npm run typecheck`, `npm run type-check`, `pnpm typecheck`, `pnpm run typecheck`, `yarn typecheck`, `cargo check`, `go vet`, `go build`, `make typecheck` |
| `deploy` | `deployed` | block | `vercel deploy`, `vercel --prod`, `netlify deploy`, `fly deploy`, `flyctl deploy`, `kubectl apply`, `helm upgrade`, `helm install`, `terraform apply`, `pulumi up`, `docker push`, `npm publish`, `pnpm publish`, `cargo publish`, `twine upload`, `uv publish`, `gcloud run deploy`, `gcloud app deploy`, `firebase deploy`, `wrangler deploy`, `serverless deploy`, `sls deploy`, `cdk deploy`, `railway up`, `git push heroku`, `make deploy` |
| `fixed` | `fixed` | block | *(every other enabled rule's commands, plus `execution_commands`)* |
| `verified` | `verified` | block | *(every other enabled rule's commands, plus `execution_commands`)* |
<!-- evidence-table:end -->

## The merged-config cache

The hook's fast path reads `<data dir>/config-cache.json` instead of parsing YAML on every
stop. Its key is `[path, mtime_ns, size]` (or `[path, null]` when a file is missing) for
`defaults.yaml` and every layer file; a hit reconstructs the config straight from that JSON
and never imports the YAML loader at all. Any change to a layer file's size or mtime — or
any error reading the cache — causes a full reparse, which rewrites the cache atomically.
