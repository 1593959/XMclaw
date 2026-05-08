"""``xmclaw session`` subcommands (B-325 split).

Read-only window onto the SQLite event log. Lifted out of
``xmclaw/cli/main.py``.
"""
from __future__ import annotations

from pathlib import Path

import typer

# ``xmclaw session <subcommand>`` — Epic #4 Phase B. Read-only window onto
# the SQLite event log (``~/.xmclaw/v2/events.db``): ``report <id>``
# renders a single conversation as markdown / JSON, ``list`` enumerates
# recent sessions. Daemon can be down — the event log is authoritative.
session_app = typer.Typer(
    help="Inspect session event logs (turns, tools, grader verdicts, cost).",
)


@session_app.command("report")
def session_report(
    session_id: str = typer.Argument(
        ..., help="Session id to report on (e.g. 'sess-abc123').",
    ),
    db: str = typer.Option(
        "", "--db",
        help=(
            "Path to the events SQLite file. Empty = "
            "~/.xmclaw/v2/events.db (the daemon's default)."
        ),
    ),
    as_json: bool = typer.Option(
        False, "--json",
        help="Emit the report as JSON instead of markdown.",
    ),
) -> None:
    """Print a single session as markdown: turns, tools, grader, cost.

    Exits 1 if the event log is missing or the session id isn't known.
    Run ``xmclaw session list`` first if you don't know the id.
    """
    from xmclaw.cli.session_report import run_session_report as _run
    db_path = Path(db) if db else None
    raise typer.Exit(code=_run(session_id, db=db_path, as_json=as_json))


@session_app.command("list")
def session_list(
    db: str = typer.Option(
        "", "--db",
        help="Events DB path. Empty = ~/.xmclaw/v2/events.db.",
    ),
    limit: int = typer.Option(
        20, "--limit", "-n",
        help="Max rows to print. Default 20.",
    ),
    as_json: bool = typer.Option(
        False, "--json",
        help="Emit a stable JSON array (scripting-friendly).",
    ),
) -> None:
    """List recent sessions (newest first) from the SQLite event log."""
    from xmclaw.cli.session_report import run_session_list as _run
    db_path = Path(db) if db else None
    raise typer.Exit(code=_run(db=db_path, limit=limit, as_json=as_json))
