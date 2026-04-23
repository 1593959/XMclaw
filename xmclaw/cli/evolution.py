"""CLI commands for evolution visibility (Epic #4 Phase A).

Reads ``~/.xmclaw/skills/*.jsonl`` history files and formats them into
human-readable tables.
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any

import typer

from xmclaw.utils.paths import skills_dir


def _parse_since(since: str | None) -> float | None:
    """Convert ``--since`` string like ``24h`` or ``7d`` to a unix timestamp."""
    if since is None:
        return None
    since = since.strip().lower()
    if since.endswith("h"):
        hours = int(since[:-1])
        return time.time() - hours * 3600
    if since.endswith("d"):
        days = int(since[:-1])
        return time.time() - days * 86400
    # Fallback: treat as integer hours
    try:
        return time.time() - int(since) * 3600
    except ValueError:
        return None


def _load_history_records(
    since_ts: float | None = None,
) -> list[dict[str, Any]]:
    """Load all promotion/rollback records from ``~/.xmclaw/skills/*.jsonl``."""
    records: list[dict[str, Any]] = []
    base = skills_dir()
    if not base.exists():
        return records
    for path in base.glob("*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = rec.get("ts")
            if since_ts is not None and ts is not None and ts < since_ts:
                continue
            records.append(rec)
    # Sort chronologically ascending
    records.sort(key=lambda r: r.get("ts", 0))
    return records


def _fmt_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _fmt_record(rec: dict[str, Any]) -> str:
    """Format a single history record into a one-line summary."""
    kind = rec.get("kind", "?")
    skill_id = rec.get("skill_id", "?")
    from_v = rec.get("from_version", 0)
    to_v = rec.get("to_version", 0)
    ts = rec.get("ts", 0)
    evidence = rec.get("evidence", [])

    arrow = f"v{from_v} → v{to_v}" if from_v != to_v else f"v{to_v}"
    time_str = _fmt_ts(ts) if ts else "?"

    # Try to extract a grader mean score from evidence strings like "mean=0.723"
    score_str = ""
    for ev in evidence:
        if isinstance(ev, str) and "mean=" in ev:
            try:
                score = float(ev.split("mean=", 1)[1])
                score_str = f" (score {score:.3f})"
                break
            except ValueError:
                pass

    if kind == "promote":
        return f"  [+] {time_str}  {skill_id:<24} {arrow}{score_str}"
    if kind == "rollback":
        reason = rec.get("reason", "")
        reason_str = f" — {reason}" if reason else ""
        return f"  [-] {time_str}  {skill_id:<24} {arrow}{reason_str}"
    return f"  [?] {time_str}  {skill_id:<24} {arrow}"


def run_evolution_show(since: str | None = None) -> int:
    """Print evolution history as a formatted table."""
    since_ts = _parse_since(since)
    records = _load_history_records(since_ts)

    if not records:
        typer.echo("No evolution events found.")
        if since:
            typer.echo(f"  (filtered by --since {since})")
        return 0

    header = f"{'Time':<18} {'Skill':<24} {'Change'}"
    typer.echo(header)
    typer.echo("-" * len(header))
    for rec in records:
        typer.echo(_fmt_record(rec))
    return 0
