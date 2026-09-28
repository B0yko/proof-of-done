#!/usr/bin/env python3
"""Latency + audit-throughput harness (PLAN §12, spec items 3/5/6, "Latency corpus").

``--generate`` writes a fast-path payload (a claim-free final message: the message alone
disqualifies it before the transcript is even opened) and three synthetic Claude Code
transcripts (~1 MB, ~10 MB, ~50 MB: a realistic mix of edits and Bash calls of varying output
size, some long enough to exercise the persisted-output path) whose last message carries an
*unsupported* claim, so a measured run always walks the full decision path through to a block,
into ``eval/.latency/`` (gitignored; never committed).

``--run`` measures 200 fresh-subprocess invocations of the real launcher
(``sh bin/proof-of-done-hook stop --data-dir D``) per case, under both the system
``/usr/bin/python3`` and a uv-managed CPython 3.12, with an empty ("cold") and a pre-warmed
("warm") merged-config cache, always with no transcript parse cache (the hook implements
none), and reports p50/p95/max per case in milliseconds. It also measures `proof-of-done audit
--format json` on the 50 MB transcript over 5 runs and reports the median MB/s.

``--quick`` (spec: "used by a test, not by CI") cuts this to 20 invocations under one
interpreter and one cache state, and one audit run, to prove the harness works without the
several minutes the full matrix takes.

Not part of the installed wheel: run directly as ``python eval/latency.py``.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from fixtures import dsl, render  # noqa: E402

LATENCY_DIR = os.path.join(_REPO_ROOT, "eval", ".latency")
RESULTS_DIR = os.path.join(_REPO_ROOT, "eval", "results")
HOOK_LAUNCHER = os.path.join(_REPO_ROOT, "bin", "proof-of-done-hook")

CASE_NAMES = ("fast_path", "1mb", "10mb", "50mb")
_TARGET_BYTES = {"1mb": 1_000_000, "10mb": 10_000_000, "50mb": 50_000_000}
_CALIBRATION_PAIRS = 20

_PROJECT_FILES = {"pyproject.toml": '[project]\nname = "demo-app"\n', "uv.lock": None}


# --------------------------------------------------------------------------------------
# --generate: synthetic transcripts and Stop payloads
# --------------------------------------------------------------------------------------


def _output_repeat_for(i: int) -> dict[str, Any]:
    # A realistic mix of output sizes; every 4th call is long enough (>30 000 chars) to
    # exercise the persisted-output path (`docs/transcript-format.md`).
    sizes_lines = (4, 18, 75, 500)
    lines = sizes_lines[i % len(sizes_lines)]
    return {"line": ("z" * 78) + f"{i:04d}", "times": lines}


def _pair_steps(n: int) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    for i in range(n):
        steps.append(
            {
                "bash": {
                    "cmd": "uv run pytest -q",
                    "output_repeat": _output_repeat_for(i),
                    "output": "42 passed in 0.90s",
                }
            }
        )
        steps.append(
            {
                "edit": {
                    "tool": "Edit",
                    "path": f"src/app/module_{i % 20}.py",
                    "old": "value_a",
                    "new": f"value_b_{i}",
                }
            }
        )
    return steps


def _size_scenario(scenario_id: str, pairs: int) -> dict[str, Any]:
    scenario = {
        "id": scenario_id,
        "ecosystem": "python",
        "project_files": dict(_PROJECT_FILES),
        "turns": [
            {
                "user": "Run the full regression suite across the module set and report status.",
                "steps": _pair_steps(pairs),
                # The session's last edit (the final pair's `edit` step) comes *after* every
                # test run above, so the claim below is unsupported for reason `stale` -- the
                # full claim-detection + evidence path runs, and the block path is exercised.
                "final": "[[tests_passed|All tests pass across every module]].",
                "labels": {
                    "tests_passed": {
                        "label": "unsupported",
                        "reason": "stale",
                        "suggest": "uv run pytest -q",
                    }
                },
            }
        ],
    }
    dsl.validate_scenario(scenario, scenario_id + ".yaml")
    return scenario


def _fast_path_scenario() -> dict[str, Any]:
    scenario = {
        "id": "latency-fast-path",
        "ecosystem": "python",
        "project_files": dict(_PROJECT_FILES),
        "turns": [
            {
                "user": "Take a look at the module layout.",
                "steps": [
                    {
                        "edit": {
                            "tool": "Edit",
                            "path": "src/app/module_0.py",
                            "old": "a",
                            "new": "b",
                        }
                    }
                ],
                # Deliberately keywordless: no rule's keyword union matches this message, so
                # the hook's own prefilter exits before the transcript is ever opened.
                "final": "Took a look; nothing else needed right now.",
                "labels": {},
            }
        ],
    }
    dsl.validate_scenario(scenario, scenario["id"] + ".yaml")
    return scenario


def _rendered_file_size(scenario: dict[str, Any], out_dir: str) -> tuple[int, Any]:
    rendered = render.render(scenario, out_dir)
    return os.path.getsize(rendered.session_path), rendered


def _calibrate_bytes_per_pair(work_dir: str) -> float:
    scenario = _size_scenario("latency-calibration", _CALIBRATION_PAIRS)
    size, _rendered = _rendered_file_size(scenario, os.path.join(work_dir, "calibration"))
    return size / _CALIBRATION_PAIRS


def generate(out_dir: str = LATENCY_DIR) -> dict[str, str]:
    """Write the fast-path payload and the 1/10/50 MB transcripts + their Stop payloads into
    `out_dir`. Returns `{case_name: payload_path}`."""
    if os.path.isdir(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(out_dir, exist_ok=True)

    payload_paths: dict[str, str] = {}

    fast_rendered = render.render(_fast_path_scenario(), os.path.join(out_dir, "fast_path"))
    payload_paths["fast_path"] = _write_payload(out_dir, "fast_path", fast_rendered)

    bytes_per_pair = _calibrate_bytes_per_pair(out_dir)
    for case_name, target_bytes in _TARGET_BYTES.items():
        pairs = max(1, round(target_bytes / bytes_per_pair))
        scenario = _size_scenario(f"latency-{case_name}", pairs)
        _size, rendered = _rendered_file_size(scenario, os.path.join(out_dir, case_name))
        payload_paths[case_name] = _write_payload(out_dir, case_name, rendered)

    return payload_paths


def _write_payload(out_dir: str, case_name: str, rendered: Any) -> str:
    stop_case = rendered.stop_cases[-1]
    payload_path = os.path.join(out_dir, f"{case_name}-payload.json")
    with open(payload_path, "w", encoding="utf-8") as fh:
        json.dump(stop_case.payload, fh)
    return payload_path


def _payload_paths(out_dir: str = LATENCY_DIR) -> dict[str, str]:
    return {name: os.path.join(out_dir, f"{name}-payload.json") for name in CASE_NAMES}


def _transcript_size(case_name: str, out_dir: str = LATENCY_DIR) -> int:
    with open(_payload_paths(out_dir)[case_name], encoding="utf-8") as fh:
        payload = json.load(fh)
    return os.path.getsize(payload["transcript_path"])


# --------------------------------------------------------------------------------------
# interpreters
# --------------------------------------------------------------------------------------


def _resolve_interpreters() -> dict[str, str]:
    interpreters: dict[str, str] = {}
    if os.path.exists("/usr/bin/python3"):
        interpreters["system_python3"] = "/usr/bin/python3"
    uv = shutil.which("uv")
    if uv:
        try:
            out = subprocess.run(
                [uv, "python", "find", "3.12"], capture_output=True, text=True, check=True
            )
            path = out.stdout.strip()
            if path:
                interpreters["uv_python_3_12"] = path
        except (OSError, subprocess.CalledProcessError):
            pass
    if not interpreters:
        interpreters["python3"] = shutil.which("python3") or sys.executable
    return interpreters


# --------------------------------------------------------------------------------------
# --run: fresh-subprocess launcher timing
# --------------------------------------------------------------------------------------


def _percentile(values_sorted: list[float], pct: float) -> float:
    idx = min(len(values_sorted) - 1, max(0, round(pct / 100 * (len(values_sorted) - 1))))
    return values_sorted[idx]


def _run_once(payload_bytes: bytes, data_dir: str, python_path: str) -> float:
    env = dict(os.environ)
    env["PROOF_OF_DONE_PYTHON"] = python_path
    start = time.perf_counter()
    subprocess.run(
        ["sh", HOOK_LAUNCHER, "stop", "--data-dir", data_dir],
        input=payload_bytes,
        capture_output=True,
        env=env,
        check=False,
    )
    return (time.perf_counter() - start) * 1000.0


def _config_cache_path(data_dir: str) -> str:
    return os.path.join(data_dir, "config-cache.json")


def measure_case(
    payload_path: str, python_path: str, *, cache_state: str, invocations: int
) -> dict[str, Any]:
    """`cache_state` is "cold" (the config cache is deleted before every timed call) or
    "warm" (pre-warmed once, then left alone for every timed call)."""
    with open(payload_path, "rb") as fh:
        payload_bytes = fh.read()

    data_dir = tempfile.mkdtemp(prefix="proof-of-done-latency-")
    try:
        if cache_state == "warm":
            _run_once(payload_bytes, data_dir, python_path)  # pre-warm

        samples: list[float] = []
        for _ in range(invocations):
            if cache_state == "cold":
                with contextlib.suppress(OSError):
                    os.remove(_config_cache_path(data_dir))
            samples.append(_run_once(payload_bytes, data_dir, python_path))
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)

    samples.sort()
    return {
        "invocations": invocations,
        "p50_ms": _percentile(samples, 50),
        "p95_ms": _percentile(samples, 95),
        "max_ms": max(samples),
    }


# --------------------------------------------------------------------------------------
# audit throughput
# --------------------------------------------------------------------------------------


def measure_audit_throughput(*, runs: int) -> dict[str, Any]:
    transcript_path = _payload_paths()["50mb"]
    with open(transcript_path, encoding="utf-8") as fh:
        payload = json.load(fh)
    file_path = payload["transcript_path"]
    size_mb = os.path.getsize(file_path) / 1_000_000

    durations: list[float] = []
    last_error: str | None = None
    for _ in range(runs):
        start = time.perf_counter()
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "proof_of_done", "audit", file_path, "--format", "json"],
                cwd=_REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:
            last_error = str(exc)
            break
        elapsed = time.perf_counter() - start
        if proc.returncode not in (0, 1):  # 1 == --ci over-threshold; both mean it ran
            last_error = (proc.stderr or proc.stdout)[-500:]
            break
        durations.append(elapsed)

    if not durations:
        return {"available": False, "error": last_error}
    mbps = [size_mb / d for d in durations if d > 0]
    return {
        "available": True,
        "runs": len(durations),
        "file_mb": size_mb,
        "median_mb_per_s": statistics.median(mbps) if mbps else None,
    }


# --------------------------------------------------------------------------------------
# machine info
# --------------------------------------------------------------------------------------


def _sysctl(name: str) -> str | None:
    try:
        out = subprocess.run(["sysctl", "-n", name], capture_output=True, text=True, check=True)
        return out.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _macos_version() -> str | None:
    try:
        out = subprocess.run(["sw_vers"], capture_output=True, text=True, check=True)
        return out.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def machine_info() -> dict[str, Any]:
    """Hardware/OS facts the README's latency numbers must be published with -- never a
    hostname or username."""
    return {
        "hw_model": _sysctl("hw.model"),
        "cpu_brand": _sysctl("machdep.cpu.brand_string"),
        "hw_memsize": _sysctl("hw.memsize"),
        "macos_version": _macos_version(),
        "python_versions": {name: path for name, path in _resolve_interpreters().items()},
    }


# --------------------------------------------------------------------------------------
# top-level run
# --------------------------------------------------------------------------------------


@dataclass
class RunOptions:
    invocations: int
    interpreters: dict[str, str]
    cache_states: tuple[str, ...]
    audit_runs: int


def run(options: RunOptions, out_dir: str = LATENCY_DIR) -> dict[str, Any]:
    if not os.path.isdir(out_dir) or not all(
        os.path.exists(p) for p in _payload_paths(out_dir).values()
    ):
        generate(out_dir)

    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "python": platform.python_version(),
        "machine": machine_info(),
        "cases": {},
    }

    payload_paths = _payload_paths(out_dir)
    for case_name in CASE_NAMES:
        case_report: dict[str, Any] = {
            "transcript_bytes": None
            if case_name == "fast_path"
            else _transcript_size(case_name, out_dir)
        }
        for interp_name, python_path in options.interpreters.items():
            for cache_state in options.cache_states:
                key = f"{interp_name}/{cache_state}"
                case_report[key] = measure_case(
                    payload_paths[case_name],
                    python_path,
                    cache_state=cache_state,
                    invocations=options.invocations,
                )
        report["cases"][case_name] = case_report

    report["audit_throughput"] = measure_audit_throughput(runs=options.audit_runs)
    return report


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def _default_out_path() -> str:
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return os.path.join(RESULTS_DIR, f"latency-{date}.json")


def _print_summary(report: dict[str, Any]) -> None:
    for case_name, case_report in report["cases"].items():
        for key, stats in case_report.items():
            if key == "transcript_bytes" or not isinstance(stats, dict):
                continue
            print(
                f"{case_name:>10} {key:>22}: p50={stats['p50_ms']:.1f}ms "
                f"p95={stats['p95_ms']:.1f}ms max={stats['max_ms']:.1f}ms "
                f"(n={stats['invocations']})"
            )
    audit = report.get("audit_throughput", {})
    if audit.get("available"):
        print(
            f"audit throughput: median {audit['median_mb_per_s']:.1f} MB/s "
            f"over {audit['runs']} run(s)"
        )
    else:
        print(f"audit throughput: unavailable ({audit.get('error')})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="latency.py", description="Hook latency + audit throughput."
    )
    parser.add_argument(
        "--generate", action="store_true", help="write the synthetic latency corpus"
    )
    parser.add_argument("--run", action="store_true", help="measure the full 200-invocation matrix")
    parser.add_argument("--quick", action="store_true", help="a fast 20-invocation smoke run")
    parser.add_argument(
        "--out", default=None, help="output path (default: eval/results/latency-<date>.json)"
    )
    args = parser.parse_args(argv)

    if args.generate:
        paths = generate()
        print(f"generated {len(paths)} case(s) in {LATENCY_DIR}")

    if args.run or args.quick:
        if args.quick:
            interpreters = dict(list(_resolve_interpreters().items())[:1])
            options = RunOptions(
                invocations=20, interpreters=interpreters, cache_states=("warm",), audit_runs=1
            )
        else:
            options = RunOptions(
                invocations=200,
                interpreters=_resolve_interpreters(),
                cache_states=("cold", "warm"),
                audit_runs=5,
            )
        report = run(options)
        _print_summary(report)
        out_path = args.out or _default_out_path()
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, sort_keys=True, ensure_ascii=False)
            fh.write("\n")
        print(f"wrote {out_path}")
    elif not args.generate:
        parser.error("pass --generate, --run and/or --quick")

    return 0


if __name__ == "__main__":
    sys.exit(main())
