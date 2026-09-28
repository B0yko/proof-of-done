# 3. The hook path is stdlib-only, with a vendored YAML parser

Status: accepted.

## Context

The Stop hook runs as a subprocess Claude Code launches on every turn's end. It must work with
no install step: whatever `python3` is already on the machine, including the macOS system
interpreter, which has no third-party packages and no permission to install any without
triggering the Xcode Command Line Tools dialog. It also needs to read YAML configuration
(`defaults.yaml`, the project's `.proof-of-done.yaml`, the user's config file), and the obvious
library for that, PyYAML, is a third-party package.

## Decision

Everything the hook imports (`hook.py`, `claims.py`, `shell.py`, `evidence.py`, `config.py`,
`suggest.py`, `message.py`, `paths.py`, `logutil.py`, `counters.py`, `tamper.py`, `turns.py`) is
stdlib-only. YAML parsing comes from a vendored, pure-Python copy of PyYAML's `safe_load` path
under `src/proof_of_done/_vendor/yaml/` (PyYAML 6.0.3, MIT-licensed, its `LICENSE` file kept
alongside it), not an installed dependency. A test runs the hook's entry point under
`python3 -I -S` (isolated mode, no site-packages) and asserts no third-party import is reachable
from that path, so a future change that accidentally imports something outside the stdlib or the
vendored copy fails CI rather than failing silently on someone's machine.

The CLI package (`proof-of-done`, installed via `uvx`/`pip`) is a different story: it declares
`jsonschema` as a regular dependency, used only by `traces.py` (agent-trace/v1 export and
validation) and imported lazily so the hook path never pays for it even when the CLI package is
installed.

## Consequences

- No install step, no virtualenv, no network access needed for the hook to function — it runs
  under a bare `python3 -I`.
- The vendored copy has to be kept in sync with upstream PyYAML manually; it is excluded from
  ruff's line-length/lint rules and from mypy (`exclude = ["src/proof_of_done/_vendor"]"`) since
  it is third-party code kept verbatim, not this project's own style.
- Only `safe_load` is used (never `yaml.load` with an unsafe loader), so a malicious
  `.proof-of-done.yaml` cannot execute arbitrary Python through YAML tag deserialization.
- Two YAML consumers (the vendored copy on the hook path, and the same vendored copy from the
  CLI, since the CLI package also uses it rather than pulling in real PyYAML as a second
  dependency) means one behaviour to reason about, not two.
