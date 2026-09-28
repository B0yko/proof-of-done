"""Tests for `proof_of_done.counters`: atomic per-session consecutive-block counters."""

from __future__ import annotations

import os
import time

from proof_of_done import counters


def test_read_defaults_to_zero_when_nothing_written(tmp_path) -> None:
    assert counters.read(str(tmp_path), "s1", None) == 0


def test_write_then_read_round_trips(tmp_path) -> None:
    counters.write(str(tmp_path), "s1", "agent-1", 2)
    assert counters.read(str(tmp_path), "s1", "agent-1") == 2


def test_different_session_or_agent_ids_are_independent(tmp_path) -> None:
    counters.write(str(tmp_path), "s1", None, 3)
    counters.write(str(tmp_path), "s2", None, 1)
    counters.write(str(tmp_path), "s1", "agent-a", 5)
    assert counters.read(str(tmp_path), "s1", None) == 3
    assert counters.read(str(tmp_path), "s2", None) == 1
    assert counters.read(str(tmp_path), "s1", "agent-a") == 5


def test_write_overwrites_previous_value(tmp_path) -> None:
    counters.write(str(tmp_path), "s1", None, 1)
    counters.write(str(tmp_path), "s1", None, 2)
    assert counters.read(str(tmp_path), "s1", None) == 2


def test_write_never_raises_when_the_directory_cannot_be_created(tmp_path) -> None:
    blocked = tmp_path / "not-a-dir"
    blocked.write_text("x", encoding="utf-8")
    counters.write(str(blocked), "s1", None, 1)  # data_dir itself is a file, not a directory


def test_read_ignores_a_corrupt_counter_file(tmp_path) -> None:
    directory = tmp_path / "counters"
    directory.mkdir()
    key = counters._key("s1", None)
    (directory / f"{key}.json").write_text("not json", encoding="utf-8")
    assert counters.read(str(tmp_path), "s1", None) == 0


def test_prune_removes_files_older_than_seven_days(tmp_path) -> None:
    counters.write(str(tmp_path), "old", None, 1)
    counters.write(str(tmp_path), "fresh", None, 1)
    old_path = os.path.join(str(tmp_path), "counters", counters._key("old", None) + ".json")
    eight_days_ago = time.time() - 8 * 24 * 60 * 60
    os.utime(old_path, (eight_days_ago, eight_days_ago))

    counters.prune(str(tmp_path))

    assert counters.read(str(tmp_path), "old", None) == 0
    assert counters.read(str(tmp_path), "fresh", None) == 1


def test_prune_never_raises_when_the_counters_directory_is_missing(tmp_path) -> None:
    counters.prune(str(tmp_path))
