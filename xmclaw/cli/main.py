"""XMclaw CLI -- top-level entry point.

Subcommands:

    xmclaw version   Print the runtime version.
    xmclaw ping      Bus round-trip smoke test.
    xmclaw serve     Foreground daemon (blocks; uvicorn.run).
    xmclaw start     Spawn the daemon detached; returns once healthy.
    xmclaw stop      Stop a running daemon (via PID file).
    xmclaw restart   Stop then start.
    xmclaw status    Report daemon state (running / stale / dead).
    xmclaw tools     List the tools wired up from config.
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

# ``xmclaw memory <subcommand>`` — grouped under a sub-typer so future
# siblings (prune, forget, etc.) can land next to ``stats`` without
# polluting the top-level help.
memory_app = typer.Typer(
    help="Inspect and maintain the agent's SQLite-vec memory store.",
)
app.add_typer(memory_app, name="memory")


def _default_memory_db_path():
    """Delegates to :func:`xmclaw.utils.paths.default_memory_db_path` so
    ``XMC_DATA_DIR`` moves this file along with the rest of the workspace."""
    from xmclaw.utils.paths import default_memory_db_path
    return default_memory_db_path()


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
    port: int = typer.Option(8765, help="Port to bind."),
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
    from xmclaw.core.bus import SqliteEventBus, default_events_db_path
    from xmclaw.daemon.app import create_app as _create_app
    from xmclaw.daemon.factory import ConfigError, build_agent_from_config, load_config
    from xmclaw.daemon.pairing import (
        default_token_path, load_or_create_token, validate_token,
    )

    # Epic #13: persistent event log. Subscribers only see events after
    # the row is on disk, so a crash mid-publish can't silently desync the
    # agent loop from what the UI replays on reconnect.
    events_db = default_events_db_path()
    bus = SqliteEventBus(events_db)
    typer.echo(f"  [ok]  event log: {events_db}")

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
def start(
    host: str = typer.Option("127.0.0.1", help="Bind address."),
    port: int = typer.Option(8765, help="Port to bind."),
    config: str = typer.Option(
        "daemon/config.json", help="Path to config JSON.",
    ),
    no_auth: bool = typer.Option(
        False, "--no-auth",
        help="DANGEROUS: skip pairing-token validation.",
    ),
    wait: float = typer.Option(
        10.0, help="Seconds to wait for /health before giving up.",
    ),
) -> None:
    """Spawn the daemon in the background, return once /health answers.

    Writes a PID file at ``~/.xmclaw/v2/daemon.pid`` and a log at
    ``~/.xmclaw/v2/daemon.log``. Use ``xmclaw stop`` to kill it, or
    ``xmclaw status`` to check on it.
    """
    from xmclaw.daemon.lifecycle import start_daemon
    try:
        status = start_daemon(
            host=host, port=port, config=config,
            no_auth=no_auth, wait_seconds=wait,
        )
    except RuntimeError as exc:
        typer.echo(f"  [x]  {exc}", err=True)
        raise typer.Exit(code=1)
    typer.echo(
        f"  [ok]  daemon started pid={status.pid} "
        f"http://{status.host}:{status.port}"
    )


@app.command()
def stop(
    grace: float = typer.Option(
        5.0, help="Seconds to wait for graceful shutdown before SIGKILL.",
    ),
) -> None:
    """Stop the daemon referenced by the PID file."""
    from xmclaw.daemon.lifecycle import read_status, stop_daemon
    before = read_status()
    if before.state == "dead":
        typer.echo("  [!]   no daemon recorded -- nothing to stop")
        raise typer.Exit(code=0)
    after = stop_daemon(grace_seconds=grace)
    if after.state == "dead":
        typer.echo(f"  [ok]  daemon stopped (was pid={before.pid})")
    else:
        typer.echo(
            f"  [!]   daemon state after stop: {after.state} pid={after.pid}",
            err=True,
        )
        raise typer.Exit(code=1)


@app.command()
def restart(
    host: str = typer.Option("127.0.0.1", help="Bind address."),
    port: int = typer.Option(8765, help="Port to bind."),
    config: str = typer.Option(
        "daemon/config.json", help="Path to config JSON.",
    ),
    no_auth: bool = typer.Option(False, "--no-auth"),
    grace: float = typer.Option(5.0),
    wait: float = typer.Option(10.0),
) -> None:
    """Stop (if running) then start. Idempotent."""
    from xmclaw.daemon.lifecycle import read_status, start_daemon, stop_daemon
    before = read_status()
    if before.state != "dead":
        stop_daemon(grace_seconds=grace)
        typer.echo(f"  [ok]  stopped previous daemon (was pid={before.pid})")
    try:
        status = start_daemon(
            host=host, port=port, config=config,
            no_auth=no_auth, wait_seconds=wait,
        )
    except RuntimeError as exc:
        typer.echo(f"  [x]  {exc}", err=True)
        raise typer.Exit(code=1)
    typer.echo(
        f"  [ok]  daemon restarted pid={status.pid} "
        f"http://{status.host}:{status.port}"
    )


@app.command()
def status() -> None:
    """Report whether a daemon is running, stale, or absent."""
    from xmclaw.daemon.lifecycle import read_status
    s = read_status()
    if s.state == "running":
        health = "healthy" if s.healthy else "not answering /health"
        typer.echo(
            f"  [ok]  running  pid={s.pid}  "
            f"http://{s.host}:{s.port}  ({health})"
        )
    elif s.state == "stale":
        typer.echo(
            f"  [!]   stale  pid={s.pid} recorded but process is gone "
            f"-- run `xmclaw start` to relaunch",
            err=True,
        )
        raise typer.Exit(code=2)
    else:
        typer.echo("  [x]  no daemon running")
        raise typer.Exit(code=1)


@app.command()
def tools(
    config: str = typer.Option(
        "daemon/config.json", help="Path to config JSON.",
    ),
) -> None:
    """List the tools the agent would be wired with for this config.

    Reads the config exactly the way ``xmclaw serve`` / ``start`` would,
    builds the same ToolProvider, and prints each tool. Useful for
    catching "tools disabled, nothing happens" confusion before spending
    model tokens on a task the agent can't actually perform.
    """
    from pathlib import Path as _Path
    from xmclaw.daemon.factory import (
        ConfigError, build_tools_from_config, load_config,
    )

    cfg_path = _Path(config)
    if not cfg_path.exists():
        typer.echo(f"  [x]  config not found at {cfg_path}", err=True)
        raise typer.Exit(code=1)
    try:
        cfg = load_config(cfg_path)
        provider = build_tools_from_config(cfg)
    except ConfigError as exc:
        typer.echo(f"  [x]  config error: {exc}", err=True)
        raise typer.Exit(code=1)
    if provider is None:
        typer.echo(
            "  [!]   no 'tools' section in config -- agent runs LLM-only"
        )
        typer.echo(
            "        add 'tools': {'allowed_dirs': ['.']} to enable file_read / file_write"
        )
        return
    specs = provider.list_tools()
    allowed = cfg.get("tools", {}).get("allowed_dirs", [])
    typer.echo(f"  [ok]  {len(specs)} tool(s) configured, "
               f"{len(allowed)} allowed dir(s)")
    for spec in specs:
        typer.echo(f"    - {spec.name}: {spec.description}")
    typer.echo("  allowed dirs:")
    for d in allowed:
        typer.echo(f"    - {d}")


@app.command()
def chat(
    url: str = typer.Option(
        "ws://127.0.0.1:8765/agent/v2/{session_id}",
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
    port: int = typer.Option(8765, help="Daemon port to probe."),
    no_daemon_probe: bool = typer.Option(
        False, "--no-daemon-probe",
        help="Skip the HTTP health probe (offline mode).",
    ),
    network: bool = typer.Option(
        False, "--network",
        help="Probe reachability of configured LLM endpoints. Off by "
             "default so the doctor stays runnable on air-gapped machines.",
    ),
    discover_plugins: bool = typer.Option(
        False, "--discover-plugins",
        help="Load third-party checks from the 'xmclaw.doctor' entry-point group.",
    ),
    fix: bool = typer.Option(
        False, "--fix",
        help="Attempt to auto-remediate failing checks that advertise a fix.",
    ),
    json_output: bool = typer.Option(
        False, "--json",
        help="Emit machine-readable JSON instead of the text report.",
    ),
) -> None:
    """Diagnose a v2 setup: config, LLM key, tools, pairing, port, daemon.

    Runs a sequence of checks without starting the daemon. Each check
    prints one line with a verdict. Exits 0 if every check passes, 1
    if any critical check fails (so CI or shell scripts can use
    ``xmclaw doctor && xmclaw serve``). ``--json`` swaps the human
    output for a single JSON document so the exit code isn't the only
    machine-readable signal.

    ``--fix`` re-runs every red check that advertises ``fix_available``
    through its ``DoctorCheck.fix()`` hook and verifies the outcome; the
    final verdict reflects post-fix state.
    """
    import json as _json
    from pathlib import Path as _Path

    from xmclaw.cli.doctor_registry import (
        CheckResult as RegistryCheckResult,
        DoctorContext,
        build_default_registry,
    )

    registry = build_default_registry()
    plugin_errors: list[RegistryCheckResult] = []
    if discover_plugins:
        plugin_errors = registry.discover_plugins()

    ctx = DoctorContext(
        config_path=_Path(config),
        host=host, port=port,
        probe_daemon=not no_daemon_probe,
        probe_network=network,
    )
    check_results = registry.run_all(ctx)
    results: list[RegistryCheckResult] = plugin_errors + check_results

    fix_attempts: list = []
    if fix:
        fix_attempts = registry.run_fixes(ctx, results)
        # Swap each fixed result in-place so the final report reflects the
        # post-fix state. Attempts list keeps the before-view for the summary.
        id_to_index = {r.name: i for i, r in enumerate(results)}
        for att in fix_attempts:
            i = id_to_index.get(att.before.name)
            if i is not None:
                results[i] = att.after

    critical_fail = any(not r.ok for r in results)
    if json_output:
        typer.echo(_json.dumps({
            "ok": not critical_fail,
            "checks": [
                {
                    "name": r.name,
                    "ok": r.ok,
                    "detail": r.detail,
                    "advisory": r.advisory,
                    "fix_available": r.fix_available,
                }
                for r in results
            ],
            "fix_attempts": [
                {
                    "check_id": a.check_id,
                    "before_ok": a.before.ok,
                    "after_ok": a.after.ok,
                    "fix_raised": a.fix_raised,
                }
                for a in fix_attempts
            ],
        }, ensure_ascii=False, indent=2))
    else:
        typer.echo("xmclaw doctor --")
        for r in results:
            typer.echo(r.render())
        if fix_attempts:
            typer.echo("")
            typer.echo("fix attempts:")
            for a in fix_attempts:
                status = "resolved" if a.after.ok else "still failing"
                extra = f" (fix raised: {a.fix_raised})" if a.fix_raised else ""
                typer.echo(f"  - {a.check_id}: {status}{extra}")
    raise typer.Exit(code=1 if critical_fail else 0)


@memory_app.command("stats")
def memory_stats(
    db: str = typer.Option(
        "",
        help=(
            "Path to the memory DB. Empty = ~/.xmclaw/v2/memory.db (the "
            "daemon's default workspace location)."
        ),
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Emit machine-readable JSON instead of a table.",
    ),
) -> None:
    """Show per-layer memory occupancy: count, bytes, pinned, age range.

    Non-mutating -- opens the DB, reads aggregates, exits. When the DB
    doesn't exist yet (fresh install, no items ever stored), reports
    that cleanly instead of silently creating an empty file.
    """
    import json as _json
    from datetime import datetime, timezone
    from pathlib import Path as _Path

    from xmclaw.providers.memory.sqlite_vec import SqliteVecMemory

    db_path = _Path(db) if db else _default_memory_db_path()
    if not db_path.exists():
        if json_output:
            typer.echo(_json.dumps({
                "ok": True,
                "db_path": str(db_path),
                "exists": False,
                "layers": {},
            }, ensure_ascii=False, indent=2))
            return
        typer.echo(f"  [!]   no memory DB at {db_path}")
        typer.echo(
            "        nothing stored yet -- run the agent once, or pass "
            "--db PATH to point at a different location"
        )
        return

    mem = SqliteVecMemory(db_path)
    try:
        stats = asyncio.run(mem.stats())
    finally:
        mem.close()

    if json_output:
        typer.echo(_json.dumps({
            "ok": True,
            "db_path": str(db_path),
            "exists": True,
            "layers": stats,
        }, ensure_ascii=False, indent=2))
        return

    def _fmt_ts(ts: float | None) -> str:
        if ts is None:
            return "-"
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%SZ"
        )

    def _fmt_bytes(n: int) -> str:
        if n < 1024:
            return f"{n}B"
        if n < 1024 * 1024:
            return f"{n / 1024:.1f}KB"
        return f"{n / (1024 * 1024):.1f}MB"

    typer.echo(f"xmclaw memory stats -- {db_path}")
    typer.echo(
        f"  {'layer':<8}  {'count':>7}  {'bytes':>10}  {'pinned':>6}  "
        f"{'oldest':<21}  {'newest':<21}"
    )
    for layer in ("short", "working", "long"):
        s = stats[layer]
        typer.echo(
            f"  {layer:<8}  {s['count']:>7}  {_fmt_bytes(s['bytes']):>10}  "
            f"{s['pinned_count']:>6}  {_fmt_ts(s['oldest_ts']):<21}  "
            f"{_fmt_ts(s['newest_ts']):<21}"
        )


if __name__ == "__main__":
    app()
