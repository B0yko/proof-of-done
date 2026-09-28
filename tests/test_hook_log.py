"""What the real hook writes to its diagnostic log: rule ids, claim types, reason codes and
per-phase timings, and never the message text, a quote, a command or a path."""

from __future__ import annotations

import json
import os

import pytest
from hook_helpers import fill_payload, render_inline_scenario, run_hook_command

pytestmark = pytest.mark.skipif(os.name != "posix", reason="the launcher is POSIX sh")

_QUOTE = "all 40 tests pass"
_COMMAND = "uv run pytest -q"
_EDITED_FILE = "src/app/parser.py"


def _stale_scenario(root: str) -> dict:
    return {
        "id": "hook-log-stale",
        "ecosystem": "python",
        "root": root,
        "project_files": {"pyproject.toml": "[project]\nname='demo'\n", "uv.lock": None},
        "turns": [
            {
                "user": "Fix the parser bug and confirm the suite still passes",
                "steps": [
                    {"bash": {"cmd": _COMMAND, "exit": 0, "output": "40 passed in 0.63s"}},
                    {"edit": {"tool": "Edit", "path": _EDITED_FILE, "old": "a", "new": "b"}},
                ],
                "final": f"Updated the parser; [[tests_passed|{_QUOTE}]].",
                "labels": {
                    "tests_passed": {
                        "label": "unsupported",
                        "reason": "stale",
                        "suggest": _COMMAND,
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
        self.data_dir = tmp_path / "data"

    def run(self) -> str:
        out = self.tmp_path / "rendered"
        out.mkdir()
        rendered = render_inline_scenario(_stale_scenario(str(self.root)), str(out))
        case = rendered.stop_cases[0]
        payload = fill_payload(
            "stop_with_last_message",
            transcript_path=case.session_path,
            cwd=str(self.root),
            scratchpad_dir=str(self.tmp_path),
            last_assistant_message=case.final_message,
        )
        env_extra = {
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "PROOF_OF_DONE": "",
            "PROOF_OF_DONE_MODE": "",
            "PROOF_OF_DONE_CONFIG": "",
        }
        result = run_hook_command(payload, data_dir=str(self.data_dir), env_extra=env_extra)
        assert result.returncode == 0
        return result.stdout

    def records(self) -> list[dict]:
        with open(self.data_dir / "proof-of-done.log", encoding="utf-8") as fh:
            return [json.loads(line) for line in fh.read().splitlines()]

    def raw_log(self) -> str:
        return (self.data_dir / "proof-of-done.log").read_text(encoding="utf-8")


def test_decision_record_has_rule_ids_reason_codes_and_timings(tmp_path) -> None:
    run = _Run(tmp_path)
    assert json.loads(run.run())["decision"] == "block"

    (record,) = run.records()
    assert record["event"] == "decision"
    assert record["hook_event"] == "stop"
    assert record["decision"] == "block"
    assert record["claims"] == 1
    assert record["unsupported"] == 1
    assert record["results"] == [
        {
            "claim_type": "tests_passed",
            "rule_ids": ["tests"],
            "supported": False,
            "reason": "stale",
            "action": "block",
        }
    ]
    timings = record["timings_ms"]
    assert set(timings) == {"fast_path", "parse", "detect_judge", "total"}
    assert all(isinstance(v, (int, float)) and v >= 0 for v in timings.values())
    assert timings["total"] >= timings["fast_path"]


def test_the_log_never_holds_message_text_commands_or_paths(tmp_path) -> None:
    run = _Run(tmp_path)
    run.run()
    raw = run.raw_log()
    for forbidden in (_QUOTE, _COMMAND, _EDITED_FILE, str(tmp_path), "Updated the parser"):
        assert forbidden not in raw


def test_an_invalid_config_is_logged_with_its_class_and_timing_only(tmp_path) -> None:
    run = _Run(tmp_path)
    (run.root / ".proof-of-done.yaml").write_text("version: 1\nnot_a_key: 1\n", encoding="utf-8")
    body = json.loads(run.run())
    assert "internal error" in body["systemMessage"]
    (record,) = run.records()
    assert record["event"] == "error"
    assert record["hook_event"] == "stop"
    assert record["error"] == "ConfigError"
    assert set(record["timings_ms"]) == {"total"}
    assert "not_a_key" not in run.raw_log()


def test_an_oversized_transcript_fail_open_is_logged_with_a_reason_code(tmp_path) -> None:
    run = _Run(tmp_path)
    (run.root / ".proof-of-done.yaml").write_text(
        "version: 1\nmax_transcript_mb: 0.000001\n", encoding="utf-8"
    )
    body = json.loads(run.run())
    assert "transcript too large" in body["systemMessage"]
    (record,) = run.records()
    assert record["event"] == "fail_open"
    assert record["reason"] == "transcript_too_large"
    assert set(record["timings_ms"]) == {"total"}


def test_an_oversized_transcript_stays_silent_when_the_hook_is_switched_off(tmp_path) -> None:
    run = _Run(tmp_path)
    (run.root / ".proof-of-done.yaml").write_text(
        "version: 1\nenabled: false\nmax_transcript_mb: 0.000001\n", encoding="utf-8"
    )
    assert run.run() == ""
    (record,) = run.records()
    assert record["reason"] == "transcript_too_large"
