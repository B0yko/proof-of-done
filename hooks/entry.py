# proof-of-done Stop/SubagentStop hook entry script.
#
# Kept syntactically valid on Python 2.7 *and* 3.x on purpose: the launcher execs whatever
# `python3` it finds without checking its version first, so this file's own top few lines have
# to run far enough, on an interpreter as old as 2.7, to print the "Python 3.9+ required"
# message instead of dying with a SyntaxError the user never sees. No f-strings, no type
# hints, no other 3.6+-only syntax below the version gate either.
#
# Not imported as a module: Claude Code (via bin/proof-of-done-hook) execs it directly as
# `python3 -I entry.py <event> --data-dir <dir>`. `-I` (isolated mode) ignores PYTHONPATH and
# the script's own directory for `sys.path`, so this file inserts "<repo root>/src" itself
# before importing anything from the package.

import json
import os
import sys

if sys.version_info < (3, 9):
    sys.stdout.write(
        json.dumps({"systemMessage": "proof-of-done: Python 3.9+ required, check skipped"})
    )
    sys.exit(0)

# `/usr/bin/python3` on some machines (observed on macOS) runs with `dont_write_bytecode`
# forced on even under `-I`, so every hook call recompiles every module from source instead of
# reading a cached .pyc -- tens of milliseconds on the fast path alone. Redirect bytecode
# caching into the data dir (never the plugin root/repo, which may be read-only or version
# controlled) before importing anything from the package. Resolved the same way
# `bin/proof-of-done-hook` and `hook._resolve_data_dir` resolve it: the `--data-dir` argv
# value, else `CLAUDE_PLUGIN_DATA`, else a per-user temp fallback when empty, unexpanded (still
# contains a literal "$") or not an absolute path. Best-effort only: any failure (an unwritable
# data dir, a `sys.pycache_prefix` that does not exist on this interpreter) is skipped silently
# -- the hook must still run, just without this speedup.


def _entry_data_dir_arg(argv):
    value = None
    i = 0
    n = len(argv)
    while i < n:
        if argv[i] == "--data-dir" and i + 1 < n:
            value = argv[i + 1]
            i += 2
        else:
            i += 1
    return value


def _entry_data_dir(argv, environ):
    candidate = _entry_data_dir_arg(argv)
    if not candidate:
        candidate = environ.get("CLAUDE_PLUGIN_DATA")
    if candidate and "$" not in candidate and os.path.isabs(candidate):
        return candidate
    uid = os.getuid() if hasattr(os, "getuid") else 0
    tmp = environ.get("TMPDIR") or "/tmp"
    return os.path.join(tmp, "proof-of-done-" + str(uid))


try:
    _pycache_dir = os.path.join(_entry_data_dir(sys.argv[1:], os.environ), "pycache")
    os.makedirs(_pycache_dir, exist_ok=True)
    sys.pycache_prefix = _pycache_dir  # 3.8+; this file already requires 3.9+
    sys.dont_write_bytecode = False
except Exception:
    pass

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

try:
    from proof_of_done.hook import main

    sys.exit(main(sys.argv[1:]))
except SystemExit:
    raise
except BaseException:
    try:
        sys.stdout.write(
            json.dumps(
                {"systemMessage": "proof-of-done: internal error, check skipped (see log)"}
            )
        )
    except BaseException:
        pass
    sys.exit(0)
