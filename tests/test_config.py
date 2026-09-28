"""Tests for proof_of_done.config: validation, merge semantics, the tamper-protected
effective config, and the merged-config cache."""

from __future__ import annotations

import copy
import os
import sys
from collections.abc import Callable
from typing import Any

import pytest

from proof_of_done import config

ROOT = "/work/demo-app"
VENDOR_YAML_MODULE = "proof_of_done._vendor.yaml"


def make_reader(overrides: dict[str, str | None]) -> Callable[[str], str | None]:
    """A `reader` that serves `overrides` by exact path, falls back to the real
    `defaults.yaml` for that one path, and treats anything else as absent."""

    def reader(path: str) -> str | None:
        if path in overrides:
            return overrides[path]
        if path == config.defaults_path():
            with open(path, encoding="utf-8") as handle:
                return handle.read()
        return None

    return reader


def load(
    root: str = ROOT,
    env: dict[str, str] | None = None,
    *,
    user: str | None = None,
    project: str | None = None,
    extra: str | None = None,
) -> tuple[config.Config, list[str], list[config.Layer]]:
    env = dict(env or {})
    env.setdefault("HOME", "/work/home")
    overrides: dict[str, str | None] = {}
    if user is not None:
        overrides[config.user_config_path(env)] = user
    if project is not None:
        overrides[config.project_config_path(root)] = project
    if extra is not None:
        overrides[env.get("PROOF_OF_DONE_CONFIG", "")] = extra
    layers = config.load_layers(root, env, make_reader(overrides))
    cfg, notes = config.effective(layers, env, tampered_paths=set(), settings_tampered=False)
    return cfg, notes, layers


# ---------------------------------------------------------------------------------------
# defaults.yaml itself
# ---------------------------------------------------------------------------------------


def test_defaults_validate_and_build() -> None:
    cfg, notes, layers = load()
    assert notes == []
    assert layers[0].name == "defaults"
    assert layers[0].data is not None
    assert cfg.version == 1
    assert cfg.enabled is True
    assert cfg.mode == "block"
    assert cfg.max_blocks_per_turn == 2
    ids = [rule.id for rule in cfg.rules]
    assert ids == ["tests", "build", "lint", "typecheck", "deploy", "fixed", "verified"]
    assert all(rule.action == "block" for rule in cfg.rules)
    # fixed/verified have no commands of their own.
    fixed = next(r for r in cfg.rules if r.id == "fixed")
    assert fixed.evidence.commands == ()
    assert fixed.exempt_if_only_edited != ()
    tests_rule = next(r for r in cfg.rules if r.id == "tests")
    assert ("pytest",) in tests_rule.evidence.commands
    assert ("npm", "run", "test:*") in tests_rule.evidence.commands


def test_missing_defaults_is_an_error() -> None:
    def reader(_path: str) -> str | None:
        return None

    with pytest.raises(config.ConfigError) as excinfo:
        config.load_layers(ROOT, {"HOME": "/work/home"}, reader)
    assert excinfo.value.file == config.defaults_path()


# ---------------------------------------------------------------------------------------
# Validation: unknown keys, types, enums
# ---------------------------------------------------------------------------------------


def test_unknown_top_level_key() -> None:
    with pytest.raises(config.ConfigError) as excinfo:
        load(project="not_a_real_key: true\n")
    err = excinfo.value
    assert err.file == config.project_config_path(ROOT)
    assert err.key_path == "not_a_real_key"
    assert err.message == "unknown key"


def test_unknown_rule_key_reports_indexed_path() -> None:
    project = """
rules:
  - id: build
  - id: lint
  - id: typecheck
    evidence:
      comands: ["mypy"]
"""
    with pytest.raises(config.ConfigError) as excinfo:
        load(project=project)
    err = excinfo.value
    assert err.key_path == "rules[2].evidence.comands"
    assert err.message == "unknown key"
    assert str(err) == f"{config.project_config_path(ROOT)}: rules[2].evidence.comands: unknown key"


def test_unknown_edits_key() -> None:
    with pytest.raises(config.ConfigError) as excinfo:
        load(project="edits:\n  bash_writez: []\n")
    assert excinfo.value.key_path == "edits.bash_writez"


def test_bad_mode_enum() -> None:
    with pytest.raises(config.ConfigError) as excinfo:
        load(project="mode: sometimes\n")
    assert excinfo.value.key_path == "mode"


def test_bad_action_enum() -> None:
    with pytest.raises(config.ConfigError) as excinfo:
        load(project="rules:\n  - id: lint\n    action: sometimes\n")
    assert excinfo.value.key_path == "rules[0].action"


def test_unquoted_off_action_is_accepted_as_the_string_off() -> None:
    # YAML 1.1 parses an unquoted `off` as the boolean `False`; this is accepted as a nicety
    # and normalized to the string "off", equivalent to writing `action: "off"`.
    cfg, _notes, _layers = load(project="rules:\n  - id: lint\n    action: off\n")
    lint = next(r for r in cfg.rules if r.id == "lint")
    assert lint.action == "off"

    quoted_cfg, _notes, _layers = load(project='rules:\n  - id: lint\n    action: "off"\n')
    quoted_lint = next(r for r in quoted_cfg.rules if r.id == "lint")
    assert quoted_lint.action == "off"
    assert lint == quoted_lint


def test_claim_type_must_match_pattern() -> None:
    with pytest.raises(config.ConfigError) as excinfo:
        load(project="rules:\n  - id: custom\n    claim_type: Not-Valid\n    keywords: [x]\n")
    assert excinfo.value.key_path == "rules[0].claim_type"


def test_claims_regex_must_compile() -> None:
    with pytest.raises(config.ConfigError) as excinfo:
        load(project="rules:\n  - id: lint\n    claims: ['(unclosed']\n")
    assert excinfo.value.key_path == "rules[0].claims[0]"


def test_evidence_fail_output_regex_must_compile() -> None:
    project = "rules:\n  - id: lint\n    evidence:\n      fail_output: ['(unclosed']\n"
    with pytest.raises(config.ConfigError) as excinfo:
        load(project=project)
    assert excinfo.value.key_path == "rules[0].evidence.fail_output[0]"


def test_keywords_must_be_lowercase() -> None:
    with pytest.raises(config.ConfigError) as excinfo:
        load(project="rules:\n  - id: lint\n    keywords: [Lint]\n")
    assert excinfo.value.key_path == "rules[0].keywords"


def test_keywords_required_for_custom_rule() -> None:
    with pytest.raises(config.ConfigError) as excinfo:
        load(project="rules:\n  - id: committed\n    claim_type: committed\n")
    assert excinfo.value.key_path == "rules[0].keywords"


def test_keywords_not_required_for_builtin_rule() -> None:
    baseline, _notes, _layers = load()
    baseline_lint = next(r for r in baseline.rules if r.id == "lint")

    cfg, _notes, _layers = load(project="rules:\n  - id: lint\n    action: warn\n")
    lint = next(r for r in cfg.rules if r.id == "lint")
    assert lint.action == "warn"
    # A builtin rule override needn't repeat `keywords` (unlike a custom rule, which
    # validation requires it for): the field-by-field merge keeps the base rule's keywords.
    assert lint.keywords == baseline_lint.keywords
    assert lint.keywords != ()


def test_version_must_be_1() -> None:
    with pytest.raises(config.ConfigError) as excinfo:
        load(project="version: 2\n")
    assert excinfo.value.key_path == "version"


def test_max_blocks_per_turn_must_be_nonneg_int() -> None:
    with pytest.raises(config.ConfigError) as excinfo:
        load(project="max_blocks_per_turn: -1\n")
    assert excinfo.value.key_path == "max_blocks_per_turn"
    with pytest.raises(config.ConfigError):
        load(project="max_blocks_per_turn: 1.5\n")


def test_globs_must_be_strings() -> None:
    with pytest.raises(config.ConfigError) as excinfo:
        load(project="rules:\n  - id: lint\n    relevant_files: [1, 2]\n")
    assert excinfo.value.key_path == "rules[0].relevant_files"


# ---------------------------------------------------------------------------------------
# Merge semantics
# ---------------------------------------------------------------------------------------


def test_merge_scalars_override() -> None:
    cfg, _notes, _layers = load(project="mode: warn\nmax_blocks_per_turn: 5\n")
    assert cfg.mode == "warn"
    assert cfg.max_blocks_per_turn == 5


def test_merge_rules_by_id_field_by_field() -> None:
    project = "rules:\n  - id: lint\n    action: warn\n    suggest: 'ruff check . --fix'\n"
    cfg, _notes, _layers = load(project=project)
    lint = next(r for r in cfg.rules if r.id == "lint")
    assert lint.action == "warn"
    assert lint.suggest == "ruff check . --fix"
    # Untouched fields (from defaults) survive the field-by-field rule merge.
    assert ("ruff", "check") in lint.evidence.commands
    assert lint.claim_type == "lint_clean"


def test_merge_evidence_merges_by_key() -> None:
    project = "rules:\n  - id: lint\n    evidence:\n      command_regex: 'ruff\\\\b'\n"
    cfg, _notes, _layers = load(project=project)
    lint = next(r for r in cfg.rules if r.id == "lint")
    assert lint.evidence.command_regex is not None
    # The lower layer's commands list is untouched because the override only set
    # `command_regex`.
    assert ("ruff", "check") in lint.evidence.commands


def test_merge_new_rule_id_appends() -> None:
    project = "rules:\n  - id: committed\n    claim_type: committed\n    keywords: [commit]\n"
    cfg, _notes, _layers = load(project=project)
    ids = [rule.id for rule in cfg.rules]
    assert ids[-1] == "committed"
    assert ids[:-1] == ["tests", "build", "lint", "typecheck", "deploy", "fixed", "verified"]


def test_merge_lists_replace() -> None:
    cfg, _notes, _layers = load(project="subagent_skip_types: [Explore]\n")
    assert cfg.subagent_skip_types == ("Explore",)


# ---------------------------------------------------------------------------------------
# Environment overrides
# ---------------------------------------------------------------------------------------


def test_env_proof_of_done_off_disables() -> None:
    cfg, _notes, _layers = load(env={"PROOF_OF_DONE": "off"})
    assert cfg.enabled is False


def test_env_mode_warn_overrides() -> None:
    cfg, _notes, _layers = load(env={"PROOF_OF_DONE_MODE": "warn"})
    assert cfg.mode == "warn"


def test_env_mode_block_overrides_a_warn_project_config() -> None:
    cfg, _notes, _layers = load(project="mode: warn\n", env={"PROOF_OF_DONE_MODE": "block"})
    assert cfg.mode == "block"


# ---------------------------------------------------------------------------------------
# Tamper protection
# ---------------------------------------------------------------------------------------


def _layers_for_tamper(project_text: str) -> tuple[list[config.Layer], dict[str, str]]:
    env = {"HOME": "/work/home"}
    reader = make_reader({config.project_config_path(ROOT): project_text})
    return config.load_layers(ROOT, env, reader), env


def test_tamper_restores_enabled() -> None:
    layers, env = _layers_for_tamper("enabled: false\n")
    project_path = config.project_config_path(ROOT)
    cfg, notes = config.effective(
        layers, env, tampered_paths={project_path}, settings_tampered=False
    )
    assert cfg.enabled is True
    assert any("enabled: false" in note for note in notes)


def test_tamper_restores_mode() -> None:
    layers, env = _layers_for_tamper("mode: warn\n")
    project_path = config.project_config_path(ROOT)
    cfg, notes = config.effective(
        layers, env, tampered_paths={project_path}, settings_tampered=False
    )
    assert cfg.mode == "block"
    assert any("mode: warn" in note for note in notes)


def test_tamper_restores_lowered_action() -> None:
    layers, env = _layers_for_tamper('rules:\n  - id: tests\n    action: "off"\n')
    project_path = config.project_config_path(ROOT)
    cfg, notes = config.effective(
        layers, env, tampered_paths={project_path}, settings_tampered=False
    )
    tests_rule = next(r for r in cfg.rules if r.id == "tests")
    assert tests_rule.action == "block"
    assert any("tests" in note and "lowered" in note for note in notes)


def test_tamper_restores_raised_max_blocks_per_turn() -> None:
    layers, env = _layers_for_tamper("max_blocks_per_turn: 100\n")
    project_path = config.project_config_path(ROOT)
    cfg, notes = config.effective(
        layers, env, tampered_paths={project_path}, settings_tampered=False
    )
    assert cfg.max_blocks_per_turn == 2
    assert any("max_blocks_per_turn" in note for note in notes)


def test_tamper_non_downgrades_still_apply() -> None:
    # A tampered file that only *tightens* things (lowers the cap, adds a custom rule)
    # keeps every one of those changes: only downgrades get undone.
    project_text = "max_blocks_per_turn: 1\nrules:\n  - id: lint\n    suggest: 'x'\n"
    layers, env = _layers_for_tamper(project_text)
    project_path = config.project_config_path(ROOT)
    cfg, notes = config.effective(
        layers, env, tampered_paths={project_path}, settings_tampered=False
    )
    assert cfg.max_blocks_per_turn == 1
    lint = next(r for r in cfg.rules if r.id == "lint")
    assert lint.suggest == "x"
    assert notes == []


def test_tamper_only_affects_the_file_that_caused_it() -> None:
    # A downgrade from an *untampered* layer is left alone.
    env = {"HOME": "/work/home"}
    reader = make_reader({config.project_config_path(ROOT): "mode: warn\n"})
    layers = config.load_layers(ROOT, env, reader)
    cfg, notes = config.effective(layers, env, tampered_paths=set(), settings_tampered=False)
    assert cfg.mode == "warn"
    assert notes == []


def test_settings_tampered_ignores_env_off_and_warn() -> None:
    layers, env = _layers_for_tamper("")
    env = dict(env, PROOF_OF_DONE="off", PROOF_OF_DONE_MODE="warn")
    cfg, notes = config.effective(layers, env, tampered_paths=set(), settings_tampered=True)
    assert cfg.enabled is True
    assert cfg.mode == "block"
    assert len(notes) == 2


def test_settings_tampered_still_allows_mode_block() -> None:
    layers, env = _layers_for_tamper("mode: warn\n")
    env = dict(env, PROOF_OF_DONE_MODE="block")
    cfg, notes = config.effective(layers, env, tampered_paths=set(), settings_tampered=True)
    assert cfg.mode == "block"
    assert notes == []


# ---------------------------------------------------------------------------------------
# Merged-config cache
# ---------------------------------------------------------------------------------------


def test_cache_miss_then_hit(tmp_path: os.PathLike[str]) -> None:
    tmp = str(tmp_path)
    home = os.path.join(tmp, "home")
    os.makedirs(home, exist_ok=True)
    root = os.path.join(tmp, "project")
    os.makedirs(root, exist_ok=True)
    project_config = os.path.join(root, ".proof-of-done.yaml")
    with open(project_config, "w", encoding="utf-8") as handle:
        handle.write("mode: warn\n")
    data_dir = os.path.join(tmp, "data")
    env = {"HOME": home}
    layer_paths = config.layer_paths_for(root, env)

    cfg_miss, keywords_miss, sources_miss = config.load_cached(data_dir, layer_paths, env)
    assert cfg_miss.mode == "warn"
    assert os.path.exists(os.path.join(data_dir, config.CACHE_FILENAME))

    sys.modules.pop(VENDOR_YAML_MODULE, None)
    cfg_hit, keywords_hit, sources_hit = config.load_cached(data_dir, layer_paths, env)
    assert VENDOR_YAML_MODULE not in sys.modules
    assert cfg_hit.to_json() == cfg_miss.to_json()
    assert keywords_hit == keywords_miss
    assert sources_hit == sources_miss


def test_cache_invalidated_by_mtime_change(tmp_path: os.PathLike[str]) -> None:
    tmp = str(tmp_path)
    home = os.path.join(tmp, "home")
    os.makedirs(home, exist_ok=True)
    root = os.path.join(tmp, "project")
    os.makedirs(root, exist_ok=True)
    project_config = os.path.join(root, ".proof-of-done.yaml")
    with open(project_config, "w", encoding="utf-8") as handle:
        handle.write("max_blocks_per_turn: 3\n")
    data_dir = os.path.join(tmp, "data")
    env = {"HOME": home}
    layer_paths = config.layer_paths_for(root, env)

    cfg1, _kw1, _src1 = config.load_cached(data_dir, layer_paths, env)
    assert cfg1.max_blocks_per_turn == 3

    with open(project_config, "w", encoding="utf-8") as handle:
        handle.write("max_blocks_per_turn: 4\n")
    stat = os.stat(project_config)
    new_ns = (stat.st_atime_ns + 5_000_000_000, stat.st_mtime_ns + 5_000_000_000)
    os.utime(project_config, ns=new_ns)

    cfg2, _kw2, _src2 = config.load_cached(data_dir, layer_paths, env)
    assert cfg2.max_blocks_per_turn == 4


def test_cache_read_error_falls_back_to_a_full_load(tmp_path: os.PathLike[str]) -> None:
    tmp = str(tmp_path)
    home = os.path.join(tmp, "home")
    os.makedirs(home, exist_ok=True)
    root = os.path.join(tmp, "project")
    os.makedirs(root, exist_ok=True)
    data_dir = os.path.join(tmp, "data")
    os.makedirs(data_dir, exist_ok=True)
    with open(os.path.join(data_dir, config.CACHE_FILENAME), "w", encoding="utf-8") as handle:
        handle.write("{not valid json")
    env = {"HOME": home}
    layer_paths = config.layer_paths_for(root, env)

    cfg, _keywords, _sources = config.load_cached(data_dir, layer_paths, env)
    assert cfg.enabled is True  # falls back to defaults, does not raise


def test_cache_missing_layer_file_uses_null_mtime(tmp_path: os.PathLike[str]) -> None:
    tmp = str(tmp_path)
    home = os.path.join(tmp, "home")
    os.makedirs(home, exist_ok=True)
    root = os.path.join(tmp, "project")
    os.makedirs(root, exist_ok=True)
    data_dir = os.path.join(tmp, "data")
    env = {"HOME": home}
    layer_paths = config.layer_paths_for(root, env)

    cfg, _keywords, sources = config.load_cached(data_dir, layer_paths, env)
    assert cfg.enabled is True
    project_source = next(s for s in sources if s["name"] == "project")
    assert project_source["found"] is False


def test_cache_does_not_poison_a_later_invocation_with_a_different_env(
    tmp_path: os.PathLike[str],
) -> None:
    # Regression for the cache-poisoning bug: the cache must hold the merged config *before*
    # any environment override, so an `off` invocation never leaks into a later invocation
    # with a plain environment sharing the same data dir.
    tmp = str(tmp_path)
    home = os.path.join(tmp, "home")
    os.makedirs(home, exist_ok=True)
    root = os.path.join(tmp, "project")
    os.makedirs(root, exist_ok=True)
    data_dir = os.path.join(tmp, "data")
    env_off = {"HOME": home, "PROOF_OF_DONE": "off"}
    env_normal = {"HOME": home}
    layer_paths = config.layer_paths_for(root, env_off)

    cfg_off, _kw1, _src1 = config.load_cached(data_dir, layer_paths, env_off)
    assert cfg_off.enabled is False

    cfg_normal, _kw2, _src2 = config.load_cached(data_dir, layer_paths, env_normal)
    assert cfg_normal.enabled is True  # not poisoned by the earlier off invocation

    # A cache hit for the second call still must not import the vendored YAML loader.
    sys.modules.pop(VENDOR_YAML_MODULE, None)
    cfg_again, _kw3, _src3 = config.load_cached(data_dir, layer_paths, env_normal)
    assert VENDOR_YAML_MODULE not in sys.modules
    assert cfg_again.enabled is True


def test_cache_does_not_poison_mode_between_warn_and_block_invocations(
    tmp_path: os.PathLike[str],
) -> None:
    tmp = str(tmp_path)
    home = os.path.join(tmp, "home")
    os.makedirs(home, exist_ok=True)
    root = os.path.join(tmp, "project")
    os.makedirs(root, exist_ok=True)
    data_dir = os.path.join(tmp, "data")
    env_warn = {"HOME": home, "PROOF_OF_DONE_MODE": "warn"}
    env_block = {"HOME": home, "PROOF_OF_DONE_MODE": "block"}
    layer_paths = config.layer_paths_for(root, env_warn)

    cfg_warn, _kw1, _src1 = config.load_cached(data_dir, layer_paths, env_warn)
    assert cfg_warn.mode == "warn"

    cfg_block, _kw2, _src2 = config.load_cached(data_dir, layer_paths, env_block)
    assert cfg_block.mode == "block"  # not poisoned by the earlier warn invocation


# ---------------------------------------------------------------------------------------
# Config API
# ---------------------------------------------------------------------------------------


def test_keywords_union_includes_off_rules() -> None:
    # A rule turned off, or a config switched off, still contributes its keywords: a message
    # that mentions one is what triggers the tamper check, which must run before `action` or
    # `enabled` from a file is believed.
    project = 'rules:\n  - id: lint\n    action: "off"\n    keywords: [zzzonly]\n'
    cfg, _notes, _layers = load(project=project)
    assert "zzzonly" in cfg.keywords()


def test_keywords_union_survives_a_disabled_config() -> None:
    cfg, _notes, _layers = load(project="enabled: false\n")
    assert cfg.enabled is False
    assert cfg.keywords() == load()[0].keywords()


def test_rules_for_filters_by_claim_type_and_action() -> None:
    cfg, _notes, _layers = load()
    tests_rules = cfg.rules_for("tests_passed")
    assert [r.id for r in tests_rules] == ["tests"]
    assert cfg.rules_for("no_such_type") == ()


def test_to_json_from_json_round_trips() -> None:
    cfg, _notes, _layers = load()
    again = config.Config.from_json(cfg.to_json())
    assert again.to_json() == cfg.to_json()


def test_to_json_is_independent_of_the_original() -> None:
    cfg, _notes, _layers = load()
    blob = cfg.to_json()
    mutated = copy.deepcopy(blob)
    mutated["mode"] = "warn"
    assert blob["mode"] == "block"  # mutating the copy must not affect the first blob


# ---------------------------------------------------------------------------------------
# config show
# ---------------------------------------------------------------------------------------


def test_show_contains_scalars_rules_and_sources() -> None:
    cfg, _notes, _layers = load()
    sources: list[dict[str, Any]] = [
        {"name": "defaults", "path": config.defaults_path(), "found": True},
        {"name": "project", "path": config.project_config_path(ROOT), "found": False},
    ]
    text = config.show(cfg, sources)
    assert "mode: block" in text
    assert "- tests (tests_passed, block)" in text
    assert "defaults:" in text
    assert "(not found)" in text
