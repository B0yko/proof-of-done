#!/usr/bin/env python3
"""Development-set scorer for tuning `claims.py` (S6c; see PLAN.md §10 and spec item 4).

Scores claim-instance detection precision/recall/F1, per claim type and overall, on three
sets, using the same matching rule as `eval/metrics.py`: a detection matches a label when the
claim type is equal and the spans overlap by at least one character, labels matched greedily
one-to-one in `(start, end)` order.

  - `train`     first hex digit of sha256(id) in 0-7 (`eval/dev/messages.yaml`) -- tune here.
  - `check`     8-f of the same file -- generalisation check only, never fixed on directly.
  - `templated` `fixtures/expand.py` -- must stay >= 0.98 precision and recall.

`train` also prints every miss (a labelled claim with no matching detection) and false
positive (a detection with no matching label), so tuning can look only at `train` errors.

Never reads or touches `eval/heldout/` -- that set is reserved for the coordinator's own,
separate check after this step.
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
from typing import Any

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from eval import metrics  # noqa: E402
from fixtures.expand import expand  # noqa: E402
from proof_of_done import claims as claims_mod  # noqa: E402
from proof_of_done import config as config_mod  # noqa: E402
from proof_of_done import yamlload  # noqa: E402

DEV_MESSAGES_PATH = os.path.join(_REPO_ROOT, "eval", "dev", "messages.yaml")

# Same marker grammar as `fixtures/dsl.py` (`MARKER_RE`); reimplemented locally (like
# `tests/test_prefilter_keywords.py` already does) so this script depends on no fixture
# renderer -- dev-set entries carry no `labels:` block, only marked spans.
_MARKER_RE = re.compile(r"\[\[(?P<type>[a-z][a-z0-9_]*)(?:#\d+)?\|(?P<text>.*?)\]\]", re.DOTALL)


def strip_markers(text: str) -> tuple[str, list[tuple[str, int, int]]]:
    """Return `(message with every marker replaced by its inner text, [(claim_type, start,
    end), ...])`, offsets into the returned message."""
    pieces: list[str] = []
    spans: list[tuple[str, int, int]] = []
    pos = 0
    length = 0
    for m in _MARKER_RE.finditer(text):
        before = text[pos : m.start()]
        pieces.append(before)
        length += len(before)
        inner = m.group("text")
        start = length
        pieces.append(inner)
        length += len(inner)
        end = length
        spans.append((m.group("type"), start, end))
        pos = m.end()
    pieces.append(text[pos:])
    return "".join(pieces), spans


# ---------------------------------------------------------------------------------------
# dev-set loading and the train/check split
# ---------------------------------------------------------------------------------------


def load_dev_entries() -> list[dict[str, Any]]:
    with open(DEV_MESSAGES_PATH, encoding="utf-8") as fh:
        data = yamlload.safe_load(fh.read())
    if not isinstance(data, list):
        raise ValueError(f"{DEV_MESSAGES_PATH}: expected a top-level list")
    return data


def split_of(entry_id: str) -> str:
    """`train` = first hex digit of sha256(id) in 0-7, `check` = 8-f (eval/dev/README.md)."""
    digest = hashlib.sha256(entry_id.encode("utf-8")).hexdigest()
    return "train" if digest[0] in "01234567" else "check"


# ---------------------------------------------------------------------------------------
# templated-set messages (built-in claim types only; no evidence/config layering needed
# for pure claim-detection scoring)
# ---------------------------------------------------------------------------------------


def _walk_step(step: dict[str, Any]) -> Any:
    if "subagent" in step:
        yield from _messages_with_labels(step["subagent"])
    elif "parallel" in step:
        for item in step["parallel"]:
            yield from _walk_step(item)


def _messages_with_labels(body: dict[str, Any]) -> Any:
    if "final" in body:
        yield strip_markers(body["final"])
    for step in body.get("steps", []):
        yield from _walk_step(step)


def templated_messages() -> list[tuple[str, list[tuple[str, int, int]]]]:
    out: list[tuple[str, list[tuple[str, int, int]]]] = []
    for scenario in expand():
        for turn in scenario["turns"]:
            out.extend(_messages_with_labels(turn))
    return out


# ---------------------------------------------------------------------------------------
# built-in rules (defaults.yaml only, no user/project/env layers)
# ---------------------------------------------------------------------------------------


def load_builtin_rules() -> tuple[claims_mod.ClaimRule, ...]:
    def reader(path: str) -> str | None:
        if path == config_mod.defaults_path():
            with open(path, encoding="utf-8") as handle:
                return handle.read()
        return None

    layers = config_mod.load_layers("/work/demo-app", {}, reader)
    cfg, _notes = config_mod.effective(layers, {}, tampered_paths=set(), settings_tampered=False)
    return tuple(
        claims_mod.ClaimRule(id=r.id, claim_type=r.claim_type, patterns=r.claims, keywords=r.keywords)
        for r in cfg.rules
        if r.action != "off"
    )


# ---------------------------------------------------------------------------------------
# detection: run claims.detect, then merge overlapping same-type spans into one instance
# each (mirrors `proof_of_done.engine._group_claims`, so a message with several rules/
# patterns matching the same claim does not spuriously multiply detections)
# ---------------------------------------------------------------------------------------


def grouped_detections(
    message: str, rules: tuple[claims_mod.ClaimRule, ...]
) -> list[tuple[str, int, int]]:
    found = claims_mod.detect(message, rules)
    groups: list[list[Any]] = []  # each: [claim_type, start, end]
    for c in found:
        merged = False
        for g in groups:
            if g[0] == c.claim_type and c.span[0] < g[2] and c.span[1] > g[1]:
                g[1] = min(g[1], c.span[0])
                g[2] = max(g[2], c.span[1])
                merged = True
                break
        if not merged:
            groups.append([c.claim_type, c.span[0], c.span[1]])
    return [(g[0], g[1], g[2]) for g in groups]


# ---------------------------------------------------------------------------------------
# scoring: per-message matching, counts summed across messages (spans are per-message, so
# pooling raw offsets across messages before matching would risk spurious cross-message
# matches; matching stays local to each message, only the tp/fp/fn counts are aggregated)
# ---------------------------------------------------------------------------------------


class Scorer:
    def __init__(self) -> None:
        self.tp = 0
        self.fp = 0
        self.fn = 0
        self.by_type: dict[str, list[int]] = {}  # claim_type -> [tp, fp, fn]
        self.misses: list[tuple[str, str, str]] = []  # (entry_id, claim_type, quote)
        self.false_positives: list[tuple[str, str, str]] = []  # (entry_id, claim_type, quote)

    def _bucket(self, claim_type: str) -> list[int]:
        return self.by_type.setdefault(claim_type, [0, 0, 0])

    def add(
        self,
        entry_id: str,
        message: str,
        labels: list[tuple[str, int, int]],
        detections: list[tuple[str, int, int]],
        *,
        record_errors: bool = False,
    ) -> None:
        label_spans = [metrics.Span(claim_type=t, start=s, end=e) for t, s, e in labels]
        det_spans = [metrics.Span(claim_type=t, start=s, end=e) for t, s, e in detections]
        m = metrics.match_claims(label_spans, det_spans)
        self.tp += len(m.pairs)
        self.fp += len(m.unmatched_detections)
        self.fn += len(m.unmatched_labels)
        for label, _det in m.pairs:
            self._bucket(label.claim_type)[0] += 1
        for det in m.unmatched_detections:
            self._bucket(det.claim_type)[1] += 1
            if record_errors:
                quote = message[det.start : det.end]
                self.false_positives.append((entry_id, det.claim_type, quote))
        for label in m.unmatched_labels:
            self._bucket(label.claim_type)[2] += 1
            if record_errors:
                quote = message[label.start : label.end]
                self.misses.append((entry_id, label.claim_type, quote))

    def overall(self) -> tuple[float | None, float | None, float | None]:
        return metrics.precision_recall_f1(self.tp, self.fp, self.fn)

    def per_type(self) -> dict[str, tuple[float | None, float | None, float | None]]:
        return {
            claim_type: metrics.precision_recall_f1(tp, fp, fn)
            for claim_type, (tp, fp, fn) in sorted(self.by_type.items())
        }


def score_dev_split(
    entries: list[dict[str, Any]], rules: tuple[claims_mod.ClaimRule, ...], split: str
) -> Scorer:
    scorer = Scorer()
    for entry in entries:
        if split_of(entry["id"]) != split:
            continue
        message, labels = strip_markers(entry["text"])
        detections = grouped_detections(message, rules)
        scorer.add(entry["id"], message, labels, detections, record_errors=(split == "train"))
    return scorer


def score_templated(rules: tuple[claims_mod.ClaimRule, ...]) -> Scorer:
    scorer = Scorer()
    for i, (message, labels) in enumerate(templated_messages()):
        detections = grouped_detections(message, rules)
        scorer.add(f"templated#{i}", message, labels, detections)
    return scorer


# ---------------------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------------------


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.4f}"


def print_report(name: str, scorer: Scorer) -> None:
    precision, recall, f1 = scorer.overall()
    print(f"\n=== {name} ===")
    print(
        f"overall: tp={scorer.tp} fp={scorer.fp} fn={scorer.fn} "
        f"precision={_fmt(precision)} recall={_fmt(recall)} f1={_fmt(f1)}"
    )
    for claim_type, (p, r, f) in scorer.per_type().items():
        tp, fp, fn = scorer.by_type[claim_type]
        print(
            f"  {claim_type:16s} tp={tp:3d} fp={fp:3d} fn={fn:3d} "
            f"precision={_fmt(p)} recall={_fmt(r)} f1={_fmt(f)}"
        )
    if scorer.misses:
        print(f"-- misses ({len(scorer.misses)}) --")
        for entry_id, claim_type, quote in scorer.misses:
            print(f"  MISS {entry_id} [{claim_type}] {quote!r}")
    if scorer.false_positives:
        print(f"-- false positives ({len(scorer.false_positives)}) --")
        for entry_id, claim_type, quote in scorer.false_positives:
            print(f"  FP   {entry_id} [{claim_type}] {quote!r}")


def main(argv: list[str] | None = None) -> int:
    del argv  # no options: this script always scores train/check/templated and prints
    rules = load_builtin_rules()
    entries = load_dev_entries()

    train_scorer = score_dev_split(entries, rules, "train")
    check_scorer = score_dev_split(entries, rules, "check")
    templated_scorer = score_templated(rules)

    print_report("train", train_scorer)
    print_report("check", check_scorer)
    print_report("templated", templated_scorer)
    return 0


if __name__ == "__main__":
    sys.exit(main())
