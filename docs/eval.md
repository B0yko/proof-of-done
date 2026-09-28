# Evaluation

`proof-of-done` ships no LLM and no model weights: every number below comes from the
deterministic claim detector and evidence engine running against three fixture sets, computed
by `uv run python eval/run_eval.py` and `uv run python eval/latency.py`. Reproduce them with the
exact commands in each section; the date and git commit of the run are recorded in
`eval/results/<date>.json` and `eval/results/latency-<date>.json`.

**Sets.** *Templated*: scenarios expanded from `fixtures/templates/` by the same author who
wrote the detector -- these measure consistency with the specification, not real-world
behaviour. *Held-out*: individually authored, free-form sessions frozen in `eval/heldout/`
before `claims.py` existed (see the freeze commit named in the README) -- the more honest
estimate, but still synthetic. *Adversarial*: sessions in `eval/adversarial/` that deliberately
try to game the gate. Metrics are never merged across sets.

**Config variants.** Gate numbers are reported twice: *shipped* (the config this repository
ships) and *forced_block* (every built-in rule's `action` forced to `block`, the "before
switch" baseline PLAN §12 asks for -- if a claim type's held-out precision ever drops the
shipped default to `warn`, this variant shows what the numbers were before that switch).

## Claim detection

Precision/recall/F1 of claim-instance detection (spec: same claim type, spans overlap by at
least one character, matched greedily one to one).

<!-- results:detection:start -->
*(run `uv run python eval/run_eval.py` to populate this table)*
<!-- results:detection:end -->

## Gate quality -- claim level

Positive class: "unsupported claim". Wilson 95% intervals for precision/recall; a seeded
bootstrap 95% interval (10,000 resamples) for F1.

<!-- results:gate-claim:start -->
*(run `uv run python eval/run_eval.py` to populate this table)*
<!-- results:gate-claim:end -->

## Gate quality -- turn level

Truth: any unsupported label in the stop attempt. Predicted: the decision was `block` (`warn`
counts as not blocked). The false-block rate is the blocked share of turns with no unsupported
label.

<!-- results:gate-turn:start -->
*(run `uv run python eval/run_eval.py` to populate this table)*
<!-- results:gate-turn:end -->

## Gate quality by claim type

Claim-level precision/recall/F1 broken out per claim type (shipped config). A type whose
held-out precision falls below 0.90 has its default `action` switched to `warn`; see the
README for whether that applies here.

<!-- results:per-type:start -->
*(run `uv run python eval/run_eval.py` to populate this table)*
<!-- results:per-type:end -->

## Reason confusion matrix

Expected vs. predicted reason code over matched, labelled unsupported claims in the held-out
set.

<!-- results:confusion:start -->
*(run `uv run python eval/run_eval.py` to populate this table)*
<!-- results:confusion:end -->

## Adversarial resistance

k of M gaming attempts that met their labelled outcome (decision and, where labelled, reason).
Every miss is a documented known limitation, not a bug fixed by tuning.

<!-- results:adversarial:start -->
*(run `uv run python eval/run_eval.py` to populate this table)*
<!-- results:adversarial:end -->

## Suggestion accuracy

Share of correctly blocked unsupported claims whose `Run:` command equals the labelled
`suggest` (`null == null` counts as equal).

<!-- results:suggestion:start -->
*(run `uv run python eval/run_eval.py` to populate this table)*
<!-- results:suggestion:end -->

## Held-out failure analysis

Every held-out error (detection false negative/positive, wrong verdict, wrong reason, wrong
suggestion, prefilter miss), grouped by its top failure mode with one example each. The full
list is in the committed `eval/results/<date>.json`. Held-out labels are never changed to make
these numbers better; a genuine label error is fixed only with an `eval/heldout/CHANGES.md`
entry recording why.

<!-- results:failures:start -->
*(run `uv run python eval/run_eval.py` to populate this table)*
<!-- results:failures:end -->

## Hook latency

p50/p95/max over fresh-subprocess invocations of the real launcher
(`sh bin/proof-of-done-hook stop --data-dir D`), stdin piped in, interpreter startup included.
Reproduce with `uv run python eval/latency.py --generate --run` (`--quick` for a fast,
non-representative smoke run).

<!-- results:latency:start -->
*(run `uv run python eval/latency.py --generate --run` to populate this table)*
<!-- results:latency:end -->

## Audit throughput

`proof-of-done audit --format json` on the 50 MB synthetic transcript, median of 5 runs.

<!-- results:throughput:start -->
*(run `uv run python eval/latency.py --generate --run` to populate this table)*
<!-- results:throughput:end -->

## Caveat

The same author wrote the detector and the fixtures, so the templated numbers above measure
consistency with the specification, not real-world agent behaviour. The held-out set, frozen
before the detector existed, is the more honest estimate. Neither set is a substitute for
auditing your own agent's real transcripts with `proof-of-done audit`.
