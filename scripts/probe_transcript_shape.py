#!/usr/bin/env python3
"""Print the key-path and type shape of a Claude Code transcript (JSONL).

Only structure is printed: key paths, JSON value types, occurrence counts,
the maximum length of string values, boolean true/false counts, the values
of a small allow-list of format discriminator keys (``type``, ``subtype``,
``role``, ...) when they look like format tokens, and counts of well-known
structural markers at the start of text fields. No free-text value, id,
path or timestamp is ever printed.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from typing import Any

DISCRIMINATORS = {
    "type",
    "subtype",
    "role",
    "level",
    "userType",
    "status",
    "operation",
    "kind",
    "promptSource",
    "turnOrigin",
    "entrypoint",
    "stop_reason",
    "hookEvent",
    "hookName",
}
ID_KEY_RE = re.compile(r"^(toolu_|agent-|call_)|[A-Za-z0-9]{20,}|\d{6,}")
TOKEN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_\-]{0,39}$")
MARKERS = [
    ("task-notification", re.compile(r"^\s*<task-notification>")),
    ("local-command-stdout", re.compile(r"^\s*<local-command-stdout>")),
    ("local-command-stderr", re.compile(r"^\s*<local-command-stderr>")),
    ("command-name", re.compile(r"^\s*<command-name>")),
    ("command-message", re.compile(r"^\s*<command-message>")),
    ("bash-input", re.compile(r"^\s*<bash-input>")),
    ("bash-stdout", re.compile(r"^\s*<bash-stdout>")),
    ("system-reminder", re.compile(r"^\s*<system-reminder>")),
    ("persisted-output", re.compile(r"^\s*<persisted-output>")),
    ("exit-code-prefix", re.compile(r"^Exit code \d+")),
    ("timed-out", re.compile(r"Command timed out after")),
    ("running-in-background", re.compile(r"^Command running in background")),
    ("request-interrupted", re.compile(r"^\[Request interrupted")),
    ("stop-hook-feedback", re.compile(r"^Stop hook feedback")),
    ("caveat", re.compile(r"^Caveat:")),
    ("continued-session", re.compile(r"^This session is being continued")),
]

ANYWHERE = [
    ("mentions-truncated", re.compile(r"truncat", re.I)),
    ("output-too-large", re.compile(r"Output too large")),
    ("exit-code-tag", re.compile(r"<exit-code>|exit code \d+", re.I)),
    ("status-tag", re.compile(r"<status>")),
]


def jtype(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, str):
        return "str"
    if isinstance(v, list):
        return "list"
    if isinstance(v, dict):
        return "object"
    return type(v).__name__


class Shape:
    def __init__(self) -> None:
        self.types: defaultdict[str, defaultdict[str, int]] = defaultdict(lambda: defaultdict(int))
        self.maxlen: dict[str, int] = {}
        self.bools: defaultdict[str, list[int]] = defaultdict(lambda: [0, 0])
        self.tokens: defaultdict[str, defaultdict[str, int]] = defaultdict(lambda: defaultdict(int))
        self.markers: defaultdict[str, defaultdict[str, int]] = defaultdict(
            lambda: defaultdict(int)
        )

    def walk(self, v: Any, path: str) -> None:
        t = jtype(v)
        self.types[path][t] += 1
        if isinstance(v, dict):
            for k, sub in v.items():
                key = k if TOKEN_RE.match(k) and not ID_KEY_RE.search(k) else "<key>"
                self.walk(sub, f"{path}.{key}" if path else key)
        elif isinstance(v, list):
            for item in v:
                self.walk(item, path + "[]")
        elif isinstance(v, str):
            self.maxlen[path] = max(self.maxlen.get(path, 0), len(v))
            leaf = path.rsplit(".", 1)[-1].rstrip("[]")
            if leaf in DISCRIMINATORS and TOKEN_RE.match(v):
                self.tokens[path][v] += 1
            for name, rx in MARKERS:
                if rx.search(v[:200]):
                    self.markers[path][name] += 1
            for name, rx in ANYWHERE:
                if rx.search(v):
                    self.markers[path][name] += 1
        elif isinstance(v, bool):
            self.bools[path][1 if v else 0] += 1


def record_kind(obj: Any) -> str:
    if not isinstance(obj, dict):
        return jtype(obj)
    parts = []
    for key in ("type", "subtype"):
        val = obj.get(key)
        if isinstance(val, str) and TOKEN_RE.match(val):
            parts.append(f"{key}={val}")
    return " ".join(parts) or "(no type)"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("transcript", nargs="+")
    ap.add_argument(
        "--marker",
        action="append",
        default=[],
        metavar="REGEX",
        help="also count string values matching REGEX (counts only)",
    )
    args = ap.parse_args(argv)
    for i, rx in enumerate(args.marker):
        ANYWHERE.append((f"marker{i}", re.compile(rx)))
    shapes: dict[str, Shape] = defaultdict(Shape)
    counts: defaultdict[str, int] = defaultdict(int)
    bad = 0
    lines = 0
    for p in args.transcript:
        with open(p, encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                lines += 1
                try:
                    obj = json.loads(raw)
                except ValueError:
                    bad += 1
                    continue
                kind = record_kind(obj)
                counts[kind] += 1
                shapes[kind].walk(obj, "")
    print(f"lines={lines} unparseable={bad}")
    for kind in sorted(shapes):
        s = shapes[kind]
        print(f"\n## {kind}  (records={counts[kind]})")
        for path in sorted(s.types):
            if not path:
                continue
            ts = ",".join(f"{t}:{n}" for t, n in sorted(s.types[path].items()))
            extra: list[str] = []
            if path in s.maxlen:
                extra.append(f"maxlen={s.maxlen[path]}")
            if path in s.bools:
                f_, t_ = s.bools[path]
                extra.append(f"true={t_} false={f_}")
            if path in s.tokens:
                toks: list[tuple[str, int]] = sorted(s.tokens[path].items())
                extra.append("values={" + ",".join(f"{k}:{n}" for k, n in toks) + "}")
            if path in s.markers:
                marker_items = sorted(s.markers[path].items())
                extra.append("markers={" + ",".join(f"{k}:{n}" for k, n in marker_items) + "}")
            print(f"  {path}: {ts} {' '.join(extra)}".rstrip())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
