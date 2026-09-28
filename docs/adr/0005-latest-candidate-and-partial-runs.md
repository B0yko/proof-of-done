# 5. The latest candidate decides, and how partial runs are treated

Status: accepted.

## Context

Between the anchor (the last edit to a relevant file) and the stop, an agent can run its test
command more than once — a first, narrower run while debugging, then a full run before
reporting success; or a full run, then more edits, then a narrower `-k` run that only re-checks
the part it just touched. Evidence judging needs one rule for which run counts.

## Decision

`evidence.judge` (see `docs/how-it-works.md` for the full reason table) always looks at the
*last* candidate command after the anchor, never the best one or the first one. A candidate that
matches `evidence.partial_args` (`-k`, `--lf`, an explicit test path, `-run`, `-t`, ...), or
whose command line's last positional argument looks like a test file or package path, is
flagged `partial` rather than disqualified — a partial run is still evidence, because "I fixed
the bug and re-ran only that test" is a legitimate workflow.

The one exception: a partial run does not override a *full* run that already failed after the
same anchor. That combination gets its own reason code, `superseded_by_failure`, and is treated
as unsupported — re-running a narrow slice of a suite you already know is red does not make the
whole "tests pass" claim true.

## Consequences

- An agent that runs the full suite, fixes one more file, then runs `pytest -k
  test_that_file_only` and reports "all tests pass" gets a `supported, partial` verdict, with
  the block message (when it *is* unsupported for some other reason) still surfacing this as a
  partial run so the agent understands why.
- An agent that runs the full suite (fails), then narrows to `-k` on the one test it just fixed
  and reports blanket success is still blocked, with a reason that names the earlier full-run
  failure, not just "stale" or "supported."
- This is a specification choice, not a detection of intent — the fixture and evaluation sets
  test it as a fixed rule (`tpl-tests-*-partial-*` in the templated set; see also the adversarial
  case for "a partial pass after a failed full run").
