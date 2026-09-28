"""Deterministic claim detection (stdlib ``re`` only).

Finds completion claims ("all tests pass", "the build succeeds", "fixed the bug", ...) in an
assistant's final message. Detection never looks at the transcript: it is pure text analysis,
so the same message always yields the same claims regardless of what actually happened in the
session (that comparison is :mod:`proof_of_done.evidence`'s job).

Pipeline (see PLAN.md §10 and spec item 4):

1. :func:`_mask` blanks out fenced code, inline code, block quotes, quoted user text and
   ``**``/``__`` emphasis markers, replacing each region with same-length filler so every
   offset computed downstream still indexes the original message.
2. :func:`_split_units` breaks the masked message into sentences, Markdown bullet items and
   table rows; :func:`_split_clauses` further splits each unit on clause boundaries (``;``,
   dashes, a handful of conjunctions, and parenthetical asides).
3. Each rule's compiled patterns are matched against each clause. A pattern is expected to
   expose its verb/adjective as a named group ``pred``; patterns that do not use the whole
   match as the predicate span.
4. A match is rejected when the clause is negated, hedged/conditional, about the future, a
   question, an instruction to the user, or a non-finite predicate ("to pass", "make the
   build pass"). See the ``_is_*`` / ``_is_negated`` helpers below.
5. Surviving matches become :class:`Claim` objects, with ``quote`` sliced from the
   *original* (unmasked) message so masked placeholder characters never leak into it.

:func:`prefilter` is the cheap first gate the hook runs before any of this: a message that
contains none of the enabled rules' keywords cannot produce a claim, so the transcript is
never even parsed.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from re import Pattern

# ---------------------------------------------------------------------------------------
# Public data model
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ClaimRule:
    """The claim-related slice of a config rule: everything :func:`detect` needs to look for
    one rule's claims. Built from ``proof_of_done.config.Rule`` by the engine; kept separate
    here so this module never imports ``config`` (the hook path stays stdlib-only end to
    end, and ``claims.py`` in particular must not need anything beyond ``re``)."""

    id: str
    claim_type: str
    patterns: Sequence[Pattern[str]]
    keywords: Sequence[str]


@dataclass(frozen=True)
class Claim:
    """One detected claim instance: which rule matched, its claim type, the verbatim quote
    from the original message, and the quote's ``(start, end)`` character span in that
    message."""

    rule_id: str
    claim_type: str
    quote: str
    span: tuple[int, int]


@dataclass(frozen=True)
class Rejection:
    """One pattern match a rule found that a rejection filter then dropped: ``filter_name``
    is one of ``negated``, ``hedged``, ``future``, ``question``, ``instruction``,
    ``nonfinite`` or ``subsumed`` (see :func:`detect_with_rejections`). Used only by the
    dev-set ``--filters`` audit (PLAN.md §10 / spec item B), which cross-checks each filter
    against labelled dev claims: how often it silently kills a real claim versus correctly
    clearing a non-claim."""

    filter_name: str
    rule_id: str
    claim_type: str
    span: tuple[int, int]


@dataclass(frozen=True)
class _Survivor:
    """A match that passed every clause-level filter (negation, hedge, future, question,
    instruction, non-finite predicate) for one unit, pending the cross-clause ``subsumed``
    check that runs once the whole unit's survivors are known."""

    rule_id: str
    claim_type: str
    start: int
    end: int
    narration: bool


# ---------------------------------------------------------------------------------------
# Masking: same-length text, offsets into the original message preserved throughout
# ---------------------------------------------------------------------------------------

_MASK_CHAR = " "
_INLINE_CODE_FILL = "·"  # `·`, a printable non-whitespace filler a pattern can match

_FENCE_RE = re.compile(r"^([ \t]*)(`{3,}|~{3,})")


def _blank(s: str) -> str:
    return _MASK_CHAR * len(s)


def _mask_fenced_code(text: str) -> str:
    """Blank fenced code blocks (``` or ~~~, matched by the same character run) to spaces.
    A closing fence only needs to *start* a line with a matching run, same as CommonMark; any
    text trailing it on that same physical line (a careless concatenation of several
    Markdown snippets onto one line, as the adversarial/decoy fixtures do, rather than a
    proper Markdown document) sits after the block and is left for later masking passes and
    detection, not swallowed into the masked region. An opening fence with no matching close
    masks through the end of the text, same as before."""
    lines = text.split("\n")
    out = list(lines)
    i = 0
    n = len(lines)
    while i < n:
        m = _FENCE_RE.match(lines[i])
        if not m:
            i += 1
            continue
        fence_char = m.group(2)[0]
        j = i + 1
        close_m = None
        while j < n:
            cm = _FENCE_RE.match(lines[j])
            if cm and cm.group(2)[0] == fence_char:
                close_m = cm
                break
            j += 1
        if close_m is None:
            for k in range(i, n):
                out[k] = _blank(out[k])
            i = n
            continue
        for k in range(i, j):
            out[k] = _blank(out[k])
        prefix_len = close_m.end()
        out[j] = _blank(lines[j][:prefix_len]) + lines[j][prefix_len:]
        i = j + 1
    return "\n".join(out)


_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")


def _mask_inline_code(text: str) -> str:
    return _INLINE_CODE_RE.sub(lambda m: _INLINE_CODE_FILL * len(m.group(0)), text)


_BLOCKQUOTE_LINE_RE = re.compile(r"^[ \t]*>")


def _mask_blockquotes(text: str) -> str:
    lines = text.split("\n")
    out = [_blank(line) if _BLOCKQUOTE_LINE_RE.match(line) else line for line in lines]
    return "\n".join(out)


# Straight or curly double quotes, one line, <= 200 chars total (quotes included).
_QUOTED_TEXT_RE = re.compile('"[^"\n]{0,198}"|“[^”\n]{0,198}”')


def _mask_quoted_text(text: str) -> str:
    return _QUOTED_TEXT_RE.sub(lambda m: _blank(m.group(0)), text)


_EMPHASIS_MARKER_RE = re.compile(r"\*\*|__")


def _mask_emphasis(text: str) -> str:
    return _EMPHASIS_MARKER_RE.sub("  ", text)


# A paired em/en-dash aside (`" — 530 tests — "`) with a *second* dash within 40 characters,
# no dash in between, and a digit somewhere inside -- the mark of a short count insertion, not
# a claim of its own. Blanked like a parenthetical so a claim's subject and predicate can still
# meet across it ("the full suite — 530 tests — passes clean" reads as one clause instead of
# three; PLAN.md §10 / spec item B, "parenthetical/dash insertions between subject and
# predicate"). The digit requirement keeps a longer dash-delimited *claim* used as a list
# separator intact instead of blanking it away ("`cargo build` — Finished release [optimized]
# target(s) — and `cargo test` — 29 passed" has no digit in the first aside, so it is left for
# the ordinary single-dash clause separator, and its "target" claim stays detectable). A lone,
# unpaired dash (no second dash nearby) is untouched too, so it still works as a clause
# separator ("Ran the suite twice to be sure — every test is green now").
_DASH_ASIDE_RE = re.compile(r"\s+[—–]\s+(?=[^—–\n]*\d)[^—–\n]{1,40}?\s+[—–](?=\s|$)")


def _mask_dash_asides(text: str) -> str:
    return _DASH_ASIDE_RE.sub(lambda m: _blank(m.group(0)), text)


def _mask(message: str) -> str:
    """Blank out fenced code, inline code, block quotes, quoted user text, paired dash
    asides and ``**``/``__`` emphasis markers, in that order, returning text the same length
    as ``message``."""
    masked = _mask_fenced_code(message)
    masked = _mask_inline_code(masked)
    masked = _mask_blockquotes(masked)
    masked = _mask_quoted_text(masked)
    masked = _mask_dash_asides(masked)
    masked = _mask_emphasis(masked)
    return masked


# ---------------------------------------------------------------------------------------
# Units: sentences, Markdown bullets, table rows
# ---------------------------------------------------------------------------------------

_BULLET_RE = re.compile(r"^([ \t]*)([-*+•]|\d+[.)])([ \t]+)")
_TABLE_ROW_RE = re.compile(r"^[ \t]*\|")

_SENT_END_RE = re.compile(r"[.!?]+")
_LAST_TOKEN_RE = re.compile(r"\S+$")
_FILENAME_EXT_RE = re.compile(
    r"\.(?:py|pyi|js|jsx|ts|tsx|mjs|cjs|go|rs|rb|java|kt|c|cc|cpp|h|hpp|md|markdown|"
    r"ya?ml|json|toml|sh|cfg|ini|txt|log|lock|cfg)$",
    re.IGNORECASE,
)


def _looks_like_filename(token: str) -> bool:
    return bool(_FILENAME_EXT_RE.search(token))


def _trim_span(text: str, start: int, end: int) -> tuple[int, int]:
    seg = text[start:end]
    left = len(seg) - len(seg.lstrip())
    right = len(seg) - len(seg.rstrip())
    return start + left, end - right


_ELLIPSIS_RE = re.compile(r"^\.\.\.+$")


# A run of text with no recognized sentence boundary (an unfenced, un-bulleted dump of log or
# path-like lines, none of them ending in `.!?` followed by whitespace -- `_looks_like_filename`
# excludes most of their periods) would otherwise become one very long "sentence" clause, and
# every pattern's `finditer` scans each clause once per rule, so clause length matters directly
# for `detect()`'s latency on a large message. `_MAX_SENTENCE_LEN` caps that: a span this long
# gets cut further at its own newlines (falling back to a hard slice if a single physical line
# is still this long), never merging distinct source lines' worth of unrelated text into one
# match target. Ordinary prose paragraphs never come close to this length between `.!?` marks.
_MAX_SENTENCE_LEN = 400


def _cap_span(text: str, start: int, end: int) -> list[tuple[int, int]]:
    if end - start <= _MAX_SENTENCE_LEN:
        return [(start, end)]
    out: list[tuple[int, int]] = []
    piece_start = start
    pos = start
    while pos < end:
        nl = text.find("\n", pos, end)
        if nl == -1:
            break
        if nl + 1 - piece_start > _MAX_SENTENCE_LEN:
            s, e = _trim_span(text, piece_start, nl + 1)
            if e > s:
                out.append((s, e))
            piece_start = nl + 1
        pos = nl + 1
    while end - piece_start > _MAX_SENTENCE_LEN:
        cut = piece_start + _MAX_SENTENCE_LEN
        s, e = _trim_span(text, piece_start, cut)
        if e > s:
            out.append((s, e))
        piece_start = cut
    s, e = _trim_span(text, piece_start, end)
    if e > s:
        out.append((s, e))
    return out


def _split_sentences(text: str) -> list[tuple[int, int]]:
    """Split `text` into sentence spans at ``.``/``!``/``?`` runs followed by whitespace (or
    end of text), skipping a boundary when it sits inside a number/decimal (no whitespace
    follows), right after a token that looks like a file name (``core.py``), or at an
    ellipsis (``...``, as in a shell glob like ``go test ./...``) immediately followed by a
    lowercase letter, the usual sign that the same sentence continues. A span that ends up
    unreasonably long (no sentence punctuation found for a while) is additionally capped by
    :func:`_cap_span`."""
    spans: list[tuple[int, int]] = []
    start = 0
    n = len(text)
    for m in _SENT_END_RE.finditer(text):
        end = m.end()
        if end < n and not text[end].isspace():
            continue
        preceding_tok_m = _LAST_TOKEN_RE.search(text[start : m.start()])
        if preceding_tok_m and _looks_like_filename(preceding_tok_m.group(0)):
            continue
        if _ELLIPSIS_RE.match(m.group(0)):
            j = end
            while j < n and text[j].isspace():
                j += 1
            if j < n and text[j].islower():
                continue
        s, e = _trim_span(text, start, end)
        if e > s:
            spans.extend(_cap_span(text, s, e))
        start = end
    if start < n:
        s, e = _trim_span(text, start, n)
        if e > s:
            spans.extend(_cap_span(text, s, e))
    return spans


def _split_units(masked: str) -> list[tuple[str, int, int]]:
    """Split masked text into ``(text, start, end)`` units: a bullet item is one unit (its
    marker stripped), a table row is one unit (pipes kept), and runs of ordinary lines
    between such markers are sentence-split as one paragraph each."""
    units: list[tuple[str, int, int]] = []
    lines: list[tuple[str, int, int]] = []
    pos = 0
    for line in masked.split("\n"):
        lines.append((line, pos, pos + len(line)))
        pos += len(line) + 1

    para_start_idx: list[int] = []  # 0 or 1 element used as a mutable box

    def flush_para(end_idx: int) -> None:
        if not para_start_idx:
            return
        first_start = lines[para_start_idx[0]][1]
        last_end = lines[end_idx - 1][2]
        para_start_idx.clear()
        if last_end <= first_start:
            return
        para_text = masked[first_start:last_end]
        for s, e in _split_sentences(para_text):
            units.append((para_text[s:e], first_start + s, first_start + e))

    for i, (line, start, end) in enumerate(lines):
        bullet_m = _BULLET_RE.match(line)
        if bullet_m:
            flush_para(i)
            content_start = start + bullet_m.end()
            if end > content_start:
                units.append((masked[content_start:end], content_start, end))
            continue
        if _TABLE_ROW_RE.match(line):
            flush_para(i)
            if end > start:
                units.append((masked[start:end], start, end))
            continue
        if not para_start_idx:
            para_start_idx.append(i)
    flush_para(len(lines))

    units.sort(key=lambda u: u[1])
    return [u for u in units if u[0].strip()]


# ---------------------------------------------------------------------------------------
# Clauses
# ---------------------------------------------------------------------------------------

_CLAUSE_SEP_RE = re.compile(
    r";|\s+—\s+|\s+–\s+|,\s+but\b|,\s+however\b|,\s+although\b|,\s+though\b|"
    r",\s+while\b|,\s+whereas\b|,\s+so\s+|,\s+and\s+",
    re.IGNORECASE,
)
_PAREN_RE = re.compile(r"\(([^()]*)\)")


def _split_clauses(text: str) -> list[tuple[int, int]]:
    """Split one unit's text into clause spans: each top-level ``(...)`` becomes its own
    clause, the surrounding text (with those spans cut out) is further split on ``;``,
    en/em dashes and a handful of conjunctions."""
    raw: list[tuple[int, int]] = []
    cursor = 0
    outer_fragments: list[tuple[int, int]] = []
    for m in _PAREN_RE.finditer(text):
        if m.start() > cursor:
            outer_fragments.append((cursor, m.start()))
        inner_start, inner_end = m.start(1), m.end(1)
        if inner_end > inner_start:
            raw.append((inner_start, inner_end))
        cursor = m.end()
    if cursor < len(text):
        outer_fragments.append((cursor, len(text)))

    for frag_start, frag_end in outer_fragments:
        frag = text[frag_start:frag_end]
        pos = 0
        for sm in _CLAUSE_SEP_RE.finditer(frag):
            if sm.start() > pos:
                raw.append((frag_start + pos, frag_start + sm.start()))
            pos = sm.end()
        if pos < len(frag):
            raw.append((frag_start + pos, frag_start + len(frag)))

    trimmed = [_trim_span(text, s, e) for s, e in raw]
    return [(s, e) for s, e in trimmed if e > s]


# ---------------------------------------------------------------------------------------
# Rejection filters
# ---------------------------------------------------------------------------------------

_WORD_RE = re.compile(r"[A-Za-z']+")
_SINGLE_NEGATORS = {"not", "never", "no", "without", "cannot", "nor", "nothing"}
_MULTI_NEGATORS = {("unable", "to")}
_STRENGTHENER_RE = re.compile(
    r"^(?:errors?|failures?|warnings?|issues?|problems?|bugs?|regressions?|crash(?:es)?|"
    r"violations?|offen[cs]es?|findings?|diagnostics?)$",
    re.IGNORECASE,
)
# A "bad-outcome" adjective right after "no"/"without" also strengthens rather than negates:
# "no failing tests" and "no broken builds" claim the positive outcome the same way "no
# errors" does, even though the noun that follows (tests/builds) is not itself a
# strengthener.
_BAD_OUTCOME_ADJ_RE = re.compile(r"^(?:failing|broken|red)$", re.IGNORECASE)
# "no longer" governing one of these complaint verbs describes a resolved, positive state
# ("the linter no longer flags anything"), not a negation of it -- unlike a good-outcome verb
# ("the tests no longer pass"), which "no longer" still negates as usual.
_NO_LONGER_COMPLAINT_RE = re.compile(
    r"^(?:flags?|flagging|complains?|complaining|finds?|finding)$", re.IGNORECASE
)
_PUNCT_BREAK_RE = re.compile(r"[,;:]")
# The neutral adverbs the built-in patterns in defaults.yaml allow between subject and
# predicate ("tests still pass"); keep the two lists in step.
_NEUTRAL_ADVERBS = {"still", "already", "now", "finally", "consistently", "reliably", "also"}


def _tokens(text: str) -> list[tuple[str, int, int]]:
    return [(m.group(0), m.start(), m.end()) for m in _WORD_RE.finditer(text)]


def _is_negated(clause: str, pred_start: int) -> bool:
    """A negator governs the predicate when it appears within a 3-token window immediately
    before it, not counting neutral adverbs ("Not all tests still pass" is negated just like
    "Not all tests pass"). ``no``/``without`` do not negate when either of the next two words
    in the clause is a "strengthener" noun (errors/failures/warnings/issues/problems/
    violations/offenses/findings/diagnostics): "No errors, tests pass" and "No lint errors
    remain" both still claim the positive outcome, whether the strengthener noun sits right
    after the negator or is itself the predicate. Nor does ``no`` negate when the very next
    word is a "bad-outcome" adjective (failing/broken/red: "no failing tests") or "longer"
    ("no longer flags anything") -- both still describe a resolved, positive state."""
    all_tokens = _tokens(clause)
    before = [t for t in all_tokens if t[1] < pred_start and t[0].lower() not in _NEUTRAL_ADVERBS]
    window = before[-3:]
    for i, (word, start, _end) in enumerate(window):
        lw = word.lower()
        is_neg = (
            lw.endswith("n't")
            or lw in _SINGLE_NEGATORS
            or (i + 1 < len(window) and (lw, window[i + 1][0].lower()) in _MULTI_NEGATORS)
        )
        if not is_neg:
            continue
        if _PUNCT_BREAK_RE.search(clause, start, pred_start):
            # "No, the tests pass" / "Nothing new, tests pass": punctuation between the
            # negator and the predicate means the negator does not govern it.
            continue
        if lw in ("no", "without"):
            after = [t for t in all_tokens if t[1] > start][:3]
            if any(_STRENGTHENER_RE.match(tok) for tok, _s, _e in after):
                continue
            if after and _BAD_OUTCOME_ADJ_RE.match(after[0][0]):
                continue
            if lw == "no" and after and after[0][0].lower() == "longer":
                after3 = [t for t in all_tokens if t[1] > start][:3]
                if len(after3) >= 2 and _NO_LONGER_COMPLAINT_RE.match(after3[1][0]):
                    # "no longer flags anything" / "no longer complains": an aspectual
                    # marker over a complaint verb describes a resolved, positive state, not
                    # a negation of the claim that follows it.
                    continue
        return True
    return False


# Hedge/conditional markers reject the whole clause wherever they sit in it: a trailing
# "if"/"once"/"unless" still makes the claim conditional ("The suite passes, if the fixture
# data is unchanged."), just as a leading "should"/"might" would.
_HEDGE_RE = re.compile(
    r"\b(?:should|would|might|may|could|expects?|expecting|likely|probably|"
    r"hopefully|once|if|unless|assuming|seems?|appears?)\b|i\s+think|i\s+believe|"
    r"after\s+you|to\s+confirm(?!\s*:)|\bexpected\s+to\b",
    re.IGNORECASE,
)
# "expected" alone is an adjective/idiom, not a hedge, in "the expected output/fixture/shape"
# and "works as expected" -- only "expected to <verb>" (spec's "expect" family) hedges.
# "to confirm" hedges a forward-looking check ("run it again to confirm") but not one already
# reported: "to confirm:" introduces the result right there ("a plain eslint pass to confirm:
# nothing left to flag" is a claim, not a hedge).


def _is_hedged(clause: str, _pred_end: int) -> bool:
    return bool(_HEDGE_RE.search(clause))


_FUTURE_RE = re.compile(
    r"\bwill\b|'ll\b|\bgoing\s+to\b|\bnext\s+step\b|\babout\s+to\b", re.IGNORECASE
)


def _is_future(clause: str) -> bool:
    return bool(_FUTURE_RE.search(clause))


def _is_question(clause: str) -> bool:
    return clause.rstrip().endswith("?")


_INSTRUCTION_RE = re.compile(
    r"^(?:re-run|rerun|run|try|execute|check|verify|confirm|make\s+sure|ensure|please|"
    r"you\s+can|you\s+should|you\s+may|feel\s+free\s+to|go\s+ahead\s+and)\b",
    re.IGNORECASE,
)


def _is_instruction(clause: str) -> bool:
    return bool(_INSTRUCTION_RE.match(clause.lstrip()))


_NONFINITE_RES = (
    re.compile(r"\bto\s+pass\b", re.IGNORECASE),
    re.compile(r"\bmake\b[^.!?]{0,60}?\bpass\b", re.IGNORECASE),
    re.compile(r"\bget\b[^.!?]{0,60}?\bto\s+pass\b", re.IGNORECASE),
)


def _is_nonfinite(clause: str) -> bool:
    return any(r.search(clause) for r in _NONFINITE_RES)


# ---------------------------------------------------------------------------------------
# Cross-clause filter: a "fixed"/"verified" lead-in subsumed by a later automated-check claim
# in the same unit (spec item C / PLAN.md §10: "fixed inside descriptions of bugs" -- "Fixed
# the broken import path, and the app compiles now" has one checkable claim, build_passed;
# "Fixed the broken import path" is scene-setting, not a second claim needing its own
# evidence). Scoped narrowly: it only drops a *narration-shaped* match (no copula before the
# predicate, so "Fixed the X" / bare "confirmed"/"checked"/"tested" qualify but "the race
# condition IS resolved" and "that's fixed" do not) that sits outside any parenthetical aside
# (a paren clause is where the real, distinct restatement usually lives: "Fixed the pagination
# bug (resolved the off-by-one)"), and only when a tests/build/lint/typecheck claim survives
# later in the very same sentence.
# ---------------------------------------------------------------------------------------

_CHECK_CLAIM_TYPES = frozenset({"tests_passed", "build_passed", "lint_clean", "typecheck_clean"})
_NARRATION_CLAIM_TYPES = frozenset({"fixed", "verified"})
_COPULA_TOKENS = frozenset({"is", "are", "was", "were", "has", "have", "being", "been"})


def _has_copula_before(clause: str, pred_start: int) -> bool:
    before = [t for t in _tokens(clause) if t[1] < pred_start]
    for word, _s, _e in before[-2:]:
        lw = word.lower()
        if lw in _COPULA_TOKENS or lw.endswith("'s") or lw.endswith("'re"):
            return True
    return False


def _paren_clause_spans(unit_text: str) -> list[tuple[int, int]]:
    return [m.span(1) for m in _PAREN_RE.finditer(unit_text) if m.end(1) > m.start(1)]


def _is_paren_clause(spans: Sequence[tuple[int, int]], cs: int, ce: int) -> bool:
    """True when the clause `(cs, ce)` *is* (up to trimmed whitespace) a top-level
    parenthetical's inner content, as opposed to merely sitting somewhere inside one."""
    return any(cs >= ps and ce <= pe and (pe - ps) - (ce - cs) <= 4 for ps, pe in spans)


def _pred_span(match: re.Match[str]) -> tuple[int, int]:
    """The `(?P<pred>...)` group's span, or the whole match's span when the pattern defines
    no such group (custom rules are not required to name a predicate)."""
    try:
        span = match.span("pred")
    except IndexError:
        return match.span()
    if span == (-1, -1):
        return match.span()
    return span


# ---------------------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------------------


def detect(message: str, rules: Sequence[ClaimRule]) -> list[Claim]:
    """Find every claim `rules` recognise in `message`, in message order.

    Each rule's patterns are matched, clause by clause, against a masked copy of `message`
    (see :func:`_mask`); a match is dropped when the clause is negated, hedged/conditional,
    about the future, a question, an instruction, a non-finite predicate, or (for `fixed`/
    `verified` narration lead-ins only) subsumed by a later automated-check claim in the same
    sentence. Surviving matches become :class:`Claim` objects with `quote` sliced from the
    original message.
    """
    claims, _rejections = _detect(message, rules)
    return claims


def detect_with_rejections(
    message: str, rules: Sequence[ClaimRule]
) -> tuple[list[Claim], list[Rejection]]:
    """Like :func:`detect`, but also returns every match a rule's pattern found that a
    rejection filter then dropped (see :class:`Rejection`). Only the dev-set `--filters`
    audit needs the rejection list; the hook and evidence engine call :func:`detect`."""
    return _detect(message, rules)


def _active_rules(message: str, rules: Sequence[ClaimRule]) -> list[ClaimRule]:
    """The rules whose own `keywords` are actually present in `message` (case-insensitive
    substring). A rule's patterns can only ever match text that contains one of its keywords
    (config.py requires every rule to declare keywords precisely so this holds -- see
    `docs/config.md` and `test_prefilter_keywords.py`), so skipping a keyword-absent rule
    entirely changes no outcome; it just spares every one of its patterns a `finditer` over
    every clause. This is `detect()`'s own inner fast path, separate from and in addition to
    the hook's outer message-level `prefilter()` (spec item 9): with many rules and a long
    message, running every pattern of every rule against every clause is the dominant cost
    (measured on a 20 KB message), so narrowing the rule list once per call keeps `detect()`
    itself fast even when the outer prefilter was never in the loop (as in `audit`/eval, or a
    custom rule set with its own keyword mix)."""
    lowered = message.lower()
    return [rule for rule in rules if any(kw in lowered for kw in rule.keywords)]


def _detect(message: str, rules: Sequence[ClaimRule]) -> tuple[list[Claim], list[Rejection]]:
    active_rules = _active_rules(message, rules)
    if not active_rules:
        return [], []
    masked = _mask(message)
    claims: list[Claim] = []
    rejections: list[Rejection] = []
    seen: set[tuple[str, int, int]] = set()
    seen_rejected: set[tuple[str, int, int]] = set()

    def _reject(filter_name: str, rule_id: str, claim_type: str, span: tuple[int, int]) -> None:
        rkey = (rule_id, span[0], span[1])
        if rkey in seen_rejected:
            return
        seen_rejected.add(rkey)
        rejections.append(Rejection(filter_name, rule_id, claim_type, span))

    for unit_text, unit_start, _unit_end in _split_units(masked):
        unit_lower = unit_text.lower()
        unit_rules = [r for r in active_rules if any(kw in unit_lower for kw in r.keywords)]
        if not unit_rules:
            # None of this unit's text contains any still-active rule's keywords, so none of
            # their patterns can match anywhere in it (same reasoning as `_active_rules`);
            # skip straight past clause-splitting and the question/instruction checks too --
            # this is what keeps a long, mostly claim-free message fast.
            continue
        paren_spans = _paren_clause_spans(unit_text)
        unit_survivors: list[_Survivor] = []
        for cs, ce in _split_clauses(unit_text):
            clause = unit_text[cs:ce]
            clause_abs_start = unit_start + cs
            clause_is_question = _is_question(clause)
            clause_is_instruction = _is_instruction(clause)
            # Not narrowed further per clause: a rule's keyword and its actual match can land
            # in different *clauses* of the same unit once a parenthetical aside splits one
            # sentence into several (e.g. "Type checking (tsc, ...): no errors" -- "tsc" and
            # the "no errors" match end up in different clauses), so this stays per-unit.
            for rule in unit_rules:
                for pattern in rule.patterns:
                    for m in pattern.finditer(clause):
                        pred_s, pred_e = _pred_span(m)
                        abs_s = clause_abs_start + m.start()
                        abs_e = clause_abs_start + m.end()
                        filter_name = None
                        if clause_is_question:
                            filter_name = "question"
                        elif clause_is_instruction:
                            filter_name = "instruction"
                        elif _is_negated(clause, pred_s):
                            filter_name = "negated"
                        elif _is_hedged(clause, pred_e):
                            filter_name = "hedged"
                        elif _is_future(clause):
                            filter_name = "future"
                        elif _is_nonfinite(clause):
                            filter_name = "nonfinite"
                        if filter_name is not None:
                            _reject(filter_name, rule.id, rule.claim_type, (abs_s, abs_e))
                            continue
                        key = (rule.id, abs_s, abs_e)
                        if key in seen:
                            continue
                        seen.add(key)
                        narration = (
                            rule.claim_type in _NARRATION_CLAIM_TYPES
                            and not _has_copula_before(clause, pred_s)
                            and not _is_paren_clause(paren_spans, cs, ce)
                        )
                        unit_survivors.append(
                            _Survivor(rule.id, rule.claim_type, abs_s, abs_e, narration)
                        )

        check_type_starts = [s.start for s in unit_survivors if s.claim_type in _CHECK_CLAIM_TYPES]
        for s in unit_survivors:
            if s.narration and any(cts > s.end for cts in check_type_starts):
                _reject("subsumed", s.rule_id, s.claim_type, (s.start, s.end))
                continue
            claims.append(
                Claim(
                    rule_id=s.rule_id,
                    claim_type=s.claim_type,
                    quote=message[s.start : s.end],
                    span=(s.start, s.end),
                )
            )
    claims.sort(key=lambda c: (c.span[0], c.span[1], c.rule_id))
    rejections.sort(key=lambda r: (r.span[0], r.span[1], r.rule_id))
    return claims, rejections


def prefilter(message: str, keywords: Iterable[str]) -> bool:
    """Cheap fast-path gate: `True` iff `message` (lower-cased) contains any of `keywords` as
    a substring. A message that fails this can never produce a claim, so callers skip parsing
    the transcript entirely when it does."""
    lowered = message.lower()
    return any(keyword in lowered for keyword in keywords)


def builtin_patterns() -> dict[str, list[str]]:
    """Empty by design: the built-in claim regexes live in ``defaults.yaml`` (one rule's
    ``claims`` list per claim type), not in code, so they can be tuned without a release.
    Kept only so a caller that expects this entry point does not fail to import; it always
    returns an empty mapping."""
    return {}
