"""The tamper check runs before `enabled`, `mode` or any rule `action` from a file is believed.

Every test here pipes a payload into the real launcher (the exact `hooks/hooks.json` command),
in a scratch project, with HOME and the config directories pointed at scratch directories. The
session edits a config or settings file, and the same content is on disk, exactly as it would be
after a real agent's edit. A session that switches its own checks off must still be blocked,
with the tamper note in the reason; the same setting made before the session is the user's
choice and must exit silently.
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest
from hook_helpers import fill_payload, render_inline_scenario, run_hook_command

pytestmark = pytest.mark.skipif(os.name != "posix", reason="the launcher is POSIX sh")

_CONFIG = ".proof-of-done.yaml"
_SETTINGS = ".claude/settings.json"
_SETTINGS_OFF = '{\n  "env": {"PROOF_OF_DONE": "off"}\n}\n'


def _scenario(root: str, writes: dict[str, str]) -> dict[str, Any]:
    steps: list[dict[str, Any]] = [
        {"edit": {"tool": "Edit", "path": "src/app/core.py", "old": "old_value", "new": "new"}}
    ]
    for path, content in writes.items():
        steps.append({"edit": {"tool": "Write", "path": path, "content": content}})
    return {
        "id": "hook-tamper",
        "ecosystem": "python",
        "root": root,
        "project_files": {"pyproject.toml": "[project]\nname = 'demo'\n", "uv.lock": None},
        "turns": [
            {
                "user": "Fix the parsing bug and confirm the tests.",
                "steps": steps,
                "final": "[[tests_passed|All tests pass]].",
                "labels": {
                    "tests_passed": {
                        "label": "unsupported",
                        "reason": "no_command",
                        "suggest": "uv run pytest -q",
                    }
                },
            }
        ],
    }


class _Run:
    def __init__(self, tmp_path) -> None:
        self.tmp_path = tmp_path
        self.root = tmp_path / "project"
        self.root.mkdir()
        (self.root / ".git").mkdir()
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.data_dir = str(tmp_path / "data")

    def write_file(self, rel: str, content: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def payload(self, session_writes: dict[str, str]) -> dict[str, Any]:
        out = self.tmp_path / "rendered"
        out.mkdir(exist_ok=True)
        rendered = render_inline_scenario(_scenario(str(self.root), session_writes), str(out))
        case = rendered.stop_cases[0]
        return fill_payload(
            "stop_with_last_message",
            transcript_path=case.session_path,
            cwd=str(self.root),
            scratchpad_dir=str(self.tmp_path),
            last_assistant_message=case.final_message,
        )

    def run(
        self,
        payload: dict[str, Any],
        env: dict[str, str] | None = None,
        stop_hook_active: bool = False,
    ) -> dict[str, Any]:
        payload = dict(payload, stop_hook_active=stop_hook_active)
        env_extra = {
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "CLAUDE_CONFIG_DIR": str(self.home / ".claude"),
            "PROOF_OF_DONE": "",
            "PROOF_OF_DONE_MODE": "",
            "PROOF_OF_DONE_CONFIG": "",
        }
        env_extra.update(env or {})
        result = run_hook_command(payload, data_dir=self.data_dir, env_extra=env_extra)
        assert result.returncode == 0
        assert result.stderr == ""
        return dict(json.loads(result.stdout)) if result.stdout else {}


def _edited_by_session(run: _Run, writes: dict[str, str], env=None) -> dict[str, Any]:
    """Session writes `writes` (and they are on disk); returns the hook's output."""
    for rel, content in writes.items():
        run.write_file(rel, content)
    return run.run(run.payload(writes), env=env)


def _note_text(body: dict[str, Any]) -> str:
    return str(body.get("reason", "")) + "\n" + str(body.get("systemMessage", ""))


# --------------------------------------------------------------------------------------
# the session edits its own configuration: every downgrade is ignored
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("content", "note"),
    [
        ("version: 1\nenabled: false\n", "'enabled: false'"),
        ('version: 1\nrules:\n  - id: tests\n    action: "off"\n', "lowered action"),
        ("version: 1\nmode: warn\n", "'mode: warn'"),
        ("version: 1\nmax_blocks_per_turn: 100\n", "max_blocks_per_turn"),
    ],
    ids=["enabled-false", "tests-action-off", "mode-warn", "max-blocks-raised"],
)
def test_project_config_edited_by_the_session_still_blocks(tmp_path, content, note) -> None:
    run = _Run(tmp_path)
    body = _edited_by_session(run, {_CONFIG: content})
    assert body.get("decision") == "block"
    assert "All tests pass" in body["reason"]
    assert note in _note_text(body)


def test_raised_max_blocks_per_turn_from_an_edited_config_is_ignored(tmp_path) -> None:
    run = _Run(tmp_path)
    writes = {_CONFIG: "version: 1\nmax_blocks_per_turn: 100\n"}
    run.write_file(_CONFIG, writes[_CONFIG])
    payload = run.payload(writes)
    assert run.run(payload, stop_hook_active=False)["decision"] == "block"
    assert run.run(payload, stop_hook_active=True)["decision"] == "block"
    third = run.run(payload, stop_hook_active=True)
    assert "decision" not in third
    assert "consecutive blocks" in third["systemMessage"]


def test_settings_edit_setting_proof_of_done_off_beats_the_environment(tmp_path) -> None:
    run = _Run(tmp_path)
    body = _edited_by_session(run, {_SETTINGS: _SETTINGS_OFF}, env={"PROOF_OF_DONE": "off"})
    assert body.get("decision") == "block"
    assert "ignored PROOF_OF_DONE=off" in _note_text(body)


def test_settings_edit_with_mode_warn_in_the_environment_still_blocks(tmp_path) -> None:
    run = _Run(tmp_path)
    body = _edited_by_session(run, {_SETTINGS: _SETTINGS_OFF}, env={"PROOF_OF_DONE_MODE": "warn"})
    assert body.get("decision") == "block"
    assert "ignored PROOF_OF_DONE_MODE=warn" in _note_text(body)


def test_tampering_alone_never_blocks_a_supported_claim(tmp_path) -> None:
    run = _Run(tmp_path)
    scenario = _scenario(str(run.root), {_CONFIG: "version: 1\nenabled: false\n"})
    scenario["turns"][0]["steps"].append(
        {"bash": {"cmd": "uv run pytest -q", "exit": 0, "output": "40 passed in 0.63s"}}
    )
    scenario["turns"][0]["labels"] = {"tests_passed": {"label": "supported"}}
    run.write_file(_CONFIG, "version: 1\nenabled: false\n")
    out = run.tmp_path / "rendered"
    out.mkdir()
    case = render_inline_scenario(scenario, str(out)).stop_cases[0]
    payload = fill_payload(
        "stop_with_last_message",
        transcript_path=case.session_path,
        cwd=str(run.root),
        scratchpad_dir=str(run.tmp_path),
        last_assistant_message=case.final_message,
    )
    body = run.run(payload)
    assert "decision" not in body


# --------------------------------------------------------------------------------------
# controls: the same settings made before the session are the user's choice
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    [
        "version: 1\nenabled: false\n",
        'version: 1\nrules:\n  - id: tests\n    action: "off"\n',
    ],
    ids=["enabled-false", "tests-action-off"],
)
def test_config_set_before_the_session_exits_silently(tmp_path, content) -> None:
    run = _Run(tmp_path)
    run.write_file(_CONFIG, content)
    assert run.run(run.payload({})) == {}


def test_mode_warn_set_before_the_session_warns_instead_of_blocking(tmp_path) -> None:
    run = _Run(tmp_path)
    run.write_file(_CONFIG, "version: 1\nmode: warn\n")
    body = run.run(run.payload({}))
    assert "decision" not in body
    assert "All tests pass" in body["systemMessage"]


def test_environment_off_without_a_settings_edit_exits_silently(tmp_path) -> None:
    run = _Run(tmp_path)
    assert run.run(run.payload({}), env={"PROOF_OF_DONE": "off"}) == {}


def test_environment_off_with_an_unrelated_settings_edit_exits_silently(tmp_path) -> None:
    # A settings file that mentions nothing about proof-of-done is not tampering.
    run = _Run(tmp_path)
    body = _edited_by_session(run, {_SETTINGS: '{"theme": "dark"}\n'}, env={"PROOF_OF_DONE": "off"})
    assert body == {}


def test_disabled_config_with_a_settings_tamper_elsewhere_stays_silent(tmp_path) -> None:
    # `enabled: false` in a file the session did not touch is still the user's choice, even if
    # the session also edited a settings file.
    run = _Run(tmp_path)
    run.write_file(_CONFIG, "version: 1\nenabled: false\n")
    body = _edited_by_session(run, {_SETTINGS: _SETTINGS_OFF})
    assert body == {}
