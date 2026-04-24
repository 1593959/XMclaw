"""Session report generator — reads the SQLite event log and produces
a human-readable summary of a single conversation.

Epic #4 Phase B partial: surfaces what the agent actually *did* during a
session (turns, tool calls, grader verdicts, evolution events, cost) so
a user or on-call can reconstruct a conversation after the fact without
tailing the raw event stream. Read-only over the SQLite event log — the
daemon can be down, the session can be hours stale, none of that matters.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import typer

from xmclaw.utils.paths import default_events_db_path


@dataclass
class ToolInvocationReport:
    name: str
    args_preview: str
    ok: bool
    error: str | None
    latency_ms: float


@dataclass
class LLMCallReport:
    model: str = ""
    content_preview: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: float = 0.0


@dataclass
class GraderReport:
    score: float = 0.0
    quality: str = ""
    side_effect_observable: bool = False


@dataclass
class TurnReport:
    turn_index: int
    user_message: str = ""
    llm: LLMCallReport | None = None
    tools: list[ToolInvocationReport] = field(default_factory=list)
    grader: GraderReport | None = None


@dataclass
class EvolutionEvent:
    type: str
    skill_name: str = ""
    detail: str = ""
    ts: float = 0.0


@dataclass
class SessionReportData:
    session_id: str
    agent_id: str
    started_at: str
    ended_at: str
    duration_seconds: float
    event_count: int
    turns: list[TurnReport] = field(default_factory=list)
    evolution_events: list[EvolutionEvent] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    cost_summary: dict[str, Any] = field(default_factory=dict)


class SessionReportGenerator:
    """Offline read-only reporter over the SQLite event log."""

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path or default_events_db_path()

    def generate(self, session_id: str) -> SessionReportData | None:
        if not self._db_path.exists():
            return None
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            # Session metadata
            meta = conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if meta is None:
                return None

            # All events for the session
            rows = conn.execute(
                "SELECT * FROM events WHERE session_id = ? ORDER BY ts, rowid",
                (session_id,),
            ).fetchall()

        return self._build_report(meta, rows)

    def list_recent(self, limit: int = 20) -> list[dict[str, Any]]:
        if not self._db_path.exists():
            return []
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM sessions ORDER BY last_ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "session_id": r["session_id"],
                "agent_id": r["agent_id"],
                "started_at": _fmt_ts(r["started_ts"]),
                "ended_at": _fmt_ts(r["last_ts"]),
                "event_count": r["event_count"],
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Internal builders
    # ------------------------------------------------------------------

    def _build_report(
        self, meta: sqlite3.Row, rows: list[sqlite3.Row]
    ) -> SessionReportData:
        started = meta["started_ts"]
        last = meta["last_ts"]
        report = SessionReportData(
            session_id=meta["session_id"],
            agent_id=meta["agent_id"],
            started_at=_fmt_ts(started),
            ended_at=_fmt_ts(last),
            duration_seconds=round(last - started, 2),
            event_count=meta["event_count"],
        )

        turns: list[TurnReport] = []
        current_turn: TurnReport | None = None
        total_cost = 0.0
        llm_calls = 0
        tool_calls = 0

        for row in rows:
            etype = row["type"]
            payload = json.loads(row["payload"])

            if etype == "user_message":
                current_turn = TurnReport(
                    turn_index=len(turns) + 1,
                    user_message=payload.get("content", ""),
                )
                turns.append(current_turn)

            elif etype == "llm_response" and current_turn is not None:
                current_turn.llm = LLMCallReport(
                    model=payload.get("model", ""),
                    content_preview=_preview(payload.get("content", "")),
                    prompt_tokens=payload.get("prompt_tokens", 0),
                    completion_tokens=payload.get("completion_tokens", 0),
                    latency_ms=round(payload.get("latency_ms", 0.0), 1),
                )
                llm_calls += 1

            elif etype == "tool_invocation_finished" and current_turn is not None:
                current_turn.tools.append(
                    ToolInvocationReport(
                        name=payload.get("name", ""),
                        args_preview=_preview(json.dumps(payload.get("args", {}))),
                        ok=payload.get("ok", False),
                        error=payload.get("error"),
                        latency_ms=round(payload.get("latency_ms", 0.0), 1),
                    )
                )
                tool_calls += 1

            elif etype == "grader_verdict" and current_turn is not None:
                current_turn.grader = GraderReport(
                    score=round(payload.get("score", 0.0), 2),
                    quality=payload.get("quality", ""),
                    side_effect_observable=payload.get("side_effect_observable", False),
                )

            elif etype in ("skill_promoted", "skill_rolled_back", "skill_candidate_proposed"):
                report.evolution_events.append(
                    EvolutionEvent(
                        type=etype,
                        skill_name=payload.get("skill_name", ""),
                        detail=json.dumps(payload)[:200],
                        ts=row["ts"],
                    )
                )

            elif etype == "anti_req_violation":
                report.violations.append(payload.get("message", etype))

            elif etype == "cost_tick":
                total_cost += payload.get("cost_usd", 0.0)

        report.turns = turns
        report.cost_summary = {
            "total_usd": round(total_cost, 6),
            "llm_calls": llm_calls,
            "tool_calls": tool_calls,
        }
        return report


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------


def format_markdown(report: SessionReportData) -> str:
    lines: list[str] = [
        f"# Session Report: {report.session_id}",
        f"**Agent**: {report.agent_id} | **Duration**: {report.duration_seconds}s | **Events**: {report.event_count}",
        "",
    ]

    for turn in report.turns:
        lines.append(f"## Turn {turn.turn_index}")
        lines.append(f"**User**: {turn.user_message}")
        if turn.llm:
            llm = turn.llm
            lines.append(
                f"**LLM**: {llm.model or 'unknown'} | "
                f"{llm.prompt_tokens} → {llm.completion_tokens} tokens | "
                f"{llm.latency_ms}ms"
            )
            if llm.content_preview:
                lines.append(f"> {llm.content_preview}")
        if turn.tools:
            lines.append("**Tools**:")
            lines.append("| Name | Args | Result | Latency |")
            lines.append("|------|------|--------|---------|")
            for t in turn.tools:
                status = "✅ ok" if t.ok else f"❌ {t.error or 'failed'}"
                lines.append(f"| {t.name} | {t.args_preview} | {status} | {t.latency_ms}ms |")
        if turn.grader:
            g = turn.grader
            emoji = "✅" if g.score >= 0.7 else ("⚠️" if g.score >= 0.4 else "❌")
            lines.append(f"**Grader**: {g.score} {emoji} (quality={g.quality}, observable={g.side_effect_observable})")
        lines.append("")

    if report.evolution_events:
        lines.append("## Evolution Events")
        for ev in report.evolution_events:
            lines.append(f"- `{ev.type}`: {ev.skill_name or ev.detail} @ {_fmt_ts(ev.ts)}")
        lines.append("")

    if report.violations:
        lines.append("## Violations")
        for v in report.violations:
            lines.append(f"- ⚠️ {v}")
        lines.append("")

    cs = report.cost_summary
    lines.append("## Cost Summary")
    lines.append(f"- **Total**: ${cs.get('total_usd', 0)}")
    lines.append(f"- **LLM calls**: {cs.get('llm_calls', 0)}")
    lines.append(f"- **Tool calls**: {cs.get('tool_calls', 0)}")

    return "\n".join(lines)


def format_json(report: SessionReportData) -> str:
    return json.dumps(asdict(report), indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fmt_ts(ts: float) -> str:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def _preview(text: str, max_len: int = 120) -> str:
    s = str(text).replace("\n", " ")
    if len(s) > max_len:
        return s[: max_len - 3] + "..."
    return s


# ---------------------------------------------------------------------------
# CLI entry points (called from xmclaw.cli.main typer wiring)
# ---------------------------------------------------------------------------


def run_session_report(
    session_id: str,
    *,
    db: Path | None = None,
    as_json: bool = False,
) -> int:
    """Print a session report. Returns the process exit code.

    Keeps typer out of the generator surface so the same entry point can
    be reused from tests, other CLIs, or the future web UI.
    """
    gen = SessionReportGenerator(db)
    report = gen.generate(session_id)
    if report is None:
        db_path = db or default_events_db_path()
        if not db_path.exists():
            typer.echo(
                f"  [!]   no event log at {db_path} — run the daemon at least once",
                err=True,
            )
        else:
            typer.echo(
                f"  [x]  session '{session_id}' not found in {db_path}",
                err=True,
            )
        return 1
    typer.echo(format_json(report) if as_json else format_markdown(report))
    return 0


def run_session_list(
    *,
    db: Path | None = None,
    limit: int = 20,
    as_json: bool = False,
) -> int:
    """List recent sessions by last-activity timestamp."""
    gen = SessionReportGenerator(db)
    rows = gen.list_recent(limit=limit)
    if as_json:
        typer.echo(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    if not rows:
        db_path = db or default_events_db_path()
        if not db_path.exists():
            typer.echo(f"  [!]   no event log at {db_path} — nothing to list")
        else:
            typer.echo("  [!]   no sessions recorded yet")
        return 0
    typer.echo(
        f"  {'session_id':<36}  {'agent_id':<16}  {'started':<22}  "
        f"{'ended':<22}  events"
    )
    for r in rows:
        typer.echo(
            f"  {r['session_id']:<36}  {r['agent_id']:<16}  "
            f"{r['started_at']:<22}  {r['ended_at']:<22}  {r['event_count']}"
        )
    return 0
