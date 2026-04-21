"""XMclaw CLI — top-level entry point.

Subcommands:

    xmclaw version   Print the runtime version.
    xmclaw ping      Bus round-trip smoke test.
    xmclaw serve     Start the daemon (FastAPI + WS + optional web UI).
    xmclaw chat      Interactive REPL that talks to a running daemon.
    xmclaw doctor    Diagnose a local setup without running anything.

v1 was deleted wholesale in Phase 4.10; there's now only one CLI.
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

app = typer.Typer(help="XMclaw — local-first, self-evolving AI agent runtime")


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
            payload={"phase": "create", "via": "xmclaw ping"},
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
    host: str = typer.Option("127.0.0.1", help="Bind address."),
    port: int = typer.Option(8766, help="Port to bind."),
    config: str = typer.Option(
        "daemon/config.json",
        help=("Path to config JSON (read for LLM provider key). "
              "Falls back to echo-mode if absent or empty."),
    ),
    no_auth: bool = typer.Option(
        False,
        "--no-auth",
        help=(
            "DANGEROUS: skip pairing-token validation. Only safe on a "
            "strictly trusted local machine with no browser usage."
        ),
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
    from xmclaw.daemon.app import create_app as _create_app
    from xmclaw.daemon.factory import ConfigError, build_agent_from_config, load_config
    from xmclaw.daemon.pairing import (
        default_token_path, load_or_create_token, validate_token,
    )

    bus = InProcessEventBus()

    # ── Anti-req #8 pairing setup ──
    auth_check = None
    if not no_auth:
        token_path = default_token_path()
        token = load_or_create_token(token_path)

        async def _auth(presented: str | None) -> bool:
            return validate_token(token, presented)
        auth_check = _auth
        typer.echo(f"  [ok]  pairing token: {token_path}")
    else:
        typer.echo(f"  [!]   --no-auth: anyone on this machine can connect")

    cfg_path = _Path(config)
    agent = None
    if cfg_path.exists():
        try:
            cfg = load_config(cfg_path)
            agent = build_agent_from_config(cfg, bus)
            if agent is None:
                typer.echo(
                    f"  [!]   config has no LLM api_key set -- running in echo mode"
                )
            else:
                model = getattr(agent._llm, "model", "?")
                typer.echo(f"  [ok]  loaded config: agent LLM = {model}")
                # Surface the tools posture so the admin can see it.
                if agent._tools is not None:
                    specs = agent._tools.list_tools()
                    tool_names = ", ".join(s.name for s in specs)
                    allowlist = cfg.get("tools", {}).get("allowed_dirs", [])
                    typer.echo(
                        f"  [ok]  tools enabled: {tool_names}"
                    )
                    typer.echo(
                        f"        allowed dirs: {allowlist}"
                    )
                else:
                    typer.echo(f"  [!]   tools disabled (no 'tools' section in config)")
        except ConfigError as exc:
            typer.echo(f"  [!]   config error -- running in echo mode: {exc}", err=True)
    else:
        typer.echo(f"  [!]   config not found at {cfg_path} -- running in echo mode")

    typer.echo(f"xmclaw v{__version__} -- binding ws://{host}:{port}")
    typer.echo(f"  health:  http://{host}:{port}/health")
    typer.echo(f"  session: ws://{host}:{port}/agent/v2/<session_id>")
    typer.echo(f"  web ui:  http://{host}:{port}/")

    # Build the app locally so the agent (if any) is wired in.
    app_instance = _create_app(bus=bus, agent=agent, auth_check=auth_check)
    uvicorn.run(app_instance, host=host, port=port, log_level="info")


@app.command()
def chat(
    url: str = typer.Option(
        "ws://127.0.0.1:8766/agent/v2/{session_id}",
        help=(
            "Daemon WS URL. ``{session_id}`` in the URL is substituted "
            "by the chosen / generated session id."
        ),
    ),
    session_id: str = typer.Option(
        "", help="Session id (auto-generated if empty).",
    ),
    token: str = typer.Option(
        "",
        help=(
            "Pairing token. Empty = read from the default pairing file "
            "(same location xmclaw serve writes)."
        ),
    ),
    no_auth: bool = typer.Option(
        False, "--no-auth", help="Skip pairing token (daemon must also be --no-auth).",
    ),
) -> None:
    """Interactive REPL that talks to a running v2 daemon.

    Connects to the daemon's WebSocket, prompts for user input, and
    renders the event stream back as a readable conversation.

    Start a daemon in another terminal first:

        xmclaw serve

    Then in this terminal:

        xmclaw chat
    """
    from xmclaw.cli.chat import run_chat
    from xmclaw.daemon.pairing import default_token_path

    effective_token: str | None
    if no_auth:
        effective_token = None
    elif token:
        effective_token = token
    else:
        p = default_token_path()
        if p.exists():
            effective_token = p.read_text(encoding="utf-8").strip()
        else:
            typer.echo(
                f"  [!]   no pairing token at {p} -- start the daemon first "
                f"(`xmclaw serve` creates one), or pass --no-auth "
                f"if the daemon is running with --no-auth.",
                err=True,
            )
            raise typer.Exit(code=2)

    exit_code = run_chat(
        url=url,
        session_id=session_id or None,
        token=effective_token,
    )
    raise typer.Exit(code=exit_code)


@app.command()
def doctor(
    config: str = typer.Option(
        "daemon/config.json", help="Path to config JSON.",
    ),
    host: str = typer.Option("127.0.0.1", help="Daemon host to probe."),
    port: int = typer.Option(8766, help="Daemon port to probe."),
    no_daemon_probe: bool = typer.Option(
        False, "--no-daemon-probe",
        help="Skip the HTTP health probe (offline mode).",
    ),
) -> None:
    """Diagnose a v2 setup: config, LLM key, tools, pairing, port, daemon.

    Runs a sequence of checks without starting the daemon. Each check
    prints one line with a verdict. Exits 0 if every check passes, 1
    if any critical check fails (so CI or shell scripts can use
    ``xmclaw doctor && xmclaw serve``).
    """
    from xmclaw.cli.doctor import run_doctor
    from pathlib import Path as _Path

    results = run_doctor(
        _Path(config),
        host=host, port=port,
        probe_daemon=not no_daemon_probe,
    )
    typer.echo("xmclaw doctor --")
    for r in results:
        typer.echo(r.render())
    critical_fail = any(not r.ok for r in results)
    raise typer.Exit(code=1 if critical_fail else 0)
