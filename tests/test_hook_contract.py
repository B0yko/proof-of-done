"""Contract tests for the real Stop/SubagentStop hook: payloads authored to match the
verified docs (`fixtures/payloads/*.json`), piped into the EXACT command string from
`hooks/hooks.json` via `sh -c`, with `CLAUDE_PLUGIN_ROOT` set to this repository and
`CLAUDE_PLUGIN_DATA` substituted per invocation.

Timeout contract (cited from `research/contracts.md`, itself sourced from the official hooks
reference): "Claude Code cancels a command ... hook that reaches its timeout, discarding the
hook's output, so on most events a timed-out hook renders no decision." `hook.main` honours
this by writing to stdout exactly once, at the very end -- so a hook killed mid-run must have
produced no output at all, which is what `test_timeout_kill_yields_empty_stdout` checks
directly (killing the real launcher process, not relying on the 10s `timeout` Claude Code
itself would apply).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest
from hook_helpers import REPO_ROOT, fill_payload, render_inline_scenario, run_hook_command

pytestmark = pytest.mark.skipif(os.name != "posix", reason="the launcher is POSIX sh")


# --------------------------------------------------------------------------------------
# scenario builders
# --------------------------------------------------------------------------------------


def _stale_tests_scenario() -> dict:
    return {
        "id": "hook-contract-stale",
        "ecosystem": "python",
        "project_files": {"pyproject.toml": "[project]\nname='demo'\n", "uv.lock": None},
        "turns": [
            {
                "user": "Fix the parser bug and confirm the suite still passes",
                "steps": [
                    {
                        "bash": {
                            "cmd": "uv run pytest -q",
                            "exit": 0,
                            "output": "40 passed in 0.63s",
                        }
                    },
                    {"edit": {"tool": "Edit", "path": "src/app/parser.py", "old": "a", "new": "b"}},
                ],
                "final": "Updated the parser; [[tests_passed|all 40 tests pass]].",
                "labels": {
                    "tests_passed": {
                        "label": "unsupported",
                        "reason": "stale",
                        "suggest": "uv run pytest -q",
                    }
                },
            }
        ],
    }


def _supported_scenario() -> dict:
    return {
        "id": "hook-contract-supported",
        "ecosystem": "python",
        "turns": [
            {
                "user": "Fix the parser bug",
                "steps": [
                    {"edit": {"tool": "Edit", "path": "src/app/parser.py", "old": "a", "new": "b"}},
                    {
                        "bash": {
                            "cmd": "uv run pytest -q",
                            "exit": 0,
                            "output": "40 passed in 0.63s",
                        }
                    },
                ],
                "final": "Fixed the parser; [[tests_passed|all 40 tests pass]].",
                "labels": {"tests_passed": {"label": "supported"}},
            }
        ],
    }


def _background_only_scenario() -> dict:
    return {
        "id": "hook-contract-background",
        "ecosystem": "python",
        "turns": [
            {
                "user": "Run the suite in the background and report back",
                "steps": [{"bash": {"cmd": "uv run pytest -q", "mode": "background"}}],
                "final": "Kicked off the suite; [[tests_passed|all tests pass]].",
                "labels": {
                    "tests_passed": {
                        "label": "unsupported",
                        "reason": "background_only",
                        "suggest": "uv run pytest -q",
                    }
                },
            }
        ],
    }


def _subagent_scenario() -> dict:
    return {
        "id": "hook-contract-subagent",
        "ecosystem": "python",
        "turns": [
            {
                "user": "Ask a subagent to fix the flaky test",
                "steps": [
                    {
                        "subagent": {
                            "type": "general-purpose",
                            "prompt": "Fix tests/test_flaky.py",
                            "result": "Done.",
                            "steps": [],
                            "final": "[[tests_passed|All tests pass]] after the fix.",
                            "labels": {
                                "tests_passed": {
                                    "label": "unsupported",
                                    "reason": "no_command",
                                    "suggest": "uv run pytest -q",
                                }
                            },
                        }
                    }
                ],
                "final": "The subagent reported the fix is complete.",
                "labels": {},
            }
        ],
    }


def _render(tmp_path, scenario):
    out = tmp_path / "rendered"
    out.mkdir()
    return render_inline_scenario(scenario, str(out))


def _project_root(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / ".git").mkdir()
    return root


# --------------------------------------------------------------------------------------
# shape / exit code / message-source contract
# --------------------------------------------------------------------------------------


def test_stop_with_last_assistant_message_blocks(tmp_path) -> None:
    rendered = _render(tmp_path, _stale_tests_scenario())
    case = rendered.stop_cases[0]
    root = _project_root(tmp_path)
    payload = fill_payload(
        "stop_with_last_message",
        transcript_path=case.session_path,
        cwd=str(root),
        scratchpad_dir=str(tmp_path),
        last_assistant_message=case.final_message,
    )
    result = run_hook_command(payload, data_dir=str(tmp_path / "data"))
    assert result.returncode == 0
    assert result.stderr == ""
    body = json.loads(result.stdout)
    assert body["decision"] == "block"
    assert "reason" in body
    assert "all 40 tests pass" in body["reason"]
    assert "uv run pytest -q" in body["reason"]


def test_stop_without_last_assistant_message_falls_back_to_transcript_tail(tmp_path) -> None:
    rendered = _render(tmp_path, _stale_tests_scenario())
    case = rendered.stop_cases[0]
    root = _project_root(tmp_path)
    payload = fill_payload(
        "stop_without_last_message",
        transcript_path=case.session_path,
        cwd=str(root),
        scratchpad_dir=str(tmp_path),
    )
    assert "last_assistant_message" not in payload
    result = run_hook_command(payload, data_dir=str(tmp_path / "data"))
    assert result.returncode == 0
    body = json.loads(result.stdout)
    assert body["decision"] == "block"
    assert "all 40 tests pass" in body["reason"]


def test_stop_with_nonempty_background_tasks(tmp_path) -> None:
    rendered = _render(tmp_path, _background_only_scenario())
    case = rendered.stop_cases[0]
    root = _project_root(tmp_path)
    payload = fill_payload(
        "stop_with_background_tasks",
        transcript_path=case.session_path,
        cwd=str(root),
        scratchpad_dir=str(tmp_path),
        last_assistant_message=case.final_message,
    )
    assert payload["background_tasks"], "fixture must carry a non-empty background_tasks"
    result = run_hook_command(payload, data_dir=str(tmp_path / "data"))
    assert result.returncode == 0
    body = json.loads(result.stdout)
    assert body["decision"] == "block"
    assert "background" in body["reason"]


def test_subagent_stop_with_agent_fields(tmp_path) -> None:
    rendered = _render(tmp_path, _subagent_scenario())
    subagent_case = next(c for c in rendered.stop_cases if c.agent_transcript_path is not None)
    root = _project_root(tmp_path)
    payload = fill_payload(
        "subagent_stop_with_agent_fields",
        transcript_path=subagent_case.payload["transcript_path"],
        cwd=str(root),
        scratchpad_dir=str(tmp_path),
        agent_id=subagent_case.payload["agent_id"],
        agent_type=subagent_case.payload["agent_type"],
        agent_transcript_path=subagent_case.agent_transcript_path,
        last_assistant_message=subagent_case.final_message,
    )
    result = run_hook_command(payload, event_name="SubagentStop", data_dir=str(tmp_path / "data"))
    assert result.returncode == 0
    body = json.loads(result.stdout)
    assert body["decision"] == "block"
    assert "All tests pass" in body["reason"]


def test_supported_claim_produces_no_stdout(tmp_path) -> None:
    rendered = _render(tmp_path, _supported_scenario())
    case = rendered.stop_cases[0]
    root = _project_root(tmp_path)
    payload = fill_payload(
        "stop_with_last_message",
        transcript_path=case.session_path,
        cwd=str(root),
        scratchpad_dir=str(tmp_path),
        last_assistant_message=case.final_message,
    )
    result = run_hook_command(payload, data_dir=str(tmp_path / "data"))
    assert result.returncode == 0
    assert result.stdout == ""


# --------------------------------------------------------------------------------------
# reason budget / skip-token scrubbing
# --------------------------------------------------------------------------------------


def test_reason_stays_within_line_and_char_budget_and_never_leaks_skip_token(tmp_path) -> None:
    rendered = _render(tmp_path, _stale_tests_scenario())
    case = rendered.stop_cases[0]
    root = _project_root(tmp_path)
    payload = fill_payload(
        "stop_with_last_message",
        transcript_path=case.session_path,
        cwd=str(root),
        scratchpad_dir=str(tmp_path),
        last_assistant_message=case.final_message,
    )
    result = run_hook_command(payload, data_dir=str(tmp_path / "data"))
    body = json.loads(result.stdout)
    reason = body["reason"]
    assert reason.count("\n") + 1 <= 20
    assert len(reason) <= 1800
    assert "#skip-proof" not in reason


# --------------------------------------------------------------------------------------
# counter behaviour
# --------------------------------------------------------------------------------------


def test_counter_resets_when_stop_hook_active_false(tmp_path) -> None:
    rendered = _render(tmp_path, _stale_tests_scenario())
    case = rendered.stop_cases[0]
    root = _project_root(tmp_path)
    data_dir = str(tmp_path / "data")

    def call(stop_hook_active: bool) -> dict:
        payload = fill_payload(
            "stop_with_last_message",
            transcript_path=case.session_path,
            cwd=str(root),
            scratchpad_dir=str(tmp_path),
            last_assistant_message=case.final_message,
        )
        payload["stop_hook_active"] = stop_hook_active
        result = run_hook_command(payload, data_dir=data_dir)
        assert result.returncode == 0
        return json.loads(result.stdout) if result.stdout else {}

    # max_blocks_per_turn defaults to 2: two consecutive blocks with stop_hook_active=True,
    # then a third must fall back to an allow + cap-warning systemMessage.
    first = call(False)
    assert first["decision"] == "block"
    second = call(True)
    assert second["decision"] == "block"
    third = call(True)
    assert "decision" not in third
    assert "systemMessage" in third
    assert "consecutive blocks" in third["systemMessage"]

    # A fresh turn (stop_hook_active=False again) resets the counter.
    fourth = call(False)
    assert fourth["decision"] == "block"


def test_counter_is_scoped_per_session_id(tmp_path) -> None:
    rendered = _render(tmp_path, _stale_tests_scenario())
    case = rendered.stop_cases[0]
    root = _project_root(tmp_path)
    data_dir = str(tmp_path / "data")

    for session_id in ("session-a", "session-b"):
        payload = fill_payload(
            "stop_with_last_message",
            transcript_path=case.session_path,
            cwd=str(root),
            scratchpad_dir=str(tmp_path),
            last_assistant_message=case.final_message,
        )
        payload["session_id"] = session_id
        payload["stop_hook_active"] = True
        result = run_hook_command(payload, data_dir=data_dir)
        body = json.loads(result.stdout)
        # Both are the *first* block for their own session_id, so neither hits the cap yet.
        assert body["decision"] == "block"


# --------------------------------------------------------------------------------------
# fail-open scenarios
# --------------------------------------------------------------------------------------


def test_fail_open_on_missing_transcript(tmp_path) -> None:
    root = _project_root(tmp_path)
    payload = fill_payload(
        "stop_with_last_message",
        transcript_path=str(tmp_path / "does-not-exist.jsonl"),
        cwd=str(root),
        scratchpad_dir=str(tmp_path),
        last_assistant_message="All 42 tests pass.",
    )
    result = run_hook_command(payload, data_dir=str(tmp_path / "data"))
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_fail_open_on_corrupt_jsonl_line(tmp_path) -> None:
    rendered = _render(tmp_path, _stale_tests_scenario())
    case = rendered.stop_cases[0]
    corrupt_path = tmp_path / "corrupt.jsonl"
    with open(case.session_path, encoding="utf-8") as fh:
        lines = fh.readlines()
    lines.insert(len(lines) // 2, "{not valid json,,,\n")
    with open(corrupt_path, "w", encoding="utf-8") as fh:
        fh.writelines(lines)

    root = _project_root(tmp_path)
    payload = fill_payload(
        "stop_with_last_message",
        transcript_path=str(corrupt_path),
        cwd=str(root),
        scratchpad_dir=str(tmp_path),
        last_assistant_message=case.final_message,
    )
    result = run_hook_command(payload, data_dir=str(tmp_path / "data"))
    assert result.returncode == 0
    body = json.loads(result.stdout)
    assert body["decision"] == "block"  # the corrupt line is skipped; everything else parses


def test_fail_open_on_bad_yaml_project_config(tmp_path) -> None:
    rendered = _render(tmp_path, _stale_tests_scenario())
    case = rendered.stop_cases[0]
    root = _project_root(tmp_path)
    (root / ".proof-of-done.yaml").write_text("rules: [\n  - id: tests\n", encoding="utf-8")

    payload = fill_payload(
        "stop_with_last_message",
        transcript_path=case.session_path,
        cwd=str(root),
        scratchpad_dir=str(tmp_path),
        last_assistant_message=case.final_message,
    )
    result = run_hook_command(payload, data_dir=str(tmp_path / "data"))
    assert result.returncode == 0
    assert result.stderr == ""
    body = json.loads(result.stdout)
    assert "systemMessage" in body
    assert "Traceback" not in result.stdout
    assert "Traceback" not in result.stderr


def test_fail_open_on_oversized_transcript(tmp_path) -> None:
    rendered = _render(tmp_path, _stale_tests_scenario())
    case = rendered.stop_cases[0]
    root = _project_root(tmp_path)
    (root / ".proof-of-done.yaml").write_text("max_transcript_mb: 0.00001\n", encoding="utf-8")

    payload = fill_payload(
        "stop_with_last_message",
        transcript_path=case.session_path,
        cwd=str(root),
        scratchpad_dir=str(tmp_path),
        last_assistant_message=case.final_message,
    )
    result = run_hook_command(payload, data_dir=str(tmp_path / "data"))
    assert result.returncode == 0
    body = json.loads(result.stdout)
    assert body == {"systemMessage": "proof-of-done: transcript too large, check skipped"}


def test_data_dir_empty_falls_back_and_still_works(tmp_path) -> None:
    rendered = _render(tmp_path, _stale_tests_scenario())
    case = rendered.stop_cases[0]
    root = _project_root(tmp_path)
    payload = fill_payload(
        "stop_with_last_message",
        transcript_path=case.session_path,
        cwd=str(root),
        scratchpad_dir=str(tmp_path),
        last_assistant_message=case.final_message,
    )
    from hook_helpers import LAUNCHER

    result = subprocess.run(
        ["sh", LAUNCHER, "stop", "--data-dir", ""],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    body = json.loads(result.stdout)
    assert body["decision"] == "block"


def test_data_dir_unexpanded_placeholder_falls_back_and_still_works(tmp_path) -> None:
    rendered = _render(tmp_path, _stale_tests_scenario())
    case = rendered.stop_cases[0]
    root = _project_root(tmp_path)
    payload = fill_payload(
        "stop_with_last_message",
        transcript_path=case.session_path,
        cwd=str(root),
        scratchpad_dir=str(tmp_path),
        last_assistant_message=case.final_message,
    )
    from hook_helpers import LAUNCHER

    result = subprocess.run(
        ["sh", LAUNCHER, "stop", "--data-dir", "${CLAUDE_PLUGIN_DATA}"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    body = json.loads(result.stdout)
    assert body["decision"] == "block"


def test_missing_interpreter_fails_open(tmp_path) -> None:
    from hook_helpers import LAUNCHER

    env = dict(os.environ)
    env["PROOF_OF_DONE_PYTHON"] = "/nonexistent/python3-does-not-exist"
    result = subprocess.run(
        ["sh", LAUNCHER, "stop", "--data-dir", str(tmp_path / "data")],
        input="{}",
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    assert result.returncode == 0
    body = json.loads(result.stdout)
    assert body == {"systemMessage": "proof-of-done: python3 not found, check skipped"}


def test_python_too_old_simulated_via_fake_interpreter_shim(tmp_path) -> None:
    """Simulates a pre-3.9 `python3` without needing one actually installed (out of this
    project's interpreter budget, which only allows provisioning 3.9 and 3.13 via uv): a tiny
    shim stands in for `PROOF_OF_DONE_PYTHON` and, in place of actually running Python 2.7/3.8
    against `hooks/entry.py`, emits exactly the JSON `entry.py`'s own version gate would have
    produced on such an interpreter. This exercises the launcher's `exec "$PY" ...` hand-off
    and its own fail-open contract end to end; `entry.py`'s version-gate *logic* itself is
    covered directly by `tests/test_hook_isolation.py`.
    """
    shim = tmp_path / "fake-old-python3"
    shim.write_text(
        "#!/bin/sh\n"
        'printf %s \'{"systemMessage": "proof-of-done: Python 3.9+ required, check skipped"}\'\n'
        "exit 0\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)

    from hook_helpers import LAUNCHER

    env = dict(os.environ)
    env["PROOF_OF_DONE_PYTHON"] = str(shim)
    result = subprocess.run(
        ["sh", LAUNCHER, "stop", "--data-dir", str(tmp_path / "data")],
        input="{}",
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    assert result.returncode == 0
    body = json.loads(result.stdout)
    assert body == {"systemMessage": "proof-of-done: Python 3.9+ required, check skipped"}


# --------------------------------------------------------------------------------------
# timeout contract
# --------------------------------------------------------------------------------------


def test_timeout_kill_yields_empty_stdout(tmp_path) -> None:
    """A command hook Claude Code cancels for reaching its timeout has its output discarded,
    so the stop proceeds with no decision (research/contracts.md, section C, "Timeout
    behavior"). `hook.py` only ever writes stdout once, at the very end of `main`, so killing
    the process well before it could plausibly finish must leave stdout completely empty --
    this is what actually makes that contract true rather than accidental.
    """
    rendered = _render(tmp_path, _stale_tests_scenario())
    case = rendered.stop_cases[0]
    # Inflate the transcript so parsing cannot plausibly finish within 50ms.
    with open(case.session_path, encoding="utf-8") as fh:
        base_lines = fh.readlines()
    padding_line = (
        '{"type":"user","uuid":"fixture-line-0",'
        '"parentUuid":null,"sessionId":"padding","timestamp":"2026-01-05T10:00:00.000Z",'
        '"cwd":"/work/demo-app","message":{"role":"user","content":"' + ("x" * 500) + '"}}\n'
    )
    big_path = tmp_path / "big.jsonl"
    with open(big_path, "w", encoding="utf-8") as fh:
        fh.writelines(base_lines[:-1])
        for _ in range(40000):
            fh.write(padding_line)
        fh.writelines(base_lines[-1:])
    assert os.path.getsize(big_path) > 20 * 1024 * 1024

    root = _project_root(tmp_path)
    payload = fill_payload(
        "stop_with_last_message",
        transcript_path=str(big_path),
        cwd=str(root),
        scratchpad_dir=str(tmp_path),
        last_assistant_message=case.final_message,
    )

    from hook_helpers import hook_command

    command = hook_command("Stop")
    env = dict(os.environ)
    env["CLAUDE_PLUGIN_ROOT"] = REPO_ROOT
    env["CLAUDE_PLUGIN_DATA"] = str(tmp_path / "data")
    proc = subprocess.Popen(
        ["sh", "-c", command],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
    )
    assert proc.stdin is not None
    proc.stdin.write(json.dumps(payload))
    proc.stdin.close()
    proc.stdin = None  # already closed; keep Popen.communicate() from re-flushing it below
    time.sleep(0.05)
    proc.kill()
    stdout, _stderr = proc.communicate(timeout=10)
    assert stdout == ""


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
