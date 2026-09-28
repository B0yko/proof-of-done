# Contributing

## Development setup

Requires [uv](https://docs.astral.sh/uv/).

```sh
git clone https://github.com/B0yko/proof-of-done
cd proof-of-done
uv sync --python 3.12 --locked
```

Run the checks CI runs, in order:

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest -q
```

CI also runs this matrix against Python 3.9, 3.12 and 3.13 on `ubuntu-latest` and
`macos-latest`. The hook path (`hook.py`, `claims.py`, `shell.py`, `evidence.py`, `config.py`,
`suggest.py`, `message.py`, `paths.py`, `logutil.py`, `counters.py`, `tamper.py`, `turns.py`) is
**stdlib-only** — no import outside the standard library or the vendored YAML copy under
`src/proof_of_done/_vendor/`. `tests/test_hook_isolation.py` runs the hook's entry
point under `python3 -I -S` (no site-packages) to enforce this; a change that adds a third-party
import to any hook-path module will fail that test, not just review.

`ruff` targets `py39` and `mypy --strict` runs against `python_version = "3.9"`, both excluding
`_vendor/`: no `match` statements, no `X | Y` union syntax at runtime, no `list[int]` outside
annotations, no `dataclass(slots=True)`, no `str.removeprefix` — every module (except
`hooks/entry.py`, which targets Python 2.7 syntax on purpose, and the vendored YAML copy) starts
with `from __future__ import annotations`.

## Repository layout

- `src/proof_of_done/` — the package: `cli.py`, `hook.py`, `claims.py`, `shell.py`,
  `evidence.py`, `suggest.py`, `message.py`, `config.py` (+ `defaults.yaml`), `transcript/`
  (per-format adapters), `audit.py`, `report.py`, `traces.py`, `redact.py`, `_vendor/`.
- `hooks/`, `bin/`, `.claude-plugin/` — the Claude Code plugin itself (hook manifest, POSIX `sh`
  launcher, plugin/marketplace manifests).
- `fixtures/` — the scenario DSL (`dsl.py`, `render.py`), hand-written scenarios, and the
  templated-set expander (`expand.py` + `templates/`). Not part of the installed wheel; `tests/`
  and `eval/` import it via a `sys.path` insertion of the repo root.
- `eval/` — the evaluation harness (`run_eval.py`, `latency.py`, `metrics.py`), the frozen
  held-out set (`heldout/`), and the adversarial set (`adversarial/`).
- `schemas/agent-trace-v1.json`, `docs/`, `examples/`, `scripts/`, `tests/`.

## How to add a rule

Built-in rules live in `src/proof_of_done/defaults.yaml`; project-specific rules go in your own
`.proof-of-done.yaml` (see `docs/config.md` for every field, and `examples/extra-rules.yaml` for
a worked example adding `committed`/`pushed` claim types). A minimal custom rule:

```yaml
rules:
  - id: my-check
    claim_type: my_check_passed          # must match ^[a-z][a-z0-9_]*$
    action: block                        # block | warn | "off"
    keywords: [mycheck]                  # required for a non-built-in rule; feeds the prefilter
    claims:
      - '(?i)\bmy[- ]?check\b.{0,20}\b(?P<pred>passe?d|clean|green)\b'
    evidence:
      commands: ["my-check-cli"]
    relevant_files: ["**"]
    suggest: "my-check-cli --fix"
```

If you are changing a **built-in** rule's evidence commands, output patterns, or keywords in
`defaults.yaml`, add or update a case in `fixtures/templates/templates.yaml` (expanded by
`fixtures/expand.py`) covering the change, and run the full eval harness (below) to see the
effect on the templated numbers before opening a PR — never on the held-out set, which is frozen
(next section).

Every claim detector regex change should keep the fast-path invariant: a test in
`tests/test_claims.py` asserts every labelled claim in the templated and adversarial sets
contains at least one of its rule's `keywords`. A held-out prefilter miss (real phrasing the
keyword list does not catch) belongs in the evaluation's failure analysis, not in a keyword-list
change made just to fix that one held-out case.

## The held-out set is frozen

`eval/heldout/*.yaml` (112 sessions, individually authored before `claims.py` existed) was
committed at `eval: freeze held-out set` and must never be tuned against. `eval/heldout/MANIFEST.sha256`
pins every file's hash; `tests/test_heldout_manifest.py` fails if any file's contents drift from
that manifest without a matching entry in `eval/heldout/CHANGES.md`.

A genuine label error (a misapplied reason code, an output pattern that does not actually match
its configured regex, a rule-precedence mistake) may be corrected, but only with a
`CHANGES.md` entry naming the file, the date and the reason — never silently, and never to make a
number look better. Adding a new session, or a new rule's label to an existing session, is not a
"correction" and does not go in `CHANGES.md`; it is ordinary held-out-set growth, still subject to
never being informed by what `claims.py` currently does or does not catch.

## Running the evaluation

```sh
uv run python eval/run_eval.py            # writes eval/results/<date>.json
uv run python eval/latency.py --generate --run   # writes eval/results/latency-<date>.json
uv run python scripts/render_results.py    # splices both into README.md and docs/eval.md
uv run python scripts/render_results.py --check   # CI: fails if the tables have drifted
```

`eval/run_eval.py --compare <path>` re-runs the harness and diffs every metric (timestamp, git
SHA and latency excluded) against a previously committed results file — this is what CI runs
against the latest committed `eval/results/<date>.json` on every push.

## Commit style

Conventional-commit messages (`feat:`, `fix:`, `test:`, `docs:`, `refactor:`, `chore:`, `eval:`
for fixture/dataset changes), small commits, no trailers.
