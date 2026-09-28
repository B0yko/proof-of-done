"""Stdlib-only fast path for a keyword-miss Stop/SubagentStop message (PLAN §9; spec item 2 of
S11-perf.md).

``hook._run`` already has a cheap prefilter (a message with none of the enabled rules'
keywords cannot produce a claim, so the transcript is never parsed) but reaching it used to
require importing ``proof_of_done.config`` first -- and `config.py`'s dataclasses/regex/shlex
machinery pulls in ``dataclasses`` (which itself imports ``inspect``, ``ast`` and more), plus
``proof_of_done.claims`` just for the one-line substring check. On a warm merged-config cache
none of that is actually needed: the cache file on disk already holds the merged config's
``enabled``/``check_subagents``/``subagent_skip_types`` and the keyword union as plain JSON.

:func:`decide` re-derives the exact same cache key `config.load_cached` would (the layer paths,
each stamped with ``[path, mtime_ns, size]`` or ``[path, None]``) using only ``os``/``json``, so
a cache hit here is provably the same merged config `config.load_cached` would produce -- if
anything doesn't line up (the cache is missing, stale, or shaped unexpectedly), this always
falls through to the real path rather than guess. It duplicates a handful of small, pure
functions from ``paths.py``/``config.py`` (project-root discovery, the layer-path list, the
cache-key shape) instead of importing those modules, which is the whole point: importing either
would defeat the purpose.

Keep this module's own top-level imports stdlib-only and cheap (``json``, ``os``); nothing here
may import ``proof_of_done.config``, ``proof_of_done.claims``, or ``proof_of_done.paths``.
"""

from __future__ import annotations

import json
import os
from typing import Any

ALLOW = "allow"  # the fast path is confident the hook produces no output; caller returns None
FALL_THROUGH = "fall_through"  # inconclusive; caller must run the normal path

_CACHE_FILENAME = "config-cache.json"  # must match config.CACHE_FILENAME


# ------------------------------------------------------------------------------------------
# duplicated (not imported) from paths.py / config.py -- see the module docstring
# ------------------------------------------------------------------------------------------


def _normalize_posix_segments(path: str) -> tuple[bool, tuple[str, ...]]:
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
            continue
        stack.append(part)
    return is_absolute, tuple(stack)


def _find_project_root(cwd: str, home: str) -> str:
    """Same algorithm as ``paths.find_project_root(cwd, home, os.path.exists)``."""
    _, cwd_segments = _normalize_posix_segments(cwd)
    _, home_segments = _normalize_posix_segments(home)
    segments = cwd_segments
    while True:
        candidate = "/" + "/".join(segments)
        if os.path.exists(candidate + "/.proof-of-done.yaml") or os.path.exists(
            candidate + "/.git"
        ):
            return candidate
        if segments == home_segments or not segments:
            break
        segments = segments[:-1]
    return cwd


def _defaults_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "defaults.yaml")


def _user_config_path(env: dict[str, str]) -> str:
    xdg = env.get("XDG_CONFIG_HOME")
    if not xdg:
        home = env.get("HOME") or os.path.expanduser("~")
        xdg = os.path.join(home, ".config")
    return os.path.join(xdg, "proof-of-done", "config.yaml")


def _layer_paths(root: str, env: dict[str, str]) -> list[str]:
    project = os.path.join(root, ".proof-of-done.yaml")
    paths_out = [_defaults_path(), _user_config_path(env), project]
    extra = env.get("PROOF_OF_DONE_CONFIG")
    if extra:
        paths_out.append(extra)
    return paths_out


def _cache_key(layer_paths: list[str]) -> list[list[Any]]:
    key: list[list[Any]] = []
    for path in layer_paths:
        try:
            info = os.stat(path)
        except OSError:
            key.append([path, None])
        else:
            key.append([path, info.st_mtime_ns, info.st_size])
    return key


# ------------------------------------------------------------------------------------------
# decide
# ------------------------------------------------------------------------------------------


def decide(
    message_text: str,
    cwd: str,
    env: dict[str, str],
    data_dir: str,
    is_subagent: bool,
    agent_type: Any,
) -> str:
    """`ALLOW` when the hook is certain to produce no output (the same conclusion
    ``hook._run``'s ``cfg.enabled``/``check_subagents``/``subagent_skip_types``/keyword-prefilter
    checks would reach), `FALL_THROUGH` whenever the cache is missing, stale, or shaped
    unexpectedly, or the message actually needs the full path (a keyword matched). Never raises:
    any unexpected condition also falls through.
    """
    try:
        home = env.get("HOME") or os.path.expanduser("~")
        root = _find_project_root(cwd, home)
        layer_paths = _layer_paths(root, env)
        key = _cache_key(layer_paths)

        cache_path = os.path.join(data_dir, _CACHE_FILENAME)
        try:
            with open(cache_path, encoding="utf-8") as handle:
                cached = json.load(handle)
        except (OSError, ValueError):
            return FALL_THROUGH
        if not isinstance(cached, dict) or cached.get("cache_key") != key:
            return FALL_THROUGH

        cfg_data = cached.get("config")
        keywords = cached.get("keywords")
        if not isinstance(cfg_data, dict) or not isinstance(keywords, list):
            return FALL_THROUGH

        if not cfg_data.get("enabled", True):
            return ALLOW
        if is_subagent:
            if not cfg_data.get("check_subagents", True):
                return ALLOW
            skip_types = cfg_data.get("subagent_skip_types")
            if (
                isinstance(skip_types, list)
                and isinstance(agent_type, str)
                and agent_type in skip_types
            ):
                return ALLOW

        lowered = message_text.lower()
        if not any(isinstance(k, str) and k in lowered for k in keywords):
            return ALLOW
        return FALL_THROUGH
    except Exception:
        return FALL_THROUGH


__all__ = ["ALLOW", "FALL_THROUGH", "decide"]
