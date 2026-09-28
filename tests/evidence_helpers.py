"""Shared helpers for test_evidence.py, test_suggest.py and test_message.py: a small builder
for `Session`/`Step` objects (precise control over step order, tool args and results, without
going through the JSONL adapter) and a config loader/patcher on top of the real
`defaults.yaml`. Not a test module itself (no `test_` prefix); pytest never collects it.
"""

from __future__ import annotations

from typing import Any

from proof_of_done import config as config_mod
from proof_of_done.transcript.model import (
    KIND_MESSAGE,
    KIND_TOOL_CALL,
    KIND_TOOL_RESULT,
    ROLE_AGENT,
    ROLE_ENVIRONMENT,
    ROLE_TOOL,
    ROLE_USER,
    Session,
    Step,
)

ROOT = "/work/demo-app"


def real_defaults(root: str = ROOT, env: dict[str, str] | None = None) -> config_mod.Config:
    """The actual packaged `defaults.yaml`, compiled through the public layer/merge API (no
    user or project file present)."""

    def reader(path: str) -> str | None:
        if path == config_mod.defaults_path():
            with open(path, encoding="utf-8") as fh:
                return fh.read()
        return None

    layers = config_mod.load_layers(root, env or {}, reader)
    cfg, _notes = config_mod.effective(
        layers, env or {}, tampered_paths=set(), settings_tampered=False
    )
    return cfg


def with_rules(
    base: config_mod.Config, rules: list[dict[str, Any]], **overrides: Any
) -> config_mod.Config:
    """A fresh `Config` built from `base`'s JSON form with `rules` replacing its rule list and
    any other top-level field overridden -- for tests that need tight control over one or two
    rules instead of the full built-in set."""
    data = base.to_json()
    data["rules"] = rules
    data.update(overrides)
    return config_mod.build_config(data)


class SessionBuilder:
    """Appends `Step`s in order, auto-numbering `i`. Every method returns `self`."""

    def __init__(self, cwd: str = ROOT) -> None:
        self.steps: list[Step] = []
        self.cwd = cwd

    def _i(self) -> int:
        return len(self.steps)

    def user(self, text: str) -> SessionBuilder:
        self.steps.append(Step(i=self._i(), kind=KIND_MESSAGE, role=ROLE_USER, content=text))
        return self

    def final(self, text: str) -> SessionBuilder:
        self.steps.append(Step(i=self._i(), kind=KIND_MESSAGE, role=ROLE_AGENT, content=text))
        return self

    def bash(
        self,
        command: str,
        *,
        exit_code: int | None = 0,
        ok: bool | None = True,
        output: str = "",
        background: bool = False,
        interrupted: bool = False,
        timed_out: bool = False,
        no_result: bool = False,
        cwd: str | None = None,
        tool_use_id: str | None = None,
        run_in_background: bool = False,
    ) -> SessionBuilder:
        i = self._i()
        tid = tool_use_id or f"toolu_bash_{i}"
        args: dict[str, Any] = {"command": command}
        if run_in_background:
            args["run_in_background"] = True
        self.steps.append(
            Step(
                i=i,
                kind=KIND_TOOL_CALL,
                role=ROLE_AGENT,
                name="Bash",
                args=args,
                tool_use_id=tid,
                cwd=cwd if cwd is not None else self.cwd,
            )
        )
        if not no_result:
            ri = self._i()
            self.steps.append(
                Step(
                    i=ri,
                    kind=KIND_TOOL_RESULT,
                    role=ROLE_TOOL,
                    name="Bash",
                    ok=ok,
                    exit_code=exit_code,
                    output_text=output,
                    background=background,
                    interrupted=interrupted,
                    timed_out=timed_out,
                    tool_use_id=tid,
                )
            )
        return self

    def edit(
        self,
        tool: str,
        path: str,
        *,
        new_string: str = "x",
        content: str = "x",
        edits: list[dict[str, Any]] | None = None,
        tool_use_id: str | None = None,
    ) -> SessionBuilder:
        i = self._i()
        tid = tool_use_id or f"toolu_edit_{i}"
        args: dict[str, Any]
        if tool == "NotebookEdit":
            args = {"notebook_path": path, "new_source": content}
        elif tool == "Write":
            args = {"file_path": path, "content": content}
        elif tool == "MultiEdit":
            args = {"file_path": path, "edits": edits or [{"new_string": new_string}]}
        else:  # Edit
            args = {"file_path": path, "old_string": "a", "new_string": new_string}
        self.steps.append(
            Step(i=i, kind=KIND_TOOL_CALL, role=ROLE_AGENT, name=tool, args=args, tool_use_id=tid)
        )
        ri = self._i()
        self.steps.append(
            Step(i=ri, kind=KIND_TOOL_RESULT, role=ROLE_TOOL, name=tool, ok=True, tool_use_id=tid)
        )
        return self

    def agent_call(
        self,
        *,
        name: str = "Agent",
        subagent_type: str | None = "general-purpose",
        tool_use_id: str | None = None,
    ) -> SessionBuilder:
        i = self._i()
        tid = tool_use_id or f"toolu_agent_{i}"
        args = {"subagent_type": subagent_type} if subagent_type is not None else {}
        self.steps.append(
            Step(i=i, kind=KIND_TOOL_CALL, role=ROLE_AGENT, name=name, args=args, tool_use_id=tid)
        )
        ri = self._i()
        self.steps.append(
            Step(i=ri, kind=KIND_TOOL_RESULT, role=ROLE_TOOL, name=name, ok=True, tool_use_id=tid)
        )
        return self

    def task_notification(self, tool_use_id: str, *, exit_code: int = 0) -> SessionBuilder:
        i = self._i()
        self.steps.append(
            Step(
                i=i,
                kind=KIND_MESSAGE,
                role=ROLE_ENVIRONMENT,
                entry="task_notification",
                tool_use_id=tool_use_id,
                exit_code=exit_code,
                content=f"task completed with exit code {exit_code}",
            )
        )
        return self

    @property
    def stop_index(self) -> int:
        return len(self.steps)

    def build(self, session_id: str = "s1", cwd: str | None = None) -> Session:
        return Session(
            session_id=session_id,
            source_format="test",
            path=None,
            steps=list(self.steps),
            cwd=cwd if cwd is not None else self.cwd,
        )
