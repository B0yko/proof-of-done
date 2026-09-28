"""`engine.decide_stop`: the decision the live hook and the evaluation harness share.

These tests use an in-memory file reader instead of a disk, and hand-built sessions. The point
under test is the order of operations: the tamper scan and the effective config come first, and
only then are `enabled`, `mode` and rule actions applied.
"""

from __future__ import annotations

from evidence_helpers import ROOT, SessionBuilder

from proof_of_done import config as config_mod
from proof_of_done import engine
from proof_of_done.probe import DictProbe

PROJECT_CONFIG = ROOT + "/.proof-of-done.yaml"
PROJECT_SETTINGS = ROOT + "/.claude/settings.json"
CLAIM = "All 42 tests pass."


class _Files:
    """A reader over an in-memory tree that counts the calls made after construction."""

    def __init__(self, files: dict[str, str]) -> None:
        self.files = files
        self.calls: list[str] = []

    def __call__(self, path: str) -> str | None:
        self.calls.append(path)
        if path == config_mod.defaults_path():
            return config_mod.disk_reader(path)
        return self.files.get(path)


def _pre_tamper_config(reader: _Files, env: dict[str, str]) -> config_mod.Config:
    layers = config_mod.load_layers(ROOT, env, reader)
    cfg, _notes = config_mod.effective(layers, env, tampered_paths=set(), settings_tampered=False)
    reader.calls.clear()
    return cfg


def _decide(
    session,
    reader: _Files,
    env: dict[str, str] | None = None,
    **kwargs,
) -> engine.Decision:
    env = {"HOME": "/home/u", **(env or {})}
    return engine.decide_stop(
        session=session,
        final_message=CLAIM,
        config=_pre_tamper_config(reader, env),
        project_root=ROOT,
        probe=DictProbe(files={}),
        env=env,
        read_file=reader,
        **kwargs,
    )


def _plain_session():
    return SessionBuilder().user("Fix it").edit("Edit", ROOT + "/src/app.py").final(CLAIM).build()


def _tampered_session(path: str, content: str):
    return (
        SessionBuilder()
        .user("Fix it")
        .edit("Edit", ROOT + "/src/app.py")
        .edit("Write", path, content=content)
        .final(CLAIM)
        .build()
    )


def test_unsupported_claim_blocks_when_nothing_is_disabled() -> None:
    decision = _decide(_plain_session(), _Files({}))
    assert decision.action == "block"
    assert decision.disabled is False
    assert decision.tampered is False


def test_config_disabled_before_the_session_is_honoured_silently() -> None:
    reader = _Files({PROJECT_CONFIG: "enabled: false\n"})
    decision = _decide(_plain_session(), reader)
    assert decision.disabled is True
    assert decision.action == "allow"
    assert decision.results == []
    assert decision.system_message is None
    assert reader.calls == []  # no tamper: the layers are not read a second time


def test_environment_off_without_a_settings_edit_is_honoured() -> None:
    decision = _decide(_plain_session(), _Files({}), env={"PROOF_OF_DONE": "off"})
    assert decision.disabled is True
    assert decision.action == "allow"


def test_enabled_false_written_by_the_session_is_ignored() -> None:
    content = "enabled: false\n"
    session = _tampered_session(PROJECT_CONFIG, content)
    decision = _decide(session, _Files({PROJECT_CONFIG: content}))
    assert decision.action == "block"
    assert decision.tampered is True
    assert decision.disabled is False
    assert decision.config is not None and decision.config.enabled is True
    assert decision.reason is not None and "enabled: false" in decision.reason


def test_rule_action_off_written_by_the_session_is_ignored() -> None:
    content = 'rules:\n  - id: tests\n    action: "off"\n'
    session = _tampered_session(PROJECT_CONFIG, content)
    decision = _decide(session, _Files({PROJECT_CONFIG: content}))
    assert decision.action == "block"
    assert decision.tampered is True


def test_mode_warn_written_by_the_session_is_ignored() -> None:
    content = "mode: warn\n"
    session = _tampered_session(PROJECT_CONFIG, content)
    decision = _decide(session, _Files({PROJECT_CONFIG: content}))
    assert decision.action == "block"


def test_mode_warn_from_an_untouched_file_warns_instead_of_blocking() -> None:
    decision = _decide(_plain_session(), _Files({PROJECT_CONFIG: "mode: warn\n"}))
    assert decision.action == "allow"
    assert decision.system_message is not None and CLAIM[:-1] in decision.system_message


def test_settings_edit_makes_the_environment_off_switch_inert() -> None:
    session = _tampered_session(PROJECT_SETTINGS, '{"env": {"PROOF_OF_DONE": "off"}}')
    decision = _decide(session, _Files({}), env={"PROOF_OF_DONE": "off"})
    assert decision.action == "block"
    assert decision.tampered is True
    assert decision.reason is not None and "ignored PROOF_OF_DONE=off" in decision.reason


def test_settings_edit_does_not_undo_a_disabled_config_it_did_not_write() -> None:
    session = _tampered_session(PROJECT_SETTINGS, '{"env": {"PROOF_OF_DONE": "off"}}')
    decision = _decide(session, _Files({PROJECT_CONFIG: "enabled: false\n"}))
    assert decision.disabled is True
    assert decision.tampered is True


def test_tampering_alone_never_blocks() -> None:
    session = (
        SessionBuilder()
        .user("Fix it")
        .edit("Edit", ROOT + "/src/app.py")
        .edit("Write", PROJECT_CONFIG, content="enabled: false\n")
        .bash("uv run pytest -q", output="42 passed in 0.5s")
        .final(CLAIM)
        .build()
    )
    decision = _decide(session, _Files({PROJECT_CONFIG: "enabled: false\n"}))
    assert decision.action == "allow"
    assert decision.tampered is True
    assert decision.system_message is not None


def test_a_subagent_stop_reads_tamper_edits_from_the_parent_session() -> None:
    content = "enabled: false\n"
    parent = _tampered_session(PROJECT_CONFIG, content)
    subagent = SessionBuilder().user("Do it").final(CLAIM).build()
    decision = _decide(
        subagent,
        _Files({PROJECT_CONFIG: content}),
        main_session=parent,
        is_subagent=True,
        agent_type="general-purpose",
    )
    assert decision.action == "block"
    assert decision.tampered is True


def test_exempt_subagent_types_are_not_checked() -> None:
    decision = _decide(_plain_session(), _Files({}), is_subagent=True, agent_type="Explore")
    assert decision.disabled is True
    assert decision.results == []


def test_subagents_are_not_checked_when_check_subagents_is_off() -> None:
    decision = _decide(
        _plain_session(),
        _Files({PROJECT_CONFIG: "check_subagents: false\n"}),
        is_subagent=True,
        agent_type="general-purpose",
    )
    assert decision.disabled is True


def test_skip_token_in_the_latest_user_prompt_still_applies() -> None:
    session = (
        SessionBuilder()
        .user("Ship it #skip-proof")
        .edit("Edit", ROOT + "/src/app.py")
        .final(CLAIM)
        .build()
    )
    decision = _decide(session, _Files({}))
    assert decision.skipped is True
    assert decision.action == "allow"
