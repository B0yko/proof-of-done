"""Shared helpers for the hook contract/isolation/fast-path tests: loading the exact command
string from `hooks/hooks.json`, filling payload templates from `fixtures/payloads/`, and
rendering small scenarios for them to point at. Not a test module itself (no `test_` prefix);
pytest never collects it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOKS_JSON = os.path.join(REPO_ROOT, "hooks", "hooks.json")
LAUNCHER = os.path.join(REPO_ROOT, "bin", "proof-of-done-hook")
PAYLOADS_DIR = os.path.join(REPO_ROOT, "fixtures", "payloads")


def hook_command(event_name: str) -> str:
    """The exact `command` string `hooks/hooks.json` registers for `event_name`
    (``"Stop"``/``"SubagentStop"``)."""
    with open(HOOKS_JSON, encoding="utf-8") as fh:
        data = json.load(fh)
    return str(data["hooks"][event_name][0]["hooks"][0]["command"])


def hook_timeout(event_name: str) -> int:
    with open(HOOKS_JSON, encoding="utf-8") as fh:
        data = json.load(fh)
    return int(data["hooks"][event_name][0]["hooks"][0]["timeout"])


def load_payload_template(name: str) -> str:
    """The raw (unparsed) text of `fixtures/payloads/<name>.json`, for placeholder
    substitution before `json.loads`."""
    with open(os.path.join(PAYLOADS_DIR, name + ".json"), encoding="utf-8") as fh:
        return fh.read()


def fill_payload(name: str, **substitutions: str) -> dict[str, Any]:
    text = load_payload_template(name)
    for key, value in substitutions.items():
        text = text.replace("__" + key.upper() + "__", value)
    return dict(json.loads(text))


def run_hook_command(
    payload: dict[str, Any],
    *,
    event_name: str = "Stop",
    data_dir: str,
    env_extra: dict[str, str] | None = None,
    timeout: float | None = 10,
) -> subprocess.CompletedProcess[str]:
    """Pipe `payload` into the exact `hooks/hooks.json` command for `event_name`, via
    ``sh -c``, with `CLAUDE_PLUGIN_ROOT`/`CLAUDE_PLUGIN_DATA` set for substitution."""
    command = hook_command(event_name)
    env = dict(os.environ)
    env["CLAUDE_PLUGIN_ROOT"] = REPO_ROOT
    env["CLAUDE_PLUGIN_DATA"] = data_dir
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["sh", "-c", command],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )


def render_inline_scenario(scenario: dict[str, Any], out_dir: str) -> Any:
    """Validate and render a scenario mapping built directly in a test (no committed YAML
    file), reusing `fixtures.dsl`/`fixtures.render`."""
    if REPO_ROOT not in sys.path:
        sys.path.insert(0, REPO_ROOT)
    from fixtures import dsl, render

    validated = dsl.validate_scenario(scenario, "inline")
    return render.render(validated, out_dir)


__all__ = [
    "LAUNCHER",
    "REPO_ROOT",
    "fill_payload",
    "hook_command",
    "hook_timeout",
    "load_payload_template",
    "render_inline_scenario",
    "run_hook_command",
]
