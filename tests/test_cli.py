"""Tests for `proof_of_done.cli`: `check` exit codes, `init` project-type detection, and
`config show` source reporting."""

from __future__ import annotations

import io
import json
import sys

import pytest
from hook_helpers import render_inline_scenario

from proof_of_done import cli


def _run_cli(argv: list[str]) -> tuple[int, str, str]:
    old_out, old_err = sys.stdout, sys.stderr
    out, err = io.StringIO(), io.StringIO()
    sys.stdout, sys.stderr = out, err
    try:
        code = cli.main(argv)
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    return code, out.getvalue(), err.getvalue()


def _project_root(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / ".git").mkdir()
    return root


def _stale_scenario() -> dict:
    return {
        "id": "cli-check-stale",
        "ecosystem": "python",
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
        "id": "cli-check-supported",
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


def _no_claim_scenario() -> dict:
    return {
        "id": "cli-check-no-claim",
        "ecosystem": "python",
        "turns": [
            {
                "user": "Rename the helper function",
                "steps": [
                    {"edit": {"tool": "Edit", "path": "src/app/util.py", "old": "a", "new": "b"}}
                ],
                "final": "Renamed the helper function for clarity.",
                "labels": {},
            }
        ],
    }


def _render(tmp_path, scenario):
    out = tmp_path / "rendered"
    out.mkdir()
    return render_inline_scenario(scenario, str(out))


# --------------------------------------------------------------------------------------
# check
# --------------------------------------------------------------------------------------


def test_check_exit_1_on_unsupported_blocking_claim(tmp_path) -> None:
    rendered = _render(tmp_path, _stale_scenario())
    case = rendered.stop_cases[0]
    code, out, err = _run_cli(["check", "--transcript", case.session_path])
    assert code == 1
    assert "uv run pytest -q" in out
    assert err == ""


def test_check_exit_0_on_supported_claim(tmp_path) -> None:
    rendered = _render(tmp_path, _supported_scenario())
    case = rendered.stop_cases[0]
    code, _out, _err = _run_cli(["check", "--transcript", case.session_path])
    assert code == 0


def test_check_exit_0_on_no_claim(tmp_path) -> None:
    rendered = _render(tmp_path, _no_claim_scenario())
    case = rendered.stop_cases[0]
    code, out, _err = _run_cli(["check", "--transcript", case.session_path])
    assert code == 0
    assert "no unsupported claims" in out


def test_check_exit_2_on_missing_transcript(tmp_path) -> None:
    code, _out, err = _run_cli(["check", "--transcript", str(tmp_path / "nope.jsonl")])
    assert code == 2
    assert "not found" in err


def test_check_exit_2_on_bad_config(tmp_path) -> None:
    rendered = _render(tmp_path, _supported_scenario())
    case = rendered.stop_cases[0]
    bad_config = tmp_path / "bad.yaml"
    bad_config.write_text("rules: [\n  - id: tests\n", encoding="utf-8")
    code, _out, err = _run_cli(
        ["check", "--transcript", case.session_path, "--config", str(bad_config)]
    )
    assert code == 2
    assert err != ""


def test_check_with_explicit_message_overrides_transcript_tail(tmp_path) -> None:
    rendered = _render(tmp_path, _no_claim_scenario())
    case = rendered.stop_cases[0]
    code, out, _err = _run_cli(
        ["check", "--transcript", case.session_path, "--message", "All 42 tests pass.", "--json"]
    )
    assert code == 1
    body = json.loads(out)
    assert body["decision"] == "block"
    assert body["claims"][0]["claim_type"] == "tests_passed"


def test_check_with_message_file(tmp_path) -> None:
    rendered = _render(tmp_path, _no_claim_scenario())
    case = rendered.stop_cases[0]
    message_file = tmp_path / "message.txt"
    message_file.write_text("All 42 tests pass.", encoding="utf-8")
    code, _out, _err = _run_cli(
        ["check", "--transcript", case.session_path, "--message-file", str(message_file)]
    )
    assert code == 1


def test_check_message_and_message_file_are_mutually_exclusive(tmp_path) -> None:
    rendered = _render(tmp_path, _no_claim_scenario())
    case = rendered.stop_cases[0]
    with pytest.raises(SystemExit) as excinfo:
        cli.build_parser().parse_args(
            [
                "check",
                "--transcript",
                case.session_path,
                "--message",
                "x",
                "--message-file",
                "y",
            ]
        )
    assert excinfo.value.code == 2


def test_check_json_output_shape(tmp_path) -> None:
    rendered = _render(tmp_path, _stale_scenario())
    case = rendered.stop_cases[0]
    code, out, _err = _run_cli(["check", "--transcript", case.session_path, "--json"])
    assert code == 1
    body = json.loads(out)
    assert body["decision"] == "block"
    assert "reason" in body
    assert body["claims"][0]["supported"] is False
    assert body["claims"][0]["command"] == "uv run pytest -q"


# --------------------------------------------------------------------------------------
# init
# --------------------------------------------------------------------------------------


def test_init_detects_python_ecosystem(tmp_path, monkeypatch) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    code, out, _err = _run_cli(["init"])
    assert code == 0
    assert "python" in out
    written = (tmp_path / ".proof-of-done.yaml").read_text(encoding="utf-8")
    assert "version: 1" in written


def test_init_detects_node_ecosystem(tmp_path, monkeypatch) -> None:
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    code, out, _err = _run_cli(["init"])
    assert code == 0
    assert "node" in out


def test_init_detects_go_ecosystem(tmp_path, monkeypatch) -> None:
    (tmp_path / "go.mod").write_text("module x\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    code, out, _err = _run_cli(["init"])
    assert code == 0
    assert "go" in out


def test_init_detects_rust_ecosystem(tmp_path, monkeypatch) -> None:
    (tmp_path / "Cargo.toml").write_text("[package]\nname='x'\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    code, out, _err = _run_cli(["init"])
    assert code == 0
    assert "rust" in out


def test_init_falls_back_to_generic(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    code, out, _err = _run_cli(["init"])
    assert code == 0
    assert "generic" in out


def test_init_never_overwrites_without_force(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".proof-of-done.yaml").write_text("custom: true\n", encoding="utf-8")
    code, _out, err = _run_cli(["init"])
    assert code == 2
    assert "already exists" in err
    assert (tmp_path / ".proof-of-done.yaml").read_text(encoding="utf-8") == "custom: true\n"


def test_init_force_overwrites(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".proof-of-done.yaml").write_text("custom: true\n", encoding="utf-8")
    code, _out, _err = _run_cli(["init", "--force"])
    assert code == 0
    assert "custom: true" not in (tmp_path / ".proof-of-done.yaml").read_text(encoding="utf-8")


# --------------------------------------------------------------------------------------
# config show
# --------------------------------------------------------------------------------------


def test_config_show_lists_sources_and_effective_values(tmp_path) -> None:
    root = _project_root(tmp_path)
    (root / ".proof-of-done.yaml").write_text("max_blocks_per_turn: 5\n", encoding="utf-8")
    code, out, _err = _run_cli(["config", "show", "--cwd", str(root)])
    assert code == 0
    assert "max_blocks_per_turn: 5" in out
    assert "sources:" in out
    assert str(root / ".proof-of-done.yaml") in out
    assert "defaults" in out


def test_config_show_reports_bad_yaml(tmp_path) -> None:
    root = _project_root(tmp_path)
    (root / ".proof-of-done.yaml").write_text("rules: [\n  - id: tests\n", encoding="utf-8")
    code, _out, err = _run_cli(["config", "show", "--cwd", str(root)])
    assert code == 2
    assert err != ""


def test_config_without_subcommand_is_a_usage_error() -> None:
    code, _out, err = _run_cli(["config"])
    assert code == 2
    assert "show" in err


# --------------------------------------------------------------------------------------
# hook
# --------------------------------------------------------------------------------------


def test_hook_subcommand_dispatches_to_hook_main(tmp_path, monkeypatch) -> None:
    calls = []

    def fake_main(argv):
        calls.append(argv)
        return 0

    monkeypatch.setattr("proof_of_done.hook.main", fake_main)
    code, _out, _err = _run_cli(["hook", "stop", "--data-dir", str(tmp_path)])
    assert code == 0
    assert calls == [["stop", "--data-dir", str(tmp_path)]]


def test_hook_subcommand_rejects_unknown_event() -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.build_parser().parse_args(["hook", "not-an-event"])
    assert excinfo.value.code == 2


# --------------------------------------------------------------------------------------
# --version
# --------------------------------------------------------------------------------------


def test_version_flag_prints_package_version() -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.build_parser().parse_args(["--version"])
    assert excinfo.value.code == 0


def test_no_subcommand_prints_help_and_exits_0() -> None:
    code, out, _err = _run_cli([])
    assert code == 0
    assert "usage" in out.lower()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
