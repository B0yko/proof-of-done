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
