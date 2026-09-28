"""The normalized session model every transcript adapter produces.

Everything downstream of parsing (claim detection, evidence, the exporter) is
format-independent and only ever sees :class:`Session` / :class:`Step`. The step list on a
`Session` IS the agent-trace step list a later exporter will write, in the same order with the
same ``i``, so step numbers in block messages and round-trip verdicts agree across formats.

Typing here deliberately spells out ``Optional``/``List``/``Dict`` (rather than the
``X | None`` / ``list[X]`` style used elsewhere in this repo) because these dataclasses may be
introspected with ``typing.get_type_hints()`` by later tooling (schema export, docs) running on
a real Python 3.9 interpreter, where postponed-evaluated ``X | Y`` annotations fail to resolve.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# Step.kind
KIND_MESSAGE = "message"
KIND_TOOL_CALL = "tool_call"
KIND_TOOL_RESULT = "tool_result"

# Step.role
ROLE_USER = "user"
ROLE_AGENT = "agent"
ROLE_TOOL = "tool"
ROLE_ENVIRONMENT = "environment"

# Step.entry (role=environment only)
ENTRY_HOOK_FEEDBACK = "hook_feedback"
ENTRY_TASK_NOTIFICATION = "task_notification"
ENTRY_META = "meta"
ENTRY_COMPACTION = "compaction"
ENTRY_LOCAL_COMMAND = "local_command"
ENTRY_BASH_MODE = "bash_mode"
ENTRY_QUEUED_COMMAND = "queued_command"
ENTRY_OTHER = "other"


@dataclass
class Step:
    """One normalized event in a session's step list."""

    i: int  # 0-based, contiguous
    kind: str  # message | tool_call | tool_result
    role: str  # user | agent | tool | environment
    name: Optional[str] = None  # tool name for tool_call/tool_result, else None
    content: Optional[str] = None  # message text, else None
    args: Optional[Dict[str, Any]] = None  # tool input for tool_call
    ok: Optional[bool] = None  # tool_result only
    output_text: Optional[str] = None  # tool_result: stdout+stderr (bounded: first+last 64 KiB)
    exit_code: Optional[int] = None
    error: Optional[str] = None
    ts: str = ""  # RFC 3339
    tool_use_id: Optional[str] = None
    # flags (exported under meta.proof_of_done.step_flags when true/non-default)
    background: bool = False  # run_in_background, moved to background, or a "running" result
    interrupted: bool = False
    timed_out: bool = False
    entry: str = ""  # role=environment: hook_feedback | task_notification | meta | compaction |
    # local_command | bash_mode | queued_command | other
    cwd: Optional[str] = None  # working directory recorded with the entry (per-entry cwd)


@dataclass
class Session:
    """One parsed transcript, normalized to the step list above."""

    session_id: str
    source_format: str
    path: Optional[str]
    steps: List[Step] = field(default_factory=list)
    starts_mid_session: bool = False
    cwd: Optional[str] = None  # first recorded cwd
    subagents: List[Session] = field(default_factory=list)  # grouped under the parent (audit)
    agent_type: Optional[str] = None
    agent_id: Optional[str] = None
    unrecognized: bool = False  # >50 lines and zero recognizable steps
