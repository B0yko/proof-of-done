"""Tests for the demo corpus: `scripts/render_demo.py --check` catches drift
between `fixtures/demo/*.yaml` and the packaged `src/proof_of_done/demo/*.jsonl`, and
`proof-of-done audit --demo` runs end to end on the packaged copy and labels its report as
synthetic.
"""

from __future__ import annotations

import glob
import importlib.resources
import io
import os
import sys

from hook_helpers import REPO_ROOT

from proof_of_done import audit, cli, report

DEMO_SCENARIOS_DIR = os.path.join(REPO_ROOT, "fixtures", "demo")
DEMO_PACKAGE_DIR = os.path.join(REPO_ROOT, "src", "proof_of_done", "demo")


def _run_cli(argv: list[str]) -> tuple[int, str, str]:
    old_out, old_err = sys.stdout, sys.stderr
    out, err = io.StringIO(), io.StringIO()
    sys.stdout, sys.stderr = out, err
    try:
        code = cli.main(argv)
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    return code, out.getvalue(), err.getvalue()


def test_at_least_30_demo_scenarios_committed() -> None:
    scenario_files = glob.glob(os.path.join(DEMO_SCENARIOS_DIR, "*.yaml"))
    assert len(scenario_files) >= 30


def test_packaged_demo_corpus_matches_scenario_count() -> None:
    scenario_ids = {
        os.path.basename(p)[: -len(".yaml")]
        for p in glob.glob(os.path.join(DEMO_SCENARIOS_DIR, "*.yaml"))
    }
    packaged_ids = {
        os.path.basename(p)[: -len(".jsonl")]
        for p in glob.glob(os.path.join(DEMO_PACKAGE_DIR, "*.jsonl"))
    }
    assert scenario_ids == packaged_ids


def test_render_demo_check_passes_on_the_committed_corpus() -> None:
    if REPO_ROOT not in sys.path:
        sys.path.insert(0, REPO_ROOT)
    import scripts.render_demo as render_demo

    old_argv = sys.argv
    old_out = sys.stdout
    try:
        sys.stdout = io.StringIO()
        code = render_demo.main(["--check"])
    finally:
        sys.stdout = old_out
        sys.argv = old_argv
    assert code == 0


def test_render_demo_check_catches_drift(tmp_path: object, monkeypatch: object) -> None:
    import scripts.render_demo as render_demo

    monkeypatch.setattr(render_demo, "OUT_DIR", str(tmp_path))  # type: ignore[attr-defined]
    old_out = sys.stdout
    try:
        sys.stdout = io.StringIO()
        code = render_demo.main(["--check"])
        output = sys.stdout.getvalue()
    finally:
        sys.stdout = old_out
    assert code == 1
    assert "missing" in output


# ------------------------------------------------------------------------------------------
# audit --demo
# ------------------------------------------------------------------------------------------


def test_demo_files_are_importable_via_importlib_resources() -> None:
    root = importlib.resources.files("proof_of_done").joinpath("demo")
    with importlib.resources.as_file(root) as demo_dir:
        names = [n for n in os.listdir(demo_dir) if n.endswith(".jsonl")]
    assert len(names) >= 30


def test_audit_demo_smoke() -> None:
    result = audit.run_audit(demo=True)
    r = result.report
    assert r.sessions >= 30
    assert r.claims_total > 0
    assert r.demo is True
    assert r.turns_with_claims > 0
    # Every scenario is judged with the built-in defaults, nothing local.
    assert r.config_description == "built-in defaults"


def test_audit_demo_report_headers_say_synthetic() -> None:
    result = audit.run_audit(demo=True)
    for fmt in ("text", "md"):
        text = report.render(result.report, fmt=fmt)
        assert "SYNTHETIC DEMO CORPUS" in text
        assert "not a measurement of any real coding agent" in text
    data_text = report.render_json(result.report)
    assert '"demo": true' in data_text


def test_cli_audit_demo_smoke() -> None:
    code, out, _err = _run_cli(["audit", "--demo"])
    assert code == 0
    assert "SYNTHETIC DEMO CORPUS" in out
    assert "sessions: " in out
