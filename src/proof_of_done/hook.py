"""Stop/SubagentStop hook entry point (PLAN §9, spec item 2).

``main(argv)`` always returns 0 and writes at most one JSON document to stdout, in a single
``write`` call at the very end -- never a traceback, never more than one document. Every
failure mode (unreadable transcript, invalid config, oversized transcript, an internal
exception) fails open: the stop is allowed, optionally with a short ``systemMessage``.

Performance: the *fast path* -- a message containing none of the enabled rules' keywords --
must return before the transcript is read or the YAML loader is imported. To keep that true,
this module's own top-level imports stay stdlib-only; everything past the keyword prefilter is
imported lazily, inside :func:`_run`.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

STDIN_CAP = 1024 * 1024  # 1 MiB
FLUSH_WAIT_SECONDS = 0.05

_FAIL_OPEN_INTERNAL = "proof-of-done: internal error, check skipped (see log)"
_FAIL_OPEN_TOO_LARGE = "proof-of-done: transcript too large, check skipped"
_FAIL_OPEN_UNRECOGNIZED = "proof-of-done: unrecognized transcript format, check skipped"


# ------------------------------------------------------------------------------------------
# argv / stdin / data-dir
# ------------------------------------------------------------------------------------------


def _parse_args(argv: list[str]) -> tuple[str, str | None]:
    event_raw = argv[0] if argv else "stop"
    event = "subagent-stop" if event_raw in ("subagent-stop", "subagent_stop") else "stop"
    data_dir_arg: str | None = None
    i = 1
    while i < len(argv):
        if argv[i] == "--data-dir" and i + 1 < len(argv):
            data_dir_arg = argv[i + 1]
            i += 2
        else:
            i += 1
    return event, data_dir_arg


def _resolve_data_dir(arg_value: str | None, env: dict[str, str]) -> str:
    """``--data-dir`` -> ``$CLAUDE_PLUGIN_DATA`` -> a per-user temp fallback when empty,
    unexpanded (contains ``$``) or relative (PLAN §9 point 1)."""
    candidate = arg_value if arg_value else env.get("CLAUDE_PLUGIN_DATA")
    if candidate and "$" not in candidate and os.path.isabs(candidate):
        return candidate
    uid = os.getuid() if hasattr(os, "getuid") else 0
    tmp = env.get("TMPDIR") or "/tmp"
    return os.path.join(tmp, f"proof-of-done-{uid}")


def _read_stdin(cap: int) -> dict[str, Any] | None:
    try:
        data = sys.stdin.buffer.read(cap + 1)
    except Exception:
        return None
    if not data or len(data) > cap:
        return None
    try:
        obj = json.loads(data.decode("utf-8", errors="replace"))
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _settings_paths(root: str, env: dict[str, str]) -> list[str]:
    home = env.get("HOME") or os.path.expanduser("~")
    config_dir = env.get("CLAUDE_CONFIG_DIR") or os.path.join(home, ".claude")
    project_claude = os.path.join(root, ".claude")
    paths_out: list[str] = []
    for base in (project_claude, config_dir):
        paths_out.append(os.path.join(base, "settings.json"))
        paths_out.append(os.path.join(base, "settings.local.json"))
    return paths_out


# ------------------------------------------------------------------------------------------
# main
# ------------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    argv = list(argv) if argv is not None else sys.argv[1:]
    event, data_dir_arg = _parse_args(argv)
    env = dict(os.environ)
    data_dir = _resolve_data_dir(data_dir_arg, env)

    payload: dict[str, Any] | None
    try:
        payload = _run(event, data_dir, env)
    except Exception as exc:  # fail open, unconditionally, for anything unexpected
        payload = {"systemMessage": _FAIL_OPEN_INTERNAL}
        try:
            from proof_of_done import logutil

            logutil.write(data_dir, "error", error=type(exc).__name__)
        except Exception:
            pass

    if payload is not None:
        try:
            sys.stdout.write(json.dumps(payload))
            sys.stdout.flush()
        except Exception:
            pass
    return 0


def _run(event: str, data_dir: str, env: dict[str, str]) -> dict[str, Any] | None:
    stdin_payload = _read_stdin(STDIN_CAP)
    if stdin_payload is None:
        return None

    session_id = stdin_payload.get("session_id")
    session_id = session_id if isinstance(session_id, str) else ""
    transcript_path = stdin_payload.get("transcript_path")
    cwd = stdin_payload.get("cwd")
    cwd = cwd if isinstance(cwd, str) and cwd else os.getcwd()
    stop_hook_active = bool(stdin_payload.get("stop_hook_active"))
    last_assistant_message = stdin_payload.get("last_assistant_message")
    background_tasks = stdin_payload.get("background_tasks")
    background_tasks = background_tasks if isinstance(background_tasks, list) else []

    is_subagent = event == "subagent-stop"
    agent_id = stdin_payload.get("agent_id")
    agent_id = agent_id if isinstance(agent_id, str) else None
    agent_type = stdin_payload.get("agent_type")
    agent_transcript_path = stdin_payload.get("agent_transcript_path")

    active_transcript = agent_transcript_path if is_subagent else transcript_path

    message_text = last_assistant_message if isinstance(last_assistant_message, str) else None
    if not message_text:
        message_text = _tail_message(active_transcript)
    if not message_text:
        return None

    from proof_of_done import fastpath

    if fastpath.decide(message_text, cwd, env, data_dir, is_subagent, agent_type) == fastpath.ALLOW:
        return None

    from proof_of_done import config as config_mod
    from proof_of_done import paths as paths_mod

    home = env.get("HOME") or os.path.expanduser("~")
    root = paths_mod.find_project_root(cwd, home, os.path.exists)
    layer_paths = config_mod.layer_paths_for(root, env)
    cfg, keywords, _sources = config_mod.load_cached(data_dir, layer_paths, env)

    if not cfg.enabled:
        return None
    if is_subagent:
        if not cfg.check_subagents:
            return None
        if isinstance(agent_type, str) and agent_type in cfg.subagent_skip_types:
            return None

    from proof_of_done import claims as claims_mod

    if not claims_mod.prefilter(message_text, keywords):
        return None

    if not isinstance(active_transcript, str) or not active_transcript:
        return None
    try:
        size = os.path.getsize(active_transcript)
    except OSError:
        return None
    if size > cfg.max_transcript_mb * 1024 * 1024:
        return {"systemMessage": _FAIL_OPEN_TOO_LARGE}

    from proof_of_done import evidence
    from proof_of_done.transcript import claude_code

    session = claude_code.parse(active_transcript)
    if session.unrecognized:
        return {"systemMessage": _FAIL_OPEN_UNRECOGNIZED}

    events = evidence.build_events(session, cfg, root)
    if any(c.no_result for c in events.commands):
        # Stop-time flush race: the last tool call's result may not have hit disk yet.
        # Re-read once after a short wait; if it is still missing, `engine.evaluate_stop`
        # downgrades a `no_result` verdict on exactly that call to `warn`.
        time.sleep(FLUSH_WAIT_SECONDS)
        session = claude_code.parse(active_transcript)
        if session.unrecognized:
            return {"systemMessage": _FAIL_OPEN_UNRECOGNIZED}
        events = evidence.build_events(session, cfg, root)

    stop_index = len(session.steps)

    if is_subagent:
        main_session = session
        if isinstance(transcript_path, str) and transcript_path and os.path.exists(transcript_path):
            try:
                main_session = claude_code.parse(transcript_path)
            except Exception:
                main_session = session
        main_events = evidence.build_events(main_session, cfg, root)
    else:
        main_session = session
        main_events = events

    from proof_of_done import engine

    skipped = engine.skip_requested(main_session, len(main_session.steps), cfg.skip_token)

    from proof_of_done import tamper

    tamper_edits = tamper.scan(
        main_events.edits,
        project_config_path=config_mod.project_config_path(root),
        user_config_path=config_mod.user_config_path(env),
        env_config_path=env.get("PROOF_OF_DONE_CONFIG"),
        settings_paths=_settings_paths(root, env),
    )
    if tamper_edits:
        layers = config_mod.load_layers_from_disk(root, env)
        cfg2, eff_notes = config_mod.effective(
            layers,
            env,
            tampered_paths=tamper.tampered_paths(tamper_edits),
            settings_tampered=tamper.settings_tampered(tamper_edits),
        )
        notes = eff_notes if eff_notes else tamper.notes(tamper_edits)
    else:
        cfg2, notes = cfg, []

    from proof_of_done.probe import OsProbe

    req = engine.StopRequest(
        session=session,
        stop_index=stop_index,
        final_message=message_text,
        config=cfg2,
        project_root=root,
        probe=OsProbe(root),
        skipped=skipped,
        tamper_notes=notes,
        background_tasks=background_tasks,
        events=events,
    )
    decision = engine.evaluate_stop(req)

    from proof_of_done import logutil

    logutil.write(
        data_dir,
        "decision",
        hook_event=event,
        decision=decision.action,
        claims=len(decision.results),
        unsupported=sum(1 for r in decision.results if not r.verdict.supported),
        tampered=bool(tamper_edits),
        skipped=decision.skipped,
    )

    if decision.skipped:
        return None

    return _apply_counters(decision, data_dir, session_id, agent_id, stop_hook_active, cfg2)


# ------------------------------------------------------------------------------------------
# helpers
# ------------------------------------------------------------------------------------------


def _tail_message(path: Any) -> str | None:
    if not isinstance(path, str) or not path:
        return None
    from proof_of_done.transcript import claude_code

    return claude_code.last_agent_text(path)


def _apply_counters(
    decision: Any,
    data_dir: str,
    session_id: str,
    agent_id: str | None,
    stop_hook_active: bool,
    cfg: Any,
) -> dict[str, Any] | None:
    from proof_of_done import counters

    counters.prune(data_dir)
    if not stop_hook_active:
        counters.write(data_dir, session_id, agent_id, 0)
    count = counters.read(data_dir, session_id, agent_id)

    if decision.action == "block":
        max_blocks = cfg.max_blocks_per_turn
        if max_blocks > 0 and count >= max_blocks:
            counters.write(data_dir, session_id, agent_id, 0)
            return {"systemMessage": _cap_warning(decision, cfg)}
        counters.write(data_dir, session_id, agent_id, count + 1)
        out: dict[str, Any] = {"decision": "block", "reason": decision.reason}
        if decision.system_message:
            out["systemMessage"] = decision.system_message
        return out

    counters.write(data_dir, session_id, agent_id, 0)
    if decision.system_message:
        return {"systemMessage": decision.system_message}
    return None


def _cap_warning(decision: Any, cfg: Any) -> str:
    from proof_of_done import message

    entries = [
        message.Entry(quote=r.quote, claim_type=r.claim_type, verdict=r.verdict, command=r.command)
        for r in decision.results
        if not r.verdict.supported and r.action == "block"
    ]
    prefix = f"proof-of-done: allowing the stop after {cfg.max_blocks_per_turn} consecutive blocks."
    if not entries:
        return prefix
    body = message.warn_message(entries, cfg.skip_token)
    return prefix + " " + body


__all__ = ["main"]
