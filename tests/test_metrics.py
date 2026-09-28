"""Hand-computed unit tests for `eval/metrics.py`: claim matching, precision/recall/F1,
Wilson intervals and the seeded bootstrap F1 interval. No fixture rendering here -- see
`tests/test_run_eval.py` for the end-to-end harness.
"""

from __future__ import annotations

import pytest

from eval import metrics


def _span(claim_type: str, start: int, end: int, payload: object = None) -> metrics.Span:
    return metrics.Span(claim_type=claim_type, start=start, end=end, payload=payload)


# --------------------------------------------------------------------------------------
# match_claims
# --------------------------------------------------------------------------------------


def test_match_claims_matches_same_type_overlapping_spans() -> None:
    labels = [_span("tests_passed", 10, 20, "label-a")]
    detections = [_span("tests_passed", 15, 25, "det-a")]
    result = metrics.match_claims(labels, detections)
    assert len(result.pairs) == 1
    assert result.pairs[0][0].payload == "label-a"
    assert result.pairs[0][1].payload == "det-a"
    assert result.unmatched_labels == ()
    assert result.unmatched_detections == ()


def test_match_claims_requires_same_claim_type() -> None:
    labels = [_span("tests_passed", 0, 10)]
    detections = [_span("lint_clean", 0, 10)]
    result = metrics.match_claims(labels, detections)
    assert result.pairs == ()
    assert len(result.unmatched_labels) == 1
    assert len(result.unmatched_detections) == 1


def test_match_claims_requires_at_least_one_char_overlap() -> None:
    labels = [_span("tests_passed", 0, 10)]
    detections = [_span("tests_passed", 10, 20)]  # touches but does not overlap
    result = metrics.match_claims(labels, detections)
    assert result.pairs == ()
    assert len(result.unmatched_labels) == 1
    assert len(result.unmatched_detections) == 1


def test_match_claims_is_greedy_one_to_one() -> None:
    # Two labels of the same type, one detection overlapping both: the earlier label (by
    # start) claims it; the later label is left unmatched, and the detection is used up.
    labels = [_span("tests_passed", 0, 5, "first"), _span("tests_passed", 3, 8, "second")]
    detections = [_span("tests_passed", 2, 6, "det")]
    result = metrics.match_claims(labels, detections)
    assert len(result.pairs) == 1
    assert result.pairs[0][0].payload == "first"
    assert len(result.unmatched_labels) == 1
    assert result.unmatched_labels[0].payload == "second"
    assert result.unmatched_detections == ()


def test_detection_metrics_spurious_and_missed() -> None:
    labels = [_span("tests_passed", 0, 10), _span("lint_clean", 20, 30)]
    detections = [
        _span("tests_passed", 0, 10),  # matches label 1
        _span("build_passed", 40, 50),  # spurious, no label
    ]
    m = metrics.detection_metrics(labels, detections)
    assert m == {
        "n_labels": 2,
        "n_detections": 2,
        "tp": 1,
        "fp": 1,
        "fn": 1,
        "precision": 0.5,
        "recall": 0.5,
        "f1": 0.5,
    }


def test_detection_metrics_per_message_never_matches_across_messages() -> None:
    # Message A has a labelled claim the detector missed. Message B has a detection with no
    # label, at the very same offsets. Pooled, the two would pair up (tp=1); per message they
    # are a miss and a false alarm.
    message_a = ([_span("tests_passed", 0, 10)], [])
    message_b = ([], [_span("tests_passed", 0, 10)])
    m = metrics.detection_metrics_per_message([message_a, message_b])
    assert (m["tp"], m["fp"], m["fn"]) == (0, 1, 1)
    assert m["n_labels"] == 1
    assert m["n_detections"] == 1
    assert m["precision"] == 0.0
    assert m["recall"] == 0.0
    assert m["f1"] == 0.0

    pooled = metrics.detection_metrics(message_a[0] + message_b[0], message_a[1] + message_b[1])
    assert (pooled["tp"], pooled["fp"], pooled["fn"]) == (1, 0, 0)  # the mistake being guarded


def test_detection_metrics_per_message_sums_counts() -> None:
    first = ([_span("tests_passed", 0, 10)], [_span("tests_passed", 2, 8)])
    second = ([_span("lint_clean", 5, 9), _span("fixed", 20, 25)], [_span("lint_clean", 5, 9)])
    m = metrics.detection_metrics_per_message([first, second])
    assert (m["tp"], m["fp"], m["fn"]) == (2, 0, 1)
    assert m["n_labels"] == 3
    assert m["n_detections"] == 2


# --------------------------------------------------------------------------------------
# precision_recall_f1
# --------------------------------------------------------------------------------------


def test_precision_recall_f1_hand_computed() -> None:
    precision, recall, f1 = metrics.precision_recall_f1(tp=3, fp=1, fn=2)
    assert precision == pytest.approx(0.75)
    assert recall == pytest.approx(0.6)
    assert f1 == pytest.approx(2 * 0.75 * 0.6 / (0.75 + 0.6))


def test_precision_recall_f1_zero_denominators_are_none() -> None:
    precision, recall, f1 = metrics.precision_recall_f1(tp=0, fp=0, fn=0)
    assert precision is None
    assert recall is None
    assert f1 is None


def test_precision_recall_f1_perfect() -> None:
    precision, recall, f1 = metrics.precision_recall_f1(tp=5, fp=0, fn=0)
    assert precision == 1.0
    assert recall == 1.0
    assert f1 == 1.0


# --------------------------------------------------------------------------------------
# wilson_interval -- reference values cross-checked against the standard Wilson-score
# formula independently of `metrics.py` (textbook example: 5/10 -> (0.2366, 0.7634)).
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "k,n,expected_lo,expected_hi",
    [
        (5, 10, 0.236593, 0.763407),
        (0, 10, 0.0, 0.277533),
        (8, 10, 0.490162, 0.943318),
        (3, 4, 0.300642, 0.954413),
    ],
)
def test_wilson_interval_matches_reference_values(k, n, expected_lo, expected_hi) -> None:
    interval = metrics.wilson_interval(k, n)
    assert interval is not None
    lo, hi = interval
    assert lo == pytest.approx(expected_lo, abs=1e-5)
    assert hi == pytest.approx(expected_hi, abs=1e-5)


def test_wilson_interval_none_for_zero_n() -> None:
    assert metrics.wilson_interval(0, 0) is None


def test_wilson_interval_full_certainty_at_n_equals_zero_successes() -> None:
    lo, hi = metrics.wilson_interval(0, 1)
    assert lo == 0.0
    assert hi < 1.0


# --------------------------------------------------------------------------------------
# confusion_counts / gate_summary / false_block_rate
# --------------------------------------------------------------------------------------


def _units(*rows: tuple[bool, bool]) -> list[metrics.ConfusionUnit]:
    return [metrics.ConfusionUnit(truth_positive=t, predicted_positive=p) for t, p in rows]


def test_confusion_counts_hand_computed() -> None:
    units = _units((True, True), (True, False), (False, True), (False, False), (False, False))
    tp, fp, fn, tn = metrics.confusion_counts(units)
    assert (tp, fp, fn, tn) == (1, 1, 1, 2)


def test_gate_summary_precision_recall_and_intervals() -> None:
    units = _units((True, True), (True, True), (True, False), (False, True), (False, False))
    summary = metrics.gate_summary(units, resamples=200)
    assert summary.tp == 2
    assert summary.fp == 1
    assert summary.fn == 1
    assert summary.tn == 1
    assert summary.precision == pytest.approx(2 / 3)
    assert summary.recall == pytest.approx(2 / 3)
    assert summary.f1 == pytest.approx(2 / 3)
    assert summary.precision_wilson95 is not None
    assert summary.recall_wilson95 is not None
    assert summary.f1_bootstrap95 is not None
    lo, hi = summary.f1_bootstrap95
    assert 0.0 <= lo <= summary.f1 <= hi <= 1.0


def test_false_block_rate_hand_computed() -> None:
    # 1 blocked turn with no unsupported label (FP), 3 correctly left alone (TN); the 2
    # truth-positive turns are irrelevant to the false-block rate's denominator.
    units = _units((True, True), (True, False), (False, True), (False, False), (False, False))
    fbr = metrics.false_block_rate(units)
    assert fbr["n"] == 3
    assert fbr["blocked"] == 1
    assert fbr["rate"] == pytest.approx(1 / 3)
    assert fbr["wilson95"] is not None


def test_false_block_rate_none_when_no_negative_turns() -> None:
    units = _units((True, True), (True, False))
    fbr = metrics.false_block_rate(units)
    assert fbr["n"] == 0
    assert fbr["rate"] is None
    assert fbr["wilson95"] is None


# --------------------------------------------------------------------------------------
# bootstrap_f1 -- determinism and a tiny hand-traceable example
# --------------------------------------------------------------------------------------


def test_bootstrap_f1_is_deterministic_for_a_fixed_seed() -> None:
    units = _units((True, True), (True, False), (False, True), (False, False))
    first = metrics.bootstrap_f1(units, seed=20260928, resamples=500)
    second = metrics.bootstrap_f1(units, seed=20260928, resamples=500)
    assert first == second


def test_bootstrap_f1_all_perfect_units_has_a_degenerate_interval() -> None:
    # Every unit is a true positive: every resample (whatever indices are drawn) reproduces
    # perfect precision and recall, so the interval collapses to (1.0, 1.0).
    units = _units((True, True), (True, True), (True, True))
    interval = metrics.bootstrap_f1(units, resamples=100)
    assert interval == (1.0, 1.0)


def test_bootstrap_f1_none_for_empty_units() -> None:
    assert metrics.bootstrap_f1([]) is None


def test_bootstrap_f1_matches_a_hand_traced_two_resample_run() -> None:
    # n=2 units; with `resamples=2` the seeded RNG draws exactly 4 indices (2 per resample).
    # Trace them independently with the same `random.Random(seed)` construction the spec
    # pins, and hand-compute the resulting F1 for each resample.
    import random

    units = _units((True, True), (False, True))  # unit 0: TP; unit 1: FP
    seed = 12345
    rng = random.Random(seed)
    n = 2
    resample_indices = [
        [int(rng.random() * n), int(rng.random() * n)],
        [int(rng.random() * n), int(rng.random() * n)],
    ]
    expected_f1s = []
    truths_preds = [(True, True), (False, True)]
    for idxs in resample_indices:
        tp = fp = fn = 0
        for i in idxs:
            t, p = truths_preds[i]
            if t and p:
                tp += 1
            elif p:
                fp += 1
            elif t:
                fn += 1
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = 0.0 if (precision + recall) == 0 else 2 * precision * recall / (precision + recall)
        expected_f1s.append(f1)
    expected_f1s.sort()

    interval = metrics.bootstrap_f1(units, seed=seed, resamples=2)
    assert interval == (pytest.approx(expected_f1s[0]), pytest.approx(expected_f1s[1]))
