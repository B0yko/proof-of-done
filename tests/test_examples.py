"""Every file under examples/ is a valid overlay on top of defaults.yaml."""

from __future__ import annotations

import glob
import os

import pytest

from proof_of_done import config

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_EXAMPLES_DIR = os.path.join(_REPO_ROOT, "examples")
_ROOT = "/work/demo-app"


def _example_paths() -> list[str]:
    return sorted(glob.glob(os.path.join(_EXAMPLES_DIR, "*.yaml")))


def _load_example(path: str) -> config.Config:
    env = {"HOME": "/work/home"}
    with open(path, encoding="utf-8") as handle:
        text = handle.read()

    def reader(candidate: str) -> str | None:
        if candidate == config.project_config_path(_ROOT):
            return text
        if candidate == config.defaults_path():
            with open(candidate, encoding="utf-8") as handle2:
                return handle2.read()
        return None

    layers = config.load_layers(_ROOT, env, reader)
    cfg, _notes = config.effective(layers, env, tampered_paths=set(), settings_tampered=False)
    return cfg


@pytest.mark.parametrize("path", _example_paths())
def test_example_is_valid_config(path: str) -> None:
    cfg = _load_example(path)
    assert cfg.rules  # every example keeps at least the built-in rules


def test_python_example_overrides_relevant_files() -> None:
    cfg = _load_example(os.path.join(_EXAMPLES_DIR, "python.yaml"))
    tests_rule = next(r for r in cfg.rules if r.id == "tests")
    assert "pyproject.toml" in tests_rule.relevant_files
    assert tests_rule.suggest == "uv run pytest -q"
    # Untouched fields still come from defaults.yaml.
    assert ("pytest",) in tests_rule.evidence.commands


def test_monorepo_example_splits_tests_and_disables_the_builtin_rule() -> None:
    cfg = _load_example(os.path.join(_EXAMPLES_DIR, "monorepo.yaml"))
    tests_rule = next(r for r in cfg.rules if r.id == "tests")
    assert tests_rule.action == "off"
    ids = {r.id for r in cfg.rules}
    assert {"tests-python", "tests-node"} <= ids
    python_tests = next(r for r in cfg.rules if r.id == "tests-python")
    node_tests = next(r for r in cfg.rules if r.id == "tests-node")
    assert python_tests.claim_type == "tests_passed"
    assert node_tests.claim_type == "tests_passed"
    assert python_tests.keywords and node_tests.keywords
    assert cfg.rules_for("tests_passed") == (python_tests, node_tests)


def test_extra_rules_example_adds_custom_rules_with_keywords() -> None:
    cfg = _load_example(os.path.join(_EXAMPLES_DIR, "extra-rules.yaml"))
    ids = {r.id for r in cfg.rules}
    assert {"committed", "pushed"} <= ids
    committed = next(r for r in cfg.rules if r.id == "committed")
    pushed = next(r for r in cfg.rules if r.id == "pushed")
    assert committed.keywords == ("commit",)
    assert pushed.keywords == ("push",)
    assert ("git", "commit") in committed.evidence.commands
    assert ("git", "push") in pushed.evidence.commands
    assert committed.claims and committed.claims[0].search("I committed the fix")
    assert pushed.claims and pushed.claims[0].search("pushed to origin")
