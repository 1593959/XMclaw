"""``xmclaw trace`` — export + replay session traces (#6, 2026-06-15).

Top-tier harnesses let you export a full run and replay it offline for
debugging. XMclaw persists every event in ``events.db``; this CLI surfaces
it without a running daemon:

    xmclaw trace export <session_id>            # JSONL to stdout
    xmclaw trace export <session_id> -o run.jsonl
    xmclaw trace export <session_id> --timeline # human-readable
    xmclaw trace replay run.jsonl               # reconstruct from a file
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import typer

trace_app = typer.Typer(help="Export + replay session traces.")


@trace_app.command("export")
def export(
    session_id: str = typer.Argument(..., help="Session id to export."),
    out: str = typer.Option("", "--out", "-o", help="Write to FILE (default: stdout)."),
    timeline: bool = typer.Option(
        False, "--timeline", help="Human-readable timeline instead of JSONL.",
    ),
    db: str = typer.Option("", "--db", help="events.db path (default: ~/.xmclaw/v2/events.db)."),
) -> None:
    """Export a session's full event trace (reads events.db directly)."""
    from xmclaw.core.bus.replay import EventFilter, replay
    from xmclaw.core.bus.sqlite import event_as_jsonable
    from xmclaw.daemon.trace_replay import events_to_jsonl, reconstruct_timeline

    async def _collect() -> list[dict]:
        evs: list[dict] = []
        async for e in replay(
            "", EventFilter(session_id=session_id), db_path=(db or None),
        ):
            evs.append(event_as_jsonable(e))
        return evs

    try:
        events = asyncio.run(_collect())
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"trace export failed: {exc}", err=True)
        raise typer.Exit(1)
    if not events:
        typer.echo(f"(no events for session {session_id!r})", err=True)
        raise typer.Exit(1)

    body = reconstruct_timeline(events) if timeline else events_to_jsonl(events)
    if out:
        Path(out).write_text(body, encoding="utf-8")
        typer.echo(f"wrote {len(events)} events → {out}")
    else:
        typer.echo(body)


@trace_app.command("replay")
def replay_file(
    file: str = typer.Argument(..., help="Exported JSONL trace file."),
) -> None:
    """Reconstruct a human-readable timeline from an exported JSONL trace."""
    from xmclaw.daemon.trace_replay import jsonl_to_events, reconstruct_timeline

    p = Path(file)
    if not p.is_file():
        typer.echo(f"file not found: {file}", err=True)
        raise typer.Exit(1)
    events = jsonl_to_events(p.read_text(encoding="utf-8"))
    if not events:
        typer.echo("(no parseable events in file)", err=True)
        raise typer.Exit(1)
    typer.echo(reconstruct_timeline(events))


__all__ = ["trace_app"]
