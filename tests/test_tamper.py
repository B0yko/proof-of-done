"""Tests for `proof_of_done.tamper`: which session edits count as tampering, and how they
feed `config.effective`'s `tampered_paths`/`settings_tampered` arguments."""

from __future__ import annotations

from proof_of_done import tamper
from proof_of_done.evidence import EditEvent

PROJECT_CONFIG = "/work/demo-app/.proof-of-done.yaml"
USER_CONFIG = "/home/u/.config/proof-of-done/config.yaml"
ENV_CONFIG = "/work/demo-app/ci-proof-of-done.yaml"
PROJECT_SETTINGS = "/work/demo-app/.claude/settings.json"
USER_SETTINGS = "/home/u/.claude/settings.json"


def _edit(path: str, text: str | None, step: int = 0) -> EditEvent:
    return EditEvent(
        step=step,
        pos=(step, 0, 1),
        abs_path=path,
        rel_path=None,
        is_dir=False,
        whole_tree=False,
        source="tool",
        tamper_text=text,
        tamper_abs_path=path,
    )


def _scan(edits, *, env_config_path=ENV_CONFIG):
    return tamper.scan(
        edits,
        project_config_path=PROJECT_CONFIG,
        user_config_path=USER_CONFIG,
        env_config_path=env_config_path,
        settings_paths=[PROJECT_SETTINGS, USER_SETTINGS],
    )


def test_project_config_edit_is_always_tampering_whatever_it_writes() -> None:
    edits = [_edit(PROJECT_CONFIG, "enabled: true\n")]
    found = _scan(edits)
    assert len(found) == 1
    assert found[0].kind == "config"
    assert tamper.tampered_paths(found) == {PROJECT_CONFIG}
    assert tamper.settings_tampered(found) is False


def test_user_config_and_env_config_edits_both_count() -> None:
    edits = [_edit(USER_CONFIG, "mode: warn\n"), _edit(ENV_CONFIG, "mode: warn\n")]
    found = _scan(edits)
    assert tamper.tampered_paths(found) == {USER_CONFIG, ENV_CONFIG}


def test_settings_edit_without_a_monitored_marker_is_not_tampering() -> None:
    edits = [_edit(PROJECT_SETTINGS, '{"theme": "dark"}')]
    found = _scan(edits)
    assert found == []
    assert tamper.settings_tampered(found) is False


def test_settings_edit_touching_proof_of_done_is_tampering() -> None:
    edits = [_edit(PROJECT_SETTINGS, '{"env": {"PROOF_OF_DONE": "off"}}')]
    found = _scan(edits)
    assert len(found) == 1
    assert found[0].kind == "settings"
    assert tamper.settings_tampered(found) is True
    assert tamper.tampered_paths(found) == set()


def test_settings_edit_touching_enabled_plugins_is_tampering() -> None:
    edits = [_edit(USER_SETTINGS, '{"enabledPlugins": {"proof-of-done@proof-of-done": false}}')]
    found = _scan(edits)
    assert tamper.settings_tampered(found) is True


def test_unrelated_file_edit_is_not_tampering() -> None:
    edits = [_edit("/work/demo-app/src/app/models.py", "def f(): pass")]
    assert _scan(edits) == []


def test_whole_tree_edit_has_no_tamper_path_and_is_ignored() -> None:
    whole_tree = EditEvent(
        step=0,
        pos=(0, 0, 1),
        abs_path=None,
        rel_path=None,
        is_dir=True,
        whole_tree=True,
        source="tree",
        tamper_text="git checkout .",
        tamper_abs_path=None,
    )
    assert _scan([whole_tree]) == []


def test_no_env_config_path_still_scans_project_and_user_and_settings() -> None:
    edits = [_edit(PROJECT_CONFIG, "enabled: false\n")]
    found = _scan(edits, env_config_path=None)
    assert len(found) == 1


def test_notes_name_the_file_and_the_one_based_step() -> None:
    edits = [_edit(PROJECT_CONFIG, "enabled: false\n", step=6)]
    found = _scan(edits)
    notes = tamper.notes(found)
    assert notes == [f"session edited {PROJECT_CONFIG} at step 7"]


def test_notes_deduplicate_repeated_edits_of_the_same_file() -> None:
    edits = [
        _edit(PROJECT_CONFIG, "enabled: false\n", step=2),
        _edit(PROJECT_CONFIG, "mode: warn\n", step=9),
    ]
    found = _scan(edits)
    notes = tamper.notes(found)
    assert notes == [f"session edited {PROJECT_CONFIG} at step 3"]


def test_scan_preserves_edit_order() -> None:
    edits = [
        _edit(PROJECT_CONFIG, "enabled: false\n", step=1),
        _edit(PROJECT_SETTINGS, '{"env":{"PROOF_OF_DONE":"off"}}', step=4),
    ]
    found = _scan(edits)
    assert [e.step for e in found] == [1, 4]
