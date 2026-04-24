"""CLI for ``xmclaw security scan`` — offline skill-source safety scan.

Runs :mod:`xmclaw.security.skill_scanner` against a file or directory
and prints human-readable findings grouped by severity.  Used by skill
authors before handing a skill off to ``SkillRegistry`` and by CI
pipelines to gate PR landings.

Pure-local — does not talk to the daemon, does not need it running.
"""
from __future__ import annotations

import json
from pathlib import Path

import typer

from xmclaw.security.skill_scanner import (
    SkillScanResult,
    scan_directory,
    scan_skill,
)
from xmclaw.security.tool_guard.models import GuardSeverity


_SEVERITY_ORDER = [
    GuardSeverity.CRITICAL,
    GuardSeverity.HIGH,
    GuardSeverity.MEDIUM,
    GuardSeverity.LOW,
    GuardSeverity.INFO,
    GuardSeverity.SAFE,
]


def _severity_exit_code(results: list[SkillScanResult]) -> int:
    """0 = clean, 1 = any HIGH/CRITICAL, 2 = MEDIUM only.  LOW/INFO/SAFE
    stay at 0 so CI noise stays manageable.  Callers who want a
    stricter gate can post-process the JSON output."""
    worst = GuardSeverity.SAFE
    order = {s: i for i, s in enumerate(reversed(_SEVERITY_ORDER))}
    for r in results:
        if order.get(r.max_severity, 0) > order.get(worst, 0):
            worst = r.max_severity
    if worst in (GuardSeverity.CRITICAL, GuardSeverity.HIGH):
        return 1
    if worst == GuardSeverity.MEDIUM:
        return 2
    return 0


def _print_text(results: list[SkillScanResult]) -> None:
    total_findings = sum(len(r.findings) for r in results)
    if total_findings == 0:
        typer.echo(f"Scanned {len(results)} file(s). No findings.")
        return
    typer.echo(f"Scanned {len(results)} file(s). {total_findings} finding(s):")
    for result in results:
        if not result.findings:
            continue
        typer.echo(f"\n{result.path}  ({result.max_severity.value})")
        # Sort findings high-severity-first for scannability.
        order = {s: i for i, s in enumerate(_SEVERITY_ORDER)}
        for f in sorted(result.findings, key=lambda x: order.get(x.severity, 99)):
            typer.echo(
                f"  [{f.severity.value.upper():<8}] {f.rule_id}  "
                f"{f.title}"
            )
            if f.description and f.description != f.title:
                typer.echo(f"      {f.description}")


def _print_json(results: list[SkillScanResult]) -> None:
    payload = [
        {
            "path": r.path,
            "max_severity": r.max_severity.value,
            "is_safe": r.is_safe,
            "findings": [
                {
                    "rule_id": f.rule_id,
                    "category": f.category,
                    "severity": f.severity.value,
                    "title": f.title,
                    "description": f.description,
                    "matched_value": f.matched_value,
                    "remediation": f.remediation,
                    "guardian": f.guardian,
                }
                for f in r.findings
            ],
        }
        for r in results
    ]
    typer.echo(json.dumps(payload, indent=2))


def run_security_scan(
    path: Path,
    as_json: bool = False,
) -> int:
    if not path.exists():
        typer.echo(f"Error: path does not exist: {path}", err=True)
        return 1
    if path.is_dir():
        results = scan_directory(path)
    else:
        results = [scan_skill(path)]
    if not results:
        typer.echo(f"No .py files found under {path}.")
        return 0
    if as_json:
        _print_json(results)
    else:
        _print_text(results)
    return _severity_exit_code(results)
