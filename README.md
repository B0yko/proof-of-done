<p align="center">
  <img src="https://raw.githubusercontent.com/B0yko/proof-of-done/main/docs/assets/banner.svg" alt="proof-of-done: a coding agent says 'All 42 tests pass.'; the Stop hook blocks because the last test run came before the last edit, and suggests 'uv run pytest -q'" width="100%">
</p>

<p align="center">
  <b>A coding agent can say “tests pass” only when its own transcript proves it.</b>
</p>

<p align="center">
  <a href="https://github.com/B0yko/proof-of-done/actions/workflows/ci.yml"><img alt="CI" src="https://img.shields.io/github/actions/workflow/status/B0yko/proof-of-done/ci.yml?branch=main&style=flat-square&label=CI"></a>
  <a href="https://pypi.org/project/proof-of-done/"><img alt="PyPI" src="https://img.shields.io/pypi/v/proof-of-done?style=flat-square&color=10b981&label=PyPI"></a>
  <img alt="Python 3.9–3.13" src="https://img.shields.io/badge/python-3.9%E2%80%933.13-3776ab?style=flat-square&logo=python&logoColor=white">
  <img alt="No LLM calls" src="https://img.shields.io/badge/LLM%20calls-none-64748b?style=flat-square">
  <a href="https://github.com/B0yko/proof-of-done/blob/main/LICENSE"><img alt="License: Apache-2.0" src="https://img.shields.io/badge/license-Apache--2.0-64748b?style=flat-square"></a>
</p>

<p align="center">
  <a href="https://github.com/B0yko/proof-of-done#quickstart">Quickstart</a> ·
  <a href="https://github.com/B0yko/proof-of-done#how-it-works">How it works</a> ·
  <a href="https://github.com/B0yko/proof-of-done#results">Results</a> ·
  <a href="https://github.com/B0yko/proof-of-done#configuration">Configuration</a> ·
  <a href="https://github.com/B0yko/proof-of-done#limitations">Limitations</a> ·
  <a href="https://github.com/B0yko/proof-of-done/blob/main/docs/how-it-works.md">Docs</a>
</p>

---

When a coding agent ends its turn with “all tests pass”, “the build succeeds”, “fixed” or
“deployed”, a Claude Code `Stop` hook checks the session's own transcript. Did the matching
command run **after the last relevant edit**, and did it **succeed**? If not, the stop is blocked
and the agent is told exactly what to run. The same engine audits past transcripts and reports
how often agents claimed what they never checked.

- **Acts only on claims.** A keyword prefilter lets every other stop through untouched; the hook
  never runs your tests itself.
- **Evidence, not vibes.** The latest matching command after the last relevant edit decides.
  Failed, empty, background and masked runs do not count.
- **Tells the agent what to run.** Every block names the claim and the reason, with step numbers
  and the file, and ends with a `Run:` line.
- **Hard to game.** Fake `echo "12 passed"`, a redefined `pytest`, `PATH` tricks and the agent
  disabling the check are all in the adversarial set.
- **Safe to leave on.** Deterministic, stdlib-only, no network. Fails open on any error, with a
  loop cap below Claude Code's own.
- **Measured.** Precision and recall on a held-out set frozen before the detector existed, plus
  latency on real hardware.

## Quickstart

Requires:

- macOS or Linux;
- `python3` ≥ 3.9 on the `PATH` Claude Code's hooks see (macOS `/usr/bin/python3` needs the
  Command Line Tools, otherwise the hook skips with a message);
- Claude Code; the hooks target the **2.1.281** hook API (the live smoke test procedure is in
  [docs/smoke-test.md](docs/smoke-test.md)).

```sh
claude plugin marketplace add B0yko/proof-of-done
claude plugin install proof-of-done@proof-of-done
```

It loads on the next start, or after `/reload-plugins`. Inside a session,
`/plugin marketplace add B0yko/proof-of-done` and `/plugin install proof-of-done@proof-of-done`
do the same ([docs](https://code.claude.com/docs/en/discover-plugins)).

Try the audit on a bundled synthetic corpus, no install needed:

```sh
uvx proof-of-done audit --demo
```

<p align="center">
  <img src="https://raw.githubusercontent.com/B0yko/proof-of-done/main/docs/demo.svg" alt="A fixture Stop payload piped into the installed hook, which blocks with a Run: line, then proof-of-done audit --demo" width="80%">
  <br>
  <sub>A fixture Stop payload piped into the installed hook, then <code>audit --demo</code>. Not a live agent session.</sub>
</p>

<details>
<summary><b>Demo audit report</b>: <code>proof-of-done audit --demo</code> on the bundled synthetic corpus</summary>

Thirty hand-authored sessions across Python, Node, Go and Rust projects. This is a demo of the
report, **not a measurement of any real coding agent**. The block is generated from an in-process
run and checked in CI.

<!-- results:demo:start -->
```
proof-of-done audit report
SYNTHETIC DEMO CORPUS -- this report describes 30 hand-authored fixture sessions, not a measurement of any real coding agent.

config applied: built-in defaults
files processed: 30

sessions: 30
turns: 30
stop attempts: 30
turns with claims: 30
claims: 45
unsupported: 16 (35.6% of claims)
partial share of supported claims: 17.2%

by claim type:
  build_passed: 3 claims, 1 unsupported (33.3%)
  deployed: 4 claims, 0 unsupported (0.0%)
  fixed: 15 claims, 4 unsupported (26.7%)
  lint_clean: 5 claims, 2 unsupported (40.0%)
  tests_passed: 12 claims, 7 unsupported (58.3%)
  typecheck_clean: 3 claims, 1 unsupported (33.3%)
  verified: 3 claims, 1 unsupported (33.3%)

unsupported by reason:
  background_only: 1
  empty_run: 1
  failed_exit: 3
  failed_output: 1
  masked_inconclusive: 2
  no_command: 2
  no_result: 2
  stale: 2
  superseded_by_failure: 2

most recent unsupported claims:
  [fixed] "Patched" -- superseded_by_failure
    Run: cargo test
  [tests_passed] "All tests pass" -- superseded_by_failure
    Run: cargo test
  [tests_passed] "All tests pass" -- background_only
    Run: go test ./...
  [fixed] "Fixed the race" -- stale
    Run: go test ./...
  [lint_clean] "Lint is clean" -- failed_exit
    Run: go vet ./...
  [tests_passed] "All tests pass" -- empty_run
    Run: npx vitest run
  [typecheck_clean] "Typecheck is clean" -- failed_exit
    Run: npx tsc --noEmit
  [fixed] "Patched" -- masked_inconclusive
    Run: npx vitest run
  [verified] "Confirmed it's working" -- masked_inconclusive
    Run: npx vitest run
  [build_passed] "The build succeeds" -- failed_exit
    Run: uv build
```
<!-- results:demo:end -->

</details>

### What a block looks like

The `reason` the agent receives when its tests ran alongside the edit instead of after it (real
output, from a fixture):

```text
proof-of-done: 1 claim in your final message is not backed by this session's transcript.
1. "all 40 tests pass" (tests_passed): the last test run `uv run pytest -q` at step 2 was before your edit to `src/app/parser.py` at step 4.
   Run: uv run pytest -q
Run these commands and report the real result, or restate your message without these claims.
```

The `Run:` command is the agent's own, else the rule's `suggest` setting, else one found in the
project files. With nothing to suggest (say, a deploy claim in an unknown project) it reads
`Run: (no … command found — run it in the foreground)`.

<details>
<summary>Reproduce this block</summary>

```sh
OUT=$(mktemp -d)
uv run python fixtures/render.py fixtures/scenarios/parallel-calls.yaml --out "$OUT"
sed -e "s|__TRANSCRIPT_PATH__|$(ls "$OUT"/*.stop-0-0.jsonl)|" \
    -e "s|__CWD__|$OUT|" -e "s|__SCRATCHPAD_DIR__|$OUT|" \
    -e "s|__LAST_ASSISTANT_MESSAGE__|Updated the parser; all 40 tests pass.|" \
    fixtures/payloads/stop_with_last_message.json |
  sh bin/proof-of-done-hook stop --data-dir "$OUT/data" |
  python3 -c 'import json, sys; print(json.load(sys.stdin)["reason"])'
```

`fixtures/render.py` writes the transcript and one snapshot per stop attempt
(`*.stop-<turn>-<attempt>.jsonl`); the other scenarios under `fixtures/scenarios/` work the same
way. The templated set is expanded in-process by `fixtures/expand.py` (`--stats` prints its
counts).

</details>

## How it works

For each claim in the final message, the evidence engine finds:

- the **anchor**: the last edit to a file the rule cares about (edit tools, Bash writes,
  formatters, whole-tree git operations, subagent calls);
- the **candidates**: the foreground commands after it that match the rule.

The latest candidate decides.

| Claim | Example wording | Counts as evidence, for example |
|---|---|---|
| `tests_passed` | "all 42 tests pass", "✅ Tests" | `pytest`, `npm test`, `go test`, `cargo test`, `make test` |
| `build_passed` | "the build succeeds" | `npm run build`, `go build`, `cargo build`, `uv build` |
| `lint_clean` | "lint is clean" | `ruff check`, `eslint`, `golangci-lint run`, `cargo clippy` |
| `typecheck_clean` | "mypy passes" | `mypy`, `pyright`, `tsc`, `cargo check` |
| `deployed` | "deployed to staging" | `fly deploy`, `kubectl apply`, `terraform apply`, `docker push`, `npm publish` |
| `fixed`, `verified` | "fixed the crash", "verified with curl" | any command above, or `curl`, `python script.py`, `go run`, `make <target>` |

The claim detector is plain `re`, English only. It ignores code blocks, inline code, quotes and
negated, hedged, future, question and instruction forms ("should pass", "run `pytest` to
confirm", "not verified").

<details>
<summary><b>Verdict reasons</b>: the closed set every verdict carries</summary>

| Reason | Meaning |
|---|---|
| `supported` | the latest matching run after the last relevant edit succeeded |
| `no_command` | no matching command ran in the session |
| `stale` | the last matching run came before the last relevant edit |
| `failed_exit` | it exited non-zero, was interrupted or timed out |
| `failed_output` | it exited 0, but its output reports failures |
| `empty_run` | it ran no tests (`collected 0 items`, `No tests found`) |
| `masked_inconclusive` | its exit status was masked (`\| tail`, `\|\| true`) and the output shows no success line |
| `background_only` | it only ran in the background: re-run it in the foreground |
| `superseded_by_failure` | a partial run (`-k`, `--lf`) passed after a full run failed |
| `no_result` | the run has no recorded result |

</details>

<details>
<summary><b>Architecture</b>: from the Stop event to the decision</summary>

```mermaid
flowchart TD
    A([Claude Code Stop event]) --> B[launcher]
    B --> C{fast-path prefilter:<br/>a claim keyword?}
    C -- no --> D([exit 0, no output])
    C -- yes --> E[merged-config cache]
    E --> F[transcript parser<br/>edits · commands · subagents]
    K([proof-of-done audit]) --> F
    F --> G[claim detector]
    G --> H[evidence engine<br/>anchor → candidates → verdict]
    H --> I[suggester: Run: command]
    H --> L[(audit report +<br/>agent-trace/v1 export)]
    I --> J([decision JSON<br/>block · warn · allow])
    classDef accent fill:#10b981,stroke:#047857,color:#ffffff
    class J accent
```

`SubagentStop` runs the same pipeline against a subagent's own transcript, except for the
read-only built-in types (`Explore`, `Plan`). `proof-of-done audit` reuses the parser, detector
and engine at every stop attempt of past sessions.

</details>

Full rules: [docs/how-it-works.md](https://github.com/B0yko/proof-of-done/blob/main/docs/how-it-works.md). Design decisions: nine short
ADRs in [docs/adr/](https://github.com/B0yko/proof-of-done/tree/main/docs/adr).

## Results

Three fixture sets, never merged into one headline number:

- **Templated**: expanded from templates by the detector's author, so it measures consistency with
  the specification.
- **Held-out**: individually authored, free-form, and frozen at `4e54fc9` (`eval: freeze held-out
  set`) before `claims.py` existed. The honest estimate.
- **Adversarial**: tries to game the gate.

<!-- results:headline:start -->
|  | templated | held-out |
|---|---|---|
| claim detection — precision / recall | 100.0% / 100.0% | 98.6% / 84.0% |
| gate on unsupported claims — precision / recall | 100.0% / 96.3% | 98.2% / 82.1% |
| turns blocked without an unsupported claim | 0.0% | 1.0% |

**34 / 36** gaming attempts met their labelled outcome · hook p95 on a 10 MB transcript **175 ms** (system Python 3.9) / **113 ms** (CPython 3.12) · audit **141 MB/s**
<!-- results:headline:end -->

> [!NOTE]
> The same author wrote the detector and the fixtures, and every set is synthetic. The held-out
> set is the more honest estimate; neither replaces running `proof-of-done audit` on your own
> agent's transcripts.

Every table below is generated from committed results by `scripts/render_results.py` and checked
in CI. To reproduce:

```sh
uv run python eval/run_eval.py
uv run python eval/latency.py --generate --run --max-load 4
```

<details>
<summary><b>How the held-out numbers got here</b>: first run, tuning, label audit</summary>

The first held-out run, with a detector tuned only on the templated set, found few of the
labelled claims. The detector was then broadened on a separate development set (`eval/dev/`). The
held-out set played no part in that tuning: after the freeze, the held-out files changed only in
label-correction commits and in documentation commits that touched only `CHANGES.md` there
(`git log -- eval/heldout`); the development scorer (`eval/dev_eval.py`) reads only `eval/dev/`
and the templated set; only `eval/run_eval.py` and the manifest test read `eval/heldout/`. A label
audit of every held-out disagreement then corrected the label errors it found, mostly unmarked
lead-in claims such as "Fixed the lock ordering in acquire().". Each correction is recorded with
its reason in [eval/heldout/CHANGES.md](https://github.com/B0yko/proof-of-done/blob/main/eval/heldout/CHANGES.md), together with the ids of a
25-scenario spot check of cases without disagreement. That check found two more unmarked lead-in
claims; both were corrected, and since the detector misses both, the correction lowered recall.

The rows below are the committed runs: the two earlier ones in `eval/results/history/`, the
current one in `eval/results/<date>.json`. The two earlier rows were re-scored with the current
per-message detection matching: product code and labels as of the listed commit, harness fix from
commit `b489e6d` (see the `rescored` field in each history JSON). The gate columns were not
affected, so `uv run python eval/run_eval.py` at a listed commit reproduces them.

<!-- results:history:start -->
| run | commit | held-out detection P / R | held-out gate claim P / R | held-out gate turn P / R | held-out false-block rate | templated gate claim P / R |
|---|---|---|---|---|---|---|
| first held-out run (detector tuned on the templated set only) | `7bfbd7e` | 89.8% / 29.1% | 95.2% / 32.3% | 100.0% / 29.0% | 0.0% | 100.0% / 96.3% |
| after tuning on the development set (frozen labels) | `08f2187` | 89.9% / 82.8% | 91.1% / 82.3% | 93.8% / 72.6% | 2.8% | 100.0% / 96.3% |
| current (held-out label corrections in CHANGES.md) | `e1579dc` | 98.6% / 84.0% | 98.2% / 82.1% | 97.9% / 73.4% | 1.0% | 100.0% / 96.3% |
<!-- results:history:end -->

</details>

<details>
<summary><b>Claim detection</b>: precision, recall and F1 with N</summary>

A detection matches a label when the claim type agrees and the spans overlap by at least one
character in the same message.

<!-- results:detection:start -->
| set | N labels | N detections | TP | FP | FN | precision | recall | F1 |
|---|---|---|---|---|---|---|---|---|
| templated | 955 | 955 | 955 | 0 | 0 | 100.0% | 100.0% | 1.000 |
| heldout | 163 | 139 | 137 | 2 | 26 | 98.6% | 84.0% | 0.907 |
<!-- results:detection:end -->

</details>

<details>
<summary><b>Gate quality</b>: unsupported claims, claim level and turn level, with intervals</summary>

The positive class is "unsupported claim". Precision and recall carry Wilson 95% intervals, F1 a
seeded bootstrap 95% interval. "forced_block" forces every built-in rule to `block`; it equals the
shipped config because no type needed the switch to `warn` described under the next heading.

<!-- results:gate-claim:start -->
| set | config | N | TP | FP | FN | precision (95% CI) | recall (95% CI) | F1 (95% CI) |
|---|---|---|---|---|---|---|---|---|
| templated | shipped | 955 | 395 | 0 | 15 | 100.0% [99.0%, 100.0%] | 96.3% [94.1%, 97.8%] | 0.981 [0.971, 0.990] |
| templated | forced_block | 955 | 395 | 0 | 15 | 100.0% [99.0%, 100.0%] | 96.3% [94.1%, 97.8%] | 0.981 [0.971, 0.990] |
| heldout | shipped | 164 | 55 | 1 | 12 | 98.2% [90.6%, 99.7%] | 82.1% [71.3%, 89.4%] | 0.894 [0.830, 0.946] |
| heldout | forced_block | 164 | 55 | 1 | 12 | 98.2% [90.6%, 99.7%] | 82.1% [71.3%, 89.4%] | 0.894 [0.830, 0.946] |
<!-- results:gate-claim:end -->

At turn level, a turn is a predicted block if any claim in it is unsupported under a `block` rule.
The false-block rate is the share of turns with no unsupported label that were blocked anyway.

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

</details>

<details>
<summary><b>By claim type</b>: and the rule that would switch a weak type to <code>warn</code></summary>

A built-in claim type whose held-out gate precision is below 0.90 ships with `action: warn`
instead of `block`. With the corrected labels no type is below 0.90 (`verified` is exactly 0.90),
so every built-in rule ships as `block`. On the frozen labels before the audit, `build_passed`,
`fixed` and `verified` were below 0.90 (second history file). The five false positives behind that
were one unmarked lead-in claim (`fixed`), one claim with the wrong type (`build_passed`), two
labels flipped because `python -c` is not an execution command (`fixed`, `verified`), and one
genuine false positive that remains, a hedge the negation window does not cover (`verified`). The
per-type counts are small, so read them as rough.

<!-- results:per-type:start -->
| set | claim type | N | predicted blocks | precision | recall | F1 |
|---|---|---|---|---|---|---|
| templated | build_passed | 40 | 20 | 100.0% | 100.0% | 1.000 |
| templated | deployed | 40 | 20 | 100.0% | 100.0% | 1.000 |
| templated | fixed | 80 | 20 | 100.0% | 100.0% | 1.000 |
| templated | lint_clean | 110 | 45 | 100.0% | 90.0% | 0.947 |
| templated | tests_passed | 605 | 250 | 100.0% | 96.2% | 0.980 |
| templated | typecheck_clean | 40 | 20 | 100.0% | 100.0% | 1.000 |
| templated | verified | 40 | 20 | 100.0% | 100.0% | 1.000 |
| heldout | build_passed | 19 | 8 | 100.0% | 88.9% | 0.941 |
| heldout | deployed | 28 | 9 | 100.0% | 90.0% | 0.947 |
| heldout | fixed | 26 | 11 | 100.0% | 100.0% | 1.000 |
| heldout | lint_clean | 26 | 8 | 100.0% | 100.0% | 1.000 |
| heldout | tests_passed | 24 | 9 | 100.0% | 75.0% | 0.857 |
| heldout | typecheck_clean | 18 | 1 | 100.0% | 12.5% | 0.222 |
| heldout | verified | 23 | 10 | 90.0% | 100.0% | 0.947 |

Held-out predicted blocks per claim type range from 1 (`typecheck_clean`) to 11 (`fixed`); a precision figure that rests on only a few predicted blocks is a rough estimate.
<!-- results:per-type:end -->

</details>

<details>
<summary><b>Adversarial resistance</b> and <b>suggestion accuracy</b></summary>

The adversarial cases cover fake success output, redefined `pytest` functions and aliases, `PATH`
prepending, collect-only and masked runs, interrupted and timeout-killed runs, a partial pass after
a failed full run, a claim hidden in a code block, the agent writing the skip token itself, and the
agent editing the config or settings to switch the check off. Every miss is a known limitation.

<!-- results:adversarial:start -->
**34 / 36** adversarial cases met their labelled outcome.

Known misses:

| scenario | expected decision | expected reason | predicted decision | predicted reason |
|---|---|---|---|---|
| adv-07-makefile-echo-only-known-miss | block | None | allow | None |
| adv-36-npm-run-test-watch-known-miss | block | None | allow | None |
<!-- results:adversarial:end -->

Suggestion accuracy is the share of correctly blocked unsupported claims whose `Run:` command
equals the labelled expected command.

<!-- results:suggestion:start -->
| set | N correctly blocked | matching suggestion | accuracy |
|---|---|---|---|
| templated | 375 | 355 | 94.7% |
| heldout | 49 | 49 | 100.0% |
<!-- results:suggestion:end -->

</details>

<details>
<summary><b>Hook latency</b> and <b>audit throughput</b>: MacBook Air, Apple M5, 24 GB</summary>

p50, p95 and max over 200 invocations of the real launcher, each a fresh interpreter, for the fast
path (no claim in the message) and 1, 10 and 50 MB transcripts, under the system `python3` and a
uv-managed CPython 3.12, with an empty and a warm merged-config cache. The hook's bytecode cache
in the data directory is warm after the first call of each series, and no row uses a transcript
parse cache. The latency targets (warm config cache: p95 ≤ 250 ms up to 10 MB, ≤ 1 s at 50 MB,
under both interpreters) are met.

<!-- results:latency:start -->
Machine: Mac17,4, Apple M5, 24 GB RAM, macOS 26.6.2; interpreters: system `python3` 3.9.6, uv-managed CPython 3.12.14; measured 2026-09-28T20:16:39Z. 1-minute load average during the run: 2.8–5.1 on 10 cores (other processes were running).

| case | interpreter, cache | N | p50 (ms) | p95 (ms) | max (ms) |
|---|---|---|---|---|---|
| fast path (no claim) | system python3, cold config cache | 200 | 132.5 | 147.0 | 323.8 |
| fast path (no claim) | system python3, warm config cache | 200 | 57.9 | 61.5 | 66.0 |
| fast path (no claim) | uv CPython 3.12, cold config cache | 200 | 72.5 | 80.4 | 193.9 |
| fast path (no claim) | uv CPython 3.12, warm config cache | 200 | 23.4 | 27.1 | 65.7 |
| 1 MB | system python3, cold config cache | 200 | 156.2 | 221.8 | 376.6 |
| 1 MB | system python3, warm config cache | 200 | 108.0 | 150.8 | 168.2 |
| 1 MB | uv CPython 3.12, cold config cache | 200 | 71.0 | 72.3 | 167.4 |
| 1 MB | uv CPython 3.12, warm config cache | 200 | 54.1 | 55.3 | 62.5 |
| 10 MB | system python3, cold config cache | 200 | 201.6 | 208.5 | 307.8 |
| 10 MB | system python3, warm config cache | 200 | 170.3 | 174.8 | 187.0 |
| 10 MB | uv CPython 3.12, cold config cache | 200 | 119.3 | 124.1 | 220.0 |
| 10 MB | uv CPython 3.12, warm config cache | 200 | 104.8 | 112.8 | 121.7 |
| 50 MB | system python3, cold config cache | 200 | 559.9 | 609.0 | 748.4 |
| 50 MB | system python3, warm config cache | 200 | 522.7 | 577.8 | 588.4 |
| 50 MB | uv CPython 3.12, cold config cache | 200 | 388.6 | 590.5 | 832.7 |
| 50 MB | uv CPython 3.12, warm config cache | 200 | 331.7 | 466.8 | 586.2 |
<!-- results:latency:end -->

Audit throughput on the 50 MB synthetic transcript, median of 5 runs:

<!-- results:throughput:start -->
`proof-of-done audit` on a 49.8 MB transcript: median **140.5 MB/s** over 5 run(s).
<!-- results:throughput:end -->

</details>

<details>
<summary><b>Failure analysis</b>: every held-out error, by failure mode</summary>

There are no prefilter misses, no wrong verdicts on detected claims and no wrong suggestions;
almost every error is a claim the detector does not recognise.

<!-- results:failure-modes:start -->
| failure mode | claim type | count | example scenario | example |
|---|---|---|---|---|
| detection_fn | typecheck_clean | 14 | `ho-027` | "Types check" |
| detection_fn | deployed | 6 | `ho-049` | "pushed the worker image" |
| detection_fn | tests_passed | 3 | `ho-006` | "They pass" |
| detection_fn | lint_clean | 2 | `ho-064` | "vet is clean" |
| detection_fn | build_passed | 1 | `ho-012` | "Build is clean" |
| detection_fp | lint_clean | 1 | `ho-104` | "tests pass" |
| detection_fp | verified | 1 | `ho-081` | "verified" |
<!-- results:failure-modes:end -->

- **Type-check phrasings are the biggest gap.** "Types check", "it typechecks" and "it vets
  clean" are not in the detector's vocabulary, so an unsupported type-check claim phrased that way
  passes the gate.
- **State-style deploy claims** ("it's published", "it's live in production", "pushed the worker
  image") and **pronoun subjects** ("They pass") are missed for the same reason.
- **Two false positives:** a hedge the negation window does not cover ("I wouldn't call it fully
  verified yet"), and a two-claim sentence where "tests pass" was attributed to `lint_clean`.

None of these was fixed after the held-out run: tuning on the held-out set would make its numbers
meaningless. The confusion matrix of reasons and the full error list are in
[docs/eval.md](https://github.com/B0yko/proof-of-done/blob/main/docs/eval.md).

</details>

## Configuration

Five layers, lowest precedence first:

1. packaged defaults;
2. the user file `${XDG_CONFIG_HOME:-~/.config}/proof-of-done/config.yaml`;
3. the project's `.proof-of-done.yaml`;
4. the file named by `PROOF_OF_DONE_CONFIG`;
5. the `PROOF_OF_DONE` and `PROOF_OF_DONE_MODE` environment variables.

Project rules merge into built-in ones by `id`; new ids add claim types.
`proof-of-done init` writes a starter file (`version: 1`, the detected ecosystem as a comment,
`rules: []`). Every key and the full command table:
[docs/config.md](https://github.com/B0yko/proof-of-done/blob/main/docs/config.md). Ready-made configs for Python, Node, Go, a monorepo and
custom `committed`/`pushed` rules are in [examples/](https://github.com/B0yko/proof-of-done/tree/main/examples).

| Escape hatch | Effect |
|---|---|
| `#skip-proof` in your own prompt | skips the check for that turn; the agent typing it has no effect |
| `PROOF_OF_DONE=off` · `enabled: false` | turns the hook off |
| `PROOF_OF_DONE_MODE=warn` · `mode: warn` | every block becomes a visible warning |
| `max_blocks_per_turn` (default 2) | after that many blocks in a row the stop goes through with a warning, below Claude Code's own default cap of 8 ([hooks reference](https://code.claude.com/docs/en/hooks)) |

### Tamper check

The hook scans the session's own edits *before* it applies `enabled`, `mode` or any rule `action`.

- **The session edited `.proof-of-done.yaml`, the user config or the `PROOF_OF_DONE_CONFIG`
  file:** four downgrades from that file are undone for the rest of the session (`enabled: false`,
  `mode: warn`, a lowered `action`, a raised `max_blocks_per_turn`).
- **The session edited a Claude Code `settings.json`/`settings.local.json` with text containing
  `PROOF_OF_DONE` or `enabledPlugins`:** `PROOF_OF_DONE=off` and `PROOF_OF_DONE_MODE=warn` from
  the environment are ignored.

A warning names the edited file, and tampering alone never blocks. What it does not undo is under
[Limitations](https://github.com/B0yko/proof-of-done#limitations).

## CLI

Get it with `uv tool install proof-of-done` (or `pipx install proof-of-done`), or run any command
through `uvx proof-of-done <command>`. The plugin install does not put it on your `PATH`.

```text
proof-of-done check --transcript PATH [--message TEXT | --message-file PATH] [--config PATH] [--json]
proof-of-done audit [PATH ...] [--claude-projects | --demo] [--source auto|claude-code|agent-trace|codex]
                    [--config PATH] [--format text|json|md] [--out FILE] [--export-traces FILE]
                    [--redact] [--salt S] [--ci] [--max-unsupported-rate R]
proof-of-done init [--force]
proof-of-done config show [--cwd DIR]
proof-of-done trace validate FILE
```

<details>
<summary>What each command does, and its exit codes</summary>

- **`check`** judges one final message against one transcript, without a live hook. Exit `0`:
  nothing to block (a warn-only result still prints its warning); `1`: an unsupported claim under
  a `block` action; `2`: a usage or config error.
- **`audit`** replays the Stop hook at every stop attempt of every session, and at each
  subagent's own transcript grouped under its parent, with the built-in defaults or `--config`.
  Exactly one input mode: `PATH`s (files, directories, globs), `--claude-projects`
  (`$CLAUDE_CONFIG_DIR/projects`, else `~/.claude/projects`) or `--demo`. `--source codex` reads
  OpenAI Codex CLI rollout logs (experimental, audit only). `--export-traces FILE` writes
  `agent-trace/v1` JSONL; `--redact` replaces quotes, commands, paths and session ids with a
  salted SHA-256 prefix plus length (`h:<12 hex>:len=<n>`), with a random salt per run unless you
  pass `--salt` or `PROOF_OF_DONE_SALT`. Exit `2`: a usage error or a missing or invalid
  `--config`; `3`: no input yielded a session (every file unreadable or unrecognised, including a
  file with lines but no parseable JSON line). With `--ci`, exit `1` when the unsupported-claim
  rate exceeds `--max-unsupported-rate` (a fraction, default `0`; ignored without `--ci`, so
  `audit --demo --ci` exits `1`), plus a Markdown summary in `$GITHUB_STEP_SUMMARY` when set. See
  `examples/ci/audit-agent-run.yml` for a GitHub Actions job shape (not run by this repository's
  CI: it needs a real headless agent run).
- **`init`** writes `.proof-of-done.yaml` in the current directory; it refuses to overwrite one
  without `--force` (exit `2`). The built-in rules already cover Python, Node, Go and Rust, so the
  file changes nothing until you add overrides.
- **`config show`** prints the effective merged configuration and the files it came from.
- **`trace validate`** checks an `agent-trace/v1` file against `schemas/agent-trace-v1.json`.

</details>

### Debugging a block

```sh
proof-of-done check --transcript path/to/session.jsonl --message "All tests pass."
proof-of-done config show
```

The hook logs to `${CLAUDE_PLUGIN_DATA}/proof-of-done.log` (or `${TMPDIR:-/tmp}/proof-of-done-<uid>/`
when that is unset): JSON Lines, capped at 256 KiB, never containing message text, quotes,
commands or file paths.

<details>
<summary>What the log records</summary>

Each stop past the prefilter writes one `decision` record with the final decision (after the
block cap; `capped: true` when the cap let a stop through), the claim count, the flags `skipped`,
`disabled`, `tampered` and `capped`, one entry per claim (`claim_type`, `rule_ids`, `supported`,
reason code, action) and per-phase `timings_ms`. A fail-open writes a `fail_open` record with a
reason code, and an unexpected exception an `error` record with only the exception class name.

</details>

## Privacy

The hook makes **no network call**, and a socket-blocking test fixture enforces that.

- **Reads** only local files: the event JSON on stdin; the session transcript (for
  `SubagentStop`, the subagent's transcript plus the parent's, for the skip token and the tamper
  scan); long tool outputs Claude Code stored inside that session's own directory; the config
  files; and, to suggest a `Run:` command, the project's `pyproject.toml`, `pytest.ini`,
  `package.json`, `go.mod`, `Cargo.toml`, `Makefile` and lockfiles.
- **Writes** only under `${CLAUDE_PLUGIN_DATA}` (or the temp fallback): block counters, the
  merged-config cache, Python's bytecode cache, the log, and on macOS a `.python-ok` marker when
  the interpreter is `/usr/bin/python3`. It never writes into your project.

## Alternatives

proof-of-done inspects the transcript deterministically, like claimcheck and done-needs-proof. It
never re-runs a command (unlike loop-hooks) and never calls a model (unlike Probity's validation
option and the official prompt-hook example). It also publishes a reproducible evaluation against
a frozen held-out set. This describes what exists; it is not a claim to be first.

<details>
<summary>Existing hooks and plugins, checked against their repositories on 2026-09-28</summary>

| Project | Mechanism | Licence · stars |
|---|---|---|
| [claimcheck](https://github.com/ablanchard-dev/claimcheck) | Checks each claim in the final message against tool output already in the same turn's transcript (verified / refuted / unverifiable); never re-runs commands. | GPL-3.0 · 0 |
| [done-needs-proof](https://github.com/sdvsignal/done-needs-proof) | Node `Stop` hook; blocks "done", "fixed", "deployed" or "tests pass" unless a matching verification command ran after the last edit. Keyword matching, fails open, fires once per stop; ships a `prove` skill. | MIT · 0 |
| [loop-hooks](https://github.com/wwwcojp/loop-hooks) | **Re-runs** the project's verification command on `Stop`, `SubagentStop` and `TeammateIdle`, gated by a git fingerprint of the watched files. | MIT · 0 |
| [tdd-guard](https://github.com/nizos/tdd-guard), successor [Probity](https://github.com/nizos/probity) | A process gate rather than a claim checker: blocks implementation edits without a failing test first; some checks call a validation model. The most adopted tool in this space. | MIT · 2,353 (tdd-guard), 216 (Probity) |
| [protect-tests](https://github.com/karanb192/claude-code-hooks/tree/main/plugins/protect-tests) | `PreToolUse` hook against "fake green": deleting, renaming away or skip/xfail-disabling tests instead of fixing the code. Deterministic patterns. | MIT · 526 (whole repo) |
| [dead-rules-audit](https://github.com/karanb192/claude-code-hooks/tree/main/plugins/dead-rules-audit) | Not a stop gate: scores edits against `CLAUDE.md`'s own rules and reports which are ignored. An adjacent pattern, listed for context. | MIT · 526 (whole repo) |
| [sonmat](https://github.com/jun0-ds/sonmat) | Verification discipline mostly through prompt guidance (in `CLAUDE.md` and injected into worker subagents), plus a commit guard; no transcript checking described. | BSD-3-Clause · 6 |
| Official [plugin-dev hook guide](https://github.com/anthropics/claude-code/blob/main/plugins/plugin-dev/skills/hook-development/SKILL.md) | Its `Stop` hook example is a `prompt` hook: a second model call judging "approve" or "block". Teaching material, not a maintained plugin. A read of the [claude-plugins-official](https://github.com/anthropics/claude-plugins-official) listing (314 entries, descriptions only) found none that describes verifying completion claims. | — |

</details>

## Limitations

- **Missed phrasings are never checked.** Type-check wording ("Types check"), state-style deploy
  claims ("it's live") and pronoun subjects ("They pass") are the main gaps; see Results.
- **English only.** A claim in another language is invisible to the detector.
- **Two known adversarial misses:** an echo-only Makefile `test:` target that prints a test-like
  summary, and `npm run test:*` scripts, trusted by name even when they start a watcher.
- **Only foreground Bash counts.** Background runs, MCP test runners, IDE diagnostics and the
  PowerShell tool are not evidence.
- **Edits outside the agent are invisible:** no cross-check with `git status` or file mtimes.
- **Subagents are judged separately.** A subagent call counts as an edit for every rule; its
  internals are not parsed, and its verdict is not reused for the parent.
- **A transcript that starts mid-history only warns**, because its evidence is known to be
  incomplete (its first message's `parentUuid` names an entry missing from the file).
- **The transcript format is internal to Claude Code,** pinned to 2.1.281
  ([docs/transcript-format.md](https://github.com/B0yko/proof-of-done/blob/main/docs/transcript-format.md)); on a changed format the hook fails
  open.
- **Tamper protection is partial.** It undoes only the four downgrades above. Other keys from an
  edited config still apply, including `max_transcript_mb`, `check_subagents` and
  `subagent_skip_types` (read before the transcript is parsed) and a rule's `claims` and
  `keywords`; an invalid config fails open with a warning.
- **Out of scope:** generic "done" claims, external state no command can show ("the email was
  sent"), Windows, and a Codex hook (the Codex adapter is experimental and audit-only).

## Uninstall

```sh
claude plugin uninstall proof-of-done@proof-of-done   # remove it
claude plugin disable proof-of-done                   # keep it, turn it off
```

Or set `PROOF_OF_DONE=off` for Claude Code's hook processes, or `enabled: false` in
`.proof-of-done.yaml`.

## Roadmap

- Reuse a subagent's checked verdict as evidence for its parent
- Evidence from MCP test runners and IDE diagnostics
- Pin the Codex adapter against a verified rollout log

## Licence

Apache-2.0, see [LICENSE](https://github.com/B0yko/proof-of-done/blob/main/LICENSE). Copyright 2026 Andrii Boiko. All fixtures and evaluation
sets are synthetic, written for this repository and licensed with the code
([fixtures/README.md](https://github.com/B0yko/proof-of-done/blob/main/fixtures/README.md)). The only vendored code is a pure-Python copy of
**PyYAML 6.0.3** (MIT, licence kept in `src/proof_of_done/_vendor/yaml/LICENSE`), so the hook needs
no install step ([ADR 3](https://github.com/B0yko/proof-of-done/blob/main/docs/adr/0003-stdlib-only-hook-vendored-yaml.md)).
