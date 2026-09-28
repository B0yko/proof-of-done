"""Tests for `scripts/render_results.py`: marker splicing and `--check`.

Uses small hand-built report dicts (shaped like `eval/run_eval.py`'s and `eval/latency.py`'s
JSON) rather than a real evaluation run, so these tests stay fast and independent of the
committed `eval/results/*.json` files.
"""

from __future__ import annotations

import importlib.util
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPT_PATH = os.path.join(_REPO_ROOT, "scripts", "render_results.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("render_results", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


render_results = _load_module()


_MINIMAL_EVAL_REPORT = {
    "sets": {
        "templated": {
            "detection": {
                "n_labels": 10,
                "n_detections": 9,
                "tp": 9,
                "fp": 0,
                "fn": 1,
                "precision": 1.0,
                "recall": 0.9,
                "f1": 0.947,
            },
            "gate": {
                "shipped": {
                    "claim_level": {
                        "n": 10,
                        "tp": 4,
                        "fp": 0,
                        "fn": 1,
                        "tn": 5,
                        "precision": 1.0,
                        "recall": 0.8,
                        "f1": 0.889,
                        "precision_wilson95": [0.6, 1.0],
                        "recall_wilson95": [0.5, 0.95],
                        "f1_bootstrap95": [0.7, 0.95],
                    },
                    "turn_level": {
                        "n": 10,
                        "tp": 4,
                        "fp": 0,
                        "fn": 1,
                        "tn": 5,
                        "precision": 1.0,
                        "recall": 0.8,
                        "f1": 0.889,
                        "precision_wilson95": [0.6, 1.0],
                        "recall_wilson95": [0.5, 0.95],
                        "f1_bootstrap95": [0.7, 0.95],
                    },
                    "false_block_rate": {"n": 5, "blocked": 0, "rate": 0.0, "wilson95": [0.0, 0.3]},
                    "per_claim_type": {
                        "tests_passed": {
                            "n": 10,
                            "tp": 4,
                            "fp": 0,
                            "fn": 1,
                            "tn": 5,
                            "precision": 1.0,
                            "recall": 0.8,
                            "f1": 0.889,
                            "precision_wilson95": [0.6, 1.0],
                            "recall_wilson95": [0.5, 0.95],
                            "f1_bootstrap95": [0.7, 0.95],
                        }
                    },
                },
                "forced_block": {
                    "claim_level": {
                        "n": 10,
                        "tp": 4,
                        "fp": 0,
                        "fn": 1,
                        "tn": 5,
                        "precision": 1.0,
                        "recall": 0.8,
                        "f1": 0.889,
                        "precision_wilson95": [0.6, 1.0],
                        "recall_wilson95": [0.5, 0.95],
                        "f1_bootstrap95": [0.7, 0.95],
                    },
                    "turn_level": {
                        "n": 10,
                        "tp": 4,
                        "fp": 0,
                        "fn": 1,
                        "tn": 5,
                        "precision": 1.0,
                        "recall": 0.8,
                        "f1": 0.889,
                        "precision_wilson95": [0.6, 1.0],
                        "recall_wilson95": [0.5, 0.95],
                        "f1_bootstrap95": [0.7, 0.95],
                    },
                    "false_block_rate": {"n": 5, "blocked": 0, "rate": 0.0, "wilson95": [0.0, 0.3]},
                    "per_claim_type": {},
                },
            },
            "reason_confusion_matrix": [{"expected": "stale", "predicted": "stale", "count": 3}],
            "suggestion_accuracy": {"n": 4, "correct": 4, "rate": 1.0},
        },
        "adversarial": {
            "adversarial": {
                "k": 9,
                "m": 10,
                "misses": [
                    {
                        "scenario_id": "adv-x",
                        "expected_decision": "block",
                        "expected_reason": None,
                        "predicted_decision": "allow",
                        "predicted_reason": None,
                    }
                ],
            }
        },
        "heldout": {
            "failures": [
                {
                    "category": "detection_fn",
                    "scenario_id": "ho-001",
                    "quote": "It passes",
                    "explanation": "no tests_passed detection overlapped this labelled claim",
                }
            ]
        },
    }
}

_MINIMAL_LATENCY_REPORT = {
    "generated_at": "2026-09-28T00:00:00Z",
    "machine": {"hw_model": "Mac17,4", "cpu_brand": "Apple M5"},
    "cases": {
        "fast_path": {
            "transcript_bytes": None,
            "system_python3/warm": {
                "invocations": 20,
                "p50_ms": 100.0,
                "p95_ms": 120.0,
                "max_ms": 130.0,
            },
        }
    },
    "audit_throughput": {"available": True, "file_mb": 50.0, "median_mb_per_s": 40.0, "runs": 5},
}


def test_render_sections_covers_every_section_when_both_reports_are_present() -> None:
    sections = render_results.render_sections(_MINIMAL_EVAL_REPORT, _MINIMAL_LATENCY_REPORT)
    assert set(sections) == set(render_results.ALL_SECTIONS)
    assert "9" in sections["detection"] or "N" in sections["detection"]
    assert "9 / 10" in sections["adversarial"]
    assert "adv-x" in sections["adversarial"]
    assert "40.0 MB/s" in sections["throughput"]


def test_render_sections_omits_sections_with_no_source_report() -> None:
    sections = render_results.render_sections(None, None)
    assert sections == {}


def test_splice_only_touches_markers_present_in_the_document() -> None:
    doc = (
        "# Doc\n\n"
        "<!-- results:detection:start -->\nold\n<!-- results:detection:end -->\n\n"
        "no markers for gate-claim here\n"
    )
    sections = {"detection": "NEW CONTENT", "gate-claim": "unused, no marker in doc"}
    new_doc, replaced = render_results.splice(doc, sections)
    assert replaced == ["detection"]
    assert "NEW CONTENT" in new_doc
    assert "old" not in new_doc
    assert "unused, no marker in doc" not in new_doc


def test_splice_is_idempotent() -> None:
    doc = "<!-- results:detection:start -->\nold\n<!-- results:detection:end -->\n"
    once, _ = render_results.splice(doc, {"detection": "same"})
    twice, _ = render_results.splice(once, {"detection": "same"})
    assert once == twice


def test_process_file_check_mode_reports_stale_without_writing(tmp_path) -> None:
    path = tmp_path / "doc.md"
    path.write_text(
        "<!-- results:detection:start -->\nold\n<!-- results:detection:end -->\n", encoding="utf-8"
    )
    ok = render_results._process_file(str(path), {"detection": "new"}, check=True)
    assert ok is False
    assert "old" in path.read_text(encoding="utf-8")  # untouched in check mode


def test_process_file_writes_and_then_matches_on_a_second_check(tmp_path) -> None:
    path = tmp_path / "doc.md"
    path.write_text(
        "<!-- results:detection:start -->\nold\n<!-- results:detection:end -->\n", encoding="utf-8"
    )
    render_results._process_file(str(path), {"detection": "new"}, check=False)
    assert "new" in path.read_text(encoding="utf-8")
    assert render_results._process_file(str(path), {"detection": "new"}, check=True) is True


def test_process_file_missing_file_is_not_an_error(tmp_path) -> None:
    path = tmp_path / "does-not-exist.md"
    assert render_results._process_file(str(path), {"detection": "new"}, check=True) is True


def test_main_check_passes_on_the_committed_stub_with_no_results_present(
    tmp_path, monkeypatch
) -> None:
    # Point RESULTS_DIR at an empty directory so this test never depends on whatever the repo's
    # own eval/results/ happens to contain when it runs.
    monkeypatch.setattr(render_results, "RESULTS_DIR", str(tmp_path))
    rc = render_results.main(["--check"])
    assert rc == 0
