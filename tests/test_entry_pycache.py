"""The hook redirects its bytecode cache into a per-user fallback directory when no data
directory is given. That directory sits under a shared temp dir at a predictable name, and
cached bytecode is executed, so it must be private and owned by the current user."""

from __future__ import annotations

import os
import stat
import subprocess
import sys

import pytest
from hook_helpers import REPO_ROOT

ENTRY = os.path.join(REPO_ROOT, "hooks", "entry.py")

pytestmark = pytest.mark.skipif(os.name != "posix", reason="the hook targets macOS/Linux")


def _run_entry(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PLUGIN_DATA"}
    env["TMPDIR"] = str(tmp_path)
    return subprocess.run(
        [sys.executable, "-I", ENTRY, "stop"],
        input=b"{}",
        env=env,
        capture_output=True,
        timeout=60,
        check=False,
    )


def _fallback(tmp_path):
    return tmp_path / f"proof-of-done-{os.getuid()}"


def test_fallback_dir_is_created_private(tmp_path):
    proc = _run_entry(tmp_path)
    assert proc.returncode == 0
    fallback = _fallback(tmp_path)
    assert stat.S_IMODE(fallback.stat().st_mode) == 0o700
    assert stat.S_IMODE((fallback / "pycache").stat().st_mode) == 0o700


def test_fallback_dir_open_to_others_gets_no_bytecode_cache(tmp_path):
    fallback = _fallback(tmp_path)
    fallback.mkdir()
    fallback.chmod(0o777)
    proc = _run_entry(tmp_path)
    assert proc.returncode == 0
    assert not (fallback / "pycache").exists()


def test_symlinked_fallback_dir_is_not_used_for_bytecode(tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir(mode=0o700)
    tmp_root = tmp_path / "tmp"
    tmp_root.mkdir()
    os.symlink(target, _fallback(tmp_root))
    proc = _run_entry(tmp_root)
    assert proc.returncode == 0
    assert not (target / "pycache").exists()
