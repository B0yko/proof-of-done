"""Statistics primitives for `eval/run_eval.py`: greedy claim matching, precision/recall/F1,
Wilson 95% intervals and a seeded bootstrap 95% interval for F1.

Deliberately generic and free of any `proof_of_done` import: every function here takes plain
`Span`/`ConfusionUnit` values, so it can be unit-tested with small hand-computed examples
without rendering a single fixture. `eval/run_eval.py` builds those values from
`fixtures.render.LabelSpan` and `proof_of_done.engine.ClaimResult`.
"""

from __future__ import annotations

import math
import random
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

# The bootstrap's seed and resample count are pinned exactly: 10,000 resamples,
# `random.Random(20260928)`, each index drawn as `int(rng.random() * n)`.
DEFAULT_SEED = 20260928
DEFAULT_RESAMPLES = 10000

_Z95 = 1.959963984540054  # two-sided 95% normal quantile


# --------------------------------------------------------------------------------------
# claim matching (spec: "Detections are matched to labels greedily, one to one.")
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Span:
    """One claim instance's type and character span, plus an opaque `payload` the caller
    attaches (a labelled `LabelSpan` or a detected `ClaimResult`) for building confusion
    units and reports after matching."""

    claim_type: str
    start: int
    end: int
    payload: Any = None


@dataclass(frozen=True)
class MatchResult:
    pairs: tuple[tuple[Span, Span], ...]
    unmatched_labels: tuple[Span, ...]
    unmatched_detections: tuple[Span, ...]


def match_claims(labels: Sequence[Span], detections: Sequence[Span]) -> MatchResult:
    """Match every label to at most one detection of the same `claim_type` whose span
    overlaps it by at least one character. Labels are visited in `(start, end)` order; each
    is matched to the first not-yet-used qualifying detection in `detections`' own order
    (callers pass detections already sorted by span, so this is deterministic)."""
    used = [False] * len(detections)
    pairs: list[tuple[Span, Span]] = []
    unmatched_labels: list[Span] = []
    for label in sorted(labels, key=lambda s: (s.start, s.end)):
        match_i = None
        for i, det in enumerate(detections):
            if used[i] or det.claim_type != label.claim_type:
                continue
            if det.start < label.end and det.end > label.start:
                match_i = i
                break
        if match_i is None:
            unmatched_labels.append(label)
        else:
            used[match_i] = True
            pairs.append((label, detections[match_i]))
    unmatched_detections = [d for i, d in enumerate(detections) if not used[i]]
    return MatchResult(tuple(pairs), tuple(unmatched_labels), tuple(unmatched_detections))


# --------------------------------------------------------------------------------------
# precision / recall / F1
# --------------------------------------------------------------------------------------


def precision_recall_f1(
    tp: int, fp: int, fn: int
) -> tuple[float | None, float | None, float | None]:
    """`None` for a metric whose denominator is zero (no detections for precision, no
    ground-truth positives for recall); F1 is `None` if either is, else their harmonic mean
    (`0.0` when both are `0.0`)."""
    precision = tp / (tp + fp) if (tp + fp) > 0 else None
    recall = tp / (tp + fn) if (tp + fn) > 0 else None
    if precision is None or recall is None:
        f1 = None
    elif precision + recall == 0:
        f1 = 0.0
    else:
        f1 = 2 * precision * recall / (precision + recall)
    return precision, recall, f1


def detection_metrics_per_message(
    messages: Iterable[tuple[Sequence[Span], Sequence[Span]]],
) -> dict[str, Any]:
    """Claim-instance detection P/R/F1 over many messages. Each item is one message's
    `(labels, detections)`; spans are character offsets into that message, so they are matched
    inside it and never against another message's spans. Only the per-message counts are summed.
    A detection matches a label when the claim type is the same and the spans overlap by at
    least one character."""
    tp = fp = fn = n_labels = n_detections = 0
    for labels, detections in messages:
        m = match_claims(labels, detections)
        tp += len(m.pairs)
        fp += len(m.unmatched_detections)
        fn += len(m.unmatched_labels)
        n_labels += len(labels)
        n_detections += len(detections)
    precision, recall, f1 = precision_recall_f1(tp, fp, fn)
    return {
        "n_labels": n_labels,
        "n_detections": n_detections,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": precision,
        "recall": recall,
        "f1": f1,
    }


def detection_metrics(labels: Sequence[Span], detections: Sequence[Span]) -> dict[str, Any]:
    """Claim-instance detection P/R/F1 for one message."""
    return detection_metrics_per_message([(labels, detections)])


# --------------------------------------------------------------------------------------
# Wilson 95% interval
# --------------------------------------------------------------------------------------


def wilson_interval(k: int, n: int, *, z: float = _Z95) -> tuple[float, float] | None:
    """Wilson score 95% confidence interval for the proportion `k / n`. `None` when `n == 0`
    (undefined). Clamped to `[0, 1]`."""
    if n <= 0:
        return None
    phat = k / n
    denom = 1 + z * z / n
    center = phat + z * z / (2 * n)
    adj = z * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n))
    lo = (center - adj) / denom
    hi = (center + adj) / denom
    return (max(0.0, lo), min(1.0, hi))


# --------------------------------------------------------------------------------------
# gate confusion units (claim level and turn level share this shape) + bootstrap F1
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ConfusionUnit:
    """One resample unit for a gate metric: whether the ground truth and the prediction are
    each positive for the positive class ("unsupported claim" at claim level, "blocked" at
    turn level) for one claim instance or one stop attempt. `key` is carried through only for
    grouping (by claim type, by scenario) and is not used by the statistics themselves."""

    truth_positive: bool
    predicted_positive: bool
    key: str = ""


def confusion_counts(units: Sequence[ConfusionUnit]) -> tuple[int, int, int, int]:
    tp = sum(1 for u in units if u.truth_positive and u.predicted_positive)
    fp = sum(1 for u in units if not u.truth_positive and u.predicted_positive)
    fn = sum(1 for u in units if u.truth_positive and not u.predicted_positive)
    tn = sum(1 for u in units if not u.truth_positive and not u.predicted_positive)
    return tp, fp, fn, tn


def bootstrap_f1(
    units: Sequence[ConfusionUnit],
    *,
    seed: int = DEFAULT_SEED,
    resamples: int = DEFAULT_RESAMPLES,
) -> tuple[float, float] | None:
    """Seeded bootstrap 95% interval for F1: `resamples` resamples of `units` drawn with
    replacement, `random.Random(seed)`, each index `int(rng.random() * n)` (spec-pinned so
    every run reproduces the same interval bit for bit). `None` when `units` is empty."""
    n = len(units)
    if n == 0:
        return None
    rng = random.Random(seed)
    pairs = [(u.truth_positive, u.predicted_positive) for u in units]
    values: list[float] = []
    for _ in range(resamples):
        tp = fp = fn = 0
        for _ in range(n):
            truth, predicted = pairs[int(rng.random() * n)]
            if truth and predicted:
                tp += 1
            elif predicted:
                fp += 1
            elif truth:
                fn += 1
        precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f1 = 0.0 if (precision + recall) == 0 else 2 * precision * recall / (precision + recall)
        values.append(f1)
    values.sort()
    lo_i = int(0.025 * resamples)
    hi_i = min(int(0.975 * resamples), resamples - 1)
    return (values[lo_i], values[hi_i])


@dataclass(frozen=True)
class GateSummary:
    n: int
    tp: int
    fp: int
    fn: int
    tn: int
    precision: float | None
    recall: float | None
    f1: float | None
    precision_wilson95: tuple[float, float] | None
    recall_wilson95: tuple[float, float] | None
    f1_bootstrap95: tuple[float, float] | None

    def to_json(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "tp": self.tp,
            "fp": self.fp,
            "fn": self.fn,
            "tn": self.tn,
            "precision": self.precision,
            "recall": self.recall,
            "f1": self.f1,
            "precision_wilson95": list(self.precision_wilson95)
            if self.precision_wilson95
            else None,
            "recall_wilson95": list(self.recall_wilson95) if self.recall_wilson95 else None,
            "f1_bootstrap95": list(self.f1_bootstrap95) if self.f1_bootstrap95 else None,
        }


def gate_summary(
    units: Sequence[ConfusionUnit],
    *,
    seed: int = DEFAULT_SEED,
    resamples: int = DEFAULT_RESAMPLES,
) -> GateSummary:
    """Precision/recall/F1 for the positive class over `units`, with Wilson intervals for P
    and R and a seeded bootstrap interval for F1 (spec item 1: "Wilson 95% intervals for P,
    R, FBR; F1 bootstrap 95% interval")."""
    tp, fp, fn, tn = confusion_counts(units)
    precision, recall, f1 = precision_recall_f1(tp, fp, fn)
    return GateSummary(
        n=len(units),
        tp=tp,
        fp=fp,
        fn=fn,
        tn=tn,
        precision=precision,
        recall=recall,
        f1=f1,
        precision_wilson95=wilson_interval(tp, tp + fp),
        recall_wilson95=wilson_interval(tp, tp + fn),
        f1_bootstrap95=bootstrap_f1(units, seed=seed, resamples=resamples),
    )


def false_block_rate(units: Sequence[ConfusionUnit]) -> dict[str, Any]:
    """Turn-level only (spec: "false-block rate = blocked share of stop attempts with no
    unsupported label"): `units` are turn-level confusion units (`truth_positive` = the stop
    attempt has an unsupported label, `predicted_positive` = it was blocked). FBR = FP / (FP
    + TN), the blocked share of the turns with no unsupported label at all."""
    _tp, fp, _fn, tn = confusion_counts(units)
    n = fp + tn
    rate = fp / n if n > 0 else None
    interval = wilson_interval(fp, n)
    return {"n": n, "blocked": fp, "rate": rate, "wilson95": list(interval) if interval else None}


__all__ = [
    "ConfusionUnit",
    "GateSummary",
    "MatchResult",
    "Span",
    "bootstrap_f1",
    "confusion_counts",
    "detection_metrics",
    "detection_metrics_per_message",
    "false_block_rate",
    "gate_summary",
    "match_claims",
    "precision_recall_f1",
    "wilson_interval",
]
