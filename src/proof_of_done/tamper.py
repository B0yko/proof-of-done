"""Tamper scan (PLAN §9, spec item 8): did *this session* edit proof-of-done's own config
files, or a Claude Code settings file in a way that touches `PROOF_OF_DONE`/`enabledPlugins`?

:func:`scan` never decides anything by itself -- it only reports the raw edits found. The
caller feeds its result into :func:`proof_of_done.config.effective` (via
:func:`tampered_paths`/:func:`settings_tampered`), which is what actually undoes the specific
downgrades a tampered file is responsible for. Tampering alone never blocks a stop.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass

from proof_of_done.evidence import EditEvent

_TAMPER_MARKERS = ("PROOF_OF_DONE", "enabledPlugins")


@dataclass(frozen=True)
class TamperEdit:
    """One edit event (in the *main* session -- SubagentStop always scans the parent
    transcript, never the subagent's own) that touched a monitored file."""

    path: str  # normalized absolute path
    step: int
    kind: str  # "config" | "settings"


def _norm(path: str) -> str:
    return os.path.normpath(path)


def scan(
    edits: Sequence[EditEvent],
    *,
    project_config_path: str,
    user_config_path: str,
    env_config_path: str | None,
    settings_paths: Sequence[str],
) -> list[TamperEdit]:
    """Every edit in `edits` that touched a monitored config or settings file.

    `edits` is normally `EventList.edits` built from the *main* session (PLAN §9: "skip token
    and tamper edits are read from the main transcript_path"). A settings-file edit only counts
    when its written text (the Edit/Write/MultiEdit new text, or the raw Bash command for a
    Bash-based write) contains `PROOF_OF_DONE` or `enabledPlugins`; a config-file edit always
    counts, whatever it wrote.
    """
    config_paths = {_norm(p) for p in (project_config_path, user_config_path) if p}
    if env_config_path:
        config_paths.add(_norm(env_config_path))
    settings_set = {_norm(p) for p in settings_paths}

    found: list[TamperEdit] = []
    for edit in edits:
        target = edit.tamper_abs_path
        if target is None:
            continue
        norm = _norm(target)
        if norm in config_paths:
            found.append(TamperEdit(path=norm, step=edit.step, kind="config"))
        elif norm in settings_set:
            text = edit.tamper_text or ""
            if any(marker in text for marker in _TAMPER_MARKERS):
                found.append(TamperEdit(path=norm, step=edit.step, kind="settings"))
    return found


def tampered_paths(edits: Sequence[TamperEdit]) -> set[str]:
    """The config-file paths (not settings files) edited this session, for
    `config.effective`'s `tampered_paths` argument."""
    return {e.path for e in edits if e.kind == "config"}


def settings_tampered(edits: Sequence[TamperEdit]) -> bool:
    """Whether any Claude Code settings file was edited to touch `PROOF_OF_DONE`/
    `enabledPlugins` this session, for `config.effective`'s `settings_tampered` argument."""
    return any(e.kind == "settings" for e in edits)


def notes(edits: Sequence[TamperEdit]) -> list[str]:
    """One human-readable line per tampered file, naming the file and the step that edited it.
    Used as a fallback warning when a tamper edit did not itself cause any downgrade for
    `config.effective` to undo (so its own notes are empty), so the user is still told the
    session touched a monitored file."""
    seen: dict[str, int] = {}
    for e in edits:
        seen.setdefault(e.path, e.step)
    return [f"session edited {path} at step {step + 1}" for path, step in seen.items()]


__all__ = ["TamperEdit", "notes", "scan", "settings_tampered", "tampered_paths"]
