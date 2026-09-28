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

## 2026-09-28 — label audit against the failure list and a 25-scenario spot check

- `ho-001.yaml` (turn 1, `tests_passed`): suggested command corrected from `uv run pytest -q` to
  `uv run pytest`. No test command ran in the session, so the suggestion falls through to
  project-type detection (PLAN §7.3): a Python project with `pyproject.toml`/`uv.lock` yields
  `uv run pytest`, with no `-q` — the agent never typed that flag, and detection does not add it.
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
  command run after the edit was `python -c "..."` (inline `-c` evaluation). PLAN §15 /
  `defaults.yaml`'s `execution_commands` list only `python *.py` (a file argument), not `-c`, so
  this command is not valid evidence for `fixed`/`verified`. Relabelled both to
  unsupported/`no_command`, suggested `uv run pytest`.
