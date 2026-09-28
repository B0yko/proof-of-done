"""The fast-path prefilter invariant (spec item 9 / PLAN.md §9 & §15): every labelled claim
quote in the templated set (`fixtures/expand.py`) and the adversarial set (`eval/adversarial/`)
must contain a keyword of its own claim type, and every built-in pattern match on those same
sets must too. If either ever broke, a real claim could silently miss the hook's keyword
prefilter and never reach the claim detector at all.
"""

from __future__ import annotations

import glob
import os
import re
import sys
from typing import Any

import pytest

from proof_of_done import config
from proof_of_done.claims import ClaimRule, detect
from proof_of_done.yamlload import safe_load

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from fixtures.expand import expand  # noqa: E402


def _load_builtin_rules() -> tuple[ClaimRule, ...]:
    def reader(path: str) -> str | None:
        if path == config.defaults_path():
            with open(path, encoding="utf-8") as handle:
                return handle.read()
        return None

    layers = config.load_layers("/work/demo-app", {}, reader)
    cfg, _notes = config.effective(layers, {}, tampered_paths=set(), settings_tampered=False)
    return tuple(
        ClaimRule(id=r.id, claim_type=r.claim_type, patterns=r.claims, keywords=r.keywords)
        for r in cfg.rules
    )


RULES = _load_builtin_rules()
KEYWORDS_BY_TYPE = {rule.claim_type: rule.keywords for rule in RULES}


def _contains_a_keyword(text: str, claim_type: str) -> bool:
    lowered = text.lower()
    return any(kw in lowered for kw in KEYWORDS_BY_TYPE[claim_type])


# ---------------------------------------------------------------------------------------
# Templated set: every labelled claim quote (including inside subagent transcripts)
# ---------------------------------------------------------------------------------------

_MARKER_RE = re.compile(r"\[\[(?P<type>[a-z][a-z0-9_]*)(?:#\d+)?\|(?P<text>.*?)\]\]")


def _strip_and_spans(final: str, labels: dict[str, Any]) -> tuple[str, list[tuple[str, int, int]]]:
    pieces: list[str] = []
    spans: list[tuple[str, int, int]] = []
    pos = 0
    for m in _MARKER_RE.finditer(final):
        pieces.append(final[pos : m.start()])
        start = sum(len(x) for x in pieces)
        text = m.group("text")
        pieces.append(text)
        end = start + len(text)
        claim_type = m.group("type")
        if claim_type in labels:
            spans.append((claim_type, start, end))
        pos = m.end()
    pieces.append(final[pos:])
    return "".join(pieces), spans


def _walk_step(step: dict[str, Any]) -> Any:
    if "subagent" in step:
        yield from _messages_with_labels(step["subagent"])
    elif "parallel" in step:
        for item in step["parallel"]:
            yield from _walk_step(item)


def _messages_with_labels(body: dict[str, Any]) -> Any:
    if "final" in body:
        yield _strip_and_spans(body["final"], body.get("labels", {}))
    for step in body.get("steps", []):
        yield from _walk_step(step)


def _templated_messages() -> list[tuple[str, list[tuple[str, int, int]]]]:
    out = []
    for scenario in expand():
        for turn in scenario["turns"]:
            out.extend(_messages_with_labels(turn))
    return out


TEMPLATED_MESSAGES = _templated_messages()

TEMPLATED_LABEL_CASES = [
    (message, claim_type, start, end)
    for message, spans in TEMPLATED_MESSAGES
    for claim_type, start, end in spans
]


def test_templated_set_is_non_trivial() -> None:
    # A canary against an accidental empty fixture set silently passing every case below.
    assert len(TEMPLATED_LABEL_CASES) >= 900


@pytest.mark.parametrize(
    "message,claim_type,start,end",
    TEMPLATED_LABEL_CASES,
    ids=[f"{ct}:{msg[start:end]!r}" for msg, ct, start, end in TEMPLATED_LABEL_CASES],
)
def test_every_labelled_claim_quote_in_templated_set_has_a_keyword(
    message: str, claim_type: str, start: int, end: int
) -> None:
    quote = message[start:end]
    assert _contains_a_keyword(quote, claim_type), (claim_type, quote)


def test_every_builtin_pattern_match_on_templated_set_contains_a_keyword() -> None:
    checked = 0
    for message, _spans in TEMPLATED_MESSAGES:
        for claim in detect(message, RULES):
            checked += 1
            assert _contains_a_keyword(claim.quote, claim.claim_type), (
                claim.claim_type,
                claim.quote,
                message,
            )
    assert checked >= 900


# ---------------------------------------------------------------------------------------
# Adversarial set: same invariant
# ---------------------------------------------------------------------------------------

_ADVERSARIAL_DIR = os.path.join(_REPO_ROOT, "eval", "adversarial")


def _adversarial_messages() -> list[tuple[str, list[tuple[str, int, int]]]]:
    out = []
    for path in sorted(glob.glob(os.path.join(_ADVERSARIAL_DIR, "*.yaml"))):
        with open(path, encoding="utf-8") as handle:
            doc = safe_load(handle.read())
        for turn in doc.get("turns", []):
            final = turn.get("final")
            if final is None:
                continue
            out.append(_strip_and_spans(final, turn.get("labels", {})))
    return out


ADVERSARIAL_MESSAGES = _adversarial_messages()

ADVERSARIAL_LABEL_CASES = [
    (message, claim_type, start, end)
    for message, spans in ADVERSARIAL_MESSAGES
    for claim_type, start, end in spans
]


def test_adversarial_set_is_non_trivial() -> None:
    assert len(ADVERSARIAL_MESSAGES) >= 30


@pytest.mark.parametrize(
    "message,claim_type,start,end",
    ADVERSARIAL_LABEL_CASES,
    ids=[f"{ct}:{msg[start:end]!r}" for msg, ct, start, end in ADVERSARIAL_LABEL_CASES],
)
def test_every_labelled_claim_quote_in_adversarial_set_has_a_keyword(
    message: str, claim_type: str, start: int, end: int
) -> None:
    quote = message[start:end]
    assert _contains_a_keyword(quote, claim_type), (claim_type, quote)


def test_every_builtin_pattern_match_on_adversarial_set_contains_a_keyword() -> None:
    for message, _spans in ADVERSARIAL_MESSAGES:
        for claim in detect(message, RULES):
            assert _contains_a_keyword(claim.quote, claim.claim_type), (
                claim.claim_type,
                claim.quote,
                message,
            )
