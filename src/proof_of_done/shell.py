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
    segments: list[Segment] = []
    cur_cwd = cwd
    cur_pipefail = pipefail
    cur_errexit = errexit
    for idx, piece in enumerate(pieces):
        is_last = idx == len(pieces) - 1
        piece_masked = outer_masked if is_last else False
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
