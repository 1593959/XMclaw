"""Chat CLI — ``xmclaw chat [--plain] [--session ID]``."""
from __future__ import annotations

import typer

chat_app = typer.Typer(help="Launch interactive chat (TUI by default)")


@chat_app.callback(invoke_without_command=True)
def chat(
    plain: bool = typer.Option(False, "--plain", help="Use basic stdin/stdout instead of TUI"),
    session: str | None = typer.Option(None, "--session", help="Resume a session ID"),
    daemon_url: str = typer.Option("ws://127.0.0.1:8765/agent/v2/default", "--url"),
) -> None:
    """Launch XMclaw chat interface."""
    if plain:
        _run_plain_chat(daemon_url, session)
    else:
        _run_tui(daemon_url, session)


def _run_tui(url: str, session_id: str | None) -> None:
    try:
        from xmclaw.tui import JarvisTUI
    except ImportError as exc:
        typer.secho(f"TUI dependencies missing: {exc}", fg=typer.colors.RED, err=True)
        typer.secho("Run: pip install textual websockets", fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(1)

    app = JarvisTUI(daemon_ws_url=url, session_id=session_id)
    app.run()


def _run_plain_chat(url: str, session_id: str | None) -> None:
    import asyncio
    import json

    import typer

    sid = session_id or f"cli_{asyncio.get_event_loop().time():.0f}"
    typer.echo(f"Session: {sid}")
    typer.echo("Type your message (Ctrl+C to quit).\n")

    async def _loop() -> None:
        try:
            import websockets
        except ImportError:
            typer.echo("websockets not installed — pip install websockets", err=True)
            return
        async with websockets.connect(url) as ws:
            while True:
                try:
                    text = typer.prompt("You")
                except (EOFError, KeyboardInterrupt):
                    break
                await ws.send(json.dumps({
                    "action": "submit",
                    "session_id": sid,
                    "message": text,
                }))
                # Simple blocking read of one response.
                raw = await ws.recv()
                msg = json.loads(raw)
                payload = msg.get("payload", {})
                typer.echo(f"Agent: {payload.get('content', payload)}")

    asyncio.run(_loop())
