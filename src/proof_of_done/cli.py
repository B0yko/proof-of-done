from __future__ import annotations

import argparse
from typing import Optional, Sequence

from proof_of_done import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="proof-of-done",
        description=(
            "Check a coding agent's completion claims against its own transcript."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    parser.parse_args(argv)
    return 0
