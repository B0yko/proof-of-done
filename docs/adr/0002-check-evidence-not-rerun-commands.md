# 2. Check evidence in the transcript instead of re-running commands

Status: accepted.

## Context

A Stop hook could instead just run the project's test/lint/build command itself whenever the
agent tries to stop, and block if it fails. Some existing tools in this space do exactly that
(see the README's Alternatives section, `loop-hooks` in particular).

## Decision

The hook never executes anything. `evidence.py` only looks at commands the agent itself already
ran, recorded in the transcript, and judges whether the latest matching one is recent enough
(after the last relevant edit) and successful.

## Consequences

- The hook only does work when a claim is actually made, not on every stop; a session with no
  "tests pass"-shaped message pays only the keyword-prefilter cost.
- No command re-execution means no new side effects: proof-of-done cannot start a deploy, write
  a file, or hang on a command the agent's environment can run but the hook's cannot (a
  container, a service dependency, a different working directory).
- It cannot catch a claim that is true only because the agent is about to run the command but
  has not yet — which is correct: the claim is unsupported *right now*, and the block message
  tells the agent to run it.
- It inherits whatever the agent's own run already proved, including its flakiness. A test
  suite that passed five minutes ago and would fail now if re-run is still "supported": the
  transcript is evidence of what happened, not a live guarantee.
- Detecting whether a command's exit status was masked (piped into `tail`, `|| true`, `; true`)
  matters a lot more under this design than it would if the hook ran the command itself with a
  clean shell, because the hook can only see what the agent's own shell invocation reported.
  `shell.py`'s masking logic exists because of this decision.
