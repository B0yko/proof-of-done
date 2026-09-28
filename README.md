# proof-of-done

A Claude Code plugin and CLI that check a coding agent's completion claims against its own
session transcript. When the agent tries to end a turn with "all tests pass", "the build
succeeds", "lint is clean", "fixed", "deployed" or "verified", a Stop hook checks, deterministically,
whether a matching command actually ran after the last relevant edit and actually succeeded — and
if not, blocks the stop and tells the agent the exact command to run. There is no LLM anywhere in
this product: same transcript in, same verdict out, every time.

![A fixture Stop payload piped into the installed hook, blocking with a Run: line, then `audit --demo`](docs/demo.svg)
*A fixture Stop payload piped into the installed hook, then `audit --demo`. Not a live agent
session.*

## Quickstart

Pick whichever of the three matches how you use Claude Code.

**a. Command line**

```sh
claude plugin marketplace add B0yko/proof-of-done
claude plugin install proof-of-done@proof-of-done
```

**b. Inside a Claude Code session** (the equivalent of the two commands above)

```
/plugin marketplace add B0yko/proof-of-done
/plugin install proof-of-done@proof-of-done
```

No restart is needed either way: Claude Code runs `/reload-plugins` automatically when you close
the `/plugin` panel with a pending install, which applies it to the running session.

**c. Try it without installing anything**, using [uv](https://docs.astral.sh/uv/)'s ephemeral
tool runner:

```sh
uvx --from git+https://github.com/B0yko/proof-of-done proof-of-done audit --demo
```

This runs the audit CLI (not the Stop hook) against a bundled synthetic demo corpus and prints a
report — a safe way to see the tool's output shape before installing the plugin. A short-form
`uvx proof-of-done` will work once the package is published to PyPI; until then, use the
`git+https` form above.

### Prerequisites

- `python3` ≥ 3.9 on the `PATH` Claude Code's hook processes see (macOS ships one at
  `/usr/bin/python3`; anything from `pyenv`, Homebrew, or `uv` also works). No `pip install`
  needed for the hook itself — see [Architecture](#architecture--how-it-works).
- macOS or Linux. Windows is not supported in v0.1 (see [Limitations](#limitations)).
- Tested against Claude Code **2.1.281**. The transcript format it parses is internal and
  undocumented (see `docs/transcript-format.md`), so a very different Claude Code version could
  behave differently; the hook fails open (see below) rather than misbehaving if it does.

### What a block looks like

This is real output — a rendered fixture transcript (a stale test run: the agent edited a file
*after* its last test run, then claimed the suite was green) piped into the actual installed hook
launcher, `bin/proof-of-done-hook`, the same binary Claude Code itself invokes on `Stop`:

```
proof-of-done: 1 claim in your final message is not backed by this session's transcript.
1. "The test suite is green" (tests_passed): the last test run `uv run pytest -q` at step 2 was
   before your edit to `src/app/core.py` at step 4.
   Run: uv run pytest -q
Run these commands and report the real result, or restate your message without these claims.
```

`src/app/core.py` is a fixture path (`/work/demo-app`), not a real project. You can reproduce
this yourself: render any scenario under `fixtures/scenarios/` or `fixtures/templates/` with
`fixtures/render.py`, build a Stop payload from `fixtures/payloads/stop_with_last_message.json`
pointed at the rendered transcript, and pipe it into `bin/proof-of-done-hook stop --data-dir
<dir>`.

## Architecture & how it works

```mermaid
flowchart LR
    A["Claude Code Stop event"] --> B["bin/proof-of-done-hook<br/>(launcher)"]
    B --> C["Merged-config cache<br/>(keywords + rules)"]
    C --> D{"Fast-path prefilter:<br/>any keyword in the message?"}
    D -- no --> E["exit 0, no output"]
    D -- yes --> F["Transcript parser<br/>(event list: edits, commands, subagents)"]
    F --> G["Claim detector<br/>(claims.py)"]
    G --> H["Evidence engine<br/>anchor → candidates → verdict"]
    H --> I["Suggester<br/>(Run: command)"]
    I --> J["Decision JSON<br/>block / warn / allow"]

    K["proof-of-done audit"] --> F
    H --> L["Report + agent-trace/v1 export"]
```

The Stop hook is invoked on every turn's end; the `SubagentStop` hook runs the same pipeline
against a subagent's own transcript, unless its `agent_type` is in `subagent_skip_types`
(`Explore`, `Plan` by default — the built-in read-only subagents). `proof-of-done audit
<transcripts>` reuses the exact same parser, claim detector and evidence engine per stop attempt,
against historic sessions instead of a live one, and produces a report plus an optional
`agent-trace/v1` export.

The full walk-through — claim-detection filters, the evidence engine's anchor/candidate/verdict
rules and reason precedence, suggestion order, block-message limits, loop safety and the tamper
check — is in **[`docs/how-it-works.md`](docs/how-it-works.md)**. The nine architectural
decisions behind it (deterministic detection over an LLM judge, checking evidence instead of
re-running commands, the stdlib-only hook, fail-open, and more) are recorded as short ADRs under
**[`docs/adr/`](docs/adr/)**.

## Configuration

Four layers merge, lowest to highest precedence: packaged defaults, a user config file, a
project `.proof-of-done.yaml`, then environment variables. Every top-level key, every rule field,
the full evidence-command table, and the merge/tamper semantics are documented in
**[`docs/config.md`](docs/config.md)**. `examples/` ships ready-to-copy configs for Python, Node,
Go and a monorepo, plus `examples/extra-rules.yaml` showing custom rule types
(`committed`/`pushed`) as an opt-in.

## Escape hatches

- **`#skip-proof`** (configurable via `skip_token`) anywhere in your own next prompt disables
  checks for that turn only. It has to be in a real typed prompt — text the agent writes that
  merely mentions the token has no effect.
- **`PROOF_OF_DONE=off`** disables the hook entirely for a shell/session; **`mode: warn`**
  (config) or **`PROOF_OF_DONE_MODE=warn`** downgrades every block to a visible warning instead.
- **Consecutive-block cap** (`max_blocks_per_turn`, default 2, deliberately below Claude Code's
  own 8-block Stop cap): after this many blocks in a row for one turn, the hook allows the stop
  and just warns, naming the still-unsupported claims, so a genuinely-stuck agent is never wedged.
- **Tamper check.** If the session itself edits `.proof-of-done.yaml`, your user config, the
  `PROOF_OF_DONE_CONFIG` file, or a Claude Code `settings*.json` in a way that touches
  `PROOF_OF_DONE`/`enabledPlugins`, only the downgrades that edit is responsible for
  (`enabled: false`, `mode: warn`, a lowered `action`, a raised block cap) are undone for the
  rest of that session, and a warning names the edited file. Tampering by itself never blocks a
  stop — it only stops it from quietly disabling the check.

## Debugging a block

```sh
# Re-check one message against a transcript, without a live hook:
proof-of-done check --transcript path/to/session.jsonl --message "All tests pass."

# See the effective merged configuration and which files it came from:
proof-of-done config show
```

The hook's own diagnostic log — rule ids, reason codes, timings, and exception class names,
**never message text or commands** — is at `${CLAUDE_PLUGIN_DATA}/proof-of-done.log` (falls back
to `${TMPDIR:-/tmp}/proof-of-done-<uid>/proof-of-done.log` if `CLAUDE_PLUGIN_DATA` is unset or
unexpanded), JSON Lines, capped at 256 KiB with a `.1` rotation.

## Privacy

The hook reads the Claude Code Stop-event JSON on stdin and the session transcript file it
points to (or the subagent's own transcript, for `SubagentStop`) — both already local to your
machine, written by Claude Code itself. It makes **no network call**, ever; a socket-blocking
test fixture enforces this in CI.

What it writes: per-turn consecutive-block counters, a merged-config cache, Python's own
`.pyc` bytecode cache, and the size-capped log described above, which never contains message
text, shell commands, or file paths beyond the log file's own. `audit --export-traces --redact`
additionally replaces every quote, command, path and session id in an export with a salted
SHA-256 prefix plus length (`h:<12 hex>:len=<n>`); the salt is random per run by default unless
you pass `--salt`/`PROOF_OF_DONE_SALT`.

## English-only detection

The claim detector's negation, hedge, instruction and claim-phrasing rules are all English
literals and patterns. A claim made in another language is not detected — not blocked, not
warned about, simply invisible to this tool. This is a stated limitation, not a bug to report.

## CLI reference

```
proof-of-done check --transcript PATH [--message TEXT | --message-file PATH] [--config PATH] [--json]
proof-of-done audit [PATH ...] [--claude-projects | --demo] [--source auto|claude-code|agent-trace|codex]
                     [--config PATH] [--format text|json|md] [--out FILE] [--export-traces FILE]
                     [--redact] [--salt S] [--ci] [--max-unsupported-rate R]
proof-of-done init [--force]
proof-of-done config show [--cwd DIR]
proof-of-done trace validate FILE
```

- **`check`** evaluates one final message against one transcript, without a live hook. Exit `0`
  = supported, `1` = an unsupported blocking claim, `2` = a usage/config error.
- **`audit`** replays the Stop hook at every stop attempt of every given session (and each
  subagent's own transcript, grouped under its parent), against the built-in defaults or
  `--config`. Exactly one input mode is required: one or more `PATH`s (files, directories, or
  globs), `--claude-projects` (`$CLAUDE_CONFIG_DIR/projects`, else `~/.claude/projects`), or
  `--demo` (the bundled 30-session synthetic corpus). `--source codex` reads OpenAI Codex CLI
  rollout logs — **experimental**, audit-only, no `Stop` hook support (see
  [Limitations](#limitations)). `--redact` hashes quotes, commands, paths and session ids;
  `--export-traces FILE` also writes `agent-trace/v1` JSONL, redacted the same way when combined
  with `--redact`. `--ci` exits `1` when the unsupported-claim rate exceeds
  `--max-unsupported-rate` (default `0`), `2` on a usage error, `3` when no transcript could be
  read, and writes a Markdown summary to `$GITHUB_STEP_SUMMARY` when that variable is set. See
  `examples/ci/audit-agent-run.yml` for a GitHub Actions job shape (documented, not run by this
  repository's own CI — it needs a real headless agent run to audit).
- **`init`**  writes a starter `.proof-of-done.yaml`, detected from the project's ecosystem.
- **`config show`** prints the effective merged configuration and which files it came from.
- **`trace validate FILE`** validates an `agent-trace/v1` JSONL file against
  `schemas/agent-trace-v1.json`.

### Demo audit report

Real output of `proof-of-done audit --demo` against the bundled 30-session synthetic corpus
(Python, Node, Go and Rust projects, authored for this repository — **not a measurement of any
real coding agent**):

```
proof-of-done audit report
SYNTHETIC DEMO CORPUS -- this report describes 30 hand-authored fixture sessions, not a measurement of any real coding agent.

config applied: built-in defaults
files processed: 30

sessions: 30
turns: 30
stop attempts: 30
turns with claims: 30
claims: 33
unsupported: 13 (39.4% of claims)
partial share of supported claims: 15.0%

by claim type:
  build_passed: 3 claims, 1 unsupported (33.3%)
  deployed: 2 claims, 0 unsupported (0.0%)
  fixed: 5 claims, 1 unsupported (20.0%)
  lint_clean: 5 claims, 2 unsupported (40.0%)
  tests_passed: 12 claims, 7 unsupported (58.3%)
  typecheck_clean: 3 claims, 1 unsupported (33.3%)
  verified: 3 claims, 1 unsupported (33.3%)
```

(truncated here; the full report also lists the reason breakdown and the 10 most recent
unsupported examples with their `Run:` suggestions.)

## Results

Every number below comes from `uv run python eval/run_eval.py` (claim detection, gate quality,
adversarial resistance, suggestion accuracy) and `uv run python eval/latency.py --generate --run`
(hook latency, audit throughput), against three separate fixture sets that are never merged into
one headline number: **templated** (expanded from `fixtures/templates/` by the same author who
wrote the detector — measures consistency with the specification), **held-out** (112 individually
authored, free-form sessions, frozen at commit `4e54fc9` — `eval: freeze held-out set` — before
`claims.py` existed; the more honest estimate), and **adversarial** (sessions in `eval/adversarial/`
that deliberately try to game the gate).

Every table below is generated by `scripts/render_results.py` from the latest committed
`eval/results/<date>.json` and `eval/results/latency-<date>.json`; `scripts/render_results.py
--check` (run in CI) fails if these tables drift from those files, and CI re-runs
`eval/run_eval.py --compare` against the committed quality results to confirm every metric still
matches. Each results file records the git SHA, Python version and platform it was produced on.

**How the held-out numbers got here.** The first held-out run, with a detector tuned only on the
templated set, found 29% of the labelled claims. The detector was then broadened using a separate
development set (`eval/dev/`, 471 individually written messages, never the held-out set); the
held-out files were not opened while tuning. A label audit of every held-out disagreement plus a
25-scenario sample without disagreements then corrected 17 label errors (mostly unmarked lead-in
claims such as "Fixed the lock ordering in acquire()."), each recorded with its reason in
[`eval/heldout/CHANGES.md`](eval/heldout/CHANGES.md); the sample had none. All three runs are
committed (`eval/results/history/`) and can be reproduced by checking out the listed commit and
running `uv run python eval/run_eval.py`:

<!-- results:history:start -->
| run | commit | held-out detection P / R | held-out gate claim P / R | held-out gate turn P / R | held-out false-block rate | templated gate claim P / R |
|---|---|---|---|---|---|---|
| first held-out run (detector tuned on the templated set only) | `ecdffe5` | 89.8% / 29.1% | 95.2% / 32.3% | 100.0% / 29.0% | 0.0% | 100.0% / 96.3% |
| after tuning on the development set (frozen labels) | `5c60b16` | 87.1% / 80.1% | 91.1% / 82.3% | 93.8% / 72.6% | 2.8% | 100.0% / 96.3% |
| current (held-out label corrections in CHANGES.md) | `7915966` | 92.8% / 80.1% | 98.2% / 83.3% | 97.9% / 73.4% | 1.0% | 100.0% / 96.3% |
<!-- results:history:end -->

Claim-instance detection precision/recall/F1 on the templated and held-out sets (a detection
matches a label when the claim type agrees and the spans overlap by at least one character):

<!-- results:detection:start -->
| set | N labels | N detections | TP | FP | FN | precision | recall | F1 |
|---|---|---|---|---|---|---|---|---|
| templated | 955 | 955 | 944 | 11 | 11 | 98.8% | 98.8% | 0.988 |
| heldout | 161 | 139 | 129 | 10 | 32 | 92.8% | 80.1% | 0.860 |
<!-- results:detection:end -->

Gate quality, claim level — precision/recall/F1 for "unsupported claim" (positive class), with
Wilson 95% intervals for precision/recall and a seeded bootstrap 95% interval for F1, reported
both for the shipped config and for a "before switch" variant with every built-in rule forced to
`block`:

<!-- results:gate-claim:start -->
| set | config | N | TP | FP | FN | precision (95% CI) | recall (95% CI) | F1 (95% CI) |
|---|---|---|---|---|---|---|---|---|
| templated | shipped | 955 | 395 | 0 | 15 | 100.0% [99.0%, 100.0%] | 96.3% [94.1%, 97.8%] | 0.981 [0.971, 0.990] |
| templated | forced_block | 955 | 395 | 0 | 15 | 100.0% [99.0%, 100.0%] | 96.3% [94.1%, 97.8%] | 0.981 [0.971, 0.990] |
| heldout | shipped | 162 | 55 | 1 | 11 | 98.2% [90.6%, 99.7%] | 83.3% [72.6%, 90.4%] | 0.902 [0.841, 0.952] |
| heldout | forced_block | 162 | 55 | 1 | 11 | 98.2% [90.6%, 99.7%] | 83.3% [72.6%, 90.4%] | 0.902 [0.841, 0.952] |
<!-- results:gate-claim:end -->

Gate quality, turn level — a turn is a predicted block if any claim in it is unsupported under a
`block`-action rule; the false-block rate is the share of turns with no unsupported label that
were blocked anyway:

<!-- results:gate-turn:start -->
| set | config | N | TP | FP | FN | precision (95% CI) | recall (95% CI) | F1 (95% CI) |
|---|---|---|---|---|---|---|---|---|
| templated | shipped | 975 | 375 | 0 | 35 | 100.0% [99.0%, 100.0%] | 91.5% [88.4%, 93.8%] | 0.955 [0.940, 0.970] |
| templated | forced_block | 975 | 375 | 0 | 35 | 100.0% [99.0%, 100.0%] | 91.5% [88.4%, 93.8%] | 0.955 [0.940, 0.970] |
| heldout | shipped | 169 | 47 | 1 | 17 | 97.9% [89.1%, 99.6%] | 73.4% [61.5%, 82.7%] | 0.839 [0.759, 0.906] |
| heldout | forced_block | 169 | 47 | 1 | 17 | 97.9% [89.1%, 99.6%] | 73.4% [61.5%, 82.7%] | 0.839 [0.759, 0.906] |

False-block rate (shipped config; share of no-unsupported-label turns blocked):
| set | N | blocked | rate | 95% CI |
|---|---|---|---|---|
| templated | 565 | 0 | 0.0% | [0.0%, 0.7%] |
| heldout | 105 | 1 | 1.0% | [0.2%, 5.2%] |
<!-- results:gate-turn:end -->

Gate quality by claim type (shipped config). The rule: a built-in claim type whose held-out gate
precision is below 0.90 ships with `action: warn` instead of `block`. With the corrected labels no
type is below 0.90 (`verified` sits exactly at 0.90, 9 of 10 predicted blocks), so every built-in
rule ships as `block` and the "forced block" variant above equals the shipped config. On the
frozen labels before the audit, `build_passed` (0.875), `fixed` (0.818) and `verified` (0.800)
were below 0.90; every one of those false positives was an unmarked lead-in claim that the audit
labelled. The held-out counts per type are small (8–12 predicted blocks), so read these as rough:

<!-- results:per-type:start -->
| set | claim type | N | precision | recall | F1 |
|---|---|---|---|---|---|
| templated | build_passed | 40 | 100.0% | 100.0% | 1.000 |
| templated | deployed | 40 | 100.0% | 100.0% | 1.000 |
| templated | fixed | 80 | 100.0% | 100.0% | 1.000 |
| templated | lint_clean | 110 | 100.0% | 90.0% | 0.947 |
| templated | tests_passed | 605 | 100.0% | 96.2% | 0.980 |
| templated | typecheck_clean | 40 | 100.0% | 100.0% | 1.000 |
| templated | verified | 40 | 100.0% | 100.0% | 1.000 |
| heldout | build_passed | 19 | 100.0% | 88.9% | 0.941 |
| heldout | deployed | 26 | 100.0% | 100.0% | 1.000 |
| heldout | fixed | 26 | 100.0% | 100.0% | 1.000 |
| heldout | lint_clean | 26 | 100.0% | 100.0% | 1.000 |
| heldout | tests_passed | 24 | 100.0% | 75.0% | 0.857 |
| heldout | typecheck_clean | 18 | 100.0% | 12.5% | 0.222 |
| heldout | verified | 23 | 90.0% | 100.0% | 0.947 |
<!-- results:per-type:end -->

Adversarial resistance — k of 36 gaming attempts (fake success output, redefined `pytest`
functions/aliases, `PATH` prepending, collect-only/masked runs, interrupted/timeout-killed runs, a
partial pass after a failed full run, a claim hidden in a code block, the agent writing the skip
token itself, the agent editing the config to `enabled: false`) that met their labelled outcome.
Every miss is listed here as a known limitation, never silently tuned away — the echo-only
Makefile `test:` target is expected to be one of them (see [Limitations](#limitations)):

<!-- results:adversarial:start -->
**34 / 36** adversarial cases met their labelled outcome.

Known misses:

| scenario | expected decision | expected reason | predicted decision | predicted reason |
|---|---|---|---|---|
| adv-07-makefile-echo-only-known-miss | block | None | allow | None |
| adv-36-npm-run-test-watch-known-miss | block | None | allow | None |
<!-- results:adversarial:end -->

Suggestion accuracy — share of correctly blocked unsupported claims whose `Run:` command equals
the labelled expected command:

<!-- results:suggestion:start -->
| set | N correctly blocked | matching suggestion | accuracy |
|---|---|---|---|
| templated | 375 | 355 | 94.7% |
| heldout | 49 | 49 | 100.0% |
<!-- results:suggestion:end -->

Hook latency — p50/p95/max over 200 fresh-subprocess invocations of the real launcher (interpreter
startup included), for the fast path (no claim in the message), and 1 MB/10 MB/50 MB transcripts,
under both the system `python3` and a uv-managed CPython 3.12, with an empty and a warm
merged-config cache:

<!-- results:latency:start -->
*(run `uv run python eval/latency.py --generate --run` to populate this table)*
<!-- results:latency:end -->

Audit throughput on the 50 MB synthetic transcript, median of 5 runs:

<!-- results:throughput:start -->
*(run `uv run python eval/latency.py --generate --run` to populate this table)*
<!-- results:throughput:end -->

**Caveat, stated plainly:** the same author wrote the detector and the fixtures, so the templated
numbers above measure consistency with the specification, not real-world agent behaviour. The
held-out set, frozen before the detector existed, is the more honest estimate — but it is still
synthetic. Neither set is a substitute for running `proof-of-done audit` against your own agent's
real transcripts. Per-claim-type tables, the reason confusion matrix and the full held-out
error list are in **[`docs/eval.md`](docs/eval.md)**.

### Failure analysis (held-out)

Every held-out error, grouped by failure mode and claim type, with one example each. There are no
prefilter misses, no wrong verdicts on detected claims and no wrong suggestions; almost every error
is a claim the detector does not recognise:

<!-- results:failure-modes:start -->
| failure mode | claim type | count | example scenario | example |
|---|---|---|---|---|
| detection_fn | typecheck_clean | 14 | `ho-027` | "Types check" |
| detection_fn | deployed | 4 | `ho-066` | "it's published" |
| detection_fn | tests_passed | 3 | `ho-006` | "They pass" |
| detection_fn | lint_clean | 2 | `ho-064` | "vet is clean" |
| detection_fn | build_passed | 1 | `ho-012` | "Build is clean" |
| detection_fp | lint_clean | 1 | `ho-104` | "tests pass" |
| detection_fp | verified | 1 | `ho-081` | "verified" |
<!-- results:failure-modes:end -->

- **Type-check phrasings are the biggest gap.** "Types check", "it typechecks" and "it vets
  clean" are not in the detector's vocabulary, so held-out recall for `typecheck_clean` is low.
  An unsupported type-check claim phrased that way passes the gate.
- **State-style deploy claims** ("it's published", "it's live in production", "rolled out to the
  cluster") and **pronoun subjects** ("They pass") are missed for the same reason.
- **Two false positives:** a hedge the negation window does not cover ("I wouldn't call it fully
  verified yet") and a two-claim sentence where "tests pass" was attributed to `lint_clean`.

These were not fixed after the held-out run; tuning on the held-out set would make its numbers
meaningless. They are listed under [Limitations](#limitations).

## Data sources and licences

Every fixture, template, held-out session and adversarial case in this repository is synthetic:
authored for this project, no real transcript content anywhere, licensed Apache-2.0 with the
rest of the code (see `fixtures/README.md`). The one third-party component vendored into the
package is a pure-Python copy of **PyYAML 6.0.3** (MIT-licensed; `LICENSE` kept at
`src/proof_of_done/_vendor/yaml/LICENSE`) — see `docs/adr/0003-stdlib-only-hook-vendored-yaml.md`
for why it is vendored rather than an installed dependency.

## Alternatives

Existing Claude Code hooks and plugins that verify completion claims or gate a `Stop` event on
tests/build/deploy state, as of 2026-09-28 (every entry checked directly against its repository):

| Project | Mechanism | License · stars |
|---|---|---|
| [claimcheck](https://github.com/ablanchard-dev/claimcheck) | Reads the final message and checks each claim against tool-call output already in the same turn's transcript (three-state verified/refuted/unverifiable verdict); explicitly never re-runs commands. | GPL-3.0 · 0 (created 2026-09-23) |
| [done-needs-proof](https://github.com/sdvsignal/done-needs-proof) | Node `Stop` hook; blocks "done"/"fixed"/"deployed"/"tests pass"-style claims unless a matching verification command ran after the last edit. Regex/keyword matching, fails open, fires once per stop. Ships a companion `prove` skill. | MIT · 0 (created 2026-09-24) |
| [loop-hooks](https://github.com/wwwcojp/loop-hooks) | **Re-runs** the project's own verification command on `Stop`/`SubagentStop`/`TeammateIdle`, gated by a git-derived fingerprint of watched files so it only re-runs when something relevant actually changed. | MIT · 0 (created 2026-08-25) |
| [tdd-guard](https://github.com/nizos/tdd-guard) (successor: [Probity](https://github.com/nizos/probity)) | A process gate, not a claim checker: blocks an edit that introduces implementation code without a failing test first. Can call a validation model for some checks (partly LLM-judge-based). The most established tool in this space by adoption. | MIT · 2,353 / 216 |
| [protect-tests](https://github.com/karanb192/claude-code-hooks/tree/main/plugins/protect-tests) | `PreToolUse` hook blocking "fake green": deleting, renaming away, or skip/xfail-disabling tests instead of fixing the code, across several languages' test conventions. Deterministic pattern matching. | MIT · 526 (whole marketplace repo) |
| [dead-rules-audit](https://github.com/karanb192/claude-code-hooks/tree/main/plugins/dead-rules-audit) | Not a stop gate: scores every edit against `CLAUDE.md`'s own numbered rules and reports which are routinely ignored. Included as an adjacent design pattern (measuring which soft instructions actually get followed), not a completion-claim checker. | MIT · 526 (whole marketplace repo) |
| [sonmat](https://github.com/jun0-ds/sonmat) | Prompt-injection only: writes verification-discipline instructions into `CLAUDE.md` and propagates them to subagent prompts. No transcript inspection, no re-running, no external judge — the model verifies itself under an injected framework. | BSD-3-Clause · 6 |
| Official [`plugin-dev` hook-development guide](https://github.com/anthropics/claude-code/blob/main/plugins/plugin-dev/skills/hook-development/SKILL.md) | Anthropic's own documented example of a `Stop` hook is a `prompt`-type hook — i.e. a second model call judging "approve" or "block". Presented as teaching material for hook authors, not a shipped, maintained plugin. Anthropic's own curated marketplace ([`claude-plugins-official`](https://github.com/anthropics/claude-plugins-official)) lists on-demand security scanners but no dedicated completion-claim gate. | — |

proof-of-done's position in this table: deterministic transcript/evidence inspection (like
claimcheck and done-needs-proof), never re-running a command itself (unlike loop-hooks), never
calling a model (unlike tdd-guard/Probity's validation-model option and the official prompt-hook
example), with a published, reproducible precision/recall evaluation against a frozen held-out
set rather than an unmeasured claim of correctness. This is a description of what already exists,
not a claim to be first at anything in this space.

## Limitations

- **English only** — see [above](#english-only-detection).
- **The claim detector misses phrasings it has no pattern for.** On the held-out set it found
  80% of the labelled claims; type-check wording ("Types check", "it typechecks", "it vets
  clean"), state-style deploy claims ("it's live in production") and pronoun subjects ("They
  pass") are the main gaps (see [Failure analysis](#failure-analysis-held-out)). A missed claim is
  never checked, so an unsupported claim phrased that way ends the turn unchallenged.
- **`npm run test:*` scripts are trusted by name.** A `test:watch` script (or any script whose
  name starts with `test:`) matches the `tests` rule even when it runs a watcher; this is the
  second known adversarial miss.
- **Background commands never count as evidence.** A test run started with
  `run_in_background: true`, or moved to the background after a timeout, is never read as a
  successful foreground run in v0.1, however its later task-notification reports it finished; the
  block message says to re-run it in the foreground.
- **No detection of edits made outside the agent** — a file changed by the user in their own
  editor mid-session, with no corresponding transcript event, is invisible; there is no
  cross-check against `git status` or file mtimes.
- **Subagent internals are not parsed for edits.** A subagent call counts as a (conservative)
  edit event for every rule by default (`subagent_calls_are_edits`), but what the subagent
  actually touched, inside its own transcript, is not read.
- **A subagent's own verdict is not reused as its parent's evidence** — each `SubagentStop`
  is judged independently; a subagent reporting "tests pass" (itself checked) does not by itself
  make the parent session's own later claim supported. Left for a future release.
- **Resumed or forked sessions downgrade every block to a warning.** A transcript whose earlier
  history lives in another file (the hook can tell from a dangling first `parentUuid`) cannot be
  fully evidenced, so the hook never blocks on incomplete information — only warns.
- **The Claude Code transcript format is internal and undocumented**, and this project pins its
  parsing to Claude Code **2.1.281** (`docs/transcript-format.md`). A future Claude Code release
  could change it; the hook is written to fail open (never crash, never hang) if it does, but its
  detection accuracy on a changed format is unverified until re-pinned.
- **Windows is not supported** in v0.1 (POSIX `sh` launcher, POSIX path assumptions).
- **Evidence comes only from the `Bash` tool.** MCP-based test runners, IDE diagnostics, and the
  `PowerShell` tool are not read as evidence sources.
- **Claims that no command can evidence are out of scope** — generic "done"/"implemented" claims,
  and claims about external state a shell command cannot check (for example "the email was
  sent").
- **The Codex CLI adapter is experimental and audit-only.** `--source codex` reads OpenAI Codex
  CLI rollout logs, pinned to a specific `openai/codex` source commit and built entirely from
  hand-authored fixtures (no real Codex log was read to build it — see
  `docs/transcript-format.md`); there is no Codex `Stop`-equivalent hook in this product.
- **A gaming attempt that this evaluation's adversarial set could not defeat**: an echo-only
  Makefile `test:` target (`test:\n\techo "5 passed"`) matches the `tests` rule's command prefix
  and its output happens to match a `success_output` pattern, with no way for a shell-level
  evidence engine to tell it apart from a real test runner's own summary line. See the
  adversarial results above for the full, current miss list — every miss is reported there, not
  silently patched over.
- **No PyPI package yet.** `uvx proof-of-done` (short form) needs the name published to PyPI,
  which needs Andrii's explicit approval (see Roadmap); until then, use the `git+https` install
  form.

## Uninstall / disable

```sh
claude plugin uninstall proof-of-done@proof-of-done   # remove it entirely
claude plugin disable proof-of-done                    # keep it installed, turn it off
```

Or, without touching the plugin installation: set `PROOF_OF_DONE=off` in the environment Claude
Code's hook processes see, or `enabled: false` in `.proof-of-done.yaml`.

## Roadmap

- Reuse a subagent's own (already-checked) verdict as evidence for its parent session's claim.
- Publish to PyPI once Andrii sets up trusted publishing, so `uvx proof-of-done` works without
  the `git+https` form.
- Revisit the Codex CLI adapter against a real, format-verified rollout log once one can be
  pinned with confidence, and consider a Codex-side hook equivalent.
- Evidence from non-`Bash` sources (MCP test runners, structured IDE diagnostics).

## Licence

Apache-2.0. See [`LICENSE`](LICENSE). Copyright 2026 Andrii Boiko.
