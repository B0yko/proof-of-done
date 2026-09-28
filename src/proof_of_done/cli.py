"""``proof-of-done`` command-line entry point (spec item 10). Subcommands: ``check``, ``init``,
``config show``, ``audit``, ``trace validate``, ``hook stop|subagent-stop`` (the launcher's own
entry point).
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

    p_audit = sub.add_parser(
        "audit",
        help="Simulate the Stop hook over historic transcripts and report the result.",
        description=(
            "Simulate the Stop hook at every stop attempt of every given session (and its "
            "subagents), and report claim/evidence statistics. Exactly one input mode is "
            "required: PATH... (files, directories or globs), --claude-projects, or --demo."
        ),
    )
    p_audit.add_argument(
        "paths", nargs="*", metavar="PATH", help="Transcript files, directories or globs."
    )
    p_audit.add_argument(
        "--claude-projects",
        action="store_true",
        help="Audit $CLAUDE_CONFIG_DIR/projects (or ~/.claude/projects).",
    )
    p_audit.add_argument(
        "--demo", action="store_true", help="Audit the bundled synthetic demo corpus."
    )
    p_audit.add_argument(
        "--source",
        choices=["auto", "claude-code", "agent-trace", "codex"],
        default="auto",
        help="Transcript format ('codex' is EXPERIMENTAL: see transcript/codex.py).",
    )
    p_audit.add_argument(
        "--config", help="An extra config file layered over the built-in defaults."
    )
    p_audit.add_argument("--format", choices=["text", "json", "md"], default="text")
    p_audit.add_argument("--out", help="Write the report here instead of stdout.")
    p_audit.add_argument(
        "--export-traces", metavar="FILE", help="Also write agent-trace/v1 JSONL here."
    )
    p_audit.add_argument(
        "--redact", action="store_true", help="Hash quotes/commands/paths/session ids."
    )
    p_audit.add_argument(
        "--salt", help="Redaction salt (default: $PROOF_OF_DONE_SALT, else random)."
    )
    p_audit.add_argument(
        "--ci",
        action="store_true",
        help="Exit 1 over --max-unsupported-rate; write $GITHUB_STEP_SUMMARY.",
    )
    p_audit.add_argument("--max-unsupported-rate", type=float, default=0.0)

    p_trace = sub.add_parser("trace", help="agent-trace/v1 commands.")
    trace_sub = p_trace.add_subparsers(dest="trace_command")
    p_trace_validate = trace_sub.add_parser(
        "validate", help="Validate an agent-trace/v1 JSONL file."
    )
    p_trace_validate.add_argument("file", metavar="FILE")

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
# audit
# ------------------------------------------------------------------------------------------


def _cmd_audit(args: argparse.Namespace) -> int:
    from proof_of_done import audit as audit_mod
    from proof_of_done import report as report_mod

    env = dict(os.environ)
    try:
        result = audit_mod.run_audit(
            paths=args.paths,
            claude_projects=args.claude_projects,
            demo=args.demo,
            source=args.source,
            config_path=args.config,
            export_traces_path=args.export_traces,
            redact=args.redact,
            salt=args.salt,
            env=env,
        )
    except audit_mod.AuditUsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except audit_mod.AuditInputError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3

    text = report_mod.render(result.report, fmt=args.format)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
    else:
        sys.stdout.write(text)

    if args.ci:
        step_summary = env.get("GITHUB_STEP_SUMMARY")
        if step_summary:
            with open(step_summary, "a", encoding="utf-8") as fh:
                fh.write(report_mod.render_md(result.report))
        if result.report.unsupported_rate() > args.max_unsupported_rate:
            return 1
    return 0


# ------------------------------------------------------------------------------------------
# trace validate
# ------------------------------------------------------------------------------------------


def _cmd_trace_validate(args: argparse.Namespace) -> int:
    if not os.path.exists(args.file):
        print(f"error: file not found: {args.file}", file=sys.stderr)
        return 2

    from proof_of_done import traces as traces_mod

    try:
        errors = traces_mod.validate_file(args.file)
    except OSError as exc:
        print(f"error: cannot read {args.file}: {exc}", file=sys.stderr)
        return 2

    if errors:
        for line in errors:
            print(line)
        return 1
    print(f"{args.file}: valid ({traces_mod.SCHEMA_NAME})")
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
    if args.command == "audit":
        return _cmd_audit(args)
    if args.command == "trace":
        if args.trace_command == "validate":
            return _cmd_trace_validate(args)
        print("error: 'proof-of-done trace' needs a subcommand (validate)", file=sys.stderr)
        return 2
    if args.command == "hook":
        return _cmd_hook(args)

    parser.print_help()
    return 0
