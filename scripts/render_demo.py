#!/usr/bin/env python3
"""Render `fixtures/demo/*.yaml` into the packaged demo corpus
(`src/proof_of_done/demo/*.jsonl`): 30+ scenarios across Python, Node, Go and
Rust so `proof-of-done audit --demo` works from an installed package via
`importlib.resources`, with no dependency on `fixtures/` (which is not part of the wheel).

Run with no arguments to (re)write the demo corpus in place; run with `--check` to verify it
is already up to date (used by CI) -- exits 1 and lists what is stale or missing/extra,
without writing anything.

Only the rendered *main* session file is copied per scenario (never the `.stop-*.jsonl`
per-attempt snapshots `fixtures.render` also writes alongside it): `audit --demo` replays a
whole session's history itself, so those snapshots would just be redundant, mis-shaped extra
files under `src/proof_of_done/demo/`.
"""

from __future__ import annotations

import argparse
import glob
import os
import sys
import tempfile
from collections.abc import Sequence

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from fixtures import dsl, render  # noqa: E402

SCENARIOS_DIR = os.path.join(_REPO_ROOT, "fixtures", "demo")
OUT_DIR = os.path.join(_REPO_ROOT, "src", "proof_of_done", "demo")
MIN_SCENARIOS = 30


def _scenario_paths() -> list[str]:
    return sorted(glob.glob(os.path.join(SCENARIOS_DIR, "*.yaml")))


def _render_one(path: str, tmp_dir: str) -> tuple[str, bytes]:
    scenario = dsl.load_scenario(path)
    scenario_id = scenario["id"]
    out = os.path.join(tmp_dir, scenario_id)
    rendered = render.render(scenario, out)
    with open(rendered.session_path, "rb") as fh:
        data = fh.read()
    return scenario_id, data


def _build(tmp_dir: str) -> dict[str, bytes]:
    scenario_paths = _scenario_paths()
    if len(scenario_paths) < MIN_SCENARIOS:
        raise SystemExit(
            f"{SCENARIOS_DIR}: found {len(scenario_paths)} scenarios, need at least {MIN_SCENARIOS}"
        )
    built: dict[str, bytes] = {}
    for path in scenario_paths:
        scenario_id, data = _render_one(path, tmp_dir)
        if scenario_id in built:
            raise SystemExit(f"duplicate scenario id {scenario_id!r} (from {path})")
        built[scenario_id] = data
    return built


def _check(built: dict[str, bytes]) -> int:
    problems: list[str] = []
    existing = {
        os.path.basename(p)[: -len(".jsonl")] for p in glob.glob(os.path.join(OUT_DIR, "*.jsonl"))
    }
    for scenario_id, data in sorted(built.items()):
        target = os.path.join(OUT_DIR, scenario_id + ".jsonl")
        if not os.path.exists(target):
            problems.append(f"missing: {target}")
            continue
        with open(target, "rb") as fh:
            current = fh.read()
        if current != data:
            problems.append(f"stale: {target}")
    extra = existing - set(built)
    for scenario_id in sorted(extra):
        problems.append(f"extra (no matching scenario): {scenario_id}.jsonl")

    if problems:
        print(f"{OUT_DIR} is out of date with {SCENARIOS_DIR}:")
        for line in problems:
            print(f"  {line}")
        print("Run `uv run python scripts/render_demo.py` to regenerate it.")
        return 1
    print(f"{OUT_DIR} is up to date ({len(built)} scenarios).")
    return 0


def _write(built: dict[str, bytes]) -> int:
    os.makedirs(OUT_DIR, exist_ok=True)
    existing = {os.path.basename(p) for p in glob.glob(os.path.join(OUT_DIR, "*.jsonl"))}
    wanted = {f"{scenario_id}.jsonl" for scenario_id in built}
    for stale_name in existing - wanted:
        os.remove(os.path.join(OUT_DIR, stale_name))
    for scenario_id, data in built.items():
        target = os.path.join(OUT_DIR, scenario_id + ".jsonl")
        with open(target, "wb") as fh:
            fh.write(data)
    print(f"wrote {len(built)} demo sessions to {OUT_DIR}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="Verify the demo corpus is up to date; write nothing."
    )
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory() as tmp_dir:
        built = _build(tmp_dir)
        if args.check:
            return _check(built)
        return _write(built)


if __name__ == "__main__":
    sys.exit(main())
