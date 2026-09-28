"""Tests for `schemas/agent-trace-v1.json`: a valid trace validates, and the
things the shared format requires stay actually enforced -- every listed key required (except
`ground_truth.details`), the closed shape everywhere but `meta`/`args`/`output`/`subject`, and
the `task.domain`/step `kind`/step `role`/`ground_truth.outcome`/`ground_truth.checked_by`
enums.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from proof_of_done import traces


def _minimal_trace() -> dict[str, Any]:
    return {
        "schema": "agent-trace/v1",
        "trace_id": "s1",
        "source": "proof-of-done/0.1.0",
        "task": {"id": "s1", "domain": "coding", "instruction": "do the thing"},
        "steps": [
            {
                "i": 0,
                "ts": "2026-01-05T10:00:00.000Z",
                "kind": "message",
                "role": "user",
                "name": None,
                "content": "do the thing",
                "args": None,
                "ok": None,
                "output": None,
                "error": None,
            }
        ],
        "final_claim": {"text": "done", "claims": []},
        "ground_truth": {"outcome": "unknown", "checked_by": "none"},
        "meta": {},
    }


def test_minimal_trace_is_valid() -> None:
    assert traces.validate_trace(_minimal_trace()) == []


def test_ground_truth_details_is_optional_but_valid_when_present() -> None:
    trace = _minimal_trace()
    trace["ground_truth"]["details"] = {"label_source": "synthetic-by-construction"}
    assert traces.validate_trace(trace) == []


@pytest.mark.parametrize(
    "key",
    ["schema", "trace_id", "source", "task", "steps", "final_claim", "ground_truth", "meta"],
)
def test_missing_top_level_key_is_rejected(key: str) -> None:
    trace = _minimal_trace()
    del trace[key]
    assert traces.validate_trace(trace) != []


def test_extra_top_level_key_is_rejected() -> None:
    trace = _minimal_trace()
    trace["extra"] = "nope"
    assert traces.validate_trace(trace) != []


def test_meta_allows_arbitrary_extra_content() -> None:
    trace = _minimal_trace()
    trace["meta"] = {"anything": {"goes": True, "nested": [1, 2, 3]}}
    assert traces.validate_trace(trace) == []


def test_args_output_subject_details_are_free_form() -> None:
    trace = _minimal_trace()
    trace["steps"][0]["kind"] = "tool_call"
    trace["steps"][0]["role"] = "agent"
    trace["steps"][0]["args"] = {"whatever": 1, "nested": {"x": 1}}
    trace["final_claim"]["claims"] = [
        {"type": "tests_passed", "subject": {"rule": "tests", "quote": "x", "extra": 1}}
    ]
    trace["ground_truth"]["details"] = {"anything": "goes"}
    assert traces.validate_trace(trace) == []


def test_extra_key_inside_a_step_is_rejected() -> None:
    trace = _minimal_trace()
    trace["steps"][0]["bogus"] = 1
    assert traces.validate_trace(trace) != []


def test_missing_key_inside_a_step_is_rejected() -> None:
    trace = _minimal_trace()
    del trace["steps"][0]["ok"]
    assert traces.validate_trace(trace) != []


def test_extra_key_inside_task_is_rejected() -> None:
    trace = _minimal_trace()
    trace["task"]["bogus"] = "x"
    assert traces.validate_trace(trace) != []


def test_extra_key_inside_final_claim_is_rejected() -> None:
    trace = _minimal_trace()
    trace["final_claim"]["bogus"] = "x"
    assert traces.validate_trace(trace) != []


def test_extra_key_inside_ground_truth_is_rejected() -> None:
    trace = _minimal_trace()
    trace["ground_truth"]["bogus"] = "x"
    assert traces.validate_trace(trace) != []


def test_extra_key_inside_a_claim_outside_subject_is_rejected() -> None:
    trace = _minimal_trace()
    trace["final_claim"]["claims"] = [{"type": "tests_passed", "subject": {}, "bogus": 1}]
    assert traces.validate_trace(trace) != []


@pytest.mark.parametrize(
    "path, value",
    [
        (("schema",), "agent-trace/v2"),
        (("task", "domain"), "shopping"),
        (("ground_truth", "outcome"), "maybe"),
        (("ground_truth", "checked_by"), "vibes"),
    ],
)
def test_bad_top_level_enum_value_is_rejected(path: tuple[str, ...], value: str) -> None:
    trace = _minimal_trace()
    node: Any = trace
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    assert traces.validate_trace(trace) != []


@pytest.mark.parametrize(
    "kind, role", [("bogus", "user"), ("message", "bogus"), ("tool_call", "bogus")]
)
def test_bad_step_enum_is_rejected(kind: str, role: str) -> None:
    trace = _minimal_trace()
    trace["steps"][0]["kind"] = kind
    trace["steps"][0]["role"] = role
    assert traces.validate_trace(trace) != []


def test_wrong_type_field_is_rejected() -> None:
    trace = _minimal_trace()
    trace["steps"][0]["i"] = "0"  # must be an integer
    assert traces.validate_trace(trace) != []


def test_negative_i_is_rejected() -> None:
    trace = _minimal_trace()
    trace["steps"][0]["i"] = -1
    assert traces.validate_trace(trace) != []


def test_bad_ts_pattern_is_rejected() -> None:
    trace = _minimal_trace()
    trace["steps"][0]["ts"] = "not-a-timestamp"
    assert traces.validate_trace(trace) != []


def test_valid_output_and_error_on_a_tool_result_step() -> None:
    trace = _minimal_trace()
    trace["steps"][0]["kind"] = "tool_result"
    trace["steps"][0]["role"] = "tool"
    trace["steps"][0]["name"] = "Bash"
    trace["steps"][0]["ok"] = False
    trace["steps"][0]["output"] = {"text": "boom", "exit_code": 1}
    trace["steps"][0]["error"] = "boom"
    assert traces.validate_trace(trace) == []


def test_deep_copy_is_untouched_by_validation() -> None:
    # validate_trace must not mutate its input.
    trace = _minimal_trace()
    before = copy.deepcopy(trace)
    traces.validate_trace(trace)
    assert trace == before


# ------------------------------------------------------------------------------------------
# validate_file
# ------------------------------------------------------------------------------------------


def _write_jsonl(path: str, objs: list[Any]) -> None:
    import json

    with open(path, "w", encoding="utf-8") as fh:
        for obj in objs:
            if isinstance(obj, str):
                fh.write(obj + "\n")
            else:
                fh.write(json.dumps(obj) + "\n")


def test_validate_file_all_valid_yields_no_errors(tmp_path: object) -> None:
    path = str(tmp_path) + "/traces.jsonl"  # type: ignore[operator]
    _write_jsonl(path, [_minimal_trace(), _minimal_trace()])
    assert traces.validate_file(path) == []


def test_validate_file_reports_line_number_and_json_pointer(tmp_path: object) -> None:
    bad = _minimal_trace()
    del bad["steps"][0]["ok"]
    path = str(tmp_path) + "/traces.jsonl"  # type: ignore[operator]
    _write_jsonl(path, [_minimal_trace(), bad])
    errors = traces.validate_file(path)
    assert len(errors) == 1
    assert errors[0].startswith(f"{path}:2:")
    assert "/steps/0" in errors[0]


def test_validate_file_reports_invalid_json_by_line(tmp_path: object) -> None:
    path = str(tmp_path) + "/traces.jsonl"  # type: ignore[operator]
    _write_jsonl(path, [_minimal_trace(), "not json {"])
    errors = traces.validate_file(path)
    assert len(errors) == 1
    assert errors[0].startswith(f"{path}:2:")
    assert "invalid JSON" in errors[0]


def test_validate_file_skips_blank_lines(tmp_path: object) -> None:
    path = str(tmp_path) + "/traces.jsonl"  # type: ignore[operator]
    _write_jsonl(path, [_minimal_trace(), "", _minimal_trace()])
    assert traces.validate_file(path) == []
