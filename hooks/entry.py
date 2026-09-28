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
