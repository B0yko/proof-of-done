"""Stop/SubagentStop hook entry point.

``main(argv)`` always returns 0 and writes at most one JSON document to stdout, in a single
``write`` call at the very end -- never a traceback, never more than one document. Every
failure mode (unreadable transcript, invalid config, oversized transcript, an internal
exception) fails open: the stop is allowed, optionally with a short ``systemMessage``.

Performance: the *fast path* -- a message containing none of the rules' keywords -- must return
before the transcript is read or the YAML loader is imported. To keep that true, this module's
own top-level imports stay stdlib-only; everything past the keyword prefilter is imported
lazily, inside :func:`_run`.

Order matters for safety: a config can only be trusted after the tamper scan, so ``enabled``,
``mode`` and the rule actions are applied inside :func:`proof_of_done.engine.decide_stop`, after
the transcript is parsed and scanned, never before.
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
    unexpanded (contains ``$``) or relative."""
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


# ------------------------------------------------------------------------------------------
# main
# ------------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    started = time.perf_counter()
    argv = list(argv) if argv is not None else sys.argv[1:]
    event, data_dir_arg = _parse_args(argv)
    env = dict(os.environ)
    data_dir = _resolve_data_dir(data_dir_arg, env)

    payload: dict[str, Any] | None
    try:
        payload = _run(event, data_dir, env, started)
    except Exception as exc:  # fail open, unconditionally, for anything unexpected
        payload = {"systemMessage": _FAIL_OPEN_INTERNAL}
        try:
            from proof_of_done import logutil

            logutil.write(
                data_dir,
                "error",
                hook_event=event,
                error=type(exc).__name__,
                timings_ms={"total": _ms(started)},
            )
        except Exception:
            pass

    if payload is not None:
        try:
            sys.stdout.write(json.dumps(payload))
            sys.stdout.flush()
        except Exception:
            pass
    return 0


def _ms(since: float) -> float:
    return round((time.perf_counter() - since) * 1000, 2)


def _fail_open(
    data_dir: str,
    event: str,
    code: str,
    started: float,
    enabled: bool,
    text: str,
) -> dict[str, Any] | None:
    """Allow the stop, log why, and tell the user unless they switched the hook off."""
    from proof_of_done import logutil

    logutil.write(
        data_dir, "fail_open", hook_event=event, reason=code, timings_ms={"total": _ms(started)}
    )
    return {"systemMessage": text} if enabled else None


def _run(event: str, data_dir: str, env: dict[str, str], started: float) -> dict[str, Any] | None:
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

    if fastpath.decide(message_text, cwd, env, data_dir) == fastpath.ALLOW:
        return None
    fast_path_ms = _ms(started)

    from proof_of_done import config as config_mod
    from proof_of_done import paths as paths_mod

    home = env.get("HOME") or os.path.expanduser("~")
    root = paths_mod.find_project_root(cwd, home, os.path.exists)
    layer_paths = config_mod.layer_paths_for(root, env)
    cfg, keywords, _sources = config_mod.load_cached(data_dir, layer_paths, env)

    # `cfg.enabled` is deliberately not acted on here: it can come from a file the session
    # edited, and the tamper scan that decides whether to believe it needs the parsed
    # transcript. `engine.decide_stop` applies it afterwards. It is used below only to decide
    # whether a fail-open notice is worth showing to a user who switched the hook off.
    from proof_of_done import engine

    if is_subagent and engine.subagent_exempt(cfg, agent_type):
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
        return _fail_open(
            data_dir, event, "transcript_too_large", started, cfg.enabled, _FAIL_OPEN_TOO_LARGE
        )

    from proof_of_done import evidence
    from proof_of_done.transcript import claude_code

    parse_started = time.perf_counter()
    session = claude_code.parse(active_transcript)
    if session.unrecognized:
        return _fail_open(
            data_dir, event, "unrecognized_format", started, cfg.enabled, _FAIL_OPEN_UNRECOGNIZED
        )

    events = evidence.build_events(session, cfg, root, home=home)
    if any(c.no_result for c in events.commands):
        # Stop-time flush race: the last tool call's result may not have hit disk yet.
        # Re-read once after a short wait; if it is still missing, `engine.evaluate_stop`
        # downgrades a `no_result` verdict on exactly that call to `warn`.
        time.sleep(FLUSH_WAIT_SECONDS)
        session = claude_code.parse(active_transcript)
        if session.unrecognized:
            return _fail_open(
                data_dir,
                event,
                "unrecognized_format",
                started,
                cfg.enabled,
                _FAIL_OPEN_UNRECOGNIZED,
            )
        events = evidence.build_events(session, cfg, root, home=home)

    if is_subagent:
        main_session = session
        if isinstance(transcript_path, str) and transcript_path and os.path.exists(transcript_path):
            try:
                main_session = claude_code.parse(transcript_path)
            except Exception:
                main_session = session
        main_events = evidence.build_events(main_session, cfg, root, home=home)
    else:
        main_session = session
        main_events = events
    parse_ms = _ms(parse_started)

    from proof_of_done.probe import OsProbe

    judge_started = time.perf_counter()
    decision = engine.decide_stop(
        session=session,
        final_message=message_text,
        config=cfg,
        project_root=root,
        probe=OsProbe(root),
        env=env,
        read_file=config_mod.disk_reader,
        main_session=main_session,
        events=events,
        main_events=main_events,
        background_tasks=background_tasks,
        is_subagent=is_subagent,
        agent_type=agent_type,
    )
    detect_judge_ms = _ms(judge_started)

    from proof_of_done import logutil

    logutil.write_decision(
        data_dir,
        hook_event=event,
        decision=decision.action,
        results=[
            logutil.ClaimLog(
                claim_type=r.claim_type,
                rule_ids=r.rule_ids,
                supported=r.verdict.supported,
                reason=r.verdict.reason,
                action=r.action,
            )
            for r in decision.results
        ],
        skipped=decision.skipped,
        disabled=decision.disabled,
        tampered=decision.tampered,
        timings_ms={
            "fast_path": fast_path_ms,
            "parse": parse_ms,
            "detect_judge": detect_judge_ms,
            "total": _ms(started),
        },
    )

    if decision.skipped or decision.disabled or decision.config is None:
        return None

    return _apply_counters(
        decision, data_dir, session_id, agent_id, stop_hook_active, decision.config
    )


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
