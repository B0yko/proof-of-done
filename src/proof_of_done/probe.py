"""File access abstraction used wherever project files need to be read without hard-wiring the
real filesystem: config loading, project-ecosystem detection, and tests/eval that want to inject
a synthetic file tree.

Paths passed to a `FileProbe` are always project-relative, POSIX-style, with no leading ``/``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class FileProbe(Protocol):
    """Read-only view of a project's files, keyed by project-relative POSIX path."""

    def exists(self, rel: str) -> bool:
        """Whether a file exists at the project-relative path `rel`."""
        ...

    def read_text(self, rel: str) -> str | None:
        """The text content at `rel`, or None if it does not exist or cannot be read as text."""
        ...


@dataclass
class OsProbe:
    """A `FileProbe` backed by a real directory on disk."""

    root: str

    def _abs(self, rel: str) -> Path:
        return Path(self.root) / rel

    def exists(self, rel: str) -> bool:
        return self._abs(rel).exists()

    def read_text(self, rel: str) -> str | None:
        try:
            return self._abs(rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None


@dataclass
class DictProbe:
    """A `FileProbe` backed by an in-memory mapping, for tests and rendered fixtures.

    A value of ``None`` means the file exists but is empty (or its content is not tracked);
    a key absent from `files` means the file does not exist.
    """

    files: dict[str, str | None]

    def exists(self, rel: str) -> bool:
        return rel in self.files

    def read_text(self, rel: str) -> str | None:
        return self.files.get(rel)
