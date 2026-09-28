"""Configuration loading, validation, merging and caching.

Layer order (lowest to highest precedence): the packaged ``defaults.yaml`` < the user config
(``${XDG_CONFIG_HOME:-~/.config}/proof-of-done/config.yaml``) < the project config
(``<root>/.proof-of-done.yaml``) < the file named by ``PROOF_OF_DONE_CONFIG`` < environment
variables (``PROOF_OF_DONE``, ``PROOF_OF_DONE_MODE``). Rules merge into the lower layer's
rules by ``id``, field by field (``evidence`` merges by key); every other list replaces the
lower layer's list wholesale.

Every entry point that needs a file's contents takes it through an injectable reader
(``reader(path) -> str | None``), so tests and the evaluation harness can supply in-memory
files instead of touching the real filesystem. ``load_cached`` is the exception: it always
reads real files, because its cache key is those files' mtimes and sizes.
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
import re
import shlex
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from proof_of_done import yamlload

# ---------------------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------------------


class ConfigError(Exception):
    """A configuration parse or validation error, naming the file and the offending key.

    ``key_path`` uses dotted/bracketed notation matching the YAML structure, for example
    ``rules[2].evidence.comands`` for an unknown key nested inside the third rule of a
    layer's ``rules`` list. It is the empty string for file-level errors (invalid YAML, a
    non-mapping top level).
    """

    def __init__(self, file: str, key_path: str, message: str) -> None:
        self.file = file
        self.key_path = key_path
        self.message = message
        super().__init__(str(self))

    def __str__(self) -> str:
        if self.key_path:
            return f"{self.file}: {self.key_path}: {self.message}"
        return f"{self.file}: {self.message}"


# ---------------------------------------------------------------------------------------
# Known schema (for hand-written validation)
# ---------------------------------------------------------------------------------------

BUILTIN_RULE_IDS = frozenset({"tests", "build", "lint", "typecheck", "deploy", "fixed", "verified"})
MODE_VALUES = frozenset({"block", "warn"})
ACTION_VALUES = frozenset({"block", "warn", "off"})
CLAIM_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]*$")

_TOP_KEYS = frozenset(
    {
        "version",
        "enabled",
        "mode",
        "max_blocks_per_turn",
        "check_subagents",
        "subagent_skip_types",
        "subagent_calls_are_edits",
        "skip_token",
        "max_transcript_mb",
        "edits",
        "execution_commands",
        "read_only_commands",
        "rules",
    }
)
_EDITS_KEYS = frozenset(
    {"tools", "subagent_tools", "bash_writes", "formatters", "tree_commands", "agent_trace_tools"}
)
_RULE_KEYS = frozenset(
    {
        "id",
        "claim_type",
        "action",
        "claims",
        "keywords",
        "evidence",
        "relevant_files",
        "ignore_files",
        "exempt_if_only_edited",
        "suggest",
    }
)
_EVIDENCE_KEYS = frozenset(
    {
        "commands",
        "command_regex",
        "exclude_args",
        "partial_args",
        "fail_output",
        "success_output",
        "empty_output",
    }
)

_ACTION_RANK = {"off": 0, "warn": 1, "block": 2}
_DEFAULT_MAX_BLOCKS_PER_TURN = 2
_DEFAULT_MODE = "block"
_LAYER_NAMES = ("defaults", "user", "project", "env_file")
CACHE_FILENAME = "config-cache.json"


def _action_rank(action: str) -> int:
    return _ACTION_RANK.get(action, _ACTION_RANK["block"])


# ---------------------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------------------


def _check_unknown(mapping: Any, known: frozenset[str], file: str, prefix: str) -> None:
    if not isinstance(mapping, dict):
        raise ConfigError(file, prefix, "must be a mapping")
    for key in mapping:
        if key not in known:
            path = f"{prefix}.{key}" if prefix else str(key)
            raise ConfigError(file, path, "unknown key")


def _check_str_list(value: Any, file: str, path: str) -> None:
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise ConfigError(file, path, "must be a list of strings")


def _check_regex_list(value: Any, file: str, path: str) -> None:
    if not isinstance(value, list):
        raise ConfigError(file, path, "must be a list of regex strings")
    for i, pattern in enumerate(value):
        if not isinstance(pattern, str):
            raise ConfigError(file, f"{path}[{i}]", "must be a string")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ConfigError(file, f"{path}[{i}]", f"invalid regex: {exc}") from exc


def _validate_evidence(value: Any, file: str, path: str) -> None:
    if not isinstance(value, dict):
        raise ConfigError(file, path, "must be a mapping")
    _check_unknown(value, _EVIDENCE_KEYS, file, path)
    if "commands" in value:
        _check_str_list(value["commands"], file, f"{path}.commands")
    if "command_regex" in value and value["command_regex"] is not None:
        if not isinstance(value["command_regex"], str):
            raise ConfigError(file, f"{path}.command_regex", "must be a string")
        try:
            re.compile(value["command_regex"])
        except re.error as exc:
            raise ConfigError(file, f"{path}.command_regex", f"invalid regex: {exc}") from exc
    for key in ("exclude_args", "partial_args"):
        if key in value:
            _check_str_list(value[key], file, f"{path}.{key}")
    for key in ("fail_output", "success_output", "empty_output"):
        if key in value:
            _check_regex_list(value[key], file, f"{path}.{key}")


def _validate_rule(rule: Any, idx: int, file: str) -> None:
    path = f"rules[{idx}]"
    if not isinstance(rule, dict):
        raise ConfigError(file, path, "must be a mapping")
    _check_unknown(rule, _RULE_KEYS, file, path)
    if "id" not in rule or not isinstance(rule["id"], str) or not rule["id"]:
        raise ConfigError(file, f"{path}.id", "required, must be a non-empty string")
    rule_id = rule["id"]
    if "claim_type" in rule:
        claim_type = rule["claim_type"]
        if not isinstance(claim_type, str) or not CLAIM_TYPE_RE.match(claim_type):
            raise ConfigError(file, f"{path}.claim_type", "must match ^[a-z][a-z0-9_]*$")
    if "action" in rule:
        action = rule["action"]
        if action is False:
            # A common YAML 1.1 trap: an unquoted `off` parses as the boolean False, not
            # the string "off". Accepted as a nicety and normalized to the string "off" in
            # place, so every later stage (merge, `_build_rule`) only ever sees a string.
            rule["action"] = "off"
            action = "off"
        if action not in ACTION_VALUES:
            raise ConfigError(file, f"{path}.action", f"must be one of {sorted(ACTION_VALUES)}")
    if "claims" in rule:
        _check_regex_list(rule["claims"], file, f"{path}.claims")
    if "keywords" in rule:
        keywords = rule["keywords"]
        if not isinstance(keywords, list) or not all(
            isinstance(k, str) and k and k == k.lower() for k in keywords
        ):
            raise ConfigError(
                file, f"{path}.keywords", "must be a list of lowercase, non-empty strings"
            )
    if rule_id not in BUILTIN_RULE_IDS and not rule.get("keywords"):
        raise ConfigError(file, f"{path}.keywords", f"required for custom rule '{rule_id}'")
    if "evidence" in rule:
        _validate_evidence(rule["evidence"], file, f"{path}.evidence")
    for key in ("relevant_files", "ignore_files", "exempt_if_only_edited"):
        if key in rule:
            _check_str_list(rule[key], file, f"{path}.{key}")
    if "suggest" in rule and rule["suggest"] is not None and not isinstance(rule["suggest"], str):
        raise ConfigError(file, f"{path}.suggest", "must be a string or null")


def _validate_edits(value: Any, file: str) -> None:
    _check_unknown(value, _EDITS_KEYS, file, "edits")
    for key in _EDITS_KEYS:
        if key in value:
            _check_str_list(value[key], file, f"edits.{key}")


def _validate_top(data: Any, file: str) -> None:
    _check_unknown(data, _TOP_KEYS, file, "")
    if "version" in data and data["version"] != 1:
        raise ConfigError(file, "version", "must be 1")
    if "enabled" in data and not isinstance(data["enabled"], bool):
        raise ConfigError(file, "enabled", "must be a boolean")
    if "mode" in data and data["mode"] not in MODE_VALUES:
        raise ConfigError(file, "mode", f"must be one of {sorted(MODE_VALUES)}")
    if "max_blocks_per_turn" in data:
        value = data["max_blocks_per_turn"]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ConfigError(file, "max_blocks_per_turn", "must be an integer >= 0")
    if "check_subagents" in data and not isinstance(data["check_subagents"], bool):
        raise ConfigError(file, "check_subagents", "must be a boolean")
    if "subagent_calls_are_edits" in data and not isinstance(
        data["subagent_calls_are_edits"], bool
    ):
        raise ConfigError(file, "subagent_calls_are_edits", "must be a boolean")
    if "subagent_skip_types" in data:
        _check_str_list(data["subagent_skip_types"], file, "subagent_skip_types")
    if "skip_token" in data and not isinstance(data["skip_token"], str):
        raise ConfigError(file, "skip_token", "must be a string")
    if "max_transcript_mb" in data:
        value = data["max_transcript_mb"]
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            raise ConfigError(file, "max_transcript_mb", "must be a positive number")
    if "edits" in data:
        _validate_edits(data["edits"], file)
    if "execution_commands" in data:
        _check_str_list(data["execution_commands"], file, "execution_commands")
    if "read_only_commands" in data:
        _check_str_list(data["read_only_commands"], file, "read_only_commands")
    if "rules" in data:
        rules = data["rules"]
        if not isinstance(rules, list):
            raise ConfigError(file, "rules", "must be a list")
        for i, rule in enumerate(rules):
            _validate_rule(rule, i, file)


def _parse_yaml(text: str, file: str) -> dict[str, Any]:
    try:
        data = yamlload.safe_load(text)
    except Exception as exc:  # the vendored loader's own error hierarchy; any failure is fatal
        raise ConfigError(file, "", f"invalid YAML: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(file, "", "top level must be a mapping")
    return data


# ---------------------------------------------------------------------------------------
# Layers
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Layer:
    """One configuration source: a name, the path it was read from, and its parsed
    mapping (``None`` when the file does not exist)."""

    name: str
    path: str
    data: dict[str, Any] | None


def defaults_path() -> str:
    """Absolute path to the packaged ``defaults.yaml``."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "defaults.yaml")


def user_config_path(env: Mapping[str, str]) -> str:
    """``${XDG_CONFIG_HOME:-~/.config}/proof-of-done/config.yaml``."""
    xdg = env.get("XDG_CONFIG_HOME")
    if not xdg:
        home = env.get("HOME") or os.path.expanduser("~")
        xdg = os.path.join(home, ".config")
    return os.path.join(xdg, "proof-of-done", "config.yaml")


def project_config_path(root: str) -> str:
    """``<root>/.proof-of-done.yaml``."""
    return os.path.join(root, ".proof-of-done.yaml")


def layer_paths_for(root: str, env: Mapping[str, str]) -> list[str]:
    """The file paths for the defaults/user/project layers, plus ``PROOF_OF_DONE_CONFIG``
    when it is set, in merge order."""
    paths = [defaults_path(), user_config_path(env), project_config_path(root)]
    extra = env.get("PROOF_OF_DONE_CONFIG")
    if extra:
        paths.append(extra)
    return paths


def _build_layers(
    names_and_paths: Sequence[tuple[str, str]], reader: Callable[[str], str | None]
) -> list[Layer]:
    layers: list[Layer] = []
    for name, path in names_and_paths:
        text = reader(path)
        if text is None:
            if name == "defaults":
                raise ConfigError(path, "", "built-in defaults.yaml is missing or unreadable")
            layers.append(Layer(name=name, path=path, data=None))
            continue
        data = _parse_yaml(text, file=path)
        _validate_top(data, file=path)
        layers.append(Layer(name=name, path=path, data=data))
    return layers


def load_layers(
    root: str, env: Mapping[str, str], reader: Callable[[str], str | None]
) -> list[Layer]:
    """Read and validate every configuration layer, in merge order.

    ``reader(path) -> str | None`` is injectable: return the file's text, or ``None`` if it
    does not exist. Raises :class:`ConfigError` on the first invalid layer.
    """
    paths = layer_paths_for(root, env)
    return _build_layers(list(zip(_LAYER_NAMES, paths)), reader)


def disk_reader(path: str) -> str | None:
    """The real-filesystem reader: the file's text, or ``None`` when it cannot be read."""
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return None


def load_layers_from_disk(root: str, env: Mapping[str, str]) -> list[Layer]:
    """:func:`load_layers` reading real files from disk. Used wherever a caller (the hook's
    tamper-recompute path, the CLI) needs a fresh, uncached read of every layer."""
    return load_layers(root, env, disk_reader)


def load_for_audit(
    config_path: str | None, reader: Callable[[str], str | None] = disk_reader
) -> tuple[Config, list[Layer]]:
    """The config `audit` judges history with: the packaged `defaults.yaml`,
    optionally overridden by exactly one extra file (`--config`). Deliberately skips the user
    and project layers and every environment override -- a historic transcript was not
    produced under *this* machine's local configuration, so audit must not silently pick one
    up. The returned layers are for describing "the config applied" in the report."""
    names_and_paths = [("defaults", defaults_path())]
    if config_path:
        names_and_paths.append(("config", config_path))
    layers = _build_layers(names_and_paths, reader)
    merged = _merge_all(layers)
    return build_config(merged), layers


# ---------------------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------------------


def _merge_rules(
    base_rules: list[dict[str, Any]], override_rules: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for rule in base_rules:
        rule_id = rule["id"]
        by_id[rule_id] = dict(rule)
        order.append(rule_id)
    for rule in override_rules:
        rule_id = rule["id"]
        if rule_id in by_id:
            by_id[rule_id] = _merge_dicts(by_id[rule_id], rule)
        else:
            by_id[rule_id] = dict(rule)
            order.append(rule_id)
    return [by_id[rule_id] for rule_id in order]


def _merge_dicts(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if key == "rules" and isinstance(value, list):
            result["rules"] = _merge_rules(base.get("rules", []), value)
        elif isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge_dicts(result[key], value)
        else:
            result[key] = value
    return result


def _merge_all(layers: Sequence[Layer]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for layer in layers:
        if layer.data is not None:
            merged = _merge_dicts(merged, layer.data)
    return merged


# ---------------------------------------------------------------------------------------
# Effective config (env overrides + tamper protection)
# ---------------------------------------------------------------------------------------


def effective(
    layers: Sequence[Layer],
    env: Mapping[str, str],
    *,
    tampered_paths: set[str],
    settings_tampered: bool,
) -> tuple[Config, list[str]]:
    """Merge `layers`, undo any downgrade contributed by a tampered layer file, then apply
    the ``PROOF_OF_DONE``/``PROOF_OF_DONE_MODE`` environment overrides.

    `tampered_paths` holds the paths (matching some layer's `path`) of config files the
    session itself edited. For each such file, a resulting `enabled: false`, `mode: warn`,
    a rule `action` lowered towards `warn`/`off`, or a raised `max_blocks_per_turn` that
    file is responsible for is replaced by the value the merge would have without that
    file; every other change from a tampered file still applies. `settings_tampered` marks
    that a Claude Code settings file was edited to touch ``PROOF_OF_DONE``/
    ``enabledPlugins`` this session, which makes ``PROOF_OF_DONE=off`` and
    ``PROOF_OF_DONE_MODE=warn`` from the environment be ignored (a strengthening
    ``PROOF_OF_DONE_MODE=block`` still applies). Returns the resulting :class:`Config`
    and human-readable notes describing anything that was overridden.
    """
    notes: list[str] = []
    merged = _merge_all(layers)
    result = copy.deepcopy(merged)

    tampered_layers = [lyr for lyr in layers if lyr.path in tampered_paths]
    _undo_tampered_downgrades(result, layers, tampered_layers, notes)

    cfg = build_config(result)
    cfg, env_notes = apply_env_overrides(cfg, env, settings_tampered=settings_tampered)
    notes.extend(env_notes)
    return cfg, notes


def _undo_tampered_downgrades(
    result: dict[str, Any],
    layers: Sequence[Layer],
    tampered_layers: Sequence[Layer],
    notes: list[str],
) -> None:
    """Mutates `result` in place, undoing exactly the downgrades each tampered layer file is
    responsible for. Split out of :func:`effective` so that function reads as: undo tamper,
    then apply env -- the same two steps :func:`load_cached` now applies separately (tamper
    already at merge time via `effective`'s callers, env per invocation)."""
    for layer in tampered_layers:
        without = _merge_all([lyr for lyr in layers if lyr is not layer])

        if result.get("enabled", True) is False and without.get("enabled", True) is not False:
            result["enabled"] = without.get("enabled", True)
            notes.append(
                f"ignored 'enabled: false' from {layer.name} ({layer.path}): "
                "edited during this session"
            )

        if result.get("mode", _DEFAULT_MODE) == "warn" and without.get("mode", _DEFAULT_MODE) != (
            "warn"
        ):
            result["mode"] = without.get("mode", _DEFAULT_MODE)
            notes.append(
                f"ignored 'mode: warn' from {layer.name} ({layer.path}): edited during this session"
            )

        base_cap = result.get("max_blocks_per_turn", _DEFAULT_MAX_BLOCKS_PER_TURN)
        without_cap = without.get("max_blocks_per_turn", _DEFAULT_MAX_BLOCKS_PER_TURN)
        if isinstance(base_cap, int) and isinstance(without_cap, int) and base_cap > without_cap:
            result["max_blocks_per_turn"] = without_cap
            notes.append(
                f"ignored a higher 'max_blocks_per_turn' from {layer.name} ({layer.path}): "
                "edited during this session"
            )

        without_rules = {r["id"]: r for r in without.get("rules", [])}
        for rule in result.get("rules", []):
            without_rule = without_rules.get(rule.get("id"))
            if without_rule is None:
                continue
            current_action = rule.get("action", "block")
            without_action = without_rule.get("action", "block")
            if _action_rank(current_action) < _action_rank(without_action):
                notes.append(
                    f"ignored a lowered action for rule '{rule.get('id')}' from "
                    f"{layer.name} ({layer.path}): edited during this session"
                )
                rule["action"] = without_action


def apply_env_overrides(
    config: Config, env: Mapping[str, str], *, settings_tampered: bool = False
) -> tuple[Config, list[str]]:
    """Apply the ``PROOF_OF_DONE``/``PROOF_OF_DONE_MODE`` environment overrides to an
    already-merged, tamper-resolved :class:`Config`.

    Split out of :func:`effective` so a cached pre-env config (:func:`load_cached`) can have
    *this* invocation's environment applied to it directly -- no re-merge, no re-import of
    the YAML loader -- instead of baking one invocation's environment into the cached value
    for every later invocation to inherit regardless of its own environment.
    """
    notes: list[str] = []
    enabled = config.enabled
    mode = config.mode

    if env.get("PROOF_OF_DONE") == "off":
        if settings_tampered:
            notes.append(
                "ignored PROOF_OF_DONE=off: a Claude Code settings file was edited "
                "during this session"
            )
        else:
            enabled = False

    mode_env = env.get("PROOF_OF_DONE_MODE")
    if mode_env == "warn":
        if settings_tampered:
            notes.append(
                "ignored PROOF_OF_DONE_MODE=warn: a Claude Code settings file was edited "
                "during this session"
            )
        else:
            mode = "warn"
    elif mode_env == "block":
        mode = "block"

    if enabled == config.enabled and mode == config.mode:
        return config, notes
    return replace(config, enabled=enabled, mode=mode), notes


# ---------------------------------------------------------------------------------------
# Compiled config
# ---------------------------------------------------------------------------------------


def _split_prefixes(strings: Sequence[str]) -> tuple[tuple[str, ...], ...]:
    return tuple(tuple(shlex.split(s)) for s in strings)


def _join_prefix(tokens: Sequence[str]) -> str:
    return shlex.join(tokens)


@dataclass(frozen=True)
class EvidenceSpec:
    commands: tuple[tuple[str, ...], ...]
    command_regex: re.Pattern[str] | None
    exclude_args: tuple[str, ...]
    partial_args: tuple[str, ...]
    fail_output: tuple[re.Pattern[str], ...]
    success_output: tuple[re.Pattern[str], ...]
    empty_output: tuple[re.Pattern[str], ...]


@dataclass(frozen=True)
class Rule:
    id: str
    claim_type: str
    action: str
    claims: tuple[re.Pattern[str], ...]
    keywords: tuple[str, ...]
    evidence: EvidenceSpec
    relevant_files: tuple[str, ...]
    ignore_files: tuple[str, ...]
    exempt_if_only_edited: tuple[str, ...]
    suggest: str | None


@dataclass(frozen=True)
class EditsConfig:
    tools: tuple[str, ...]
    subagent_tools: tuple[str, ...]
    bash_writes: tuple[tuple[str, ...], ...]
    formatters: tuple[tuple[str, ...], ...]
    tree_commands: tuple[tuple[str, ...], ...]
    agent_trace_tools: tuple[str, ...]


@dataclass(frozen=True)
class Config:
    version: int
    enabled: bool
    mode: str
    max_blocks_per_turn: int
    check_subagents: bool
    subagent_skip_types: tuple[str, ...]
    subagent_calls_are_edits: bool
    skip_token: str
    max_transcript_mb: float
    edits: EditsConfig
    execution_commands: tuple[tuple[str, ...], ...]
    read_only_commands: tuple[tuple[str, ...], ...]
    rules: tuple[Rule, ...] = field(default_factory=tuple)

    def keywords(self) -> frozenset[str]:
        """Union of every rule's keywords, whatever its ``action`` and whether or not the
        config is ``enabled``: the fast-path prefilter.

        A rule turned ``off`` (or a whole config switched off) still has to contribute its
        keywords, because a message that mentions one is the trigger for the tamper check, and
        that check has to run before any ``enabled``/``action`` setting from a file is trusted.
        """
        return frozenset(keyword for rule in self.rules for keyword in rule.keywords)

    def rules_for(self, claim_type: str) -> tuple[Rule, ...]:
        """Enabled rules (``action != "off"``) whose `claim_type` matches, in config
        order."""
        return tuple(
            rule for rule in self.rules if rule.claim_type == claim_type and rule.action != "off"
        )

    def to_json(self) -> dict[str, Any]:
        """A JSON-serializable mapping equivalent to the validated merged config this
        `Config` was built from. Round-trips through :meth:`from_json`."""
        return {
            "version": self.version,
            "enabled": self.enabled,
            "mode": self.mode,
            "max_blocks_per_turn": self.max_blocks_per_turn,
            "check_subagents": self.check_subagents,
            "subagent_skip_types": list(self.subagent_skip_types),
            "subagent_calls_are_edits": self.subagent_calls_are_edits,
            "skip_token": self.skip_token,
            "max_transcript_mb": self.max_transcript_mb,
            "edits": {
                "tools": list(self.edits.tools),
                "subagent_tools": list(self.edits.subagent_tools),
                "bash_writes": [_join_prefix(p) for p in self.edits.bash_writes],
                "formatters": [_join_prefix(p) for p in self.edits.formatters],
                "tree_commands": [_join_prefix(p) for p in self.edits.tree_commands],
                "agent_trace_tools": list(self.edits.agent_trace_tools),
            },
            "execution_commands": [_join_prefix(p) for p in self.execution_commands],
            "read_only_commands": [_join_prefix(p) for p in self.read_only_commands],
            "rules": [_rule_to_json(rule) for rule in self.rules],
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Config:
        """Inverse of :meth:`to_json`. Recompiles every regex and re-splits every prefix
        list, so this never imports the YAML loader."""
        return build_config(data)


def _rule_to_json(rule: Rule) -> dict[str, Any]:
    evidence = rule.evidence
    return {
        "id": rule.id,
        "claim_type": rule.claim_type,
        "action": rule.action,
        "claims": [p.pattern for p in rule.claims],
        "keywords": list(rule.keywords),
        "evidence": {
            "commands": [_join_prefix(p) for p in evidence.commands],
            "command_regex": evidence.command_regex.pattern if evidence.command_regex else None,
            "exclude_args": list(evidence.exclude_args),
            "partial_args": list(evidence.partial_args),
            "fail_output": [p.pattern for p in evidence.fail_output],
            "success_output": [p.pattern for p in evidence.success_output],
            "empty_output": [p.pattern for p in evidence.empty_output],
        },
        "relevant_files": list(rule.relevant_files),
        "ignore_files": list(rule.ignore_files),
        "exempt_if_only_edited": list(rule.exempt_if_only_edited),
        "suggest": rule.suggest,
    }


def _build_rule(data: dict[str, Any]) -> Rule:
    raw_evidence = data.get("evidence") or {}
    command_regex = raw_evidence.get("command_regex")
    evidence = EvidenceSpec(
        commands=_split_prefixes(raw_evidence.get("commands", [])),
        command_regex=re.compile(command_regex) if command_regex else None,
        exclude_args=tuple(raw_evidence.get("exclude_args", [])),
        partial_args=tuple(raw_evidence.get("partial_args", [])),
        fail_output=tuple(re.compile(p) for p in raw_evidence.get("fail_output", [])),
        success_output=tuple(re.compile(p) for p in raw_evidence.get("success_output", [])),
        empty_output=tuple(re.compile(p) for p in raw_evidence.get("empty_output", [])),
    )
    return Rule(
        id=data["id"],
        claim_type=data.get("claim_type", data["id"]),
        action=data.get("action", "block"),
        claims=tuple(re.compile(p) for p in data.get("claims", [])),
        keywords=tuple(data.get("keywords", [])),
        evidence=evidence,
        relevant_files=tuple(data.get("relevant_files", [])),
        ignore_files=tuple(data.get("ignore_files", [])),
        exempt_if_only_edited=tuple(data.get("exempt_if_only_edited", [])),
        suggest=data.get("suggest"),
    )


def build_config(data: dict[str, Any]) -> Config:
    """Compile a validated, merged raw config mapping into a :class:`Config`: split every
    prefix list and compile every regex."""
    raw_edits = data.get("edits") or {}
    edits = EditsConfig(
        tools=tuple(raw_edits.get("tools", [])),
        subagent_tools=tuple(raw_edits.get("subagent_tools", [])),
        bash_writes=_split_prefixes(raw_edits.get("bash_writes", [])),
        formatters=_split_prefixes(raw_edits.get("formatters", [])),
        tree_commands=_split_prefixes(raw_edits.get("tree_commands", [])),
        agent_trace_tools=tuple(raw_edits.get("agent_trace_tools", [])),
    )
    return Config(
        version=int(data.get("version", 1)),
        enabled=bool(data.get("enabled", True)),
        mode=str(data.get("mode", _DEFAULT_MODE)),
        max_blocks_per_turn=int(data.get("max_blocks_per_turn", _DEFAULT_MAX_BLOCKS_PER_TURN)),
        check_subagents=bool(data.get("check_subagents", True)),
        subagent_skip_types=tuple(data.get("subagent_skip_types", [])),
        subagent_calls_are_edits=bool(data.get("subagent_calls_are_edits", True)),
        skip_token=str(data.get("skip_token", "#skip-proof")),
        max_transcript_mb=float(data.get("max_transcript_mb", 200)),
        edits=edits,
        execution_commands=_split_prefixes(data.get("execution_commands", [])),
        read_only_commands=_split_prefixes(data.get("read_only_commands", [])),
        rules=tuple(_build_rule(r) for r in data.get("rules", [])),
    )


# ---------------------------------------------------------------------------------------
# Merged-config cache
# ---------------------------------------------------------------------------------------


def _cache_key(layer_paths: Sequence[str]) -> list[list[Any]]:
    key: list[list[Any]] = []
    for path in layer_paths:
        try:
            info = os.stat(path)
        except OSError:
            key.append([path, None])
        else:
            key.append([path, info.st_mtime_ns, info.st_size])
    return key


def _read_cache(path: str) -> dict[str, Any] | None:
    try:
        with open(path, encoding="utf-8") as handle:
            loaded: Any = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(loaded, dict):
        return None
    return loaded


def _write_json_atomic(path: str, data: Any) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".config-cache-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


def _load_full(
    layer_paths: Sequence[str],
) -> tuple[Config, frozenset[str], list[dict[str, Any]]]:
    """Parse, validate and merge every layer from disk into a `Config` with **no**
    environment override and **no** tamper adjustment applied -- this is the only shape
    :func:`load_cached` may persist to disk, since the cache key covers only the layer
    files' own `(path, mtime_ns, size)`, not the invoking process's environment (an invocation
    with ``PROOF_OF_DONE=off`` must not make a later invocation with a different environment
    see `enabled: False`)."""
    layers = _build_layers(list(zip(_LAYER_NAMES, layer_paths)), disk_reader)
    merged = _merge_all(layers)
    cfg = build_config(merged)
    sources = [
        {"name": layer.name, "path": layer.path, "found": layer.data is not None}
        for layer in layers
    ]
    return cfg, cfg.keywords(), sources


def load_cached(
    data_dir: str, layer_paths: Sequence[str], env: Mapping[str, str]
) -> tuple[Config, frozenset[str], list[dict[str, Any]]]:
    """Load the merged config through ``<data_dir>/config-cache.json``, then apply *this*
    invocation's ``PROOF_OF_DONE``/``PROOF_OF_DONE_MODE`` environment overrides.

    The cache key is ``[path, mtime_ns, size]`` (or ``[path, null]`` when a file is
    missing) for every entry in `layer_paths`. On a hit, this reconstructs the `Config`
    straight from the cached JSON and never imports the YAML loader. On a miss (or any
    cache read error), it parses and validates every layer, merges them, writes the cache
    back atomically, and returns the result. Either way, what is *cached* is the merged
    config before any environment override or tamper adjustment -- two invocations sharing
    one data dir but different `env` (or one that tampered and one that didn't) each get
    their own correct answer from the same cache entry, instead of one invocation's
    environment leaking into another's. `layer_paths` is typically
    :func:`layer_paths_for`'s result. Tamper protection is not applied here — callers that
    need it call :func:`effective` directly with the tamper information from the transcript.
    """
    cache_path = os.path.join(data_dir, CACHE_FILENAME)
    key = _cache_key(layer_paths)
    cached = _read_cache(cache_path)
    if cached is not None and cached.get("cache_key") == key:
        try:
            cfg = Config.from_json(cached["config"])
            keywords = frozenset(cached["keywords"])
            sources = cached["sources"]
            if not isinstance(sources, list):
                raise ValueError("cached sources must be a list")
        except (KeyError, TypeError, ValueError, ConfigError, re.error):
            pass  # any corruption in the cached payload falls through to a full reload
        else:
            cfg, _env_notes = apply_env_overrides(cfg, env)
            return cfg, keywords, sources

    cfg, keywords, sources = _load_full(layer_paths)
    payload = {
        "cache_key": key,
        "config": cfg.to_json(),
        "keywords": sorted(keywords),
        "sources": sources,
    }
    with contextlib.suppress(OSError):
        _write_json_atomic(cache_path, payload)
    cfg, _env_notes = apply_env_overrides(cfg, env)
    return cfg, keywords, sources


# ---------------------------------------------------------------------------------------
# `proof-of-done config show`
# ---------------------------------------------------------------------------------------


def show(config: Config, sources: Sequence[Mapping[str, Any]]) -> str:
    """Render the effective config and the layer files it came from, for
    ``proof-of-done config show``."""
    lines = [
        "proof-of-done effective configuration",
        "",
        f"version: {config.version}",
        f"enabled: {str(config.enabled).lower()}",
        f"mode: {config.mode}",
        f"max_blocks_per_turn: {config.max_blocks_per_turn}",
        f"check_subagents: {str(config.check_subagents).lower()}",
        f"subagent_skip_types: {', '.join(config.subagent_skip_types) or '(none)'}",
        f"subagent_calls_are_edits: {str(config.subagent_calls_are_edits).lower()}",
        f"skip_token: {config.skip_token}",
        f"max_transcript_mb: {config.max_transcript_mb}",
        "",
        "rules:",
    ]
    for rule in config.rules:
        lines.append(f"  - {rule.id} ({rule.claim_type}, {rule.action})")
    lines.append("")
    lines.append("sources:")
    for source in sources:
        name = source.get("name")
        path = source.get("path")
        found = source.get("found")
        state = "" if found else "  (not found)"
        lines.append(f"  - {name}: {path}{state}")
    return "\n".join(lines) + "\n"
