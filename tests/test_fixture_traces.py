"""agent-trace/v1 export of rendered fixtures with synthetic ground truth (ADR 9)."""

from __future__ import annotations

import json
import os
import sys
from typing import Any

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from fixtures import render  # noqa: E402
from proof_of_done import traces  # noqa: E402


def _scenario(
    sid: str, steps: list[dict[str, Any]], final: str, labels: dict[str, Any]
) -> dict[str, Any]:
    return {
        "id": sid,
        "ecosystem": "python",
        "project_files": {"pyproject.toml": None, "uv.lock": None},
        "turns": [{"user": "Fix the parser", "steps": steps, "final": final, "labels": labels}],
    }


EDIT = {"edit": {"tool": "Edit", "path": "src/app/parser.py", "old": "a", "new": "b"}}
PASS = {"bash": {"cmd": "uv run pytest -q", "exit": 0, "output": "42 passed in 0.8s"}}
FAIL = {"bash": {"cmd": "uv run pytest -q", "exit": 1, "output": "1 failed, 41 passed in 0.9s"}}
PARTIAL = {"bash": {"cmd": "uv run pytest -q -k parser", "exit": 0, "output": "3 passed in 0.1s"}}


@pytest.mark.parametrize(
    ("steps", "outcome"),
    [
        ([EDIT, PASS], "success"),
        ([EDIT, FAIL], "failure"),
        ([PASS, EDIT], "unknown"),
        ([EDIT, PARTIAL], "unknown"),
        ([EDIT], "unknown"),
    ],
)
def test_ground_truth_rule(tmp_path: Any, steps: list[dict[str, Any]], outcome: str) -> None:
    sc = _scenario(f"gt-{outcome}-{len(steps)}", steps, "Updated the parser.", {})
    out = render.export_agent_traces(sc, str(tmp_path))
    assert len(out) == 1
    gt = out[0]["ground_truth"]
    assert gt == {
        "outcome": outcome,
        "checked_by": "none",
        "details": {"label_source": "synthetic-by-construction"},
    }
    assert traces.validate_trace(out[0]) == []


def test_cli_writes_valid_jsonl(tmp_path: Any) -> None:
    scenario_path = os.path.join(REPO, "fixtures", "scenarios", "parallel-calls.yaml")
    trace_file = tmp_path / "traces.jsonl"
    assert (
        render._cli([scenario_path, "--out", str(tmp_path / "r"), "--agent-trace", str(trace_file)])
        == 0
    )
    lines = trace_file.read_text(encoding="utf-8").splitlines()
    assert lines
    for line in lines:
        trace = json.loads(line)
        assert traces.validate_trace(trace) == []
        assert trace["ground_truth"]["details"]["label_source"] == "synthetic-by-construction"
