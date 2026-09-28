"""Project-relative POSIX path and glob matching, with no filesystem dependency except an
injectable existence check for project-root discovery.

Patterns are always relative to the project root: ``*.md`` matches only a top-level file,
``**/*.md`` matches at any depth, and a trailing ``/**`` matches everything beneath a prefix.
There is no fnmatch-style "match the basename anywhere" behaviour.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from functools import cache

# A compiled pattern is a tuple of segment matchers: each element is either the literal string
# "**" (matches zero or more whole path segments) or a compiled regex matching exactly one
# path segment (never crossing "/").
_STAR_STAR = "**"


@cache
def _compile_pattern(pattern: str) -> tuple[str | re.Pattern[str], ...]:
    """Compile a glob pattern into a tuple of per-segment matchers, cached by pattern string."""
    if pattern == "":
        return ()
    compiled: list[str | re.Pattern[str]] = []
    for segment in pattern.split("/"):
        if segment == _STAR_STAR:
            compiled.append(_STAR_STAR)
        else:
            compiled.append(re.compile(_translate_segment(segment) + r"\Z"))
    return tuple(compiled)


def _translate_segment(segment: str) -> str:
    """Translate one glob path-segment (no ``/``, no ``**``) into a regex pattern string.

    Supports ``*`` (zero or more characters, never ``/``), ``?`` (one character, never
    ``/``), and ``[...]``/``[!...]`` bracket classes. Everything else is matched literally.
    """
    i = 0
    n = len(segment)
    out: list[str] = []
    while i < n:
        c = segment[i]
        i += 1
        if c == "*":
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            j = i
            if j < n and segment[j] in ("!", "^"):
                j += 1
            if j < n and segment[j] == "]":
                j += 1
            while j < n and segment[j] != "]":
                j += 1
            if j >= n:
                # Unterminated bracket: treat the '[' as a literal character.
                out.append(re.escape(c))
            else:
                inner = segment[i:j]
                if inner.startswith("!"):
                    inner = "^" + inner[1:]
                inner = inner.replace("\\", "\\\\")
                out.append("[" + inner + "]")
                i = j + 1
        else:
            out.append(re.escape(c))
    return "".join(out)


def _match_segments(pattern: tuple[str | re.Pattern[str], ...], path: tuple[str, ...]) -> bool:
    if not pattern:
        return not path
    head, rest = pattern[0], pattern[1:]
    if head == _STAR_STAR:
        if _match_segments(rest, path):
            return True
        return bool(path) and _match_segments(pattern, path[1:])
    if not path:
        return False
    assert isinstance(head, re.Pattern)
    if not head.match(path[0]):
        return False
    return _match_segments(rest, path[1:])


def glob_match(pattern: str, path: str) -> bool:
    """Match a project-relative POSIX glob `pattern` against a project-relative POSIX `path`.

    ``**`` matches zero or more whole path segments; ``*`` and ``?`` never cross ``/``;
    ``[...]``/``[!...]`` bracket classes are supported. Both `pattern` and `path` are taken
    as already relative to the project root, with no leading or trailing ``/``.
    """
    pattern_segments = _compile_pattern(pattern)
    path_segments = tuple(path.split("/")) if path != "" else ()
    return _match_segments(pattern_segments, path_segments)


def _could_match_under(
    pattern: tuple[str | re.Pattern[str], ...], dir_segments: tuple[str, ...]
) -> bool:
    if not dir_segments:
        # The directory prefix is fully accounted for: whatever remains of the pattern (if
        # anything) can be satisfied by some file somewhere beneath it. Conservative by design.
        return True
    if not pattern:
        return False
    head, rest = pattern[0], pattern[1:]
    if head == _STAR_STAR:
        if _could_match_under(rest, dir_segments):
            return True
        return _could_match_under(pattern, dir_segments[1:])
    assert isinstance(head, re.Pattern)
    if not head.match(dir_segments[0]):
        return False
    return _could_match_under(rest, dir_segments[1:])


def could_match_under(pattern: str, dir_prefix: str) -> bool:
    """Conservatively decide whether some path beneath `dir_prefix` could match `pattern`.

    Used for edit events whose exact touched file is unknown (a directory-scoped move, an
    `rsync` destination, and similar), so evidence matching can stay on the safe (matching)
    side instead of silently ignoring evidence it cannot fully account for.
    """
    pattern_segments = _compile_pattern(pattern)
    dir_segments = tuple(s for s in dir_prefix.split("/") if s != "")
    return _could_match_under(pattern_segments, dir_segments)


def _normalize_posix_segments(path: str) -> tuple[bool, tuple[str, ...]]:
    """Lexically normalise a POSIX path into ``(is_absolute, segments)``.

    Collapses repeated/trailing slashes, ``.`` components and ``..`` components, purely by
    string manipulation: no filesystem access, no symlink resolution.
    """
    is_absolute = path.startswith("/")
    stack: list[str] = []
    for part in path.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if stack and stack[-1] != "..":
                stack.pop()
            elif not is_absolute:
                stack.append("..")
            # An absolute path's ".." above the root is a no-op and is dropped.
            continue
        stack.append(part)
    return is_absolute, tuple(stack)


def to_rel(abs_path: str, root: str) -> str | None:
    """Return `abs_path` as a POSIX-normalised path relative to `root`, or None if it is
    outside `root`.

    Both arguments are normalised lexically (handling ``..`` and trailing slashes) with no
    filesystem access or symlink resolution. The project root itself normalises to ``""``.
    """
    path_is_abs, path_segments = _normalize_posix_segments(abs_path)
    root_is_abs, root_segments = _normalize_posix_segments(root)
    if path_is_abs != root_is_abs:
        return None
    if len(path_segments) < len(root_segments):
        return None
    if path_segments[: len(root_segments)] != root_segments:
        return None
    return "/".join(path_segments[len(root_segments) :])


def find_project_root(cwd: str, home: str, exists: Callable[[str], bool]) -> str:
    """Find the nearest ancestor of `cwd` (inclusive) containing ``.proof-of-done.yaml`` or
    ``.git``, without walking above `home`. Returns `cwd` unchanged if no marker is found.

    `exists` is injected (``exists(path) -> bool``) so this needs no real filesystem access.
    """
    _, cwd_segments = _normalize_posix_segments(cwd)
    _, home_segments = _normalize_posix_segments(home)
    segments = cwd_segments
    while True:
        candidate = "/" + "/".join(segments)
        if exists(candidate + "/.proof-of-done.yaml") or exists(candidate + "/.git"):
            return candidate
        if segments == home_segments or not segments:
            break
        segments = segments[:-1]
    return cwd
