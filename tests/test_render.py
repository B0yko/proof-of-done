"""Tests for fixtures/render.py: determinism, id derivation, and the pinned output layout."""

from __future__ import annotations

import filecmp
import glob
import json
import os
import uuid

import pytest

from fixtures import dsl, render

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCENARIOS_DIR = os.path.join(_REPO_ROOT, "fixtures", "scenarios")
SCENARIO_PATHS = sorted(glob.glob(os.path.join(SCENARIOS_DIR, "*.yaml")))


def _all_files(root: str) -> list[str]:
    found = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            found.append(os.path.relpath(os.path.join(dirpath, name), root))
    return sorted(found)


@pytest.mark.parametrize("path", SCENARIO_PATHS, ids=[os.path.basename(p) for p in SCENARIO_PATHS])
def test_rendering_twice_is_byte_identical(path: str, tmp_path: object) -> None:
    scenario = dsl.load_scenario(path)
    out1 = os.path.join(str(tmp_path), "a")
    out2 = os.path.join(str(tmp_path), "b")
    render.render(scenario, out1)
    render.render(scenario, out2)

    files1 = _all_files(out1)
    files2 = _all_files(out2)
    assert files1 == files2
    for rel in files1:
        assert filecmp.cmp(os.path.join(out1, rel), os.path.join(out2, rel), shallow=False), rel


def test_session_id_matches_the_documented_uuid5_formula(tmp_path: object) -> None:
    scenario = dsl.load_scenario(os.path.join(SCENARIOS_DIR, "parallel-calls.yaml"))
    result = render.render(scenario, str(tmp_path))
    expected = uuid.uuid5(render.NAMESPACE, "parallel-calls/session/0")
    assert result.session_id == str(expected)


def test_tool_use_ids_are_uuid5_derived_toolu_prefixed(tmp_path: object) -> None:
    scenario = dsl.load_scenario(os.path.join(SCENARIOS_DIR, "parallel-calls.yaml"))
    result = render.render(scenario, str(tmp_path))
    with open(result.session_path, encoding="utf-8") as fh:
        lines = [json.loads(line) for line in fh if line.strip()]
    tool_use_ids = [
        block["id"]
        for obj in lines
        if obj.get("type") == "assistant"
        for block in obj["message"]["content"]
        if block.get("type") == "tool_use"
    ]
    assert tool_use_ids
    for i, tool_use_id in enumerate(tool_use_ids):
        assert tool_use_id.startswith("toolu_")
        expected = uuid.uuid5(render.NAMESPACE, f"parallel-calls/tool/{i}")
        assert tool_use_id == "toolu_" + expected.hex


def test_namespace_matches_the_documented_constant() -> None:
    expected = uuid.uuid5(uuid.NAMESPACE_URL, "https://github.com/B0yko/proof-of-done/fixtures")
    assert expected == render.NAMESPACE
    assert str(render.NAMESPACE) == "d986b061-b9aa-5b68-bc11-e2574d5004db"


@pytest.mark.parametrize("path", SCENARIO_PATHS, ids=[os.path.basename(p) for p in SCENARIO_PATHS])
def test_rendered_content_has_no_real_host_paths(path: str, tmp_path: object) -> None:
    scenario = dsl.load_scenario(path)
    result = render.render(scenario, str(tmp_path))
    with open(result.session_path, encoding="utf-8") as fh:
        text = fh.read()
    assert str(tmp_path) not in text
    assert "/Users/" not in text
    assert "/home/" not in text


def test_mid_session_first_line_parent_uuid_is_dangling(tmp_path: object) -> None:
    scenario = dsl.load_scenario(os.path.join(SCENARIOS_DIR, "mid-session-env-entries.yaml"))
    result = render.render(scenario, str(tmp_path))
    with open(result.session_path, encoding="utf-8") as fh:
        first = json.loads(fh.readline())
        rest_uuids = {json.loads(line)["uuid"] for line in fh if line.strip()}
    assert first["parentUuid"] is not None
    assert first["parentUuid"] not in rest_uuids
    expected_parent = uuid.uuid5(render.NAMESPACE, "mid-session-env-entries/line/-1")
    assert first["parentUuid"] == str(expected_parent)


def test_fresh_session_first_line_parent_uuid_is_null(tmp_path: object) -> None:
    scenario = dsl.load_scenario(os.path.join(SCENARIOS_DIR, "parallel-calls.yaml"))
    result = render.render(scenario, str(tmp_path))
    with open(result.session_path, encoding="utf-8") as fh:
        first = json.loads(fh.readline())
    assert first["parentUuid"] is None


def test_stop_case_prefix_is_a_true_prefix_of_the_full_file(tmp_path: object) -> None:
    scenario = dsl.load_scenario(os.path.join(SCENARIOS_DIR, "attempts-retry.yaml"))
    result = render.render(scenario, str(tmp_path))
    with open(result.session_path, encoding="utf-8") as fh:
        full_lines = fh.read().splitlines()
    prefix_lengths = []
    for stop_case in result.stop_cases:
        with open(stop_case.session_path, encoding="utf-8") as fh:
            prefix_lines = fh.read().splitlines()
        assert prefix_lines == full_lines[: len(prefix_lines)]
        prefix_lengths.append(len(prefix_lines))
    # the first attempt's prefix must not already contain the whole session (attempt 2 exists).
    assert prefix_lengths[0] < len(full_lines)
    assert prefix_lengths[-1] == len(full_lines)


def test_label_spans_index_into_the_stripped_final_message(tmp_path: object) -> None:
    for path in SCENARIO_PATHS:
        scenario = dsl.load_scenario(path)
        result = render.render(scenario, str(tmp_path))
        for stop_case in result.stop_cases:
            for span in stop_case.labels:
                quoted = stop_case.final_message[span.start : span.end]
                assert quoted  # non-empty
                assert "[[" not in quoted and "]]" not in quoted


def test_no_marker_syntax_leaks_into_rendered_messages(tmp_path: object) -> None:
    for path in SCENARIO_PATHS:
        scenario = dsl.load_scenario(path)
        result = render.render(scenario, str(tmp_path))
        with open(result.session_path, encoding="utf-8") as fh:
            text = fh.read()
        assert "[[" not in text
        assert "]]" not in text


def test_cli_renders_to_the_given_directory(
    tmp_path: object, capsys: pytest.CaptureFixture[str]
) -> None:
    out = os.path.join(str(tmp_path), "cli-out")
    scenario_path = os.path.join(SCENARIOS_DIR, "parallel-calls.yaml")
    rc = render._cli([scenario_path, "--out", out])
    assert rc == 0
    assert os.path.isdir(out)
    captured = capsys.readouterr()
    assert "wrote" in captured.out
