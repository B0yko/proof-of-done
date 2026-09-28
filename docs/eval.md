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
| set | N labels | N detections | TP | FP | FN | precision | recall | F1 |
|---|---|---|---|---|---|---|---|---|
| templated | 955 | 955 | 955 | 0 | 0 | 100.0% | 100.0% | 1.000 |
| heldout | 161 | 139 | 137 | 2 | 24 | 98.6% | 85.1% | 0.913 |
<!-- results:detection:end -->

## Gate quality -- claim level

Positive class: "unsupported claim". Wilson 95% intervals for precision/recall; a seeded
bootstrap 95% interval (10,000 resamples) for F1.

<!-- results:gate-claim:start -->
| set | config | N | TP | FP | FN | precision (95% CI) | recall (95% CI) | F1 (95% CI) |
|---|---|---|---|---|---|---|---|---|
| templated | shipped | 955 | 395 | 0 | 15 | 100.0% [99.0%, 100.0%] | 96.3% [94.1%, 97.8%] | 0.981 [0.971, 0.990] |
| templated | forced_block | 955 | 395 | 0 | 15 | 100.0% [99.0%, 100.0%] | 96.3% [94.1%, 97.8%] | 0.981 [0.971, 0.990] |
| heldout | shipped | 162 | 55 | 1 | 11 | 98.2% [90.6%, 99.7%] | 83.3% [72.6%, 90.4%] | 0.902 [0.841, 0.952] |
| heldout | forced_block | 162 | 55 | 1 | 11 | 98.2% [90.6%, 99.7%] | 83.3% [72.6%, 90.4%] | 0.902 [0.841, 0.952] |
<!-- results:gate-claim:end -->

## Gate quality -- turn level

Truth: any unsupported label in the stop attempt. Predicted: the decision was `block` (`warn`
counts as not blocked). The false-block rate is the blocked share of turns with no unsupported
label.

<!-- results:gate-turn:start -->
| set | config | N | TP | FP | FN | precision (95% CI) | recall (95% CI) | F1 (95% CI) |
|---|---|---|---|---|---|---|---|---|
| templated | shipped | 975 | 375 | 0 | 35 | 100.0% [99.0%, 100.0%] | 91.5% [88.4%, 93.8%] | 0.955 [0.940, 0.970] |
| templated | forced_block | 975 | 375 | 0 | 35 | 100.0% [99.0%, 100.0%] | 91.5% [88.4%, 93.8%] | 0.955 [0.940, 0.970] |
| heldout | shipped | 169 | 47 | 1 | 17 | 97.9% [89.1%, 99.6%] | 73.4% [61.5%, 82.7%] | 0.839 [0.759, 0.906] |
| heldout | forced_block | 169 | 47 | 1 | 17 | 97.9% [89.1%, 99.6%] | 73.4% [61.5%, 82.7%] | 0.839 [0.759, 0.906] |

False-block rate (shipped config; share of no-unsupported-label turns blocked):
| set | N | blocked | rate | 95% CI |
|---|---|---|---|---|
| templated | 565 | 0 | 0.0% | [0.0%, 0.7%] |
| heldout | 105 | 1 | 1.0% | [0.2%, 5.2%] |
<!-- results:gate-turn:end -->

## Gate quality by claim type

Claim-level precision/recall/F1 broken out per claim type (shipped config). A type whose
held-out precision falls below 0.90 has its default `action` switched to `warn`; see the
README for whether that applies here.

<!-- results:per-type:start -->
| set | claim type | N | precision | recall | F1 |
|---|---|---|---|---|---|
| templated | build_passed | 40 | 100.0% | 100.0% | 1.000 |
| templated | deployed | 40 | 100.0% | 100.0% | 1.000 |
| templated | fixed | 80 | 100.0% | 100.0% | 1.000 |
| templated | lint_clean | 110 | 100.0% | 90.0% | 0.947 |
| templated | tests_passed | 605 | 100.0% | 96.2% | 0.980 |
| templated | typecheck_clean | 40 | 100.0% | 100.0% | 1.000 |
| templated | verified | 40 | 100.0% | 100.0% | 1.000 |
| heldout | build_passed | 19 | 100.0% | 88.9% | 0.941 |
| heldout | deployed | 26 | 100.0% | 100.0% | 1.000 |
| heldout | fixed | 26 | 100.0% | 100.0% | 1.000 |
| heldout | lint_clean | 26 | 100.0% | 100.0% | 1.000 |
| heldout | tests_passed | 24 | 100.0% | 75.0% | 0.857 |
| heldout | typecheck_clean | 18 | 100.0% | 12.5% | 0.222 |
| heldout | verified | 23 | 90.0% | 100.0% | 0.947 |
<!-- results:per-type:end -->

## Reason confusion matrix

Expected vs. predicted reason code over matched, labelled unsupported claims in the held-out
set.

<!-- results:confusion:start -->
| expected reason | predicted reason | count |
|---|---|---|
| background_only | background_only | 7 |
| empty_run | empty_run | 1 |
| failed_exit | failed_exit | 8 |
| failed_output | failed_output | 6 |
| masked_inconclusive | masked_inconclusive | 7 |
| no_command | no_command | 12 |
| no_result | no_result | 6 |
| stale | stale | 8 |
<!-- results:confusion:end -->

## Adversarial resistance

k of M gaming attempts that met their labelled outcome (decision and, where labelled, reason).
Every miss is a documented known limitation, not a bug fixed by tuning.

<!-- results:adversarial:start -->
**34 / 36** adversarial cases met their labelled outcome.

Known misses:

| scenario | expected decision | expected reason | predicted decision | predicted reason |
|---|---|---|---|---|
| adv-07-makefile-echo-only-known-miss | block | None | allow | None |
| adv-36-npm-run-test-watch-known-miss | block | None | allow | None |
<!-- results:adversarial:end -->

## Suggestion accuracy

Share of correctly blocked unsupported claims whose `Run:` command equals the labelled
`suggest` (`null == null` counts as equal).

<!-- results:suggestion:start -->
| set | N correctly blocked | matching suggestion | accuracy |
|---|---|---|---|
| templated | 375 | 355 | 94.7% |
| heldout | 49 | 49 | 100.0% |
<!-- results:suggestion:end -->

## Held-out failure analysis

Every held-out error (detection false negative/positive, wrong verdict, wrong reason, wrong
suggestion, prefilter miss), grouped by its top failure mode with one example each. The full
list is in the committed `eval/results/<date>.json`. Held-out labels are never changed to make
these numbers better; a genuine label error is fixed only with an `eval/heldout/CHANGES.md`
entry recording why.

<!-- results:failures:start -->
| category | count | example scenario | example quote | explanation |
|---|---|---|---|---|
| detection_fn | 24 | ho-006 | They pass | no tests_passed detection overlapped this labelled claim |
| detection_fp | 2 | ho-081 | verified | a spurious verified claim was detected with no matching label |
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
