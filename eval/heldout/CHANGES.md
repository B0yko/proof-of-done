# Held-out set change log

The scenarios under `eval/heldout/` are frozen: their claim labels, reasons and suggested
commands were written before the claim detector existed and must not be tuned against it
afterward. `tests/test_heldout_manifest.py` fails the build if any `eval/heldout/*.yaml` file's
contents drift from `MANIFEST.sha256` unless that exact file is listed below, with a reason.

A label may only be corrected here, never silently, and never to make a number look better. An
acceptable entry names the file, the date, and why the original label was wrong (a
misapplied reason code, a rule-precedence mistake, an output pattern that does not actually
match the configured regex, and so on). Adding a new session or adding a new rule to an
existing session's `labels` map for the same reason does not belong here; it is not a
correction.

No entries yet.
