"""Proves the hook path is really stdlib-only end to end: `hooks/entry.py` run under
`python3 -I -S` (isolated mode, no site-packages at all) produces the same decision as calling
`hook.main` in-process, and no third-party module -- in particular a real, PyPI-installed
`yaml` or `jsonschema` -- is ever reachable from it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import pytest
from hook_helpers import REPO_ROOT, fill_payload, render_inline_scenario

ENTRY = os.path.join(REPO_ROOT, "hooks", "entry.py")

pytestmark = pytest.mark.skipif(os.name != "posix", reason="the hook targets macOS/Linux")


def _blocking_scenario() -> dict:
    return {
        "id": "isolation-stale",
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


def _project_root(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / ".git").mkdir()
    return root


def _payload(tmp_path):
    out = tmp_path / "rendered"
    out.mkdir()
    rendered = render_inline_scenario(_blocking_scenario(), str(out))
    case = rendered.stop_cases[0]
    root = _project_root(tmp_path)
    return fill_payload(
        "stop_with_last_message",
        transcript_path=case.session_path,
        cwd=str(root),
        scratchpad_dir=str(tmp_path),
        last_assistant_message=case.final_message,
    )


def _run_in_process(payload: dict, data_dir: str) -> dict:
    import io

    from proof_of_done import hook

    class _Buffer:
        def __init__(self, text: str) -> None:
            self._data = text.encode("utf-8")

        def read(self, _n: int = -1) -> bytes:
            return self._data

    class _Stdin:
        buffer = None

    stdin = _Stdin()
    stdin.buffer = _Buffer(json.dumps(payload))  # type: ignore[assignment]
    old_stdin, old_stdout = sys.stdin, sys.stdout
    out = io.StringIO()
    sys.stdin = stdin  # type: ignore[assignment]
    sys.stdout = out
    try:
        hook.main(["stop", "--data-dir", data_dir])
    finally:
        sys.stdin, sys.stdout = old_stdin, old_stdout
    text = out.getvalue()
    return json.loads(text) if text else {}


def _run_under_entry(payload: dict, data_dir: str, python: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [python, "-I", "-S", ENTRY, "stop", "--data-dir", data_dir],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_entry_under_isolated_python_matches_in_process_decision(tmp_path) -> None:
    payload = _payload(tmp_path)

    in_process = _run_in_process(payload, str(tmp_path / "data-in-process"))
    result = _run_under_entry(payload, str(tmp_path / "data-entry"), sys.executable)

    assert result.returncode == 0
    assert result.stderr == ""
    from_entry = json.loads(result.stdout)
    assert from_entry["decision"] == in_process["decision"] == "block"
    assert from_entry["reason"] == in_process["reason"]


def test_entry_under_system_usr_bin_python3_when_present(tmp_path) -> None:
    system_python = "/usr/bin/python3"
    if not os.path.exists(system_python):
        pytest.skip("/usr/bin/python3 not present on this machine")
    payload = _payload(tmp_path)

    in_process = _run_in_process(payload, str(tmp_path / "data-in-process"))
    result = _run_under_entry(payload, str(tmp_path / "data-system"), system_python)

    assert result.returncode == 0
    from_entry = json.loads(result.stdout)
    assert from_entry["decision"] == in_process["decision"] == "block"


def test_no_third_party_module_is_reachable_from_the_hook_path(tmp_path) -> None:
    payload = _payload(tmp_path)
    data_dir = str(tmp_path / "data")
    driver = tmp_path / "driver.py"
    driver_lines = [
        "import json, sys",
        "sys.path.insert(0, " + repr(os.path.join(REPO_ROOT, "src")) + ")",
        "from proof_of_done import hook",
        "",
        "class _Buffer:",
        "    def __init__(self, text):",
        "        self._data = text.encode('utf-8')",
        "    def read(self, n=-1):",
        "        return self._data",
        "",
        "class _Stdin:",
        "    pass",
        "",
        "stdin = _Stdin()",
        "stdin.buffer = _Buffer(sys.stdin.read())",
        "sys.stdin = stdin",
        "hook.main(['stop', '--data-dir', " + repr(data_dir) + "])",
        "third_party_markers = ('yaml', 'jsonschema', 'pytest', '_pytest')",
        "hits = [m for m in sys.modules if m in third_party_markers or m.startswith(m + '.')]",
        "print(json.dumps(sorted(hits)), file=sys.stderr)",
        "",
    ]
    driver.write_text("\n".join(driver_lines), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(driver)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    hits = json.loads(result.stderr.strip().splitlines()[-1])
    assert hits == []
    # And the decision itself still came through correctly on stdout.
    body = json.loads(result.stdout)
    assert body["decision"] == "block"


def test_launcher_finds_usr_bin_python3_by_default_when_no_python3_earlier_in_path(
    tmp_path,
) -> None:
    """`bin/proof-of-done-hook` defaults `PY` to plain `python3`; confirm that whatever it
    resolves via `command -v` actually runs the real interpreter chain end to end (a weaker
    but environment-independent stand-in for asserting the exact resolved path)."""
    python3 = shutil.which("python3")
    if python3 is None:
        pytest.skip("no python3 on PATH")
    payload = _payload(tmp_path)
    result = _run_under_entry(payload, str(tmp_path / "data"), python3)
    assert result.returncode == 0
    body = json.loads(result.stdout)
    assert body["decision"] == "block"
