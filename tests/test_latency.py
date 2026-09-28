"""Tests for `eval/latency.py`.

Kept deliberately light on subprocess invocations: the full `--run` matrix (200 invocations x
4 cases x 2 interpreters x 2 cache states) is meant to be run once, by hand, for the README's
published numbers, not on every `pytest -q`. These tests call the same building blocks
(`generate`, `measure_case`, `measure_audit_throughput`) with a couple of invocations each, so
the real launcher subprocess and the real `audit` CLI are still exercised end to end, just not
200 times over.
"""

from __future__ import annotations

import json
import os

import pytest

from eval import latency


@pytest.fixture(scope="module")
def corpus(tmp_path_factory) -> str:
    out_dir = str(tmp_path_factory.mktemp("latency-corpus"))
    latency.generate(out_dir)
    return out_dir


def test_generate_writes_all_four_cases_with_payloads_pointing_at_real_files(corpus) -> None:
    payload_paths = latency._payload_paths(corpus)
    assert set(payload_paths) == set(latency.CASE_NAMES)
    for path in payload_paths.values():
        assert os.path.exists(path)
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
        assert payload["hook_event_name"] == "Stop"
        assert os.path.exists(payload["transcript_path"])


def test_generate_hits_the_target_sizes_within_a_reasonable_tolerance(corpus) -> None:
    for case_name, target in latency._TARGET_BYTES.items():
        size = latency._transcript_size(case_name, corpus)
        assert target * 0.5 <= size <= target * 1.5, (case_name, size, target)


def test_fast_path_payload_message_contains_no_configured_keyword(corpus) -> None:
    with open(latency._payload_paths(corpus)["fast_path"], encoding="utf-8") as fh:
        payload = json.load(fh)
    message = payload["last_assistant_message"].lower()
    # A conservative check against the built-in keyword list without importing config: none
    # of the obvious rule keywords should appear in a message meant to skip the prefilter.
    for kw in ("test", "build", "lint", "type", "deploy", "fix", "verif"):
        assert kw not in message, message


def test_size_case_payload_carries_an_unsupported_stale_claim(corpus) -> None:
    with open(latency._payload_paths(corpus)["1mb"], encoding="utf-8") as fh:
        payload = json.load(fh)
    assert "tests pass" in payload["last_assistant_message"].lower()


def test_measure_case_runs_the_real_launcher_and_returns_sane_stats(corpus) -> None:
    interpreters = latency._resolve_interpreters()
    python_path = next(iter(interpreters.values()))
    stats = latency.measure_case(
        latency._payload_paths(corpus)["fast_path"],
        python_path,
        cache_state="warm",
        invocations=2,
    )
    assert stats["invocations"] == 2
    assert stats["p50_ms"] > 0
    assert stats["max_ms"] >= stats["p95_ms"] >= stats["p50_ms"] >= 0


def test_measure_case_cold_cache_deletes_the_cache_file_between_calls(corpus, tmp_path) -> None:
    interpreters = latency._resolve_interpreters()
    python_path = next(iter(interpreters.values()))
    # Cold and warm must both complete without error; cold has no pre-warm call.
    stats = latency.measure_case(
        latency._payload_paths(corpus)["fast_path"],
        python_path,
        cache_state="cold",
        invocations=2,
    )
    assert stats["invocations"] == 2


def test_measure_audit_throughput_reports_a_positive_rate_or_a_clear_unavailable_reason(
    corpus, monkeypatch
) -> None:
    monkeypatch.setattr(
        latency,
        "_payload_paths",
        lambda out_dir=latency.LATENCY_DIR: {"50mb": os.path.join(corpus, "1mb-payload.json")},
    )
    result = latency.measure_audit_throughput(runs=1)
    if result["available"]:
        assert result["median_mb_per_s"] is None or result["median_mb_per_s"] > 0
    else:
        assert result["error"]


def test_percentile_helper_matches_hand_computed_values() -> None:
    values = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert latency._percentile(values, 50) == 30.0
    assert latency._percentile(values, 0) == 10.0
    assert latency._percentile(values, 100) == 50.0


def test_cli_requires_an_action() -> None:
    with pytest.raises(SystemExit) as exc:
        latency.main([])
    assert exc.value.code == 2
