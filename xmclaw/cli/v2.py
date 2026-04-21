"""v2 CLI subcommands — isolated namespace during strangler-fig transition.

Use ``xmclaw v2 <cmd>`` for v2 functionality. v1 commands on the top level
still work unchanged. As v2 reaches parity, top-level commands will be
rewritten to target v2 and the ``v2`` prefix dropped.
"""
from __future__ import annotations

import asyncio
import typer

from xmclaw import __version__
from xmclaw.core.bus import (
    BehavioralEvent,
    EventType,
    InProcessEventBus,
    make_event,
)
from xmclaw.core.bus.memory import accept_all

app = typer.Typer(help="XMclaw v2 commands (self-evolving runtime, in development)")


@app.command()
def version() -> None:
    """Print the v2 runtime version."""
    typer.echo(f"xmclaw v{__version__}")


@app.command()
def ping() -> None:
    """End-to-end smoke test — publish a BehavioralEvent, subscribe, observe.

    Exits 0 if the bus wires up correctly, 1 otherwise. This is the minimum
    signal that the v2 skeleton is intact; it's what CI runs before anything
    else gets exercised.
    """
    received: list[BehavioralEvent] = []

    async def _subscriber(event: BehavioralEvent) -> None:
        received.append(event)

    async def _run() -> int:
        bus = InProcessEventBus()
        bus.subscribe(accept_all, _subscriber)

        event = make_event(
            session_id="ping-session",
            agent_id="ping-agent",
            type=EventType.SESSION_LIFECYCLE,
            payload={"phase": "create", "via": "xmclaw v2 ping"},
        )
        await bus.publish(event)
        await bus.drain()

        if len(received) != 1:
            typer.echo(f"FAIL: expected 1 event, got {len(received)}", err=True)
            return 1
        got = received[0]
        if got.id != event.id or got.type != EventType.SESSION_LIFECYCLE:
            typer.echo(f"FAIL: event round-trip mismatch: {got}", err=True)
            return 1

        typer.echo(f"OK — bus received event id={got.id} type={got.type.value}")
        return 0

    raise typer.Exit(code=asyncio.run(_run()))


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="Bind address (keep loopback until anti-req #8 auth lands)."),
    port: int = typer.Option(8766, help="Port to bind."),
    config: str = typer.Option(
        "daemon/config.json",
        help=("Path to config JSON (read for LLM provider key). "
              "Falls back to echo-mode if absent or empty."),
    ),
    reload: bool = typer.Option(False, help="Uvicorn auto-reload (dev only)."),
) -> None:
    """Start the v2 daemon (FastAPI + WebSocket).

    Connect a client to ``ws://{host}:{port}/agent/v2/{session_id}``.
    Send frames like ``{"type": "user", "content": "hello"}`` and
    receive BehavioralEvent frames back.

    If ``config`` points to a valid JSON file with an LLM ``api_key``
    set, the daemon starts with a full AgentLoop (LLM ↔ tool). Missing
    or key-less config starts the daemon in echo mode.
    """
    import uvicorn
    from pathlib import Path as _Path
    from xmclaw.core.bus import InProcessEventBus
    from xmclaw.daemon.app_v2 import create_app as _create_app
    from xmclaw.daemon.factory import ConfigError, build_agent_from_config, load_config

    bus = InProcessEventBus()
    cfg_path = _Path(config)
    agent = None
    if cfg_path.exists():
        try:
            cfg = load_config(cfg_path)
            agent = build_agent_from_config(cfg, bus)
            if agent is None:
                typer.echo(
                    f"  ⚠ config has no LLM api_key set — running in echo mode"
                )
            else:
                model = getattr(agent._llm, "model", "?")
                typer.echo(f"  ✓ loaded config: agent LLM = {model}")
        except ConfigError as exc:
            typer.echo(f"  ⚠ config error — running in echo mode: {exc}", err=True)
    else:
        typer.echo(f"  ⚠ config not found at {cfg_path} — running in echo mode")

    typer.echo(f"xmclaw v{__version__} — binding ws://{host}:{port}")
    typer.echo(f"  health:  http://{host}:{port}/health")
    typer.echo(f"  session: ws://{host}:{port}/agent/v2/<session_id>")

    # Build the app locally so the agent (if any) is wired in.
    app_instance = _create_app(bus=bus, agent=agent)
    uvicorn.run(app_instance, host=host, port=port, log_level="info")
