"""Scenario DSL: load and strictly validate the YAML scenarios `fixtures/render.py` renders.

Not part of the installed package: imported by tests and `eval/` via a `sys.path` insertion of
the repo root. Uses only the vendored, stdlib-only YAML loader (`proof_of_done.yamlload`), never
a real third-party `yaml` install.
"""

from __future__ import annotations

import re
from typing import Any

from proof_of_done.yamlload import safe_load

# The marker a rendered `final` message uses to mark a labelled claim: `[[type|text]]`.
MARKER_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]*(?:#\d+)?$")
MARKER_RE = re.compile(r"\[\[(?P<type>[a-z][a-z0-9_]*(?:#\d+)?)\|(?P<text>.*?)\]\]", re.DOTALL)

CLOSED_REASONS = {
    "supported",
    "no_command",
    "stale",
    "failed_exit",
    "failed_output",
    "empty_run",
    "masked_inconclusive",
    "background_only",
    "superseded_by_failure",
    "no_result",
}

ECOSYSTEMS = {"python", "node", "go", "rust", "mixed"}
START_VALUES = {"fresh", "mid_session"}
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
BASH_MODES = {
    "foreground",
    "background",
    "interrupted",
    "timeout",
    "moved_to_background",
    "no_result",
}
ENV_ENTRY_KINDS = {
    "hook_feedback",
    "task_notification",
    "meta",
    "compaction",
    "local_command",
    "bash_mode",
}
STEP_KEYS = {
    "edit",
    "bash",
    "parallel",
    "subagent",
    "text",
    "env_entry",
    "user_bash",
    "queued_prompt",
}

DEFAULT_ROOT = "/work/demo-app"


class DslError(ValueError):
    """A scenario failed validation. The message names the file and the offending key path."""


def _path(path: str, key: Any) -> str:
    if isinstance(key, int):
        return f"{path}[{key}]"
    return f"{path}.{key}" if path else str(key)


def _fail(file: str, path: str, message: str) -> None:
    raise DslError(f"{file}: {path}: {message}")


def _require_dict(value: Any, file: str, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(file, path, f"expected a mapping, got {type(value).__name__}")
    return value  # type: ignore[return-value]


def _require_list(value: Any, file: str, path: str) -> list[Any]:
    if not isinstance(value, list):
        _fail(file, path, f"expected a list, got {type(value).__name__}")
    return value  # type: ignore[return-value]


def _require_str(value: Any, file: str, path: str) -> str:
    if not isinstance(value, str):
        _fail(file, path, f"expected a string, got {type(value).__name__}")
    return value  # type: ignore[return-value]


def _require_int(value: Any, file: str, path: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        _fail(file, path, f"expected an integer, got {type(value).__name__}")
    return value  # type: ignore[return-value]


def _require_bool(value: Any, file: str, path: str) -> bool:
    if not isinstance(value, bool):
        _fail(file, path, f"expected a boolean, got {type(value).__name__}")
    return value  # type: ignore[return-value]


def _require_enum(value: Any, choices: Any, file: str, path: str) -> str:
    s = _require_str(value, file, path)
    if s not in choices:
        _fail(file, path, f"unknown value {s!r} (expected one of {sorted(choices)})")
    return s


def _no_extra_keys(d: dict[str, Any], allowed: Any, file: str, path: str) -> None:
    extra = sorted(set(d) - set(allowed))
    if extra:
        _fail(file, _path(path, extra[0]), "unknown key")


def _require_keys(d: dict[str, Any], required: Any, file: str, path: str) -> None:
    for key in required:
        if key not in d:
            _fail(file, path, f"missing required key {key!r}")


# --------------------------------------------------------------------------------------
# steps
# --------------------------------------------------------------------------------------


def _validate_edit(step: dict[str, Any], file: str, path: str) -> None:
    allowed = {"tool", "path", "old", "new", "content", "edits", "notebook_path", "new_source"}
    _no_extra_keys(step, allowed, file, path)
    _require_keys(step, ["tool", "path"], file, path)
    tool = _require_enum(step["tool"], EDIT_TOOLS, file, _path(path, "tool"))
    _require_str(step["path"], file, _path(path, "path"))
    if tool == "Edit":
        _require_keys(step, ["old", "new"], file, path)
        _require_str(step["old"], file, _path(path, "old"))
        _require_str(step["new"], file, _path(path, "new"))
    elif tool == "Write":
        _require_keys(step, ["content"], file, path)
        _require_str(step["content"], file, _path(path, "content"))
    elif tool == "MultiEdit":
        _require_keys(step, ["edits"], file, path)
        _require_list(step["edits"], file, _path(path, "edits"))
    elif tool == "NotebookEdit":
        _require_keys(step, ["new_source"], file, path)
        _require_str(step["new_source"], file, _path(path, "new_source"))


def _validate_bash(step: dict[str, Any], file: str, path: str) -> None:
    allowed = {"cmd", "exit", "output", "mode", "notify", "output_repeat", "cwd"}
    _no_extra_keys(step, allowed, file, path)
    _require_keys(step, ["cmd"], file, path)
    _require_str(step["cmd"], file, _path(path, "cmd"))
    if "exit" in step:
        _require_int(step["exit"], file, _path(path, "exit"))
    if "output" in step:
        _require_str(step["output"], file, _path(path, "output"))
    if "mode" in step:
        _require_enum(step["mode"], BASH_MODES, file, _path(path, "mode"))
    if "notify" in step:
        notify = _require_dict(step["notify"], file, _path(path, "notify"))
        _no_extra_keys(notify, {"exit"}, file, _path(path, "notify"))
        if "exit" in notify:
            _require_int(notify["exit"], file, _path(path, "notify.exit"))
    if "output_repeat" in step:
        rep = _require_dict(step["output_repeat"], file, _path(path, "output_repeat"))
        _no_extra_keys(rep, {"line", "times"}, file, _path(path, "output_repeat"))
        _require_keys(rep, ["line", "times"], file, _path(path, "output_repeat"))
        _require_str(rep["line"], file, _path(path, "output_repeat.line"))
        times = _require_int(rep["times"], file, _path(path, "output_repeat.times"))
        if times < 1:
            _fail(file, _path(path, "output_repeat.times"), "must be >= 1")


def _validate_env_entry(step: dict[str, Any], file: str, path: str) -> None:
    allowed = {"entry", "text"}
    _no_extra_keys(step, allowed, file, path)
    _require_keys(step, ["entry", "text"], file, path)
    _require_enum(step["entry"], ENV_ENTRY_KINDS, file, _path(path, "entry"))
    _require_str(step["text"], file, _path(path, "text"))


def _validate_user_bash(step: dict[str, Any], file: str, path: str) -> None:
    allowed = {"cmd", "output"}
    _no_extra_keys(step, allowed, file, path)
    _require_keys(step, ["cmd"], file, path)
    _require_str(step["cmd"], file, _path(path, "cmd"))
    if "output" in step:
        _require_str(step["output"], file, _path(path, "output"))


def _validate_subagent(step: dict[str, Any], file: str, path: str) -> None:
    allowed = {"type", "prompt", "result", "steps", "final", "labels"}
    _no_extra_keys(step, allowed, file, path)
    _require_keys(step, ["type", "prompt", "result", "final"], file, path)
    _require_str(step["type"], file, _path(path, "type"))
    _require_str(step["prompt"], file, _path(path, "prompt"))
    _require_str(step["result"], file, _path(path, "result"))
    _require_str(step["final"], file, _path(path, "final"))
    steps = step.get("steps", [])
    _require_list(steps, file, _path(path, "steps"))
    for i, sub in enumerate(steps):
        _validate_step(sub, file, _path(_path(path, "steps"), i))
    labels = step.get("labels", {})
    _require_dict(labels, file, _path(path, "labels"))
    _validate_final_labels(step["final"], labels, file, _path(path, "final"), _path(path, "labels"))


def _validate_step(step: Any, file: str, path: str) -> None:
    d = _require_dict(step, file, path)
    keys = set(d)
    present = keys & STEP_KEYS
    if len(present) != 1:
        _fail(
            file,
            path,
            f"expected exactly one of {sorted(STEP_KEYS)}, got {sorted(keys) or 'nothing'}",
        )
    kind = next(iter(present))
    body = d[kind]
    sub_path = _path(path, kind)
    if kind == "edit":
        _validate_edit(_require_dict(body, file, sub_path), file, sub_path)
    elif kind == "bash":
        _validate_bash(_require_dict(body, file, sub_path), file, sub_path)
    elif kind == "parallel":
        items = _require_list(body, file, sub_path)
        for i, item in enumerate(items):
            _validate_step(item, file, _path(sub_path, i))
    elif kind == "subagent":
        _validate_subagent(_require_dict(body, file, sub_path), file, sub_path)
    elif kind == "text":
        _require_str(body, file, sub_path)
    elif kind == "env_entry":
        _validate_env_entry(_require_dict(body, file, sub_path), file, sub_path)
    elif kind == "user_bash":
        _validate_user_bash(_require_dict(body, file, sub_path), file, sub_path)
    elif kind == "queued_prompt":
        _require_str(body, file, sub_path)


# --------------------------------------------------------------------------------------
# markers and labels
# --------------------------------------------------------------------------------------


def parse_markers(text: str) -> list[tuple[str, str, int, int]]:
    """Return `(type, inner_text, match_start, match_end)` for every `[[type|text]]` marker."""
    return [
        (m.group("type"), m.group("text"), m.start(), m.end()) for m in MARKER_RE.finditer(text)
    ]


def _validate_label_entry(entry: Any, file: str, path: str) -> None:
    d = _require_dict(entry, file, path)
    _no_extra_keys(d, {"label", "reason", "suggest", "partial"}, file, path)
    _require_keys(d, ["label"], file, path)
    label = _require_enum(d["label"], {"supported", "unsupported"}, file, _path(path, "label"))
    if label == "unsupported":
        _require_keys(d, ["reason", "suggest"], file, path)
        _require_enum(d["reason"], CLOSED_REASONS, file, _path(path, "reason"))
        suggest = d["suggest"]
        if suggest is not None:
            _require_str(suggest, file, _path(path, "suggest"))
    elif "reason" in d:
        _require_enum(d["reason"], CLOSED_REASONS, file, _path(path, "reason"))
    if "partial" in d:
        _require_bool(d["partial"], file, _path(path, "partial"))


def _validate_final_labels(
    final: str, labels: dict[str, Any], file: str, final_path: str, labels_path: str
) -> None:
    for m in MARKER_RE.finditer(final):
        if not MARKER_TYPE_RE.match(m.group("type")):
            _fail(file, final_path, f"invalid marker type {m.group('type')!r}")
    marker_types = {m.group("type") for m in MARKER_RE.finditer(final)}
    if not marker_types:
        if labels:
            _fail(file, labels_path, "turn has no [[marker]] in `final` but `labels` is non-empty")
        return
    for claim_type in marker_types:
        if claim_type not in labels:
            _fail(file, labels_path, f"missing label entry for marker {claim_type!r}")
    for key, entry in labels.items():
        if key not in marker_types:
            _fail(file, labels_path, f"label {key!r} has no matching [[marker]] in `final`")
        _validate_label_entry(entry, file, _path(labels_path, key))


# --------------------------------------------------------------------------------------
# turns / attempts
# --------------------------------------------------------------------------------------


def _validate_attempt(attempt: dict[str, Any], file: str, path: str) -> None:
    allowed = {"steps", "final", "labels", "payload", "expect"}
    _no_extra_keys(attempt, allowed, file, path)
    _require_keys(attempt, ["final"], file, path)
    steps = attempt.get("steps", [])
    _require_list(steps, file, _path(path, "steps"))
    for i, step in enumerate(steps):
        _validate_step(step, file, _path(_path(path, "steps"), i))
    final = _require_str(attempt["final"], file, _path(path, "final"))
    labels = attempt.get("labels", {})
    _require_dict(labels, file, _path(path, "labels"))
    _validate_final_labels(final, labels, file, _path(path, "final"), _path(path, "labels"))
    if "payload" in attempt:
        _require_dict(attempt["payload"], file, _path(path, "payload"))
    if "expect" in attempt:
        expect = _require_dict(attempt["expect"], file, _path(path, "expect"))
        _no_extra_keys(expect, {"decision", "reason"}, file, _path(path, "expect"))
        if "decision" in expect:
            _require_enum(
                expect["decision"], {"block", "warn", "allow"}, file, _path(path, "expect.decision")
            )


def _validate_turn(turn: dict[str, Any], file: str, path: str) -> None:
    allowed = {"user", "steps", "final", "labels", "payload", "expect", "attempts"}
    _no_extra_keys(turn, allowed, file, path)
    _require_keys(turn, ["user"], file, path)
    _require_str(turn["user"], file, _path(path, "user"))
    if "attempts" in turn:
        for key in ("steps", "final", "labels", "payload", "expect"):
            if key in turn:
                _fail(file, path, f"turn has both `attempts` and `{key}`")
        attempts = _require_list(turn["attempts"], file, _path(path, "attempts"))
        if not attempts:
            _fail(file, _path(path, "attempts"), "must be non-empty")
        for i, attempt in enumerate(attempts):
            _validate_attempt(
                _require_dict(attempt, file, _path(_path(path, "attempts"), i)),
                file,
                _path(_path(path, "attempts"), i),
            )
    else:
        implicit_attempt = {k: v for k, v in turn.items() if k != "user"}
        _validate_attempt(implicit_attempt, file, path)


# --------------------------------------------------------------------------------------
# top level
# --------------------------------------------------------------------------------------


def validate_scenario(obj: Any, file: str) -> dict[str, Any]:
    """Validate a parsed scenario mapping, filling in documented defaults. Raises `DslError`."""
    d = _require_dict(obj, file, "")
    allowed = {
        "id",
        "description",
        "ecosystem",
        "root",
        "project_files",
        "config",
        "env",
        "start",
        "turns",
    }
    _no_extra_keys(d, allowed, file, "")
    _require_keys(d, ["id", "ecosystem", "turns"], file, "")
    _require_str(d["id"], file, "id")
    if "description" in d:
        _require_str(d["description"], file, "description")
    _require_enum(d["ecosystem"], ECOSYSTEMS, file, "ecosystem")
    d.setdefault("root", DEFAULT_ROOT)
    _require_str(d["root"], file, "root")
    project_files = d.setdefault("project_files", {})
    _require_dict(project_files, file, "project_files")
    for key, value in project_files.items():
        if value is not None:
            _require_str(value, file, _path("project_files", key))
    if "config" in d:
        _require_dict(d["config"], file, "config")
    env = d.setdefault("env", {})
    _require_dict(env, file, "env")
    for key, value in env.items():
        _require_str(value, file, _path("env", key))
    d.setdefault("start", "fresh")
    _require_enum(d["start"], START_VALUES, file, "start")
    turns = _require_list(d["turns"], file, "turns")
    if not turns:
        _fail(file, "turns", "must be non-empty")
    for i, turn in enumerate(turns):
        _validate_turn(_require_dict(turn, file, _path("turns", i)), file, _path("turns", i))
    return d


def parse_scenario(text: str, file: str) -> dict[str, Any]:
    """Parse and validate a scenario given as a YAML string (used directly by tests)."""
    obj = safe_load(text)
    return validate_scenario(obj, file)


def load_scenario(path: str) -> dict[str, Any]:
    """Load and validate a scenario YAML file from disk."""
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    file = path.rsplit("/", 1)[-1]
    return parse_scenario(text, file)


def scenario_id_from_filename(path: str) -> str | None:
    """The scenario id a file name implies (its basename without `.yaml`), or None."""
    base = path.rsplit("/", 1)[-1]
    if base.endswith(".yaml"):
        return base[: -len(".yaml")]
    return None
