"""Tests for `proof_of_done.audit`: counts/rates/reasons/partial share on rendered fixtures,
subagent grouping, `--claude-projects` honouring `CLAUDE_CONFIG_DIR`, redaction (no quote,
command, path or session id survives in the report or an export), `--source codex`/`auto`
wiring (spec item: wire the experimental Codex adapter into audit), and CLI exit codes /
`$GITHUB_STEP_SUMMARY` handled by `cli._cmd_audit`.
"""

from __future__ import annotations

import io
import json
import os
import sys

from hook_helpers import REPO_ROOT, render_inline_scenario

from proof_of_done import audit, cli, traces

FIXTURES_DIR = os.path.join(REPO_ROOT, "fixtures")
CODEX_FIXTURES_DIR = os.path.join(FIXTURES_DIR, "codex")
SCENARIOS_DIR = os.path.join(FIXTURES_DIR, "scenarios")


def _run_cli(argv: list[str]) -> tuple[int, str, str]:
    old_out, old_err = sys.stdout, sys.stderr
    out, err = io.StringIO(), io.StringIO()
    sys.stdout, sys.stderr = out, err
    try:
        try:
            code = cli.main(argv)
        except SystemExit as exc:  # argparse's own --help/--version handling
            code = 0 if exc.code is None else int(exc.code)
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    return code, out.getvalue(), err.getvalue()


def _supported_scenario(scenario_id: str) -> dict:
    return {
        "id": scenario_id,
        "ecosystem": "python",
        "turns": [
            {
                "user": "Fix the parser bug and confirm the suite still passes",
                "steps": [
                    {
                        "edit": {
                            "tool": "Edit",
                            "path": "src/app/parser.py",
                            "old": "a",
                            "new": "b",
                        }
                    },
                    {
                        "bash": {
                            "cmd": "uv run pytest -q",
                            "exit": 0,
                            "output": "40 passed in 0.63s",
                        }
                    },
                ],
                "final": "Fixed it. [[tests_passed|All 40 tests pass]].",
                "labels": {"tests_passed": {"label": "supported"}},
            }
        ],
    }


def _unsupported_scenario(scenario_id: str) -> dict:
    return {
        "id": scenario_id,
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
                    {
                        "edit": {
                            "tool": "Edit",
                            "path": "src/app/parser.py",
                            "old": "a",
                            "new": "b",
                        }
                    },
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


# ------------------------------------------------------------------------------------------
# counts, rates, reasons, partial share
# ------------------------------------------------------------------------------------------


def test_counts_across_two_rendered_sessions(tmp_path: object) -> None:
    out_dir = str(tmp_path)  # type: ignore[str-bytes-safe]
    render_inline_scenario(_supported_scenario("audit-count-supported"), out_dir)
    render_inline_scenario(_unsupported_scenario("audit-count-unsupported"), out_dir)

    result = audit.run_audit(paths=[out_dir])
    r = result.report
    assert r.sessions == 2
    assert r.turns == 2
    assert r.stop_attempts == 2
    assert r.turns_with_claims == 2
    # The supported scenario's "Fixed it." lead-in is also a (supported) `fixed` claim, on
    # top of the labelled `tests_passed` one; the unsupported scenario's "Updated the parser"
    # lead-in makes no claim of its own.
    assert r.claims_total == 3
    assert r.supported_total == 2
    assert r.unsupported_total == 1
    assert r.unsupported_rate() == 1 / 3
    assert r.by_type["tests_passed"].claims == 2
    assert r.by_type["tests_passed"].unsupported == 1
    assert r.by_type["fixed"].claims == 1
    assert r.by_type["fixed"].unsupported == 0
    assert r.by_reason == {"stale": 1}
    assert r.partial_share() == 0.0
    assert r.config_description == "built-in defaults"
    assert len(r.recent_unsupported) == 1
    assert r.recent_unsupported[0].reason == "stale"
    assert r.recent_unsupported[0].command == "uv run pytest -q"


def test_config_description_names_the_extra_config_file(tmp_path: object) -> None:
    out_dir = os.path.join(str(tmp_path), "sessions")
    render_inline_scenario(_supported_scenario("audit-config-desc"), out_dir)
    config_path = os.path.join(str(tmp_path), "extra.yaml")
    with open(config_path, "w", encoding="utf-8") as fh:
        fh.write("version: 1\n")

    result = audit.run_audit(paths=[out_dir], config_path=config_path)
    assert result.report.config_description == f"built-in defaults + {config_path}"


# ------------------------------------------------------------------------------------------
# subagent grouping
# ------------------------------------------------------------------------------------------


def test_subagent_grouped_under_parent_session(tmp_path: object) -> None:
    out_dir = str(tmp_path)  # type: ignore[str-bytes-safe]
    import sys as _sys

    if REPO_ROOT not in _sys.path:
        _sys.path.insert(0, REPO_ROOT)
    from fixtures import dsl, render

    scenario = dsl.load_scenario(os.path.join(SCENARIOS_DIR, "subagent-investigation.yaml"))
    render.render(scenario, out_dir)

    result = audit.run_audit(paths=[out_dir])
    r = result.report
    # One top-level session even though a subagent transcript also exists on disk.
    assert r.sessions == 1
    # The parent's own turn has no claim; the subagent's final message makes two: the
    # labelled "fixed the regression" and "a 20x repeat run confirmed the fix" (verified).
    # Both are supported by the same `--count=20` pytest run.
    assert r.turns == 2
    assert r.stop_attempts == 2
    assert r.claims_total == 2
    assert r.supported_total == 2
    assert r.by_type["fixed"].claims == 1
    assert r.by_type["verified"].claims == 1


# ------------------------------------------------------------------------------------------
# --claude-projects / CLAUDE_CONFIG_DIR
# ------------------------------------------------------------------------------------------


def test_claude_projects_honours_claude_config_dir(tmp_path: object) -> None:
    config_dir = os.path.join(str(tmp_path), "claude-config")
    projects_dir = os.path.join(config_dir, "projects")
    render_inline_scenario(_supported_scenario("audit-claude-projects"), projects_dir)

    result = audit.run_audit(claude_projects=True, env={"CLAUDE_CONFIG_DIR": config_dir})
    assert result.report.sessions == 1
    # "Fixed it." + the labelled "All 40 tests pass" -- both supported.
    assert result.report.supported_total == 2


def test_claude_projects_falls_back_to_home(tmp_path: object) -> None:
    home = str(tmp_path)  # type: ignore[str-bytes-safe]
    projects_dir = os.path.join(home, ".claude", "projects")
    render_inline_scenario(_supported_scenario("audit-claude-home"), projects_dir)

    result = audit.run_audit(claude_projects=True, env={"HOME": home})
    assert result.report.sessions == 1


# ------------------------------------------------------------------------------------------
# usage / input errors
# ------------------------------------------------------------------------------------------


def test_no_input_mode_is_a_usage_error() -> None:
    try:
        audit.run_audit()
        raise AssertionError("expected AuditUsageError")
    except audit.AuditUsageError:
        pass


def test_two_input_modes_is_a_usage_error(tmp_path: object) -> None:
    try:
        audit.run_audit(paths=[str(tmp_path)], demo=True)
        raise AssertionError("expected AuditUsageError")
    except audit.AuditUsageError:
        pass


def test_unreadable_path_is_an_input_error(tmp_path: object) -> None:
    empty_dir = os.path.join(str(tmp_path), "empty")
    os.makedirs(empty_dir)
    try:
        audit.run_audit(paths=[empty_dir])
        raise AssertionError("expected AuditInputError")
    except audit.AuditInputError:
        pass


# ------------------------------------------------------------------------------------------
# redaction: no quote/command/path/session id survives, in the report or an export
# ------------------------------------------------------------------------------------------


def test_redact_leaves_no_quote_command_path_or_session_id_in_report_or_traces(
    tmp_path: object,
) -> None:
    out_dir = os.path.join(str(tmp_path), "sessions")
    scenario = _unsupported_scenario("audit-redact-secret")
    scenario["turns"][0]["steps"][1]["edit"]["path"] = "src/app/very_secret_module.py"
    rendered = render_inline_scenario(scenario, out_dir)

    from proof_of_done import report as report_mod

    export_path = os.path.join(str(tmp_path), "traces.jsonl")
    result = audit.run_audit(
        paths=[out_dir], redact=True, salt="pepper", export_traces_path=export_path
    )
    for fmt in ("text", "json", "md"):
        text = report_mod.render(result.report, fmt=fmt)
        assert "uv run pytest -q" not in text
        assert "all 40 tests pass" not in text
        assert "very_secret_module.py" not in text
        assert rendered.session_id not in text

    with open(export_path, encoding="utf-8") as fh:
        traces_blob = fh.read()
    assert "uv run pytest -q" not in traces_blob
    assert "very_secret_module.py" not in traces_blob
    assert rendered.session_id not in traces_blob
    for line in traces_blob.splitlines():
        assert traces.validate_trace(json.loads(line)) == []


def test_without_redact_the_quote_and_command_do_survive(tmp_path: object) -> None:
    out_dir = os.path.join(str(tmp_path), "sessions")
    render_inline_scenario(_unsupported_scenario("audit-no-redact"), out_dir)

    from proof_of_done import report as report_mod

    result = audit.run_audit(paths=[out_dir])
    text = report_mod.render_text(result.report)
    assert "uv run pytest -q" in text


# ------------------------------------------------------------------------------------------
# --export-traces
# ------------------------------------------------------------------------------------------


def test_export_traces_writes_one_valid_trace_per_session(tmp_path: object) -> None:
    out_dir = os.path.join(str(tmp_path), "sessions")
    render_inline_scenario(_supported_scenario("audit-export-a"), out_dir)
    render_inline_scenario(_unsupported_scenario("audit-export-b"), out_dir)
    export_path = os.path.join(str(tmp_path), "traces.jsonl")

    audit.run_audit(paths=[out_dir], export_traces_path=export_path)

    with open(export_path, encoding="utf-8") as fh:
        lines = [json.loads(line) for line in fh if line.strip()]
    assert len(lines) == 2
    for obj in lines:
        assert traces.validate_trace(obj) == []


# ------------------------------------------------------------------------------------------
# --source codex / auto (wiring the experimental adapter)
# ------------------------------------------------------------------------------------------


def test_source_codex_end_to_end_on_fixtures() -> None:
    # End to end (claim detection + evidence), not the fixture-by-fixture direct
    # `evidence.judge` calls `test_codex_adapter.py` makes: `claim-free`'s prose makes no
    # claim (by construction); the other three each make one tests_passed claim.
    result = audit.run_audit(paths=[CODEX_FIXTURES_DIR], source="codex")
    r = result.report
    assert r.sessions == 4
    assert r.claims_total == 3  # passing-after-edit, stale-after-apply-patch, failing-exit-code
    assert r.supported_total == 1  # passing-after-edit
    assert r.unsupported_total == 2  # stale-after-apply-patch, failing-exit-code
    assert r.by_reason.get("stale") == 1
    assert r.by_reason.get("failed_exit") == 1


def test_source_auto_detects_codex_fixtures() -> None:
    result = audit.run_audit(paths=[CODEX_FIXTURES_DIR], source="auto")
    assert result.report.sessions == 4
    assert result.report.claims_total == 3


def test_detect_source_sniffs_each_format(tmp_path: object) -> None:
    assert audit.detect_source(os.path.join(CODEX_FIXTURES_DIR, "passing-after-edit.jsonl")) == (
        "codex"
    )


def test_experimental_sources_names_codex() -> None:
    assert frozenset({"codex"}) == audit.EXPERIMENTAL_SOURCES


# ------------------------------------------------------------------------------------------
# CLI: exit codes, --ci, $GITHUB_STEP_SUMMARY, trace validate
# ------------------------------------------------------------------------------------------


def test_cli_audit_usage_error_exit_code_2(tmp_path: object) -> None:
    code, _out, err = _run_cli(["audit"])
    assert code == 2
    assert "error" in err


def test_cli_audit_unreadable_input_exit_code_3(tmp_path: object) -> None:
    empty_dir = os.path.join(str(tmp_path), "empty")
    os.makedirs(empty_dir)
    code, _out, err = _run_cli(["audit", empty_dir])
    assert code == 3
    assert "error" in err


def test_cli_audit_ci_exceeds_rate_exit_code_1(tmp_path: object) -> None:
    out_dir = os.path.join(str(tmp_path), "sessions")
    render_inline_scenario(_unsupported_scenario("cli-ci-exceeds"), out_dir)
    code, out, _err = _run_cli(["audit", out_dir, "--ci", "--max-unsupported-rate", "0"])
    assert code == 1
    assert "audit report" in out


def test_cli_audit_ci_within_rate_exit_code_0(tmp_path: object) -> None:
    out_dir = os.path.join(str(tmp_path), "sessions")
    render_inline_scenario(_unsupported_scenario("cli-ci-within"), out_dir)
    code, _out, _err = _run_cli(["audit", out_dir, "--ci", "--max-unsupported-rate", "1.0"])
    assert code == 0


def test_cli_audit_writes_github_step_summary(tmp_path: object, monkeypatch: object) -> None:
    out_dir = os.path.join(str(tmp_path), "sessions")
    render_inline_scenario(_supported_scenario("cli-step-summary"), out_dir)
    summary_path = os.path.join(str(tmp_path), "summary.md")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", summary_path)  # type: ignore[attr-defined]
    code, _out, _err = _run_cli(["audit", out_dir, "--ci"])
    assert code == 0
    with open(summary_path, encoding="utf-8") as fh:
        content = fh.read()
    assert "# proof-of-done audit report" in content


def test_cli_audit_out_writes_to_file(tmp_path: object) -> None:
    out_dir = os.path.join(str(tmp_path), "sessions")
    render_inline_scenario(_supported_scenario("cli-out-file"), out_dir)
    out_file = os.path.join(str(tmp_path), "report.json")
    code, stdout, _err = _run_cli(["audit", out_dir, "--format", "json", "--out", out_file])
    assert code == 0
    assert stdout == ""
    with open(out_file, encoding="utf-8") as fh:
        data = json.load(fh)
    assert data["sessions"] == 1


def test_cli_audit_help_marks_codex_experimental() -> None:
    code, out, _err = _run_cli(["audit", "--help"])
    assert code == 0
    assert "EXPERIMENTAL" in out
    assert "codex" in out


def test_cli_trace_validate_valid_file(tmp_path: object) -> None:
    out_dir = os.path.join(str(tmp_path), "sessions")
    render_inline_scenario(_supported_scenario("cli-trace-validate-ok"), out_dir)
    export_path = os.path.join(str(tmp_path), "traces.jsonl")
    audit.run_audit(paths=[out_dir], export_traces_path=export_path)

    code, out, _err = _run_cli(["trace", "validate", export_path])
    assert code == 0
    assert "valid" in out


def test_cli_trace_validate_invalid_file(tmp_path: object) -> None:
    path = os.path.join(str(tmp_path), "bad.jsonl")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"schema": "agent-trace/v1"}) + "\n")
    code, out, _err = _run_cli(["trace", "validate", path])
    assert code == 1
    assert str(path) in out


def test_cli_trace_validate_missing_file_exit_code_2() -> None:
    code, _out, err = _run_cli(["trace", "validate", "/no/such/file.jsonl"])
    assert code == 2
    assert "error" in err


def test_cli_trace_no_subcommand_exit_code_2() -> None:
    code, _out, err = _run_cli(["trace"])
    assert code == 2
    assert "error" in err
