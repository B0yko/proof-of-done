# Changelog

All notable changes to this project are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.1.1] - 2026-09-28

First release on PyPI (`uvx proof-of-done`, `uv tool install proof-of-done`).

### Changed

- README redesign: banner, badges, highlights, claim-type and verdict-reason tables, an
  at-a-glance results table generated from the committed results, detailed tables in collapsible
  sections, and absolute links so the page also renders on PyPI.
- The terminal recording uses the same palette as the banner; a social preview image is added.

### Fixed

- The hook-timeout contract test kills the whole process group, so it also holds where `sh -c`
  forks instead of exec-ing (dash on Ubuntu).

## [0.1.0] - 2026-09-28

Initial release.

### Added

- `Stop`/`SubagentStop` Claude Code hook that checks a coding agent's completion claims
  ("tests pass", "the build succeeds", "lint is clean", "fixed", "deployed", "verified") against
  its own session transcript, deterministically, with no LLM call anywhere in the product.
- Deterministic claim detector (`claims.py`): negation, hedging, future-tense, question and
  instruction filtering; fenced-code, inline-code, block-quote and quoted-user-text masking;
  English-only.
- Evidence engine (`evidence.py`): anchor/candidate/verdict judging with a closed set of reason
  codes, shell command segmenting and masking analysis (`shell.py`), formatter and whole-tree
  git-operation edit detection, exemption globs for doc-only changes.
- Actionable block messages with a suggested `Run:` command, derived from the agent's own prior
  commands, a rule's configured suggestion, or cheap project-ecosystem detection.
- YAML configuration with five merge layers (built-in defaults, user config, project config,
  the `PROOF_OF_DONE_CONFIG` file, environment), hand-written validation, and `examples/` for Python, Node, Go and monorepo
  projects.
- Escape hatches and loop safety: a configurable skip token, `PROOF_OF_DONE=off`, `mode: warn`,
  a consecutive-block cap below Claude Code's own default cap of 8, and a tamper check that stops
  a session from quietly disabling the hook through its own config or settings edits.
- Fail-open behaviour throughout: an unreadable transcript, invalid config, oversized transcript,
  missing/too-old Python, or internal error always allows the stop.
- `proof-of-done` CLI: `check`, `audit` (with `--redact`, `--export-traces`, `--ci`, and a
  `--source` covering Claude Code JSONL, `agent-trace/v1`, and an experimental, audit-only Codex
  CLI adapter), `init`, `config show`, `trace validate`.
- `agent-trace/v1` JSON Schema (`schemas/agent-trace-v1.json`) and export/validation
  (`traces.py`); the same format is read and written by
  [agent-claimcheck](https://github.com/B0yko/agent-claimcheck) and
  [booking-truth](https://github.com/B0yko/booking-truth).
- Bundled 30-session synthetic demo corpus (`proof-of-done audit --demo`) across Python, Node,
  Go and Rust projects.
- Evaluation harness (`eval/run_eval.py`, `eval/latency.py`) reporting claim-detection and gate
  precision/recall/F1 with Wilson/bootstrap intervals, adversarial resistance, suggestion
  accuracy, hook latency and audit throughput, against templated, held-out and adversarial
  fixture sets — see `docs/eval.md` and the README's Results section.
- Claude Code plugin and marketplace manifests at the repository root
  (`.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`).
