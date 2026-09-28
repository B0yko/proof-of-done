"""Renders an :class:`~proof_of_done.audit.AuditReport` as text, JSON or Markdown (spec item
10/11): counts, unsupported rate overall and by type, a reason breakdown, the partial-run
share, and the 10 most recent unsupported examples. The Markdown form doubles as the
``$GITHUB_STEP_SUMMARY`` body `cli.py` writes under ``--ci``.
"""

from __future__ import annotations

import json

from proof_of_done.audit import AuditReport

_DEMO_BANNER = (
    "SYNTHETIC DEMO CORPUS -- this report describes 30 hand-authored fixture sessions, "
    "not a measurement of any real coding agent."
)

FORMATS = ("text", "json", "md")


def render(report: AuditReport, fmt: str = "text") -> str:
    if fmt == "json":
        return render_json(report)
    if fmt == "md":
        return render_md(report)
    if fmt == "text":
        return render_text(report)
    raise ValueError(f"unknown report format {fmt!r} (choose from {', '.join(FORMATS)})")


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


# ------------------------------------------------------------------------------------------
# text
# ------------------------------------------------------------------------------------------


def render_text(report: AuditReport) -> str:
    lines: list[str] = ["proof-of-done audit report"]
    if report.demo:
        lines.append(_DEMO_BANNER)
    lines.append("")
    lines.append(f"config applied: {report.config_description}")
    if report.redacted:
        note = (
            " (random per run -- not reproducible without recording it)"
            if report.salt_is_random
            else ""
        )
        lines.append(f"redacted: yes{note}")
    lines.append(f"files processed: {report.files_processed}")
    lines.append("")
    lines.append(f"sessions: {report.sessions}")
    lines.append(f"turns: {report.turns}")
    lines.append(f"stop attempts: {report.stop_attempts}")
    lines.append(f"turns with claims: {report.turns_with_claims}")
    lines.append(f"claims: {report.claims_total}")
    lines.append(
        f"unsupported: {report.unsupported_total} ({_pct(report.unsupported_rate())} of claims)"
    )
    lines.append(f"partial share of supported claims: {_pct(report.partial_share())}")
    lines.append("")
    lines.append("by claim type:")
    if not report.by_type:
        lines.append("  (none)")
    for claim_type in sorted(report.by_type):
        t = report.by_type[claim_type]
        lines.append(
            f"  {claim_type}: {t.claims} claims, {t.unsupported} unsupported "
            f"({_pct(report.type_rate(claim_type))})"
        )
    lines.append("")
    lines.append("unsupported by reason:")
    if not report.by_reason:
        lines.append("  (none)")
    for reason in sorted(report.by_reason):
        lines.append(f"  {reason}: {report.by_reason[reason]}")
    lines.append("")
    lines.append("most recent unsupported claims:")
    if not report.recent_unsupported:
        lines.append("  (none)")
    for ex in report.recent_unsupported:
        cmd = ex.command or "(no command found)"
        lines.append(f'  [{ex.claim_type}] "{ex.quote}" -- {ex.reason}')
        lines.append(f"    Run: {cmd}")
    if report.warnings:
        lines.append("")
        lines.append(f"warnings ({len(report.warnings)}):")
        for w in report.warnings:
            lines.append(f"  - {w}")
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------------------------------
# json
# ------------------------------------------------------------------------------------------


def render_json(report: AuditReport) -> str:
    data = {
        "demo": report.demo,
        "config_applied": report.config_description,
        "redacted": report.redacted,
        "salt_is_random": report.salt_is_random,
        "files_processed": report.files_processed,
        "sessions": report.sessions,
        "turns": report.turns,
        "stop_attempts": report.stop_attempts,
        "turns_with_claims": report.turns_with_claims,
        "claims_total": report.claims_total,
        "supported_total": report.supported_total,
        "unsupported_total": report.unsupported_total,
        "unsupported_rate": report.unsupported_rate(),
        "partial_share": report.partial_share(),
        "by_type": {
            claim_type: {
                "claims": t.claims,
                "unsupported": t.unsupported,
                "unsupported_rate": report.type_rate(claim_type),
            }
            for claim_type, t in sorted(report.by_type.items())
        },
        "by_reason": dict(sorted(report.by_reason.items())),
        "recent_unsupported": [
            {
                "claim_type": ex.claim_type,
                "quote": ex.quote,
                "reason": ex.reason,
                "command": ex.command,
                "session_id": ex.session_id,
                "ts": ex.ts,
            }
            for ex in report.recent_unsupported
        ],
        "warnings": list(report.warnings),
    }
    return json.dumps(data, indent=2) + "\n"


# ------------------------------------------------------------------------------------------
# markdown
# ------------------------------------------------------------------------------------------


def render_md(report: AuditReport) -> str:
    lines: list[str] = ["# proof-of-done audit report", ""]
    if report.demo:
        lines.append(f"> **{_DEMO_BANNER}**")
        lines.append("")
    lines.append(f"- config applied: `{report.config_description}`")
    if report.redacted:
        note = " (random per run)" if report.salt_is_random else ""
        lines.append(f"- redacted: yes{note}")
    lines.append(f"- files processed: {report.files_processed}")
    lines.append(f"- sessions: {report.sessions}")
    lines.append(f"- turns: {report.turns}")
    lines.append(f"- stop attempts: {report.stop_attempts}")
    lines.append(f"- turns with claims: {report.turns_with_claims}")
    lines.append(f"- claims: {report.claims_total}")
    lines.append(
        f"- unsupported: {report.unsupported_total} ({_pct(report.unsupported_rate())} of claims)"
    )
    lines.append(f"- partial share of supported claims: {_pct(report.partial_share())}")
    lines.append("")
    lines.append("| claim type | claims | unsupported | rate |")
    lines.append("|---|---|---|---|")
    for claim_type in sorted(report.by_type):
        t = report.by_type[claim_type]
        rate = _pct(report.type_rate(claim_type))
        lines.append(f"| {claim_type} | {t.claims} | {t.unsupported} | {rate} |")
    lines.append("")
    lines.append("| reason | count |")
    lines.append("|---|---|")
    for reason in sorted(report.by_reason):
        lines.append(f"| {reason} | {report.by_reason[reason]} |")
    lines.append("")
    lines.append("**Most recent unsupported claims:**")
    lines.append("")
    if not report.recent_unsupported:
        lines.append("(none)")
    for ex in report.recent_unsupported:
        cmd = ex.command or "(no command found)"
        lines.append(f'- **{ex.claim_type}**: "{ex.quote}" — {ex.reason} — `Run: {cmd}`')
    if report.warnings:
        lines.append("")
        lines.append(f"**Warnings ({len(report.warnings)}):**")
        lines.append("")
        for w in report.warnings:
            lines.append(f"- {w}")
    return "\n".join(lines) + "\n"


__all__ = ["FORMATS", "render", "render_json", "render_md", "render_text"]
