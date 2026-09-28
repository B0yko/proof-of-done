"""Shell-command segmentation, wrapper stripping, masking detection and evidence matching.

A Bash tool call is one string that may hold several logical commands, joined by `;`, `&&`,
`||`, `|`, `&` or bare newlines, nested inside `( ... )` subshells, `{ ...; }` groups, and
`bash -c "..."`/`sh -c "..."`/`zsh -c "..."` strings (unwrapped recursively). This module turns
that string into an ordered list of :class:`Segment` objects — one per simple command — with
wrappers (`env`, `timeout`, `uv run`, ...) stripped, the program name normalised, and a
``masked`` flag recording whether the segment's own exit status decides the whole command's
outcome. It also extracts the file paths a Bash call may have written to (:func:`bash_edits`).

Nothing here reads the filesystem or resolves shell expansions (`$VAR`, `` $(...) ``, globs):
those are left as literal text, or skipped when a resolved edit target would need them.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache

# A one-shot standalone command like `pytest(){ ... }` or `PATH=x pytest` never contains
# NUL bytes in practice, so it is a safe sentinel for "a bare newline stood here".
_NEWLINE_SENTINEL = "\x00POD_NL\x00"
_TOP_LEVEL_SEPARATORS = {";", "&&", "||", "|", "&", _NEWLINE_SENTINEL}
_GROUP_OPEN = {"(", "{"}
_GROUP_CLOSE = {")", "}"}
_FILE_REDIRECT_OPS = {">", ">>", "&>", ">|", "<", "<<"}
_MAX_UNWRAP_DEPTH = 3

_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ASSIGN_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.DOTALL)
_ALIAS_ASSIGN_RE = re.compile(r"^([^=\s]+)=(.*)$", re.DOTALL)


class ParseError(Exception):
    """Raised when a Bash call cannot be tokenised (unbalanced quotes, an unterminated
    heredoc). The caller should log this and treat the command as yielding no candidate."""


@dataclass
class Redirect:
    op: str  # '>', '>>', '&>', '>|', '<', '<<'
    target: str


@dataclass
class Segment:
    index: int
    tokens: list[str]
    argv: list[str]
    wrappers: list[str]
    env: dict[str, str]
    program: str
    op_before: str | None
    op_after: str | None
    in_pipeline: bool
    pipefail: bool
    errexit: bool
    masked: bool
    background: bool
    redirects: list[Redirect]
    cwd: str | None
    display: str
    defines: set[str]
    sets_path: bool


@dataclass
class ParsedCommand:
    raw: str
    segments: list[Segment]
    defines: set[str]
    sets_path: bool


@dataclass
class EditTarget:
    path: str | None
    is_dir: bool
    whole_tree: bool
    seg_index: int
    phase: int
    source: str  # 'bash' | 'formatter' | 'tree'


# --------------------------------------------------------------------------------------------
# Pre-pass: continuation joining, heredoc stripping, newline marking
# --------------------------------------------------------------------------------------------


def _join_continuations(s: str) -> str:
    """Remove ``\\`` immediately followed by a newline, outside single quotes (where a
    backslash has no special meaning in POSIX shells, so the pair stays literal)."""
    out: list[str] = []
    i = 0
    n = len(s)
    in_single = False
    in_double = False
    while i < n:
        c = s[i]
        if c == "\\" and i + 1 < n and s[i + 1] == "\n" and not in_single:
            i += 2
            continue
        if c == "\\" and i + 1 < n and not in_single:
            out.append(c)
            out.append(s[i + 1])
            i += 2
            continue
        if c == "'" and not in_double:
            in_single = not in_single
        elif c == '"' and not in_single:
            in_double = not in_double
        out.append(c)
        i += 1
    return "".join(out)


_HEREDOC_MARKER_RE = re.compile(r"<<(-)?\s*(['\"]?)(\w+)\2")


def _strip_heredocs(s: str) -> str:
    """Remove heredoc bodies, keeping the ``<<WORD`` operator text in place so the tokenizer
    still sees a (harmless, non-file) redirect operator there."""
    out: list[str] = []
    i = 0
    n = len(s)
    while i < n:
        nl = s.find("\n", i)
        line_end = nl if nl != -1 else n
        line = s[i:line_end]
        markers = list(_HEREDOC_MARKER_RE.finditer(line))
        if not markers:
            out.append(s[i : line_end + (1 if nl != -1 else 0)])
            i = line_end + 1 if nl != -1 else n
            continue
        out.append(line)
        if nl == -1:
            raise ParseError("heredoc marker with no body")
        out.append("\n")
        pos = nl + 1
        for m in markers:
            dash = m.group(1) == "-"
            word = m.group(3)
            terminated = False
            while pos <= n:
                body_nl = s.find("\n", pos)
                body_line_end = body_nl if body_nl != -1 else n
                body_line = s[pos:body_line_end]
                test_line = body_line.lstrip("\t") if dash else body_line
                pos = body_line_end + 1 if body_nl != -1 else n
                if test_line == word:
                    terminated = True
                    break
                if body_nl == -1:
                    break
            if not terminated:
                raise ParseError(f"unterminated heredoc <<{word}")
        i = pos
    return "".join(out)


def _mask_command_substitutions(s: str) -> str:
    """Collapse ``$( ... )`` and `` ` ... ` `` spans into one opaque, unresolvable token.

    Left alone, the parentheses of ``$(...)`` are punctuation characters to the tokenizer and
    would be read as if they opened a `( ... )` subshell group, and would scatter the
    substitution across several argv tokens instead of the one unresolvable value it is.
    """
    out: list[str] = []
    i = 0
    n = len(s)
    in_single = False
    in_double = False
    while i < n:
        c = s[i]
        if c == "\\" and i + 1 < n and not in_single:
            out.append(c)
            out.append(s[i + 1])
            i += 2
            continue
        if c == "'" and not in_double:
            in_single = not in_single
            out.append(c)
            i += 1
            continue
        if c == '"' and not in_single:
            in_double = not in_double
            out.append(c)
            i += 1
            continue
        if not in_single and c == "$" and i + 1 < n and s[i + 1] == "(":
            depth = 1
            j = i + 2
            while j < n and depth > 0:
                cj = s[j]
                if cj == "\\" and j + 1 < n:
                    j += 2
                    continue
                if cj == "(":
                    depth += 1
                elif cj == ")":
                    depth -= 1
                j += 1
            out.append("$__cmdsubst__")
            i = j
            continue
        if not in_single and c == "`":
            j = i + 1
            while j < n and s[j] != "`":
                if s[j] == "\\" and j + 1 < n:
                    j += 2
                    continue
                j += 1
            j = min(j + 1, n)
            out.append("$__cmdsubst__")
            i = j
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _mark_newlines(s: str) -> str:
    """Replace bare (unquoted) newlines with a sentinel word so the tokenizer keeps them as
    a distinct, whitespace-delimited separator token instead of silently discarding them."""
    out: list[str] = []
    i = 0
    n = len(s)
    in_single = False
    in_double = False
    while i < n:
        c = s[i]
        if c == "\\" and i + 1 < n and not in_single:
            out.append(c)
            out.append(s[i + 1])
            i += 2
            continue
        if c == "'" and not in_double:
            in_single = not in_single
        elif c == '"' and not in_single:
            in_double = not in_double
        if c == "\n" and not in_single and not in_double:
            out.append(" " + _NEWLINE_SENTINEL + " ")
        else:
            out.append(c)
        i += 1
    return "".join(out)


_PUNCT_CHARS = frozenset("();<>|&")
_MULTI_CHAR_OPS = ("&&", "||", ">>", "&>", ">&", "<&", "<<", ">|")


def _resplit_punctuation_run(tok: str) -> list[str]:
    """`shlex` (with `punctuation_chars=True`) glues any *run* of adjacent punctuation
    characters into one token, e.g. ``pytest();`` tokenizes as ``['pytest', '();']`` -- not
    the `(`, `)`, `;` this module's segmenter needs. Re-split such a run greedily, preferring
    the known two-character operators (``&&``, ``||``, ``>>``, ...) over single characters."""
    out: list[str] = []
    i = 0
    n = len(tok)
    while i < n:
        two = tok[i : i + 2]
        if two in _MULTI_CHAR_OPS:
            out.append(two)
            i += 2
        else:
            out.append(tok[i])
            i += 1
    return out


def _tokenize(s: str) -> list[str]:
    try:
        lex = shlex.shlex(s, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        lex.commenters = ""
        raw_tokens = list(lex)
    except ValueError as exc:
        raise ParseError(str(exc)) from exc
    tokens: list[str] = []
    for tok in raw_tokens:
        if tok and all(c in _PUNCT_CHARS for c in tok) and tok not in _MULTI_CHAR_OPS:
            tokens.extend(_resplit_punctuation_run(tok))
        else:
            tokens.append(tok)
    return tokens


# --------------------------------------------------------------------------------------------
# Redirect extraction
# --------------------------------------------------------------------------------------------


def _is_all_digits(tok: str) -> bool:
    return tok.isdigit()


def _extract_redirects(tokens: list[str]) -> tuple[list[str], list[Redirect]]:
    """Pull file-target redirects out of `tokens`, dropping fd-duplication forms
    (``>&N``, ``N>&M``, ``<&N``) entirely. Returns (tokens without any redirects, redirects)."""
    cleaned: list[str] = []
    redirects: list[Redirect] = []
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        j = i
        if _is_all_digits(tok) and i + 1 < n and _looks_like_redir_op(tokens[i + 1]):
            j = i + 1
        op_tok = tokens[j]
        if not _looks_like_redir_op(op_tok):
            cleaned.append(tok)
            i += 1
            continue
        if op_tok in (">&", "<&"):
            # fd duplication: never a file target.
            i = j + 2 if j + 1 < n else j + 1
            continue
        if op_tok in _FILE_REDIRECT_OPS:
            if j + 1 < n:
                redirects.append(Redirect(op=op_tok, target=tokens[j + 1]))
                i = j + 2
            else:
                i = j + 1
            continue
        cleaned.append(tok)
        i += 1
    return cleaned, redirects


def _looks_like_redir_op(tok: str) -> bool:
    return tok in _FILE_REDIRECT_OPS or tok in (">&", "<&")


# --------------------------------------------------------------------------------------------
# Wrapper stripping and program normalisation
# --------------------------------------------------------------------------------------------

_UV_RUN_VALUE_OPTS = {
    "--with",
    "--python",
    "-p",
    "--project",
    "--directory",
    "--extra",
    "--group",
    "--package",
    "--env-file",
    "--with-requirements",
}
_NPX_VALUE_OPTS = {"-p", "--package"}
_TIMEOUT_VALUE_OPTS = {"-s", "--signal", "-k", "--kill-after"}


def _split_flag_value(tok: str) -> tuple[str, str | None]:
    if "=" in tok and tok.startswith("-"):
        flag, _, val = tok.partition("=")
        return flag, val
    return tok, None


def _strip_wrappers(
    tokens: list[str],
) -> tuple[list[str], list[str], dict[str, str], list[str]]:
    """Strip leading env assignments and known wrapper programs from `tokens` (repeatable).

    Returns (wrappers, remaining_tokens, env, program_hint) where `remaining_tokens` is what's
    left after stripping (the real command, still un-normalised)."""
    wrappers: list[str] = []
    env: dict[str, str] = {}
    i = 0
    n = len(tokens)
    changed = True
    while changed and i < n:
        changed = False
        # Leading VAR=val assignments (env prefix form).
        while i < n:
            m = _ASSIGN_RE.match(tokens[i])
            if not m:
                break
            env[m.group(1)] = m.group(2)
            i += 1
            changed = True
        if i >= n:
            break
        head = tokens[i]
        if head == "env":
            wrappers.append(tokens[i])
            i += 1
            while i < n and tokens[i] in ("-i", "-0"):
                wrappers.append(tokens[i])
                i += 1
            while i < n and tokens[i] == "-u" and i + 1 < n:
                wrappers.append(tokens[i])
                wrappers.append(tokens[i + 1])
                i += 2
            while i < n:
                m = _ASSIGN_RE.match(tokens[i])
                if not m:
                    break
                env[m.group(1)] = m.group(2)
                i += 1
            changed = True
        elif head == "time":
            wrappers.append(tokens[i])
            i += 1
            changed = True
        elif head == "timeout":
            wrappers.append(tokens[i])
            i += 1
            while i < n:
                flag, val = _split_flag_value(tokens[i])
                if flag in _TIMEOUT_VALUE_OPTS and val is None:
                    wrappers.append(tokens[i])
                    i += 1
                    if i < n:
                        wrappers.append(tokens[i])
                        i += 1
                elif (flag in _TIMEOUT_VALUE_OPTS and val is not None) or tokens[
                    i
                ] == "--foreground":
                    wrappers.append(tokens[i])
                    i += 1
                else:
                    break
            if i < n:
                wrappers.append(tokens[i])  # the DURATION positional
                i += 1
            changed = True
        elif head == "nice":
            wrappers.append(tokens[i])
            i += 1
            if i < n and tokens[i] == "-n" and i + 1 < n:
                wrappers.append(tokens[i])
                wrappers.append(tokens[i + 1])
                i += 2
            elif i < n and re.match(r"^-\d+$", tokens[i]):
                wrappers.append(tokens[i])
                i += 1
            changed = True
        elif head in ("command", "exec"):
            wrappers.append(tokens[i])
            i += 1
            changed = True
        elif head == "uv" and i + 1 < n and tokens[i + 1] == "run":
            wrappers.append(tokens[i])
            wrappers.append(tokens[i + 1])
            i += 2
            while i < n:
                flag, val = _split_flag_value(tokens[i])
                if flag in _UV_RUN_VALUE_OPTS and val is None:
                    wrappers.append(tokens[i])
                    i += 1
                    if i < n:
                        wrappers.append(tokens[i])
                        i += 1
                elif flag in _UV_RUN_VALUE_OPTS and val is not None:
                    wrappers.append(tokens[i])
                    i += 1
                else:
                    break
            if i < n and tokens[i] == "--":
                wrappers.append(tokens[i])
                i += 1
            changed = True
        elif head in ("poetry", "pdm") and i + 1 < n and tokens[i + 1] == "run":
            wrappers.append(tokens[i])
            wrappers.append(tokens[i + 1])
            i += 2
            changed = True
        elif head == "hatch" and i + 1 < n and tokens[i + 1] == "run":
            wrappers.append(tokens[i])
            wrappers.append(tokens[i + 1])
            i += 2
            if i < n and ":" in tokens[i] and not tokens[i].startswith(":"):
                env_prefix, _, rest = tokens[i].partition(":")
                wrappers.append(env_prefix + ":")
                if rest:
                    tokens = [*tokens[:i], rest, *tokens[i + 1 :]]
                    n = len(tokens)
                else:
                    i += 1
            changed = True
        elif head == "npx":
            wrappers.append(tokens[i])
            i += 1
            while i < n:
                flag, val = _split_flag_value(tokens[i])
                if tokens[i] in ("-y", "--yes"):
                    wrappers.append(tokens[i])
                    i += 1
                elif flag in _NPX_VALUE_OPTS and val is None:
                    wrappers.append(tokens[i])
                    i += 1
                    if i < n:
                        wrappers.append(tokens[i])
                        i += 1
                elif flag in _NPX_VALUE_OPTS and val is not None:
                    wrappers.append(tokens[i])
                    i += 1
                else:
                    break
            changed = True
        elif (head == "pnpm" and i + 1 < n and tokens[i + 1] in ("exec", "dlx")) or (
            head == "yarn" and i + 1 < n and tokens[i + 1] == "exec"
        ):
            wrappers.append(tokens[i])
            wrappers.append(tokens[i + 1])
            i += 2
            changed = True
        elif head == "bunx":
            wrappers.append(tokens[i])
            i += 1
            changed = True
    return wrappers, tokens[i:], env, tokens


_PY3_RE = re.compile(r"^python3(\.\d+)?$")


def _normalize_program(raw: str) -> str:
    if raw.startswith("./") or raw.startswith("../"):
        return raw
    base = raw.rsplit("/", 1)[-1] if "/" in raw else raw
    if _PY3_RE.match(base):
        return "python"
    if base == "py":
        return "python"
    if base == "py.test":
        return "pytest"
    return base


# --------------------------------------------------------------------------------------------
# `set` builtin: pipefail / errexit tracking
# --------------------------------------------------------------------------------------------


def _apply_set_builtin(argv: list[str], pipefail: bool, errexit: bool) -> tuple[bool, bool]:
    i = 1
    n = len(argv)
    while i < n:
        tok = argv[i]
        if tok in ("-e",):
            errexit = True
        elif tok in ("+e",):
            errexit = False
        elif tok == "-o" and i + 1 < n:
            if argv[i + 1] == "pipefail":
                pipefail = True
            i += 1
        elif tok == "+o" and i + 1 < n:
            if argv[i + 1] == "pipefail":
                pipefail = False
            i += 1
        elif re.match(r"^-[A-Za-z]+$", tok):
            if "e" in tok:
                errexit = True
            if "o" in tok and i + 1 < n and argv[i + 1] == "pipefail":
                pipefail = True
        i += 1
    return pipefail, errexit


# --------------------------------------------------------------------------------------------
# Path helpers
# --------------------------------------------------------------------------------------------


def _norm_posix(path: str) -> str:
    is_abs = path.startswith("/")
    stack: list[str] = []
    for part in path.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if stack and stack[-1] != "..":
                stack.pop()
            elif not is_abs:
                stack.append("..")
            continue
        stack.append(part)
    body = "/".join(stack)
    return ("/" + body) if is_abs else body


def _join_cwd(base: str | None, part: str) -> str:
    if part.startswith("/"):
        return _norm_posix(part)
    if not base:
        return _norm_posix(part)
    return _norm_posix(base.rstrip("/") + "/" + part)


_GLOB_CLASS_RE = re.compile(r"\[[^\]]*\]")


def _replace_glob_chars(path: str) -> str:
    path = _GLOB_CLASS_RE.sub("x", path)
    return path.replace("*", "x").replace("?", "x")


def _is_unresolvable(target: str) -> bool:
    return "$" in target or "`" in target


# --------------------------------------------------------------------------------------------
# Segment construction
# --------------------------------------------------------------------------------------------


class _Ctx:
    __slots__ = ("defines", "next_index", "sets_path")

    def __init__(self) -> None:
        self.next_index = 0
        self.defines: set[str] = set()
        self.sets_path = False


def _record_defines(ctx: _Ctx, raw_tokens: list[str]) -> None:
    if not raw_tokens:
        return
    t0 = raw_tokens[0]
    if (
        _NAME_RE.match(t0)
        and len(raw_tokens) >= 3
        and raw_tokens[1] == "("
        and raw_tokens[2] == ")"
    ):
        ctx.defines.add(t0)
        return
    if t0 == "function" and len(raw_tokens) >= 2 and _NAME_RE.match(raw_tokens[1]):
        ctx.defines.add(raw_tokens[1])
        return
    if t0 == "alias" and len(raw_tokens) >= 2:
        m = _ALIAS_ASSIGN_RE.match(raw_tokens[1])
        if m:
            ctx.defines.add(m.group(1))
        return
    if t0 == "export" and len(raw_tokens) >= 2:
        for tok in raw_tokens[1:]:
            m = _ASSIGN_RE.match(tok)
            if m and m.group(1) == "PATH":
                ctx.sets_path = True


@dataclass
class _Piece:
    op_before: str | None
    tokens: list[str]
    op_after: str | None
    background: bool


def _split_top_level(tokens: list[str]) -> list[_Piece]:
    pieces: list[_Piece] = []
    depth = 0
    current: list[str] = []
    op_before: str | None = None
    i = 0
    n = len(tokens)
    while i <= n:
        at_sep = i < n and depth == 0 and tokens[i] in _TOP_LEVEL_SEPARATORS
        if i == n or at_sep:
            raw_sep = tokens[i] if i < n else None
            norm_sep = ";" if raw_sep == _NEWLINE_SENTINEL else raw_sep
            pieces.append(
                _Piece(
                    op_before=op_before,
                    tokens=current,
                    op_after=norm_sep,
                    background=raw_sep == "&",
                )
            )
            current = []
            op_before = norm_sep
            i += 1
            continue
        tok = tokens[i]
        if tok in _GROUP_OPEN:
            depth += 1
        elif tok in _GROUP_CLOSE:
            depth -= 1
        current.append(tok)
        i += 1
    return [p for p in pieces if p.tokens]


def _local_masked(op_after: str | None, pipefail: bool, errexit: bool) -> bool:
    if op_after == "|" and not pipefail:
        return True
    if op_after == "||":
        return True
    if op_after == ";":
        return not errexit
    return False


def _make_segment_body(
    ctx: _Ctx,
    piece_tokens: list[str],
    *,
    op_before: str | None,
    op_after: str | None,
    background: bool,
    cwd: str | None,
    pipefail: bool,
    errexit: bool,
    masked: bool,
    in_pipeline: bool,
    depth: int,
) -> tuple[list[Segment], str | None, bool, bool]:
    """Build the Segment(s) for one already-split top-level piece, unwrapping a single level
    of `(...)`/`{...}` group or `bash -c`/`sh -c`/`zsh -c` if that's what the piece is.

    Returns (segments, cwd_after, pipefail_after, errexit_after) -- the state changes only
    propagate to the caller for same-shell constructs (brace groups, the top level); a real
    subshell `(...)` or a `bash -c` child process keeps its `cd`/`set` changes to itself.
    """
    _record_defines(ctx, piece_tokens)

    is_paren_group = len(piece_tokens) >= 2 and piece_tokens[0] == "(" and piece_tokens[-1] == ")"
    is_brace_group = len(piece_tokens) >= 2 and piece_tokens[0] == "{" and piece_tokens[-1] == "}"
    if is_paren_group or is_brace_group:
        inner = piece_tokens[1:-1]
        own_masked = masked or _local_masked(op_after, pipefail, errexit)
        inner_segments, inner_cwd, inner_pipefail, inner_errexit = _process_scope(
            ctx,
            inner,
            cwd=cwd,
            pipefail=pipefail,
            errexit=errexit,
            outer_masked=own_masked,
            in_pipeline_ctx=in_pipeline or op_before == "|" or op_after == "|",
            depth=depth,
        )
        if inner_segments:
            inner_segments[0].op_before = (
                op_before if inner_segments[0].op_before is None else (inner_segments[0].op_before)
            )
            inner_segments[-1].op_after = op_after
            inner_segments[-1].background = background
        if is_brace_group:
            return inner_segments, inner_cwd, inner_pipefail, inner_errexit
        return inner_segments, cwd, pipefail, errexit

    unwrapped = _try_unwrap_shell_c(piece_tokens, depth)
    if unwrapped is not None:
        inner_text = unwrapped
        own_masked = masked or _local_masked(op_after, pipefail, errexit)
        try:
            inner_tokens = _pretokenize(inner_text)
        except ParseError:
            inner_tokens = None
        if inner_tokens is not None:
            inner_segments, _cwd, _pf, _ee = _process_scope(
                ctx,
                inner_tokens,
                cwd=None,
                pipefail=False,
                errexit=False,
                outer_masked=own_masked,
                in_pipeline_ctx=in_pipeline or op_before == "|" or op_after == "|",
                depth=depth + 1,
            )
            if inner_segments:
                inner_segments[-1].op_after = op_after
                inner_segments[-1].background = background
            return inner_segments, cwd, pipefail, errexit

    tokens_no_redirects, redirects = _extract_redirects(piece_tokens)
    wrappers, remaining, env, _ = _strip_wrappers(tokens_no_redirects)
    display = shlex.join(tokens_no_redirects) if tokens_no_redirects else ""

    if remaining:
        raw_program = remaining[0]
        program = _normalize_program(raw_program)
        argv = [program, *remaining[1:]]
    else:
        program = ""
        argv = []

    seg_pipefail, seg_errexit = pipefail, errexit
    new_cwd = cwd
    if program == "cd" and len(argv) >= 2 and not _is_unresolvable(argv[1]):
        new_cwd = _join_cwd(cwd, argv[1])
    elif program == "set":
        seg_pipefail, seg_errexit = _apply_set_builtin(argv, pipefail, errexit)

    seg_masked = masked or _local_masked(op_after, pipefail, errexit)
    if background:
        seg_masked = False

    seg = Segment(
        index=ctx.next_index,
        tokens=list(piece_tokens),
        argv=argv,
        wrappers=wrappers,
        env=env,
        program=program,
        op_before=op_before,
        op_after=op_after,
        in_pipeline=in_pipeline or op_before == "|" or op_after == "|",
        pipefail=pipefail,
        errexit=errexit,
        masked=seg_masked,
        background=background,
        redirects=redirects,
        cwd=cwd,
        display=display,
        defines=set(ctx.defines),
        sets_path=ctx.sets_path,
    )
    ctx.next_index += 1
    if env.get("PATH") is not None:
        ctx.sets_path = True
    return [seg], new_cwd, seg_pipefail, seg_errexit


_SHELL_C_PROGRAMS = {"bash", "sh", "zsh"}
_SHELL_C_FLAGS = {"-c", "-lc"}


def _try_unwrap_shell_c(piece_tokens: list[str], depth: int) -> str | None:
    if depth >= _MAX_UNWRAP_DEPTH:
        return None
    tokens_no_redirects, _ = _extract_redirects(piece_tokens)
    _wrappers, remaining, _env, _ = _strip_wrappers(tokens_no_redirects)
    if len(remaining) < 3:
        return None
    program = _normalize_program(remaining[0])
    if program not in _SHELL_C_PROGRAMS:
        return None
    if remaining[1] not in _SHELL_C_FLAGS:
        return None
    return remaining[2]


def _process_scope(
    ctx: _Ctx,
    tokens: list[str],
    *,
    cwd: str | None,
    pipefail: bool,
    errexit: bool,
    outer_masked: bool,
    in_pipeline_ctx: bool,
    depth: int,
) -> tuple[list[Segment], str | None, bool, bool]:
    pieces = _split_top_level(tokens)
    # A scope's own masking (e.g. this whole group is followed by `|| true`) decides the
    # scope's *own* exit status, which is the exit status of whichever piece actually runs
    # last. That is always the last piece, plus — walking backwards from it — every piece
    # immediately before it that is joined to what follows by a pure `&&` chain: if such a
    # piece fails, the chain short-circuits and *its* exit status becomes the scope's exit
    # status instead, so it is exactly as masked as the scope itself. A `;` or `||` break in
    # the chain stops the propagation (the scope's exit status no longer depends on it).
    propagates = [False] * len(pieces)
    for idx in range(len(pieces) - 1, -1, -1):
        is_last = idx == len(pieces) - 1
        if is_last or (pieces[idx].op_after == "&&" and propagates[idx + 1]):
            propagates[idx] = True
        else:
            break
    segments: list[Segment] = []
    cur_cwd = cwd
    cur_pipefail = pipefail
    cur_errexit = errexit
    for idx, piece in enumerate(pieces):
        piece_masked = outer_masked if propagates[idx] else False
        piece_segments, cur_cwd, cur_pipefail, cur_errexit = _make_segment_body(
            ctx,
            piece.tokens,
            op_before=piece.op_before,
            op_after=piece.op_after,
            background=piece.background,
            cwd=cur_cwd,
            pipefail=cur_pipefail,
            errexit=cur_errexit,
            masked=piece_masked,
            in_pipeline=in_pipeline_ctx,
            depth=depth,
        )
        segments.extend(piece_segments)
    return segments, cur_cwd, cur_pipefail, cur_errexit


def _pretokenize(cmd: str) -> list[str]:
    text = _join_continuations(cmd)
    text = _strip_heredocs(text)
    text = _mask_command_substitutions(text)
    text = _mark_newlines(text)
    return _tokenize(text)


def parse_command(cmd: str, cwd: str | None = None) -> ParsedCommand:
    """Segment a raw Bash tool call into :class:`Segment` objects.

    Raises :class:`ParseError` when the string cannot be tokenised (unbalanced quotes, an
    unterminated heredoc).
    """
    tokens = _pretokenize(cmd)
    ctx = _Ctx()
    segments, _cwd, _pf, _ee = _process_scope(
        ctx,
        tokens,
        cwd=cwd,
        pipefail=False,
        errexit=False,
        outer_masked=False,
        in_pipeline_ctx=False,
        depth=0,
    )
    for seg in segments:
        seg.defines = set(ctx.defines)
        seg.sets_path = ctx.sets_path
    return ParsedCommand(raw=cmd, segments=segments, defines=ctx.defines, sets_path=ctx.sets_path)


# --------------------------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------------------------


def match_prefix(argv: Sequence[str], prefix: Sequence[str]) -> bool:
    """fnmatch (case-sensitive) each token of `prefix` against the same position in `argv`."""
    from fnmatch import fnmatchcase

    if len(argv) < len(prefix):
        return False
    return all(fnmatchcase(a, p) for a, p in zip(argv, prefix))


def matches_any(
    seg: Segment, prefixes: Sequence[Sequence[str]], regex: re.Pattern[str] | None
) -> bool:
    if any(match_prefix(seg.argv, p) for p in prefixes):
        return True
    return regex is not None and bool(regex.search(" ".join(seg.argv)))


def is_read_only(seg: Segment, read_only: Sequence[Sequence[str]]) -> bool:
    return any(match_prefix(seg.argv, p) for p in read_only)


def and_chain_indices(segments: Sequence[Segment]) -> list[bool]:
    """For a flat, already-parsed segment list (one Bash tool call), which segments' own exit
    status a single reported call-level status could reflect: the last segment, plus --
    walking backward -- every one immediately before it that is joined to what follows by a
    bare ``&&`` (a ``;``, ``||``, ``|`` or backgrounding op stops the chain). Mirrors
    `_process_scope`'s own per-scope `propagates` computation (used there for group masking),
    applied here to the call as a whole so `evidence.py` can tell which segments of a *failed*
    multi-command call are even in the running to be the one whose own status the call's
    single reported exit status actually reflects -- an earlier `&&` segment is otherwise
    indistinguishable from one that never ran at all (PLAN §6 / spec item 5)."""
    n = len(segments)
    propagates = [False] * n
    for idx in range(n - 1, -1, -1):
        is_last = idx == n - 1
        if is_last or (segments[idx].op_after == "&&" and propagates[idx + 1]):
            propagates[idx] = True
        else:
            break
    return propagates


# A segment's own argv[0] (the program) is a literal string, so most `match_prefix` calls
# against a fixed prefix list only ever have one candidate: the prefixes whose own first token
# happens to equal it. `PrefixIndex` buckets a prefix list by that literal first token so a
# hot loop (`evidence._judge_rule`, once per rule per command segment) can look candidates up
# by `argv[0]` instead of `fnmatchcase`-ing every prefix against every segment (spec S11 item
# 3: ~70 000 `match_prefix` calls at 10 MB). A prefix whose first token itself contains a glob
# character (`./*`, tokens with a `?`/`[...]`) cannot be bucketed by equality and is kept in a
# short `glob_first` list checked against every `argv`; a zero-length prefix -- `match_prefix`
# already treats it as matching any `argv` unconditionally, since `zip(argv, ())` is empty and
# `all()` of nothing is `True` -- sets `always`, short-circuiting the whole index.
class PrefixIndex:
    __slots__ = ("always", "glob_first", "literal")

    def __init__(
        self,
        literal: dict[str, tuple[tuple[str, ...], ...]],
        glob_first: tuple[tuple[str, ...], ...],
        always: bool,
    ) -> None:
        self.literal = literal
        self.glob_first = glob_first
        self.always = always


_GLOB_TOKEN_CHARS = frozenset("*?[")


def _token_has_glob(tok: str) -> bool:
    return any(c in _GLOB_TOKEN_CHARS for c in tok)


@lru_cache(maxsize=512)
def build_prefix_index(prefixes: tuple[tuple[str, ...], ...]) -> PrefixIndex:
    """Build a :class:`PrefixIndex` for `prefixes`, cached by value (`prefixes` must already be
    a tuple of tuples of str -- every hot-path caller's prefix lists already are). Equivalent,
    for every `argv`, to ``any(match_prefix(argv, p) for p in prefixes)`` via
    :func:`prefix_index_match`."""
    buckets: dict[str, list[tuple[str, ...]]] = {}
    glob_first: list[tuple[str, ...]] = []
    always = False
    for p in prefixes:
        if not p:
            always = True
            continue
        first = p[0]
        if _token_has_glob(first):
            glob_first.append(p)
        else:
            buckets.setdefault(first, []).append(p)
    return PrefixIndex(
        literal={k: tuple(v) for k, v in buckets.items()},
        glob_first=tuple(glob_first),
        always=always,
    )


def prefix_index_match(index: PrefixIndex, argv: Sequence[str]) -> bool:
    if index.always:
        return True
    if not argv:
        return False
    head = argv[0]
    for p in index.literal.get(head, ()):
        if match_prefix(argv, p):
            return True
    return any(match_prefix(argv, p) for p in index.glob_first)


def _flag_name(tok: str) -> str:
    return tok.split("=", 1)[0] if "=" in tok and tok.startswith("-") else tok


def is_excluded(seg: Segment, exclude_args: Sequence[str]) -> bool:
    exclude = set(exclude_args)
    return any(_flag_name(tok) in exclude for tok in seg.argv[1:])


_PARTIAL_PATH_EXTS = (".py", ".js", ".ts", ".tsx", ".mjs", ".go", ".rs")
_NON_PARTIAL_POSITIONALS = {".", "./", "./..."}


def is_partial(seg: Segment, partial_args: Sequence[str]) -> bool:
    partial = set(partial_args)
    for tok in seg.argv[1:]:
        if _flag_name(tok) in partial:
            return True
    for tok in seg.argv[1:]:
        if tok.startswith("-") or tok in _NON_PARTIAL_POSITIONALS:
            continue
        if "/" in tok or "::" in tok or tok.endswith(_PARTIAL_PATH_EXTS):
            return True
    return False


def disqualified(cmd: ParsedCommand, seg: Segment) -> bool:
    return seg.program in cmd.defines or cmd.sets_path


# --------------------------------------------------------------------------------------------
# Bash edit extraction
# --------------------------------------------------------------------------------------------

_BUILTIN_FORMATTERS: list[
    tuple[tuple[str, ...], tuple[str, ...] | None, tuple[str, ...] | None]
] = [
    (("ruff", "format"), None, ("--check", "--diff")),
    (("ruff", "check"), ("--fix",), None),
    (("black",), None, ("--check", "--diff")),
    (("isort",), None, ("--check", "--check-only", "--diff", "-c")),
    (("autopep8",), ("-i", "--in-place"), None),
    (("prettier",), ("--write", "-w"), None),
    (("eslint",), ("--fix",), None),
    (("cargo", "fmt"), None, ("--check",)),
    (("rustfmt",), None, ("--check",)),
    (("gofmt",), ("-w",), None),
    (("go", "fmt"), None, None),
    (("goimports",), ("-w",), None),
]

# `_formatter_match` runs for every Bash segment in every session (`build_events` extracts
# edit targets unconditionally, not just on the claim-judging path), so bucketing this fixed
# 12-entry table by literal first token -- same idea as `PrefixIndex`, just keeping each
# entry's `require_any`/`exclude_any` metadata alongside its prefix -- avoids `fnmatchcase`-ing
# every one of the 12 prefixes against every segment's argv (spec S11 item 3).
_BUILTIN_FORMATTER_INDEX: dict[
    str, list[tuple[tuple[str, ...], tuple[str, ...] | None, tuple[str, ...] | None]]
] = {}
for _bf_entry in _BUILTIN_FORMATTERS:
    _BUILTIN_FORMATTER_INDEX.setdefault(_bf_entry[0][0], []).append(_bf_entry)
del _bf_entry

_BUILTIN_TREE_COMMANDS: list[tuple[str, ...]] = [
    ("git", "checkout"),
    ("git", "switch"),
    ("git", "restore"),
    ("git", "pull"),
    ("git", "merge"),
    ("git", "rebase"),
    ("git", "reset"),
    ("git", "stash"),
    ("git", "cherry-pick"),
    ("git", "revert"),
    ("git", "am"),
    ("git", "clean"),
]


def _non_flag_positionals(argv_rest: list[str]) -> list[str]:
    return [tok for tok in argv_rest if not tok.startswith("-")]


def _resolve_target(raw: str, base_cwd: str | None) -> str | None:
    if _is_unresolvable(raw):
        return None
    joined = _join_cwd(base_cwd, raw)
    return _replace_glob_chars(joined)


def _base_cwd_for(seg: Segment, call_cwd: str | None) -> str | None:
    return seg.cwd if seg.cwd is not None else call_cwd


def _looks_like_dir(raw: str) -> bool:
    if raw in (".", "./", "..", "../") or raw.endswith("/"):
        return True
    last = raw.rsplit("/", 1)[-1]
    if last in (".", ".."):
        return True
    return "." not in last


def _edit(
    seg: Segment, base_cwd: str | None, raw_path: str, *, is_dir: bool, phase: int, source: str
) -> EditTarget | None:
    resolved = _resolve_target(raw_path, base_cwd)
    if resolved is None:
        return None
    return EditTarget(
        path=resolved,
        is_dir=is_dir,
        whole_tree=False,
        seg_index=seg.index,
        phase=phase,
        source=source,
    )


def _whole_tree(seg: Segment, phase: int, source: str) -> EditTarget:
    return EditTarget(
        path=None, is_dir=True, whole_tree=True, seg_index=seg.index, phase=phase, source=source
    )


_SED_PERL_SCRIPT_FLAGS = ("-e", "-E", "-f", "--expression", "--file")


def _sed_perl_targets(argv: list[str], *, has_explicit_in_place_flag_index: int) -> list[str]:
    rest = argv[has_explicit_in_place_flag_index + 1 :]
    cleaned: list[str] = []
    has_script_flag = False
    k = 0
    while k < len(rest):
        tok = rest[k]
        if tok in _SED_PERL_SCRIPT_FLAGS and k + 1 < len(rest):
            has_script_flag = True
            k += 2
            continue
        if tok.startswith("--expression=") or tok.startswith("--file="):
            has_script_flag = True
            k += 1
            continue
        cleaned.append(tok)
        k += 1
    positionals = _non_flag_positionals(cleaned)
    if has_script_flag:
        return positionals
    return positionals[1:] if positionals else []


def _bash_writes_for_segment(seg: Segment, call_cwd: str | None) -> list[EditTarget]:
    targets: list[EditTarget] = []
    base_cwd = _base_cwd_for(seg, call_cwd)
    argv = seg.argv
    program = seg.program

    # Phase 0: redirect and tee targets.
    for r in seg.redirects:
        if r.op in ("<", "<<"):
            continue
        if r.target in ("/dev/null",):
            continue
        et = _edit(seg, base_cwd, r.target, is_dir=False, phase=0, source="bash")
        if et is not None:
            targets.append(et)
    if program == "tee":
        rest = argv[1:]
        files = [t for t in rest if not t.startswith("-")]
        for f in files:
            et = _edit(seg, base_cwd, f, is_dir=False, phase=0, source="bash")
            if et is not None:
                targets.append(et)

    # Phase 1: effects.
    if program == "sed":
        idx = None
        for k, tok in enumerate(argv[1:], start=1):
            if tok == "-i" or tok.startswith("-i.") or tok.startswith("--in-place"):
                idx = k
                break
        if idx is not None:
            if argv[idx] == "-i" and idx + 1 < len(argv) and argv[idx + 1] == "":
                idx += 1
            for f in _sed_perl_targets(argv, has_explicit_in_place_flag_index=idx):
                et = _edit(seg, base_cwd, f, is_dir=False, phase=1, source="bash")
                if et is not None:
                    targets.append(et)
    elif program == "perl":
        idx = None
        for k, tok in enumerate(argv[1:], start=1):
            if tok.startswith("-") and not tok.startswith("--") and "i" in tok:
                idx = k
                break
        if idx is not None:
            for f in _sed_perl_targets(argv, has_explicit_in_place_flag_index=idx):
                et = _edit(seg, base_cwd, f, is_dir=False, phase=1, source="bash")
                if et is not None:
                    targets.append(et)
    elif program == "mv":
        for f in _non_flag_positionals(argv[1:]):
            et = _edit(seg, base_cwd, f, is_dir=False, phase=1, source="bash")
            if et is not None:
                targets.append(et)
    elif program == "cp":
        pos = _non_flag_positionals(argv[1:])
        if pos:
            et = _edit(seg, base_cwd, pos[-1], is_dir=False, phase=1, source="bash")
            if et is not None:
                targets.append(et)
    elif program == "rm" or program == "touch":
        for f in _non_flag_positionals(argv[1:]):
            et = _edit(seg, base_cwd, f, is_dir=False, phase=1, source="bash")
            if et is not None:
                targets.append(et)
    elif program == "ln":
        pos = _non_flag_positionals(argv[1:])
        if pos:
            et = _edit(seg, base_cwd, pos[-1], is_dir=False, phase=1, source="bash")
            if et is not None:
                targets.append(et)
    elif program == "truncate":
        rest = argv[1:]
        cleaned = []
        k = 0
        while k < len(rest):
            if rest[k] in ("-s", "--size") and k + 1 < len(rest):
                k += 2
                continue
            if rest[k].startswith("-s") or rest[k].startswith("--size="):
                k += 1
                continue
            cleaned.append(rest[k])
            k += 1
        for f in _non_flag_positionals(cleaned):
            et = _edit(seg, base_cwd, f, is_dir=False, phase=1, source="bash")
            if et is not None:
                targets.append(et)
    elif program == "dd":
        for tok in argv[1:]:
            if tok.startswith("of="):
                et = _edit(seg, base_cwd, tok[3:], is_dir=False, phase=1, source="bash")
                if et is not None:
                    targets.append(et)
    elif program == "rsync":
        pos = _non_flag_positionals(argv[1:])
        if pos:
            et = _edit(seg, base_cwd, pos[-1], is_dir=True, phase=1, source="bash")
            if et is not None:
                targets.append(et)
    elif program == "install":
        rest = argv[1:]
        cleaned = []
        k = 0
        while k < len(rest):
            if rest[k] in ("-m", "--mode", "-o", "--owner", "-g", "--group") and k + 1 < len(rest):
                k += 2
                continue
            cleaned.append(rest[k])
            k += 1
        pos = _non_flag_positionals(cleaned)
        if pos:
            is_dir = len(pos) > 2
            et = _edit(seg, base_cwd, pos[-1], is_dir=is_dir, phase=1, source="bash")
            if et is not None:
                targets.append(et)
    elif program == "patch":
        pos = _non_flag_positionals(argv[1:])
        if pos:
            et = _edit(seg, base_cwd, pos[-1], is_dir=False, phase=1, source="bash")
            if et is not None:
                targets.append(et)
        else:
            targets.append(_whole_tree(seg, 1, "bash"))
    elif program == "git" and len(argv) >= 2:
        sub = argv[1]
        if sub == "apply":
            targets.append(_whole_tree(seg, 1, "bash"))
        elif sub == "mv" or sub == "rm":
            for f in _non_flag_positionals(argv[2:]):
                et = _edit(seg, base_cwd, f, is_dir=False, phase=1, source="bash")
                if et is not None:
                    targets.append(et)
        elif sub == "checkout":
            if "--" in argv:
                dash = argv.index("--")
                for f in argv[dash + 1 :]:
                    et = _edit(seg, base_cwd, f, is_dir=False, phase=1, source="tree")
                    if et is not None:
                        targets.append(et)
            else:
                pos = _non_flag_positionals(argv[2:])
                if len(pos) == 1:
                    targets.append(_whole_tree(seg, 1, "tree"))
                else:
                    targets.append(_whole_tree(seg, 1, "tree"))
        elif sub == "stash":
            action = argv[2] if len(argv) > 2 else None
            if action not in ("list", "show"):
                targets.append(_whole_tree(seg, 1, "tree"))
        elif sub == "reset":
            if "--hard" in argv:
                targets.append(_whole_tree(seg, 1, "tree"))
        elif any(sub == p[1] for p in _BUILTIN_TREE_COMMANDS if p[0] == "git"):
            targets.append(_whole_tree(seg, 1, "tree"))
    elif program == "tar":
        if len(argv) > 1:
            mode = argv[1]
            is_extract = mode == "--extract" or (
                not mode.startswith("--")
                and "x" in mode.lstrip("-")
                and "c" not in mode.lstrip("-")
            )
            if is_extract:
                targets.append(_whole_tree(seg, 1, "tree"))
    elif program == "unzip":
        targets.append(_whole_tree(seg, 1, "tree"))

    return targets


def _tree_command_matches(argv: list[str], extra: Sequence[Sequence[str]]) -> bool:
    # Built-in whole-tree git commands are already handled specifically inside
    # `_bash_writes_for_segment` (including the `git checkout -- file` / `git stash list`
    # exceptions); only caller-supplied extensions are matched generically here.
    return any(match_prefix(argv, list(prefix)) for prefix in extra)


def _formatter_match(
    argv: list[str], extra: Sequence[Sequence[str]]
) -> tuple[bool, tuple[str, ...]] | None:
    if argv:
        for prefix, require_any, exclude_any in _BUILTIN_FORMATTER_INDEX.get(argv[0], ()):
            if match_prefix(argv, prefix):
                if exclude_any and any(f in argv for f in exclude_any):
                    return None
                if require_any and not any(f in argv for f in require_any):
                    return None
                return True, prefix
    for extra_prefix in extra:
        if match_prefix(argv, list(extra_prefix)):
            return True, tuple(extra_prefix)
    return None


def bash_edits(
    cmd: ParsedCommand,
    cwd: str | None,
    *,
    formatters: Sequence[Sequence[str]] = (),
    tree_commands: Sequence[Sequence[str]] = (),
    bash_writes: Sequence[Sequence[str]] = (),
) -> list[EditTarget]:
    """Extract file-edit targets from every Bash segment in `cmd`.

    `cwd` is the step's own cwd (from the transcript); a segment whose `.cwd` is unset falls
    back to it. `formatters`, `tree_commands` and `bash_writes` are extra prefix lists (beyond
    the built-in tables this module already knows) coming from project configuration:
    - `formatters`: prefixes whose trailing non-flag positional args are edit targets (dirs
      allowed), or the whole tree when there are none.
    - `tree_commands`: prefixes that always touch the whole tree.
    - `bash_writes`: prefixes whose trailing non-flag positional args are edit targets (files).
    """
    formatters = list(formatters)
    tree_commands = list(tree_commands)
    bash_writes = list(bash_writes)
    out: list[EditTarget] = []
    for seg in cmd.segments:
        if not seg.program:
            continue
        base_cwd = _base_cwd_for(seg, cwd)

        out.extend(_bash_writes_for_segment(seg, cwd))

        fmt = _formatter_match(seg.argv, formatters)
        if fmt is not None:
            _matched, matched_prefix = fmt
            pos = _non_flag_positionals(seg.argv[len(matched_prefix) :])
            if not pos:
                out.append(_whole_tree(seg, 1, "formatter"))
            else:
                for p in pos:
                    et = _edit(
                        seg, base_cwd, p, is_dir=_looks_like_dir(p), phase=1, source="formatter"
                    )
                    if et is not None:
                        out.append(et)
            continue

        if _tree_command_matches(seg.argv, tree_commands):
            out.append(_whole_tree(seg, 1, "tree"))
            continue

        for prefix in bash_writes:
            if match_prefix(seg.argv, list(prefix)):
                for p in _non_flag_positionals(seg.argv[1:]):
                    et = _edit(seg, base_cwd, p, is_dir=False, phase=1, source="bash")
                    if et is not None:
                        out.append(et)
                break

    return out
