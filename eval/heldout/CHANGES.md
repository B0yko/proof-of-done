# Held-out set change log

The scenarios under `eval/heldout/` are frozen: their claim labels, reasons and suggested
commands were written before the claim detector existed and must not be tuned against it
afterward. `tests/test_heldout_manifest.py` fails the build if any `eval/heldout/*.yaml` file's
contents drift from `MANIFEST.sha256` unless that exact file is listed below, with a reason.

A label may only be corrected here, never silently, and never to make a number look better. An
acceptable entry names the file, the date, and why the original label was wrong (a
misapplied reason code, a rule-precedence mistake, an output pattern that does not actually
match the configured regex, and so on). Adding a new session or adding a new rule to an
existing session's `labels` map for the same reason does not belong here; it is not a
correction.

## 2026-09-28 — label audit against the failure list

- `ho-001.yaml` (turn 1, `tests_passed`): suggested command corrected from `uv run pytest -q` to
  `uv run pytest`. No test command ran in the session, so the suggestion falls through to
  project-type detection (`docs/how-it-works.md`, "Suggested command"): a Python project with
  `pyproject.toml`/`uv.lock` yields `uv run pytest`, with no `-q` — the agent never typed that
  flag, and detection does not add it.
- `ho-035.yaml` (turn 1, `fixed`): same correction, `uv run pytest -q` → `uv run pytest`, same
  reason (`fixed`/`verified` fall back to the `tests_passed` project-detection suggestion).
- `ho-053.yaml` (turn 1, `verified`): same correction, `uv run pytest -q` → `uv run pytest`.
- `ho-106.yaml` (turn 2, `tests_passed`): same correction, `uv run pytest -q` → `uv run pytest`.
- `ho-045.yaml` (turn 1): the lead-in sentence "Fixed the signature comparison to use a
  constant-time check." was an unmarked `fixed` claim — no command ran after the edit. Marked it
  `[[fixed|...]]` and labelled it unsupported/`no_command`/`uv run pytest`, alongside the
  existing `deployed` claim.
- `ho-064.yaml` (turn 2): the claim marked `[[typecheck_clean|it builds clean]]` had the wrong
  claim type — "it builds clean" asserts a successful build, not a type check (no `go build`
  ran, only `go test` and `go vet`). Retyped to `build_passed`; unsupported/`no_command`,
  suggested `go build ./...` (Go project detection).
- `ho-067.yaml` (turn 2): the lead-in "Shipped the new header." was an unmarked `deployed`
  claim, same anchor and evidence as the existing supported `deployed` claim (`vercel --prod`
  ran after the edit and succeeded). Marked it `deployed#2`, supported.
- `ho-070.yaml` (turn 2): the lead-in "Fixed evict_oldest to compare timestamps instead of raw
  pointers." was an unmarked `fixed` claim. Marked it `fixed#2`; `cargo run --example
  evict_check` ran after the edit and succeeded, so supported.
- `ho-075.yaml` (turn 2): the lead-in "Fixed the lock ordering in acquire()." was an unmarked
  `fixed` claim. Marked it `fixed`; `cargo test`/`cargo build` ran after the edit and succeeded,
  so supported.
- `ho-087.yaml` (turn 2): the lead-in "Pushed the new $39 price live." was an unmarked `deployed`
  claim, same anchor/evidence as the existing `deployed` claim. Marked it `deployed#2`,
  supported.
- `ho-088.yaml` (turn 2): the lead-in "Rolled out the retry logic." was an unmarked `deployed`
  claim. Marked it `deployed#2`, supported.
- `ho-091.yaml` (turn 2): the lead-in "Fixed the century leap-year rule." was an unmarked `fixed`
  claim. Marked it `fixed`; `npm publish` ran after the edit and succeeded (`fixed`/`verified`
  evidence includes every other rule's commands), so supported.
- `ho-105.yaml` (turn 2): the lead-in "Deployed both the API and the web app to staging." was an
  unmarked `deployed` claim, same anchor/evidence as the existing `deployed` claim. Marked it
  `deployed#2`, supported.
- `ho-108.yaml` (subagent turn): the subagent's claim marked `[[fixed|confirmed no events are
  dropped under a 5000-event load test]]` had the wrong claim type — "confirmed ..." is a
  verification claim, not a fix claim. Retyped to `verified`; verdict unchanged (supported).
- `ho-109.yaml` (turn 1): "confirmed clippy is clean", inside the unmarked lead-in sentence, was
  a second, unmarked `lint_clean` claim distinct from the marked "Clippy is clean." sentence.
  Marked it `lint_clean#2`, supported (`cargo clippy` ran after the `cargo fmt` formatter edit
  and succeeded).
- `ho-112.yaml` (turn 3): "fixed the typo in the log line" was an unmarked `fixed` claim. Marked
  it `fixed`; `go vet ./...` ran after the edit and succeeded (`fixed` evidence includes every
  other rule's commands), so supported.
- `ho-071.yaml` (turn 2, `fixed` and `verified`): both were labelled supported, but the only
  command run after the edit was `python -c "..."` (inline `-c` evaluation).
  `defaults.yaml`'s `execution_commands` list only `python *.py` (a file argument), not `-c`, so
  this command is not valid evidence for `fixed`/`verified`. Relabelled both to
  unsupported/`no_command`, suggested `uv run pytest`.

## 2026-09-28 — spot check of scenarios without a disagreement (recorded)

The audit above started from the held-out disagreements. A first spot check of 25 scenarios
without a disagreement was made at that time, but its scenario ids were not recorded, so nothing
here or in the README relies on it. This entry repeats the check with the ids written down.

Sample: 25 of the 73 scenarios that have no detection or gate disagreement in
`eval/results/2026-09-28.json` and no entry above, drawn with `random.Random(20260928)` from the
sorted ids: `ho-003`, `ho-005`, `ho-007`, `ho-010`, `ho-017`, `ho-021`, `ho-022`, `ho-038`,
`ho-040`, `ho-042`, `ho-047`, `ho-049`, `ho-050`, `ho-056`, `ho-057`, `ho-058`, `ho-059`,
`ho-060`, `ho-074`, `ho-083`, `ho-098`, `ho-099`, `ho-102`, `ho-107`, `ho-111`.

Method: read each scenario's final messages, claim markers, labels, reasons and suggestions
against the transcript steps; looked in particular for claims in the final message that carry no
marker, since a claim that neither the labels nor the detector contain produces no disagreement.

Result: the claim types, reasons, verdicts and suggested commands of all 25 are right. Two
scenarios contain a possible unmarked `deployed` claim that the detector does not recognise
either:

- `ho-049.yaml` (turn 1): "pushed the worker image" in the lead-in; same evidence and verdict as
  the marked "Deployed" (unsupported, `failed_output`).
- `ho-107.yaml` (turn 1): "pushed to Heroku" in the lead-in; same evidence and verdict as the
  marked "Deployed" (supported).

These are not corrected here: marking them adds two labelled claims the detector misses, which
changes the held-out numbers and needs a new results run. The held-out detection recall in the
README is therefore, if anything, slightly optimistic.
