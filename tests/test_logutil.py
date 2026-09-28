"""Tests for `proof_of_done.logutil`: the size-capped, JSON-lines diagnostic log."""

from __future__ import annotations

import json
import os

from proof_of_done import logutil


def test_write_appends_one_json_line(tmp_path) -> None:
    logutil.write(str(tmp_path), "decision", decision="block", claims=1)
    with open(logutil.log_path(str(tmp_path)), encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["event"] == "decision"
    assert record["decision"] == "block"
    assert record["claims"] == 1
    assert "ts" in record


def test_write_never_logs_arbitrary_positional_message_text(tmp_path) -> None:
    # The API only accepts keyword fields (rule ids, reasons, timings, error class names);
    # there is no way to pass free-form message text or a path through it by accident.
    logutil.write(str(tmp_path), "decision", rule_id="tests", reason="stale")
    with open(logutil.log_path(str(tmp_path)), encoding="utf-8") as fh:
        record = json.loads(fh.readline())
    assert set(record) == {"ts", "event", "rule_id", "reason"}


def test_write_decision_records_rule_ids_reason_codes_and_timings(tmp_path) -> None:
    logutil.write_decision(
        str(tmp_path),
        hook_event="stop",
        decision="block",
        results=[
            logutil.ClaimLog(
                claim_type="tests_passed",
                rule_ids=("tests",),
                supported=False,
                reason="stale",
                action="block",
            ),
            logutil.ClaimLog(
                claim_type="lint_clean",
                rule_ids=("lint", "my-lint"),
                supported=True,
                reason="ok",
                action="block",
            ),
        ],
        skipped=False,
        disabled=False,
        tampered=True,
        timings_ms={"fast_path": 0.4, "parse": 12.1, "detect_judge": 3.2, "total": 18.0},
    )
    with open(logutil.log_path(str(tmp_path)), encoding="utf-8") as fh:
        record = json.loads(fh.readline())
    assert record["event"] == "decision"
    assert record["hook_event"] == "stop"
    assert record["decision"] == "block"
    assert record["claims"] == 2
    assert record["unsupported"] == 1
    assert record["tampered"] is True
    assert record["skipped"] is False
    assert record["disabled"] is False
    assert record["results"] == [
        {
            "claim_type": "tests_passed",
            "rule_ids": ["tests"],
            "supported": False,
            "reason": "stale",
            "action": "block",
        },
        {
            "claim_type": "lint_clean",
            "rule_ids": ["lint", "my-lint"],
            "supported": True,
            "reason": "ok",
            "action": "block",
        },
    ]
    assert record["timings_ms"] == {
        "fast_path": 0.4,
        "parse": 12.1,
        "detect_judge": 3.2,
        "total": 18.0,
    }


def test_a_claim_log_entry_has_no_place_for_message_text() -> None:
    assert logutil.ClaimLog._fields == ("claim_type", "rule_ids", "supported", "reason", "action")


def test_write_decision_caps_the_number_of_logged_claims(tmp_path) -> None:
    many = [logutil.ClaimLog("fixed", ("fixed",), True, "ok", "block") for _ in range(50)]
    logutil.write_decision(
        str(tmp_path),
        hook_event="stop",
        decision="allow",
        results=many,
        skipped=False,
        disabled=False,
        tampered=False,
        timings_ms={"total": 1.0},
    )
    with open(logutil.log_path(str(tmp_path)), encoding="utf-8") as fh:
        record = json.loads(fh.readline())
    assert record["claims"] == 50
    assert len(record["results"]) == 20


def test_write_never_raises_when_the_directory_cannot_be_created(tmp_path) -> None:
    blocked = tmp_path / "not-a-dir"
    blocked.write_text("x", encoding="utf-8")
    logutil.write(str(blocked), "decision")  # data_dir itself is a file, not a directory


def test_write_rotates_once_the_log_exceeds_the_size_cap(tmp_path) -> None:
    path = logutil.log_path(str(tmp_path))
    os.makedirs(str(tmp_path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("x" * (256 * 1024))

    logutil.write(str(tmp_path), "decision")

    assert os.path.exists(path + ".1")
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["event"] == "decision"
