"""A message that matches none of the rules' keywords must exit before the transcript is even
opened, and without importing the vendored YAML loader. This is what makes the hook cheap
enough to run on every single Stop event. The fast path decides nothing else: it never treats
a config as disabled, because that can only be trusted after the tamper scan.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys

import pytest
from evidence_helpers import real_defaults

from proof_of_done import claims as claims_mod
from proof_of_done import config as config_mod
from proof_of_done import fastpath, hook

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC_DIR = os.path.join(_REPO_ROOT, "src")


class _Buffer:
    def __init__(self, text: str) -> None:
        self._data = text.encode("utf-8")

    def read(self, _n: int = -1) -> bytes:
        return self._data


class _Stdin:
    buffer: _Buffer


def _run_hook(payload: dict, data_dir: str) -> str:
    stdin = _Stdin()
    stdin.buffer = _Buffer(json.dumps(payload))
    old_stdin, old_stdout = sys.stdin, sys.stdout
    out = io.StringIO()
    sys.stdin = stdin  # type: ignore[assignment]
    sys.stdout = out
    try:
        code = hook.main(["stop", "--data-dir", data_dir])
    finally:
        sys.stdin, sys.stdout = old_stdin, old_stdout
    assert code == 0
    return out.getvalue()


def test_no_keyword_message_never_opens_the_transcript(tmp_path) -> None:
    unreadable = tmp_path / "no-permission.jsonl"
    unreadable.write_text("{}\n", encoding="utf-8")
    unreadable.chmod(0o000)
    try:
        payload = {
            "session_id": "s1",
            "transcript_path": str(unreadable),
            "cwd": str(tmp_path),
            "hook_event_name": "Stop",
            "stop_hook_active": False,
            "last_assistant_message": "Updated the README with a note about the new option.",
            "background_tasks": [],
            "session_crons": [],
        }
        out = _run_hook(payload, str(tmp_path / "data"))
        assert out == ""
    finally:
        unreadable.chmod(0o644)


def test_no_keyword_message_never_reads_a_fifo_transcript(tmp_path) -> None:
    fifo_path = tmp_path / "transcript.jsonl"
    os.mkfifo(fifo_path)
    try:
        payload = {
            "session_id": "s1",
            "transcript_path": str(fifo_path),
            "cwd": str(tmp_path),
            "hook_event_name": "Stop",
            "stop_hook_active": False,
            "last_assistant_message": "Renamed the helper function for clarity.",
            "background_tasks": [],
            "session_crons": [],
        }
        # Opening a FIFO with nothing writing to it would block forever; the fast path must
        # never even attempt it.
        out = _run_hook(payload, str(tmp_path / "data"))
        assert out == ""
    finally:
        fifo_path.unlink()


def test_no_keyword_message_never_imports_the_vendored_yaml_module_on_a_warm_cache(
    tmp_path,
) -> None:
    data_dir = str(tmp_path / "data")
    root = "/work/demo-app"
    env: dict[str, str] = {}
    layer_paths = config_mod.layer_paths_for(root, env)
    # Warm the config cache first, exactly like a real second-and-later hook invocation.
    config_mod.load_cached(data_dir, layer_paths, env)
    for name in list(sys.modules):
        if name.startswith("proof_of_done._vendor"):
            del sys.modules[name]

    payload = {
        "session_id": "s1",
        "transcript_path": str(tmp_path / "does-not-matter.jsonl"),
        "cwd": root,
        "hook_event_name": "Stop",
        "stop_hook_active": False,
        "last_assistant_message": "Renamed a helper function for clarity, nothing else changed.",
        "background_tasks": [],
        "session_crons": [],
    }
    out = _run_hook(payload, data_dir)
    assert out == ""
    assert "proof_of_done._vendor.yaml" not in sys.modules


def test_no_keyword_message_avoids_config_and_dataclasses_imports_on_warm_cache(tmp_path) -> None:
    """Once the merged-config cache is warm, a keyword-miss message must be decided by
    `fastpath.py` alone -- never `proof_of_done.config`/`proof_of_done.claims`,
    and therefore never the heavy stdlib machinery `config.py`'s dataclasses pull in
    (`dataclasses` itself imports `inspect`/`ast`; `config.py` also imports `shutil`). Runs in
    a fresh, isolated interpreter (`-I -S`, no site-packages) so `sys.modules` reflects only
    what this one hook call actually imported, not whatever pytest itself already loaded.
    """
    data_dir = str(tmp_path / "data")
    root = "/work/demo-app"
    env: dict[str, str] = {}
    layer_paths = config_mod.layer_paths_for(root, env)
    config_mod.load_cached(data_dir, layer_paths, env)  # warm the cache, like a real 2nd+ call

    payload = {
        "session_id": "s1",
        "transcript_path": str(tmp_path / "does-not-matter.jsonl"),
        "cwd": root,
        "hook_event_name": "Stop",
        "stop_hook_active": False,
        "last_assistant_message": "Renamed a helper function for clarity, nothing else changed.",
        "background_tasks": [],
        "session_crons": [],
    }
    driver = tmp_path / "driver.py"
    driver_lines = [
        "import json, sys",
        "sys.path.insert(0, " + repr(_SRC_DIR) + ")",
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
        "markers = ("
        "'dataclasses', 'inspect', 'ast', 'shutil', "
        "'proof_of_done.config', 'proof_of_done.claims'"
        ")",
        "hits = sorted(",
        "    m for m in sys.modules",
        "    if m in markers or any(m.startswith(p + '.') for p in markers)",
        ")",
        "print(json.dumps(hits), file=sys.stderr)",
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
    assert result.returncode == 0, result.stderr
    hits = json.loads(result.stderr.strip().splitlines()[-1])
    assert hits == []
    assert result.stdout == ""


def test_fast_path_never_decides_disabled_from_the_environment(tmp_path) -> None:
    # `PROOF_OF_DONE=off` is honoured only after the tamper scan, so a message with a keyword
    # must fall through to the full path even when this invocation's environment says off.
    data_dir = str(tmp_path / "data")
    root = "/work/demo-app"
    layer_paths = config_mod.layer_paths_for(root, {})
    config_mod.load_cached(data_dir, layer_paths, {})  # warm the cache with a plain env
    env = {"PROOF_OF_DONE": "off"}
    assert fastpath.decide("All 42 tests pass.", root, env, data_dir) == fastpath.FALL_THROUGH
    assert fastpath.decide("Renamed a helper for clarity.", root, env, data_dir) == fastpath.ALLOW


def test_fast_path_never_decides_disabled_from_a_disabled_config(tmp_path) -> None:
    home = tmp_path / "home"
    root = tmp_path / "project"
    root.mkdir()
    (root / ".git").mkdir()
    (root / ".proof-of-done.yaml").write_text("enabled: false\n", encoding="utf-8")
    env = {"HOME": str(home)}
    data_dir = str(tmp_path / "data")
    config_mod.load_cached(data_dir, config_mod.layer_paths_for(str(root), env), env)
    assert fastpath.decide("All 42 tests pass.", str(root), env, data_dir) == fastpath.FALL_THROUGH


def test_fast_path_keyword_union_covers_rules_turned_off(tmp_path) -> None:
    home = tmp_path / "home"
    root = tmp_path / "project"
    root.mkdir()
    (root / ".git").mkdir()
    (root / ".proof-of-done.yaml").write_text(
        'rules:\n  - id: tests\n    action: "off"\n', encoding="utf-8"
    )
    env = {"HOME": str(home)}
    data_dir = str(tmp_path / "data")
    config_mod.load_cached(data_dir, config_mod.layer_paths_for(str(root), env), env)
    assert fastpath.decide("All 42 tests pass.", str(root), env, data_dir) == fastpath.FALL_THROUGH


def test_every_labelled_keyword_message_passes_its_own_type_s_prefilter() -> None:
    """Regression guard for the fast path's own soundness: a message that *should* be
    checked must always pass the keyword prefilter, for every built-in claim type."""
    cfg = real_defaults()
    keywords = cfg.keywords()
    samples = {
        "tests_passed": "All 42 tests pass.",
        "build_passed": "The build succeeds now.",
        "lint_clean": "Lint is clean.",
        "typecheck_clean": "Typecheck is clean.",
        "deployed": "Deployed to production.",
        "fixed": "Fixed the bug.",
        "verified": "I verified it works.",
    }
    for claim_type, message in samples.items():
        assert claims_mod.prefilter(message, keywords), claim_type


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
