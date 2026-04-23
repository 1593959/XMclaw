"""XMclaw CLI -- top-level entry point.

Subcommands:

    xmclaw version                  Print the runtime version.
    xmclaw ping                     Bus round-trip smoke test.
    xmclaw serve                    Foreground daemon (blocks; uvicorn.run).
    xmclaw start                    Spawn the daemon detached; returns once healthy.
    xmclaw stop                     Stop a running daemon (via PID file).
    xmclaw restart                  Stop then start.
    xmclaw status                   Report daemon state (running / stale / dead).
    xmclaw tools                    List the tools wired up from config.
    xmclaw chat                     Interactive REPL that talks to a running daemon.
    xmclaw doctor                   Diagnose a local setup without running anything.
    xmclaw memory stats             Per-layer memory occupancy.
    xmclaw config init              Write a daemon/config.json skeleton.
    xmclaw config set <key> <val>   Mutate a dotted key in config.json.

v1 was deleted wholesale in Phase 4.10; there's now only one CLI.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

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

# ``xmclaw config <subcommand>`` — ``init`` writes a fresh daemon/config.json,
# ``set`` mutates a dotted key. README has been advertising both since v2
# rewrite; this typer group is what makes those promises real.
config_app = typer.Typer(
    help="Create or tweak daemon/config.json (LLM keys, tools, gateway).",
)
app.add_typer(config_app, name="config")

# ``xmclaw backup <subcommand>`` — Epic #20. ``create`` / ``list`` /
# ``restore`` of the ``~/.xmclaw/`` workspace into portable tar.gz
# archives. Not an alias for ``git`` or ``rsync``: the archive format is
# versioned via ``manifest.json`` so cross-version restores are safe.
backup_app = typer.Typer(
    help="Create, list, and restore backups of the ~/.xmclaw/ workspace.",
)
app.add_typer(backup_app, name="backup")


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


# ── config subcommands ──────────────────────────────────────────────────


def _default_config_template() -> dict:
    """Thin wrapper around the shared template so this module keeps its
    import surface stable. See :mod:`xmclaw.cli.config_template`."""
    from xmclaw.cli.config_template import default_config_template
    return default_config_template()


def _parse_dotted_value(raw: str):
    """``config set`` argument parsing: JSON literal first, then string.

    ``xmclaw config set gateway.port 9000`` -> int 9000.
    ``xmclaw config set llm.anthropic.api_key sk-ant-xxx`` -> string.
    ``xmclaw config set evolution.enabled true`` -> bool True.
    """
    import json as _json
    try:
        return _json.loads(raw)
    except _json.JSONDecodeError:
        return raw


@config_app.command("init")
def config_init(
    path: str = typer.Option(
        "daemon/config.json", "--path",
        help="Where to write the config (default: daemon/config.json).",
    ),
    provider: str = typer.Option(
        "", "--provider",
        help="Optional: pre-set the default LLM provider ('anthropic' or 'openai').",
    ),
    api_key: str = typer.Option(
        "", "--api-key",
        help="Optional: populate the chosen provider's api_key non-interactively.",
    ),
    force: bool = typer.Option(
        False, "--force",
        help="Overwrite an existing config file.",
    ),
) -> None:
    """Write a fresh daemon/config.json skeleton.

    The skeleton covers the three sections the daemon needs to boot:
    ``llm`` (with empty api_key placeholders for both providers),
    ``gateway``, and ``security.prompt_injection``. Anything else
    (``tools``, ``memory``, ``evolution``, ``mcp_servers``,
    ``integrations``) defaults at daemon level and can be added by hand
    from ``daemon/config.example.json`` when you need it.

    Refuses to overwrite an existing file unless ``--force`` is passed,
    so re-running this command is always safe.
    """
    import json as _json
    from pathlib import Path as _Path

    target = _Path(path)
    if target.exists() and not force:
        typer.echo(
            f"  [!]   config already exists at {target}", err=True,
        )
        typer.echo(
            "        pass --force to overwrite, or use "
            "'xmclaw config set <key> <value>' to edit in place",
            err=True,
        )
        raise typer.Exit(code=1)

    if provider and provider not in ("anthropic", "openai"):
        typer.echo(
            f"  [x]  unknown provider '{provider}' "
            "(expected 'anthropic' or 'openai')",
            err=True,
        )
        raise typer.Exit(code=2)

    template = _default_config_template()
    if provider:
        template["llm"]["default_provider"] = provider
        if api_key:
            template["llm"][provider]["api_key"] = api_key

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        _json.dumps(template, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    typer.echo(f"  [ok]  wrote {target}")
    if not api_key:
        typer.echo(
            "        next: set an LLM api_key -- e.g. "
            "'xmclaw config set llm.anthropic.api_key sk-ant-...' "
            "or edit the file directly"
        )
    typer.echo("        then run 'xmclaw doctor' to verify")


@config_app.command("set")
def config_set(
    key: str = typer.Argument(
        ..., help="Dotted key path, e.g. 'llm.anthropic.api_key'.",
    ),
    value: str = typer.Argument(
        ..., help="Value. Parsed as JSON when valid (true/false/123/[...]); "
                  "otherwise treated as a string.",
    ),
    path: str = typer.Option(
        "daemon/config.json", "--path",
        help="Config file to mutate (default: daemon/config.json).",
    ),
) -> None:
    """Set one dotted key in a config JSON file.

    Creates intermediate objects as needed. Refuses to touch a missing
    file (run 'xmclaw config init' first) or one that isn't a JSON
    object at its root -- the daemon factory expects ``dict`` and
    nothing else.
    """
    import json as _json
    from pathlib import Path as _Path

    target = _Path(path)
    if not target.exists():
        typer.echo(
            f"  [x]  no config at {target} -- run 'xmclaw config init' first",
            err=True,
        )
        raise typer.Exit(code=1)

    try:
        data = _json.loads(target.read_text(encoding="utf-8"))
    except _json.JSONDecodeError as exc:
        typer.echo(f"  [x]  {target} is not valid JSON: {exc}", err=True)
        raise typer.Exit(code=1)
    if not isinstance(data, dict):
        typer.echo(
            f"  [x]  {target} must have a JSON object at its root, "
            f"got {type(data).__name__}",
            err=True,
        )
        raise typer.Exit(code=1)

    parts = [p for p in key.split(".") if p]
    if not parts:
        typer.echo("  [x]  key must be non-empty", err=True)
        raise typer.Exit(code=2)

    parsed_value = _parse_dotted_value(value)

    cursor = data
    for segment in parts[:-1]:
        existing = cursor.get(segment)
        if not isinstance(existing, dict):
            # Either missing or a scalar that we need to overwrite with a
            # dict to make room for the nested key. Overwriting a scalar
            # mid-path is deliberate: 'config set llm.anthropic.x 1'
            # against a config where 'llm.anthropic' was accidentally set
            # to "" should recover rather than error-out.
            cursor[segment] = {}
            existing = cursor[segment]
        cursor = existing
    cursor[parts[-1]] = parsed_value

    target.write_text(
        _json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    typer.echo(f"  [ok]  {target}: {key} = {_json.dumps(parsed_value)}")


# ── xmclaw config set-secret / get-secret / delete-secret / list-secrets ──
# Epic #16 Phase 1 entry. Thin shell over xmclaw.utils.secrets. Kept in
# the config_app group so users find all credential-management commands
# together. Writes default to the file backend; callers can opt into
# the OS keyring with --backend keyring (requires `keyring` installed).


@config_app.command("set-secret")
def config_set_secret(
    name: str = typer.Argument(
        ..., help="Secret name, e.g. 'llm.anthropic.api_key'.",
    ),
    value: str = typer.Option(
        None, "--value",
        help=(
            "Plaintext value. Omit to read from stdin (safer — value "
            "does not land in shell history)."
        ),
    ),
    backend: str = typer.Option(
        "file", "--backend",
        help="Where to store: 'file' (~/.xmclaw/secrets.json) or 'keyring'.",
    ),
) -> None:
    """Store a secret in the chosen backend."""
    import sys as _sys

    from xmclaw.utils.secrets import set_secret

    if value is None:
        # Read from stdin without echoing to avoid shell-history leaks.
        # getpass doesn't work reliably when stdin isn't a tty (CI),
        # so fall back to a line-read there.
        if _sys.stdin.isatty():
            import getpass

            value = getpass.getpass(f"value for {name}: ")
        else:
            value = _sys.stdin.readline().rstrip("\n")
    if not value:
        typer.echo("  [x]  empty value refused", err=True)
        raise typer.Exit(code=2)
    try:
        set_secret(name, value, backend=backend)  # type: ignore[arg-type]
    except (RuntimeError, ValueError) as exc:
        typer.echo(f"  [x]  {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"  [ok]  stored {name} in {backend} backend")


@config_app.command("get-secret")
def config_get_secret(
    name: str = typer.Argument(..., help="Secret name to resolve."),
    reveal: bool = typer.Option(
        False, "--reveal",
        help=(
            "Print the plaintext value. Default only prints the source "
            "backend and a preview — safer for screen-share / logs."
        ),
    ),
) -> None:
    """Resolve a secret through env > file > keyring precedence."""
    from xmclaw.utils.secrets import _env_var_for, get_secret

    val = get_secret(name)
    if val is None:
        typer.echo(
            f"  [!]  {name} not set "
            f"(tried env {_env_var_for(name)}, secrets.json, keyring)"
        )
        raise typer.Exit(code=1)
    if reveal:
        typer.echo(val)
        return
    # Non-reveal: show length + first 2/last 2 chars so the user can
    # tell "did I set the right key" without leaking the full secret.
    if len(val) <= 4:
        preview = "*" * len(val)
    else:
        preview = f"{val[:2]}{'*' * (len(val) - 4)}{val[-2:]}"
    typer.echo(f"  [ok]  {name}: {preview}  (len={len(val)})")


@config_app.command("delete-secret")
def config_delete_secret(
    name: str = typer.Argument(..., help="Secret name to delete."),
) -> None:
    """Remove a secret from file and keyring layers (env is read-only)."""
    from xmclaw.utils.secrets import delete_secret

    deleted = delete_secret(name)
    if deleted:
        typer.echo(f"  [ok]  removed {name}")
    else:
        typer.echo(f"  [!]  {name} was not set in any writable backend")


@config_app.command("list-secrets")
def config_list_secrets() -> None:
    """List the names of secrets in the file backend."""
    from xmclaw.utils.secrets import (
        iter_env_override_names,
        list_secret_names,
        secrets_file_path,
    )

    names = list_secret_names()
    if not names:
        typer.echo(f"no secrets at {secrets_file_path()}")
        return
    env_overrides = set(iter_env_override_names())
    for n in names:
        marker = "  (overridden by env)" if n in env_overrides else ""
        typer.echo(f"  {n}{marker}")


# ── xmclaw config show ────────────────────────────────────────────────
# Epic #16 Phase 1 complement: read the daemon config and dump it with
# sensitive fields masked. Most users reach for ``cat daemon/config.json``
# today — that's fine alone but dangerous during screenshare / paste-into-
# chat. This command gives a safe-by-default view and an explicit
# ``--reveal`` for when full content is actually needed.
#
# Masking is *path-based* (key names match a denylist) rather than value-
# based (entropy / format sniffing) so it doesn't depend on the secret's
# shape — a custom-format self-hosted key stays masked too.

_SENSITIVE_KEY_SUFFIXES = (
    "api_key",
    "apikey",
    "token",
    "secret",
    "password",
    "passwd",
    "access_key",
    "private_key",
)
"""Lowercase key-name suffixes whose values are masked by ``config show``.

Path-based matching: a key is sensitive when its *leaf* name ends in one
of these (case-insensitive). Intermediate nodes like ``auth`` are never
masked — otherwise you'd lose the structure preview that makes this
command useful."""


def _is_sensitive_key(name: str) -> bool:
    low = name.lower()
    return any(low.endswith(suf) for suf in _SENSITIVE_KEY_SUFFIXES)


def _mask_value(val: Any) -> Any:
    """Render a sensitive value as a length-preserving hint.

    Keeps first/last 2 chars so the operator can still disambiguate
    "did I paste the right key" without leaking the full value. Short
    values (<=4 chars) collapse to all-stars so the prefix/suffix
    doesn't effectively reveal everything.
    """
    if val is None:
        return None
    if not isinstance(val, str):
        # Non-string sensitive values are rare (mostly numeric tokens).
        # Mask them wholesale — a partial reveal of a number is a bigger
        # leak than a string because the space is smaller.
        return "***"
    if val == "":
        return ""
    if len(val) <= 4:
        return "*" * len(val)
    return f"{val[:2]}{'*' * (len(val) - 4)}{val[-2:]}"


def _mask_config(obj: Any, *, path: tuple[str, ...] = ()) -> Any:
    """Walk a parsed config dict, masking sensitive leaves by key name.

    Args:
        obj: Node being walked. Dicts recurse; lists recurse element-wise
            with the parent key applied to each element (so a list of
            tokens is uniformly masked); scalars pass through unless
            the parent key flagged them sensitive.
        path: Dotted path of ancestor keys for debugging / future
            reference. Not used for masking decisions — only the
            immediate parent key is.
    """
    if isinstance(obj, dict):
        return {
            k: (
                _mask_value(v)
                if _is_sensitive_key(k) and not isinstance(v, (dict, list))
                else _mask_config(v, path=path + (k,))
            )
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        # List context inherits sensitivity from the parent key via the
        # caller's dict-level branch. When we hit a list here it means
        # the parent key wasn't sensitive, so recurse plain.
        return [_mask_config(v, path=path) for v in obj]
    return obj


_CONFIG_KEY_MISSING = object()
"""Sentinel — lets `_lookup_dotted` distinguish "key absent" from "value = None"."""


def _lookup_dotted(data: dict, key: str) -> Any:
    """Resolve ``a.b.c`` against a dict, returning ``_CONFIG_KEY_MISSING``
    when any segment is missing or isn't a dict.

    Matches :func:`config_set` dotted semantics: empty segments (``..``
    or leading ``.``) are ignored; a fully-empty key errors upstream.
    """
    parts = [p for p in key.split(".") if p]
    if not parts:
        return _CONFIG_KEY_MISSING
    cursor: Any = data
    for segment in parts:
        if not isinstance(cursor, dict) or segment not in cursor:
            return _CONFIG_KEY_MISSING
        cursor = cursor[segment]
    return cursor


@config_app.command("get")
def config_get(
    key: str = typer.Argument(
        ..., help="Dotted key path, e.g. 'gateway.port' or 'llm.anthropic.api_key'.",
    ),
    path: str = typer.Option(
        "daemon/config.json", "--path",
        help="Config file to read (default: daemon/config.json).",
    ),
    reveal: bool = typer.Option(
        False, "--reveal",
        help=(
            "Print the raw value for sensitive leaves (api_key / token / secret / "
            "password / etc). Default masks them so the output is safe to paste."
        ),
    ),
    json_output: bool = typer.Option(
        False, "--json",
        help="Emit the value as JSON (strings get quoted). Scripting-friendly.",
    ),
) -> None:
    """Read a single dotted key from the config file.

    Companion to ``config set`` — after ``config set gateway.port 9000`` you
    can confirm with ``config get gateway.port``. Prints just the value
    (no surrounding object) so shell pipelines are easy.

    Exits 1 when the file is missing, not valid JSON, or the key isn't set.
    Missing keys are a hard error rather than printing empty: the common
    case of ``config get | xargs ...`` would silently do the wrong thing
    against blank output.
    """
    import json as _json

    target = Path(path)
    if not target.exists():
        typer.echo(
            f"  [x]  no config at {target} -- run 'xmclaw config init' first",
            err=True,
        )
        raise typer.Exit(code=1)
    try:
        data = _json.loads(target.read_text(encoding="utf-8"))
    except _json.JSONDecodeError as exc:
        typer.echo(f"  [x]  {target} is not valid JSON: {exc}", err=True)
        raise typer.Exit(code=1)
    if not isinstance(data, dict):
        typer.echo(
            f"  [x]  {target} must have a JSON object at its root, "
            f"got {type(data).__name__}",
            err=True,
        )
        raise typer.Exit(code=1)

    parts = [p for p in key.split(".") if p]
    if not parts:
        typer.echo("  [x]  key must be non-empty", err=True)
        raise typer.Exit(code=2)

    value = _lookup_dotted(data, key)
    if value is _CONFIG_KEY_MISSING:
        typer.echo(f"  [x]  key not set: {key}", err=True)
        raise typer.Exit(code=1)

    leaf = parts[-1]
    rendered: Any
    if reveal or not _is_sensitive_key(leaf):
        rendered = value
    else:
        rendered = _mask_value(value)

    if json_output:
        typer.echo(_json.dumps(rendered, ensure_ascii=False))
    elif isinstance(rendered, str):
        # Plain string — emit bare so it can be used as `$(xmclaw config get ...)`.
        typer.echo(rendered)
    else:
        # Numbers / bools / null / containers get JSON-encoded even in text
        # mode — printing Python's `True` / `None` would surprise scripts.
        typer.echo(_json.dumps(rendered, ensure_ascii=False))


@config_app.command("show")
def config_show(
    path: str = typer.Option(
        "daemon/config.json", "--path",
        help="Config file to read (default: daemon/config.json).",
    ),
    reveal: bool = typer.Option(
        False, "--reveal",
        help=(
            "Print sensitive values in full. Default masks api_key / token "
            "/ secret / password fields so this command is safe to paste "
            "into a chat or run on a screenshare."
        ),
    ),
    json_output: bool = typer.Option(
        False, "--json",
        help="Emit JSON instead of indented text (for piping).",
    ),
) -> None:
    """Print daemon config with sensitive values masked by default.

    Exits 1 when the file is missing or not valid JSON (the daemon
    factory would also reject it — fail early, say why).
    """
    import json as _json

    target = Path(path)
    if not target.exists():
        typer.echo(
            f"  [x]  no config at {target} -- run 'xmclaw config init' first",
            err=True,
        )
        raise typer.Exit(code=1)
    try:
        raw = _json.loads(target.read_text(encoding="utf-8"))
    except _json.JSONDecodeError as exc:
        typer.echo(f"  [x]  {target} is not valid JSON: {exc}", err=True)
        raise typer.Exit(code=1)

    rendered = raw if reveal else _mask_config(raw)
    if json_output:
        typer.echo(_json.dumps(rendered, indent=2, ensure_ascii=False))
    else:
        typer.echo(f"  [ok]  {target}")
        typer.echo(_json.dumps(rendered, indent=2, ensure_ascii=False))


# ── xmclaw backup ──────────────────────────────────────────────────────
# Epic #20 entry. The CLI is a thin shell; all real work lives in
# xmclaw.backup so other frontends (future web UI, scheduled task) can
# reuse the same code path. Kept at the bottom so the rest of main.py's
# ordering isn't disturbed.


def _default_backup_source() -> Path:
    from xmclaw.utils.paths import data_dir

    return data_dir()


@backup_app.command("create")
def backup_create(
    name: str = typer.Argument(
        None,
        help="Backup name. Defaults to 'auto-YYYY-MM-DD-HHMMSS'.",
    ),
    source: Path = typer.Option(
        None, "--source",
        help="Workspace to back up. Defaults to $XMC_DATA_DIR or ~/.xmclaw.",
    ),
    dest: Path = typer.Option(
        None, "--dest",
        help="Backups directory. Defaults to <source>/backups.",
    ),
    overwrite: bool = typer.Option(
        False, "--overwrite",
        help="Replace an existing backup with the same name.",
    ),
) -> None:
    """Archive ``~/.xmclaw/`` to a versioned tar.gz + manifest."""
    import time as _time

    from xmclaw.backup import create_backup
    from xmclaw.backup.create import BackupError

    src = source or _default_backup_source()
    if name is None:
        name = "auto-" + _time.strftime("%Y-%m-%d-%H%M%S", _time.gmtime())
    try:
        manifest = create_backup(
            src, name, backups_dir=dest, overwrite=overwrite,
        )
    except BackupError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"  [ok]  {name}: {manifest.entries} file(s), "
               f"{manifest.archive_bytes} bytes, "
               f"sha256={manifest.archive_sha256[:12]}...")


def _manifest_to_dict(entry: Any) -> dict[str, Any]:
    """Flatten a BackupEntry into a JSON-safe dict for `--json` output.

    Includes the on-disk ``path`` next to the manifest fields so
    scripts can pipe straight into restore / delete without re-
    resolving the backups root.
    """
    m = entry.manifest
    return {
        "name": entry.name,
        "path": str(entry.dir),
        "schema_version": m.schema_version,
        "created_ts": m.created_ts,
        "xmclaw_version": m.xmclaw_version,
        "archive_sha256": m.archive_sha256,
        "archive_bytes": m.archive_bytes,
        "source_dir": m.source_dir,
        "excluded": list(m.excluded),
        "entries": m.entries,
    }


@backup_app.command("list")
def backup_list(
    dest: Path = typer.Option(
        None, "--dest",
        help="Backups directory. Defaults to ~/.xmclaw/backups.",
    ),
    as_json: bool = typer.Option(
        False, "--json",
        help="Emit a JSON array for scripting (stable schema).",
    ),
) -> None:
    """Show every backup on disk.

    Default text mode is columnar for eyeballing; ``--json`` emits a
    stable array with one dict per backup (name / path / all manifest
    fields) — pipe into ``jq`` to filter / sort / feed into another
    ``xmclaw backup ...`` invocation.
    """
    import json as _json

    from xmclaw.backup import list_backups

    entries = list_backups(dest)
    if as_json:
        payload = [_manifest_to_dict(e) for e in entries]
        typer.echo(_json.dumps(payload, indent=2))
        return
    if not entries:
        typer.echo("no backups found.")
        return
    for entry in entries:
        m = entry.manifest
        typer.echo(
            f"  {entry.name:30s}  "
            f"{m.entries:6d} files  "
            f"{m.archive_bytes:>10d} bytes  "
            f"v{m.xmclaw_version}"
        )


@backup_app.command("verify")
def backup_verify(
    name: str = typer.Argument(..., help="Name of the backup to verify."),
    dest: Path = typer.Option(
        None, "--dest",
        help="Backups directory. Defaults to ~/.xmclaw/backups.",
    ),
) -> None:
    """Re-hash an existing backup and confirm it still matches its manifest.

    Read-only — does not extract. Use before restoring, after moving a
    backup to slower storage, or to catch bit-rot on long-lived archives.
    Exits non-zero on any failure (missing, corrupt, schema too new,
    checksum drift).
    """
    from xmclaw.backup import verify_backup
    from xmclaw.backup.restore import RestoreError

    try:
        manifest = verify_backup(name, backups_dir=dest)
    except RestoreError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        f"  [ok]  {name}: sha256 verified "
        f"({manifest.entries} files, {manifest.archive_bytes} bytes)"
    )


def _format_bytes(n: int) -> str:
    """Render ``n`` bytes as KiB/MiB/GiB with one decimal (operator-friendly).

    ``list`` shows raw bytes because the column needs to sort numerically.
    ``info`` is a read-by-one inspector so we can afford to be readable.
    """
    step = 1024.0
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < step:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= step
    return f"{n:.1f} PiB"


@backup_app.command("info")
def backup_info(
    name: str = typer.Argument(..., help="Name of the backup to inspect."),
    dest: Path = typer.Option(
        None, "--dest",
        help="Backups directory. Defaults to ~/.xmclaw/backups.",
    ),
    show_excluded: bool = typer.Option(
        False, "--show-excluded",
        help="Also print the list of glob patterns that were excluded.",
    ),
    as_json: bool = typer.Option(
        False, "--json",
        help="Emit the full manifest as a JSON dict (implies --show-excluded).",
    ),
) -> None:
    """Pretty-print a single backup's manifest without re-hashing.

    Cheaper than ``verify`` — this only reads ``manifest.json`` and
    echoes the metadata. Use when you want to know *what* a backup is
    (when it was taken, what version, how big) without paying the
    sha256 cost. Exits non-zero when the backup is missing or malformed.

    ``--json`` emits the same dict shape as ``backup list --json``
    produces for each element (always includes the full ``excluded``
    list).
    """
    import datetime as _dt
    import json as _json

    from xmclaw.backup import get_backup
    from xmclaw.backup.store import BackupNotFoundError

    try:
        entry = get_backup(name, backups_dir=dest)
    except (BackupNotFoundError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    if as_json:
        typer.echo(_json.dumps(_manifest_to_dict(entry), indent=2))
        return

    m = entry.manifest
    created = _dt.datetime.fromtimestamp(
        m.created_ts, tz=_dt.timezone.utc
    ).isoformat(timespec="seconds")
    typer.echo(f"  name           {entry.name}")
    typer.echo(f"  path           {entry.dir}")
    typer.echo(f"  created        {created}")
    typer.echo(f"  xmclaw_version {m.xmclaw_version}")
    typer.echo(f"  source_dir     {m.source_dir}")
    typer.echo(f"  entries        {m.entries}")
    typer.echo(f"  archive_bytes  {m.archive_bytes} ({_format_bytes(m.archive_bytes)})")
    typer.echo(f"  sha256         {m.archive_sha256[:16]}…")
    typer.echo(f"  schema_version {m.schema_version}")
    if show_excluded:
        if m.excluded:
            typer.echo("  excluded:")
            for pat in m.excluded:
                typer.echo(f"    - {pat}")
        else:
            typer.echo("  excluded       (none)")


@backup_app.command("delete")
def backup_delete(
    name: str = typer.Argument(..., help="Name of the backup to delete."),
    dest: Path = typer.Option(
        None, "--dest",
        help="Backups directory. Defaults to ~/.xmclaw/backups.",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y",
        help="Skip the confirmation prompt.",
    ),
) -> None:
    """Remove a single backup directory (archive + manifest)."""
    from xmclaw.backup import delete_backup
    from xmclaw.backup.store import BackupNotFoundError

    if not yes:
        confirm = typer.confirm(
            f"delete backup {name!r}? this cannot be undone",
            default=False,
        )
        if not confirm:
            typer.echo("aborted.")
            raise typer.Exit(code=1)
    try:
        path = delete_backup(name, backups_dir=dest)
    except BackupNotFoundError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(f"  [ok]  deleted {path}")


@backup_app.command("prune")
def backup_prune(
    keep: int = typer.Option(
        5, "--keep",
        help="Number of newest backups to retain. Older ones are deleted.",
    ),
    dest: Path = typer.Option(
        None, "--dest",
        help="Backups directory. Defaults to ~/.xmclaw/backups.",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y",
        help="Skip the confirmation prompt.",
    ),
) -> None:
    """Keep only the ``--keep`` newest backups; drop the rest."""
    from xmclaw.backup import list_backups, prune_backups

    entries = list_backups(dest)
    if len(entries) <= keep:
        typer.echo(
            f"nothing to prune: {len(entries)} backup(s) <= keep={keep}."
        )
        return
    will_remove = entries[: len(entries) - keep]
    if not yes:
        typer.echo(f"would remove {len(will_remove)} backup(s):")
        for e in will_remove:
            typer.echo(f"  - {e.name}")
        if not typer.confirm("proceed?", default=False):
            typer.echo("aborted.")
            raise typer.Exit(code=1)
    try:
        removed = prune_backups(backups_dir=dest, keep=keep)
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(f"  [ok]  removed {len(removed)} backup(s).")


@backup_app.command("restore")
def backup_restore(
    name: str = typer.Argument(..., help="Name of the backup to restore."),
    target: Path = typer.Option(
        None, "--target",
        help="Destination workspace. Defaults to $XMC_DATA_DIR or ~/.xmclaw.",
    ),
    dest: Path = typer.Option(
        None, "--dest",
        help="Backups directory. Defaults to ~/.xmclaw/backups.",
    ),
    keep_previous: bool = typer.Option(
        True, "--keep-previous/--no-keep-previous",
        help=(
            "When the target exists, move it aside to <target>.prev-<ts> "
            "before extracting (default on — lets you roll back a bad "
            "restore)."
        ),
    ),
) -> None:
    """Extract a backup back into the workspace.

    Does not stop or restart the daemon. Run ``xmclaw stop`` first; after
    the restore completes, run ``xmclaw start`` to bring it back up.
    """
    from xmclaw.backup import restore_backup
    from xmclaw.backup.restore import RestoreError

    tgt = target or _default_backup_source()
    try:
        result = restore_backup(
            name, tgt, backups_dir=dest, keep_previous=keep_previous,
        )
    except RestoreError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"  [ok]  restored {name} -> {result}")
    typer.echo("    next: run 'xmclaw start' to bring the daemon back up.")


if __name__ == "__main__":
    app()
