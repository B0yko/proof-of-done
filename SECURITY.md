# Security policy

## Reporting a vulnerability

Please report security issues through GitHub's private vulnerability reporting for this
repository (the repository's **Security** tab → **Report a vulnerability**), rather than a
public issue. This opens a private advisory visible only to the maintainer and you, so a fix can
land before any details are public.

## Threat model

`proof-of-done` runs as a `Stop`/`SubagentStop` hook: a short-lived subprocess Claude Code
invokes on your machine, reading a transcript file Claude Code itself already wrote to your
disk.

- **No network access.** The hook makes no outbound connection of any kind. A test fixture
  blocks socket creation during the test suite specifically to enforce this for the whole
  codebase, not just the hook path.
- **Reads only local files it is told about**: the Stop-event JSON on stdin, the session
  transcript file it names, the merged YAML configuration layers (built-in defaults, your user
  config, the project's `.proof-of-done.yaml`, `$PROOF_OF_DONE_CONFIG`), and, for suggestion
  purposes only, a handful of well-known project files (`pyproject.toml`, `package.json`,
  `go.mod`, `Cargo.toml`, a `Makefile`) at the project root.
- **YAML parsing uses only `safe_load`** (a vendored, pure-Python PyYAML, `src/proof_of_done/_vendor/`),
  never an unsafe loader, so a malicious or malformed config file cannot cause arbitrary Python
  object construction.
- **Fails open.** Every unreadable, oversized, or malformed input — a corrupt transcript line, a
  transcript over the configured size cap, invalid YAML, an internal exception — results in the
  hook allowing the stop, optionally with a short diagnostic message, never a hang and never a
  crash visible to the agent session. See `docs/adr/0004-fail-open.md`.
- **Writes only inside its own data directory** (`${CLAUDE_PLUGIN_DATA}` or a per-user temp
  fallback created with mode 0700, whose owner and permissions are checked before it is used
  for cached bytecode): consecutive-block counters, a merged-config cache, and a size-capped
  diagnostic log that never contains message text, shell commands, or file paths (see the
  README's Privacy section).
- **Shell command parsing (`shell.py`) is read-only analysis**, never execution: it tokenizes and
  classifies commands the agent already ran, to decide whether they count as evidence. It does
  not execute, retry, or modify any command.

## Out of scope

This tool does not sandbox, restrict, or review what the agent itself is allowed to do — it only
checks, after the fact, whether a *claim in the agent's own final message* is backed by evidence
already in the transcript. It is not a permissions system, a code-execution sandbox, or a
guarantee that an agent's actions were safe; that is Claude Code's own permission model, not this
plugin's job.
