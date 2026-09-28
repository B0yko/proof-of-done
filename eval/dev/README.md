# eval/dev/

A development set of free-form agent final messages, used to tune the claim detector. Every
entry is synthetic and individually authored: no real transcript content, no real paths, no
real names. Licensed Apache-2.0, same as the rest of the repository.

This set is separate from `eval/heldout/`. Tuning against the held-out set would make its
pass rate an optimistic estimate of generalisation, so the detector is tuned here instead and
the held-out set stays untouched until a final check.

## Format

`messages.yaml` is a list of `{id, text}` entries. `text` is a realistic agent final message
with each claim marked `[[type|core phrase]]` (`type#2` for a second claim of the same type in
one message); a message with no marker is a claim-free look-alike. Validate marker syntax with
the parser in `fixtures/dsl.py` (`MARKER_RE`, `MARKER_TYPE_RE`, `parse_markers`) — the same
grammar the scenario DSL uses.

## Split

Entries are split into two halves by hashing the `id`: take `sha256(id)`, look at the first hex
digit of the digest. `0`–`7` is `train`, `8`–`f` is `check`. Tune on `train`; use `check` as a
quick sanity read while iterating, not as a substitute for the frozen `eval/heldout/` set.
