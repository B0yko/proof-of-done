"""`plugin.json`, the marketplace's own entry for the plugin, and `pyproject.toml` must all
agree with `proof_of_done.__version__`, or a release can silently ship an inconsistent set."""

from __future__ import annotations

import json
import os
import re

from proof_of_done import __version__

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_json(*parts: str) -> dict:
    with open(os.path.join(REPO_ROOT, *parts), encoding="utf-8") as fh:
        return json.load(fh)


def test_plugin_json_version_matches_package_version() -> None:
    plugin = _load_json(".claude-plugin", "plugin.json")
    assert plugin["version"] == __version__
    assert plugin["name"] == "proof-of-done"


def test_marketplace_plugin_entry_version_matches_package_version() -> None:
    marketplace = _load_json(".claude-plugin", "marketplace.json")
    entries = [p for p in marketplace["plugins"] if p["name"] == "proof-of-done"]
    assert len(entries) == 1
    assert entries[0]["version"] == __version__


def test_pyproject_version_matches_package_version() -> None:
    # A small regex read instead of a TOML parser: `tomllib` is 3.11+ only and this repo's
    # CI matrix runs tests on 3.9 too, where pulling in a `tomli` dependency just for this
    # one assertion is not worth it.
    pyproject_path = os.path.join(REPO_ROOT, "pyproject.toml")
    with open(pyproject_path, encoding="utf-8") as fh:
        text = fh.read()
    project_section = text.split("[project]", 1)[1].split("\n[", 1)[0]
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', project_section)
    assert match is not None, "pyproject.toml [project] has no version key"
    assert match.group(1) == __version__


def test_hooks_json_command_and_timeout_match_the_documented_contract() -> None:
    hooks = _load_json("hooks", "hooks.json")
    for event_name, event_cmd in (("Stop", "stop"), ("SubagentStop", "subagent-stop")):
        entry = hooks["hooks"][event_name][0]["hooks"][0]
        assert entry["type"] == "command"
        assert entry["timeout"] == 10
        assert event_cmd in entry["command"]
        assert "${CLAUDE_PLUGIN_ROOT}" in entry["command"]
        assert "${CLAUDE_PLUGIN_DATA}" in entry["command"]
        assert "bin/proof-of-done-hook" in entry["command"]
    # Stop has no matcher support (research/contracts.md, section C); the plugin manifest must
    # not declare one.
    assert "matcher" not in hooks["hooks"]["Stop"][0]
