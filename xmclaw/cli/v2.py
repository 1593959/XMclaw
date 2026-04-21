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
