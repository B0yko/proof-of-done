#!/usr/bin/env python3
"""Render the evidence-command table in docs/config.md from defaults.yaml.

The table between the ``<!-- evidence-table:start -->`` / ``<!-- evidence-table:end -->``
markers in docs/config.md is generated, not hand-maintained, so it can never drift from the
shipped defaults. Run with no arguments to (re)write it in place; run with ``--check`` to
verify it is already up to date (exits 1 and prints a diff-free message if it is stale).
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from typing import Any

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC_ROOT = os.path.join(_REPO_ROOT, "src")
if _SRC_ROOT not in sys.path:
    sys.path.insert(0, _SRC_ROOT)

from proof_of_done import yamlload  # noqa: E402

START_MARKER = "<!-- evidence-table:start -->"
END_MARKER = "<!-- evidence-table:end -->"

DEFAULTS_PATH = os.path.join(_SRC_ROOT, "proof_of_done", "defaults.yaml")
DOC_PATH = os.path.join(_REPO_ROOT, "docs", "config.md")


def _load_defaults() -> dict[str, Any]:
    with open(DEFAULTS_PATH, encoding="utf-8") as handle:
        data = yamlload.safe_load(handle.read())
    if not isinstance(data, dict):
        raise SystemExit(f"{DEFAULTS_PATH}: top level must be a mapping")
    return data


def _format_commands(commands: list[str]) -> str:
    if not commands:
        return "*(every other enabled rule's commands, plus `execution_commands`)*"
    return ", ".join(f"`{cmd}`" for cmd in commands)


def render_table(data: dict[str, Any]) -> str:
    lines = [
        "| rule id | claim type | default action | evidence commands (prefixes) |",
        "|---|---|---|---|",
    ]
    for rule in data.get("rules", []):
        commands = rule.get("evidence", {}).get("commands", [])
        lines.append(
            f"| `{rule['id']}` | `{rule['claim_type']}` | {rule['action']} | "
            f"{_format_commands(commands)} |"
        )
    return "\n".join(lines)


def generated_section() -> str:
    return f"{START_MARKER}\n{render_table(_load_defaults())}\n{END_MARKER}"


def _splice(doc: str) -> str:
    if START_MARKER not in doc or END_MARKER not in doc:
        raise SystemExit(f"{DOC_PATH} is missing the {START_MARKER} / {END_MARKER} markers")
    before, rest = doc.split(START_MARKER, 1)
    _old, after = rest.split(END_MARKER, 1)
    return before + generated_section() + after


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if docs/config.md's evidence table is stale, instead of rewriting it",
    )
    args = parser.parse_args(argv)

    with open(DOC_PATH, encoding="utf-8") as handle:
        doc = handle.read()
    new_doc = _splice(doc)

    if args.check:
        if new_doc != doc:
            print(
                f"{DOC_PATH}: evidence table is stale; run `python scripts/render_config_doc.py`",
                file=sys.stderr,
            )
            return 1
        return 0

    if new_doc != doc:
        with open(DOC_PATH, "w", encoding="utf-8") as handle:
            handle.write(new_doc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
