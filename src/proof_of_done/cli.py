"""``proof-of-done`` command-line entry point (spec item 10). Subcommands: ``check``, ``init``,
``config show``, ``hook stop|subagent-stop`` (the launcher's own entry point). ``audit`` and
``trace validate`` are not registered yet -- they land with the exporter/auditor (a later
step).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from typing import Any

from proof_of_done import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="proof-of-done",
        description=("Check a coding agent's completion claims against its own transcript."),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")

    p_check = sub.add_parser(
        "check", help="Evaluate one final message against a transcript, without a live hook."
    )
    p_check.add_argument(
        "--transcript", required=True, help="Path to a Claude Code JSONL transcript."
    )
    msg_group = p_check.add_mutually_exclusive_group()
    msg_group.add_argument(
        "--message", help="The message to check (default: the transcript's last)."
    )
    msg_group.add_argument("--message-file", help="Read the message to check from this file.")
    p_check.add_argument(
        "--config", help="An extra config file, layered like PROOF_OF_DONE_CONFIG."
    )
    p_check.add_argument("--json", action="store_true", help="Print the decision as JSON.")

    p_init = sub.add_parser("init", help="Write a starter .proof-of-done.yaml for this project.")
    p_init.add_argument("--force", action="store_true", help="Overwrite an existing file.")

    p_config = sub.add_parser("config", help="Configuration commands.")
    config_sub = p_config.add_subparsers(dest="config_command")
    p_config_show = config_sub.add_parser("show", help="Print the effective merged configuration.")
    p_config_show.add_argument("--cwd", help="Directory to resolve the project root from.")

    p_hook = sub.add_parser("hook", help="Stop-hook entry point (used by bin/proof-of-done-hook).")
    p_hook.add_argument("hook_event", choices=["stop", "subagent-stop"])
    p_hook.add_argument("--data-dir")

    return parser


# ------------------------------------------------------------------------------------------
# check
# ------------------------------------------------------------------------------------------


def _project_root_for(path: str, env: dict[str, str]) -> str:
    from proof_of_done import paths as paths_mod

    home = env.get("HOME") or os.path.expanduser("~")
    cwd = os.path.dirname(os.path.abspath(path)) or os.getcwd()
    return paths_mod.find_project_root(cwd, home, os.path.exists)


def _load_effective_config(root: str, env: dict[str, str], extra_config: str | None) -> Any:
    from proof_of_done import config as config_mod

    env_eff = dict(env)
    if extra_config:
        env_eff["PROOF_OF_DONE_CONFIG"] = extra_config
    layers = config_mod.load_layers_from_disk(root, env_eff)
    cfg, _notes = config_mod.effective(
        layers, env_eff, tampered_paths=set(), settings_tampered=False
    )
    return cfg


def _decision_json(decision: Any) -> dict[str, Any]:
    return {
        "decision": decision.action,
        "reason": decision.reason,
        "systemMessage": decision.system_message,
        "claims": [
            {
                "claim_type": r.claim_type,
                "quote": r.quote,
                "supported": r.verdict.supported,
                "reason": r.verdict.reason,
                "action": r.action,
                "command": r.command,
            }
            for r in decision.results
        ],
    }


def _cmd_check(args: argparse.Namespace) -> int:
    if not os.path.exists(args.transcript):
        print(f"error: transcript not found: {args.transcript}", file=sys.stderr)
        return 2

    from proof_of_done.transcript import claude_code

    session = claude_code.parse(args.transcript)

    if args.message is not None:
        message_text: str | None = args.message
    elif args.message_file is not None:
        try:
            with open(args.message_file, encoding="utf-8") as fh:
                message_text = fh.read()
        except OSError as exc:
            print(f"error: cannot read --message-file: {exc}", file=sys.stderr)
            return 2
    else:
        message_text = claude_code.last_agent_text(args.transcript)
    if not message_text:
        print("error: transcript has no assistant message; pass --message", file=sys.stderr)
        return 2

    env = dict(os.environ)
    root = _project_root_for(args.transcript, env)
    try:
        cfg = _load_effective_config(root, env, args.config)
    except Exception as exc:  # ConfigError, or any file-level parse failure
        print(f"error: {exc}", file=sys.stderr)
        return 2

    from proof_of_done import engine
    from proof_of_done.probe import OsProbe

    stop_index = len(session.steps)
    skipped = engine.skip_requested(session, stop_index, cfg.skip_token)
    req = engine.StopRequest(
        session=session,
        stop_index=stop_index,
        final_message=message_text,
        config=cfg,
        project_root=root,
        probe=OsProbe(root),
        skipped=skipped,
    )
    decision = engine.evaluate_stop(req)

    if args.json:
        print(json.dumps(_decision_json(decision)))
    elif decision.action == "block":
        print(decision.reason)
    elif decision.skipped:
        print("proof-of-done: skip token present, check skipped.")
    elif decision.system_message:
        print(decision.system_message)
    else:
        print("proof-of-done: no unsupported claims.")

    return 1 if decision.action == "block" else 0


# ------------------------------------------------------------------------------------------
# init
# ------------------------------------------------------------------------------------------


def _detect_ecosystem(root: str) -> str:
    def exists(name: str) -> bool:
        return os.path.exists(os.path.join(root, name))

    if exists("pyproject.toml") or exists("pytest.ini") or exists("setup.py"):
        return "python"
    if exists("package.json"):
        return "node"
    if exists("go.mod"):
        return "go"
    if exists("Cargo.toml"):
        return "rust"
    return "generic"


def _starter_yaml(ecosystem: str) -> str:
    lines = [
        "# proof-of-done project configuration.",
        "# Reference: https://github.com/B0yko/proof-of-done/blob/main/docs/config.md",
        "version: 1",
        "",
    ]
    if ecosystem == "generic":
        lines.append("# No known project ecosystem was detected here. The built-in rules")
        lines.append("# still apply (defaults.yaml); add project-specific overrides below.")
    else:
        lines.append(f"# Detected ecosystem: {ecosystem}. The built-in rules already cover its")
        lines.append("# usual commands; add overrides below only where yours differ.")
    lines.append("rules: []")
    return "\n".join(lines) + "\n"


def _cmd_init(args: argparse.Namespace) -> int:
    target = os.path.join(os.getcwd(), ".proof-of-done.yaml")
    if os.path.exists(target) and not args.force:
        print(f"error: {target} already exists (use --force to overwrite)", file=sys.stderr)
        return 2
    ecosystem = _detect_ecosystem(os.getcwd())
    with open(target, "w", encoding="utf-8") as fh:
        fh.write(_starter_yaml(ecosystem))
    print(f"wrote {target} (detected ecosystem: {ecosystem})")
    return 0


# ------------------------------------------------------------------------------------------
# config show
# ------------------------------------------------------------------------------------------


def _cmd_config_show(args: argparse.Namespace) -> int:
    from proof_of_done import config as config_mod
    from proof_of_done import paths as paths_mod

    env = dict(os.environ)
    cwd = args.cwd or os.getcwd()
    home = env.get("HOME") or os.path.expanduser("~")
    root = paths_mod.find_project_root(cwd, home, os.path.exists)
    try:
        layers = config_mod.load_layers_from_disk(root, env)
        cfg, _notes = config_mod.effective(
            layers, env, tampered_paths=set(), settings_tampered=False
        )
    except config_mod.ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    sources = [
        {"name": lyr.name, "path": lyr.path, "found": lyr.data is not None} for lyr in layers
    ]
    sys.stdout.write(config_mod.show(cfg, sources))
    return 0


# ------------------------------------------------------------------------------------------
# hook
# ------------------------------------------------------------------------------------------


def _cmd_hook(args: argparse.Namespace) -> int:
    from proof_of_done import hook as hook_mod

    argv = [args.hook_event]
    if args.data_dir is not None:
        argv += ["--data-dir", args.data_dir]
    return hook_mod.main(argv)


# ------------------------------------------------------------------------------------------
# entry point
# ------------------------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "check":
        return _cmd_check(args)
    if args.command == "init":
        return _cmd_init(args)
    if args.command == "config":
        if args.config_command == "show":
            return _cmd_config_show(args)
        print("error: 'proof-of-done config' needs a subcommand (show)", file=sys.stderr)
        return 2
    if args.command == "hook":
        return _cmd_hook(args)

    parser.print_help()
    return 0
