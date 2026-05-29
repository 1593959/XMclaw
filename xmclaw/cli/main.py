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
    xmclaw onboard                  Interactive first-time setup wizard.
    xmclaw doctor                   Diagnose a local setup without running anything.
    xmclaw memory stats             Per-layer memory occupancy.
    xmclaw config init              Write a daemon/config.json skeleton.
    xmclaw config set <key> <val>   Mutate a dotted key in config.json.

v1 was deleted wholesale in Phase 4.10; there's now only one CLI.
"""
from __future__ import annotations

import asyncio
from typing import Any

import typer

from xmclaw import __version__
from xmclaw.core.bus import (
    BehavioralEvent,
    EventType,
    InProcessEventBus,
    make_event,
)
from xmclaw.cli.onboard import run_onboard
from xmclaw.core.bus.memory import accept_all

app = typer.Typer(help="XMclaw — local-first, self-evolving AI agent runtime")

# ``xmclaw memory <subcommand>`` — grouped under a sub-typer so future
# siblings (prune, forget, etc.) can land next to ``stats`` without
# polluting the top-level help.
memory_app = typer.Typer(
    help="Inspect and maintain the agent's SQLite-vec memory store.",
)
app.add_typer(memory_app, name="memory")

# B-325: 7 sub-apps moved to sibling files so this entry-point stays
# walkable. Each ``_<name>_cmds`` module owns its own typer +
# command definitions; we just register them here under their CLI
# names. ``evolution`` gets a second alias ``evolve`` for the
# verb-style shorthand already used in docs.
from xmclaw.cli._acp_cmds import acp_app  # noqa: E402  Jarvis Phase J3
from xmclaw.cli._approvals_cmds import approvals_app  # noqa: E402
from xmclaw.cli._backup_cmds import backup_app  # noqa: E402
from xmclaw.cli._chat_cmds import chat_app  # noqa: E402  Jarvis Phase J3
from xmclaw.cli._codebase_cmds import codebase_app  # noqa: E402  Jarvis Phase J1
from xmclaw.cli._config_cmds import config_app  # noqa: E402
from xmclaw.cli._curriculum_cmds import curriculum_app  # noqa: E402
from xmclaw.cli._evolution_cmds import evolution_app  # noqa: E402
from xmclaw.cli._security_cmds import security_app  # noqa: E402
from xmclaw.cli._session_cmds import session_app  # noqa: E402
from xmclaw.cli.skill_marketplace import skill_app  # noqa: E402  B-390
from xmclaw.cli.eval import eval_app  # noqa: E402  Sprint 4: A/B harness
app.add_typer(config_app, name="config")
app.add_typer(backup_app, name="backup")
app.add_typer(chat_app, name="chat")  # Jarvis Phase J3
app.add_typer(codebase_app, name="codebase")  # Jarvis Phase J1
app.add_typer(evolution_app, name="evolution")
app.add_typer(evolution_app, name="evolve")
app.add_typer(approvals_app, name="approvals")
app.add_typer(curriculum_app, name="curriculum")
app.add_typer(security_app, name="security")
app.add_typer(session_app, name="session")
app.add_typer(skill_app, name="skill")  # B-390 (Sprint 2): skill marketplace
app.add_typer(eval_app, name="eval")  # Sprint 4: A/B benchmark harness
app.add_typer(acp_app, name="acp")  # Jarvis Phase J3

# B-325 back-compat re-exports: tests / external callers used to
# ``from xmclaw.cli.main import _default_config_template`` etc when
# these helpers lived here. Re-export so the move is invisible.
# ``X as X`` form makes mypy treat them as explicit re-exports.
from xmclaw.cli._config_cmds import (  # noqa: E402, F401
    _default_config_template as _default_config_template,
    _parse_dotted_value as _parse_dotted_value,
)


def _default_memory_db_path():
    """Delegates to :func:`xmclaw.utils.paths.default_memory_db_path` so
    ``XMC_DATA_DIR`` moves this file along with the rest of the workspace."""
    from xmclaw.utils.paths import default_memory_db_path
    return default_memory_db_path()


@app.command()
def version(
    as_json: bool = typer.Option(
        False, "--json",
        help=(
            "Emit `{name, version, python, platform}` as JSON. "
            "Use in bug-reports and CI where a stable shape matters."
        ),
    ),
) -> None:
    """Print the v2 runtime version.

    Default text is ``xmclaw v<version>`` — deliberately terse because
    shell auto-completion, log banners, and README snippets all read it
    by eyeball. ``--json`` carries three extra fields (python version,
    platform) because a bug report is useless without those — folding
    them into the human line would clutter every other caller.
    """
    if as_json:
        import json as _json
        import platform as _platform
        import sys as _sys

        typer.echo(
            _json.dumps(
                {
                    "name": "xmclaw",
                    "version": __version__,
                    "python": _sys.version.split()[0],
                    "platform": _platform.platform(),
                }
            )
        )
        return
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
    port: int = typer.Option(
        0,
        help=(
            "Port to bind. 0 = read XMC_DAEMON_PORT env (default 8765). "
            "B-315: set explicitly (e.g. --port 8766) to run multiple "
            "worktrees side-by-side without colliding on 8765."
        ),
    ),
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

    # B-298: production daemon must call setup_logging() exactly once at
    # startup. Pre-B-298 ``log.info(...)`` calls from B-294/295/296/297
    # modules (evolution_agent, evolution_evaluation_trigger,
    # variant_selector) all routed through ``logging.getLogger(__name__)``
    # against a root logger with NO handlers — every event silently
    # dropped on the floor. This made it indistinguishable whether the
    # evolution chain was firing, failing, or never reached. With
    # ``setup_logging()`` wired here, the structlog JSON file at
    # ``<data>/logs/xmclaw.log`` becomes the canonical audit trail; the
    # ``daemon.log`` subprocess stdout capture remains as the uvicorn
    # access log. Idempotent; safe to call before the typer.echo lines
    # below since those go to stdout (caught by daemon.log) not through
    # the structlog pipeline.
    from xmclaw.utils.log import setup_logging, set_log_level
    setup_logging()

    # B-315: resolve port. --port 0 (default) → XMC_DAEMON_PORT env →
    # 8765 fallback. Lets multiple worktrees coexist without colliding
    # on 8765 by setting env per-worktree (e.g. ``XMC_DAEMON_PORT=8766
    # xmclaw start``).
    if port == 0:
        import os as _os
        try:
            port = int(_os.environ.get("XMC_DAEMON_PORT", "8765"))
        except ValueError:
            port = 8765

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
        typer.echo("  [!]   --no-auth: anyone on this machine can connect")

    # Resolve config path with a search list so `xmclaw start` works no
    # matter what CWD it inherits. Prior behaviour: only ``daemon/config.json``
    # relative to CWD, which silently fell through to echo mode whenever
    # the daemon was launched from anywhere other than the repo root —
    # exactly the "agent_wired: false, 0 tools" symptom users hit when
    # `xmclaw start` is wired into a desktop shortcut or systemd unit.
    #
    # Order:
    #   1. Whatever the caller passed via ``--config``, IF it exists.
    #   2. ``~/.xmclaw/config.json`` — canonical user-level location.
    #   3. ``daemon/config.json`` relative to CWD (legacy repo-root run).
    #   4. ``daemon/config.json`` relative to the repo root that contains
    #      the running ``xmclaw`` package (``pip install -e .`` case).
    from xmclaw.utils.paths import data_dir
    _explicit = _Path(config)
    _candidates = [
        _explicit,
        data_dir() / "config.json",
        _Path("daemon/config.json"),
        _Path(__file__).resolve().parent.parent.parent / "daemon" / "config.json",
    ]
    cfg_path = next(
        (p for p in _candidates if p.exists()),
        _explicit,  # fall back to the literal path so the warning stays accurate
    )
    if cfg_path != _explicit and cfg_path.exists():
        typer.echo(f"  [ok]  config resolved: {cfg_path}")
    agent = None
    cfg: dict | None = None
    if cfg_path.exists():
        try:
            cfg = load_config(cfg_path)
            # B-311: now that cfg is parsed, apply user-configured log
            # level (overridden by XMC_LOG_LEVEL env if set).
            try:
                set_log_level(
                    (cfg.get("logging") or {}).get("level"),
                )
            except Exception:  # noqa: BLE001
                pass
            agent = build_agent_from_config(cfg, bus)
            if agent is None:
                typer.echo(
                    "  [!]   config has no LLM api_key set -- running in echo mode"
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
                    typer.echo("  [!]   tools disabled (no 'tools' section in config)")
        except ConfigError as exc:
            typer.echo(f"  [!]   config error -- running in echo mode: {exc}", err=True)
    else:
        typer.echo(f"  [!]   config not found at {cfg_path} -- running in echo mode")

    # Epic #4 Phase C: build the EvolutionOrchestrator here — the CLI
    # layer is the right place because ``xmclaw/daemon/`` must not
    # import ``xmclaw.skills.*`` (see xmclaw/daemon/AGENTS.md §2). The
    # registry persists its promote/rollback audit log under
    # ``skills_dir()`` so ``xmclaw evolution show`` can read it back.
    orchestrator = None
    if cfg is not None:
        ev_cfg = cfg.get("evolution") or {}
        if ev_cfg.get("enabled", True):
            from xmclaw.skills.orchestrator import EvolutionOrchestrator
            from xmclaw.skills.registry import SkillRegistry
            from xmclaw.utils.paths import skills_dir
            registry = SkillRegistry(history_dir=skills_dir())
            auto_apply = bool(ev_cfg.get("auto_apply", True))
            orchestrator = EvolutionOrchestrator(
                registry, bus, auto_apply=auto_apply,
            )
            mode = "auto-apply" if auto_apply else "observe-only"
            typer.echo(f"  [ok]  evolution orchestrator: {mode}")

            # B-127 + Epic #24 Phase 5 + B-163 + B-173 + B-234: roots
            # resolved via the shared :func:`resolve_skill_roots` helper
            # so boot-time loader and the runtime SkillsWatcher agree on
            # what to scan. Canonical path (``~/.xmclaw/skills_user/``)
            # plus the open-agent-skills marketplace
            # (``~/.agents/skills``) unless
            # ``evolution.skill_paths.extra`` overrides. B-234 dropped
            # ``~/.claude/skills`` — that's Claude Code's user-level
            # config space, not XMclaw's.
            from xmclaw.skills.user_loader import (
                UserSkillsLoader, resolve_skill_roots,
            )
            user_root, extra_roots = resolve_skill_roots(cfg)
            results = UserSkillsLoader(
                registry, user_root, extra_roots=extra_roots,
            ).load_all()
            if results:
                ok_n = sum(1 for r in results if r.ok)
                py_n = sum(1 for r in results if r.ok and r.kind == "python")
                md_n = sum(1 for r in results if r.ok and r.kind == "markdown")
                fail = [r for r in results if not r.ok]
                roots_msg = str(user_root)
                if extra_roots:
                    roots_msg += " + " + ", ".join(str(r) for r in extra_roots)
                typer.echo(
                    f"  [ok]  user skills: {ok_n}/{len(results)} loaded "
                    f"({py_n} python, {md_n} markdown) from {roots_msg}"
                )
                for r in fail:
                    typer.echo(
                        f"  [!]   user skill {r.skill_id!r}: {r.error}",
                        err=True,
                    )

            # B-174: replay promote/rollback audit log so HEAD pointers
            # survive daemon restart. Without this, a mutator-promoted
            # v2 silently reverted to v1 on every restart.
            try:
                replayed = registry.replay_history()
            except Exception as exc:  # noqa: BLE001
                typer.echo(
                    f"  [!]   skill HEAD replay failed: {exc}", err=True,
                )
                replayed = {}
            if replayed:
                head_msg = ", ".join(
                    f"{sid}=v{ver}" for sid, ver in sorted(replayed.items())
                )
                typer.echo(
                    f"  [ok]  skill HEAD replayed for "
                    f"{len(replayed)} skill(s): {head_msg}"
                )

            # B-174 #1: surface a hint when DSPy isn't installed so the
            # user knows MutationOrchestrator will silently no-op.
            try:
                from xmclaw.core.evolution.mutator import SkillMutator
                if not SkillMutator().is_available:
                    typer.echo(
                        "  [i]   skill mutator: DSPy not installed — "
                        "MutationOrchestrator will register but never "
                        "produce v2. Install with `pip install dspy-ai` "
                        "to enable iteration."
                    )
            except Exception:  # noqa: BLE001
                pass

    # B-338 (audit #9): host-binding hardening. The pairing.py docstring
    # used to promise "Phase 4.7+ ed25519 device pairing" — that work
    # never landed; what ships is a shared-secret-from-file. That's
    # safe on loopback (other processes can't read the 0600 file), but
    # NOT on 0.0.0.0 or LAN-bound hosts where the secret travels in
    # WS query params + appears in any reverse-proxy log line. Refuse
    # to bind non-loopback unless the operator explicitly acknowledges
    # the trade-off via ``--allow-non-loopback``. Pre-B-338 this was
    # only a docstring warning that nobody read.
    _LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
    if host not in _LOOPBACK_HOSTS:
        if not no_auth:
            typer.echo(
                f"  [REFUSED] host={host} is not loopback. The pairing "
                f"token is a shared secret (Phase 4.4 — full ed25519 "
                f"device pairing was promised but never landed); on "
                f"non-loopback bind it travels in WS query params and "
                f"shows up in reverse-proxy logs. Either:\n"
                f"    * bind --host 127.0.0.1 (default; safe), OR\n"
                f"    * front the daemon with a reverse proxy that adds\n"
                f"      mTLS / OAuth at the proxy layer, OR\n"
                f"    * run with --no-auth AND --allow-non-loopback to\n"
                f"      acknowledge that you accept the risk.\n"
                f"  Pin a tracking issue before flipping it on."
            )
            raise typer.Exit(code=2)
        typer.echo(
            f"  [warn] host={host} non-loopback + --no-auth. The "
            f"daemon's WS endpoint accepts ANY connection. This is the "
            f"OpenWebUI / Open ChatGPT exposure shape — make sure you "
            f"know what you're doing."
        )

    typer.echo(f"xmclaw v{__version__} -- binding ws://{host}:{port}")
    typer.echo(f"  health:  http://{host}:{port}/health")
    typer.echo(f"  session: ws://{host}:{port}/agent/v2/<session_id>")
    typer.echo(f"  web ui:  http://{host}:{port}/")

    # Build the app locally so the agent (if any) is wired in.
    app_instance = _create_app(
        bus=bus, agent=agent, auth_check=auth_check,
        config=cfg, config_path=cfg_path,
        orchestrator=orchestrator,
    )
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
        60.0, help="Seconds to wait for /health before giving up.",
    ),
) -> None:
    """Spawn the daemon in the background, return once /health answers.

    Writes a PID file at ``~/.xmclaw/v2/daemon.pid`` and a log at
    ``~/.xmclaw/v2/daemon.log``. Use ``xmclaw stop`` to kill it, or
    ``xmclaw status`` to check on it.

    The wait timeout defaults to 60s (was 10s pre-2026-05-11, bumped
    to 30s then 60s after real-machine measurement). On Windows +
    cold module cache + Defender scanning, subprocess boot + uvicorn
    binding + lifespan startup can stretch past 30s — the user's
    machine consistently took ~40s for a fresh ``xmclaw serve``
    subprocess to reach its first log line, even though the lifespan
    itself ran in 0.13s. 60s is the new pessimistic ceiling; the
    loop polls every 0.3s and exits as soon as /health answers, so
    healthy boots are not slowed.
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


# 2026-05-26 (audit F1): explicit pairing-token revocation. Pre-fix
# a leaked token stayed valid forever — there was no way to
# invalidate it short of manually deleting the file. ``revoke-token``
# does that deletion + reminds the operator that the daemon needs
# a restart for live WS connections to be re-challenged.
@app.command(name="revoke-token")
def revoke_token_cmd() -> None:
    """Delete the on-disk pairing token. The next daemon start mints a fresh one."""
    from xmclaw.daemon.pairing import default_token_path, revoke_token
    path = default_token_path()
    if revoke_token(path):
        typer.echo(f"  [ok]  pairing token at {path} deleted")
        typer.echo(
            "        Run `xmclaw restart` so live WS connections are "
            "re-challenged with the new token."
        )
    else:
        typer.echo(
            f"  [!]   no pairing token at {path} -- nothing to revoke"
        )


@app.command()
def trust(
    workspace: str = typer.Argument(
        ".",
        help="Workspace directory to mark as trusted (default: cwd).",
    ),
    revoke: bool = typer.Option(
        False, "--revoke",
        help="Remove the trust marker instead of adding it.",
    ),
    status_only: bool = typer.Option(
        False, "--status",
        help="Report the current trust level without modifying anything.",
    ),
) -> None:
    """Mark a workspace as trusted so XMclaw hooks (command / function
    runners) can run there. Wave-32 (2026-05-18).

    The marker is a single empty-ish file ``.xmclaw-trust`` in the
    workspace root. Presence = trusted. ``command`` / ``function``
    hook runners refuse to execute on untrusted workspaces because
    both have arbitrary-code-execution surface. ``http`` / ``prompt``
    / ``agent`` runners ignore the trust marker (their config is
    fixed in your config.json so the operator already vetted them).

    Run this once per workspace after reviewing your hook config:

        cd ~/my-project
        xmclaw trust

    To revoke:

        xmclaw trust ~/my-project --revoke

    To check status without changing anything:

        xmclaw trust ~/my-project --status
    """
    from pathlib import Path as _P
    from xmclaw.core.hooks.trust import (
        mark_workspace_trusted,
        unmark_workspace_trusted,
        workspace_trust_level,
    )
    p = _P(workspace).expanduser().resolve()
    if status_only:
        level = workspace_trust_level(p)
        typer.echo(f"  trust: {level}  ({p})")
        raise typer.Exit(code=0 if level == "trusted" else 1)
    if revoke:
        removed = unmark_workspace_trusted(p)
        if removed:
            typer.echo(f"  [ok]  revoked trust for {p}")
        else:
            typer.echo(f"  [-]   no trust marker at {p}")
        return
    marker = mark_workspace_trusted(p)
    typer.echo(f"  [ok]  trusted {p}")
    typer.echo(f"        marker: {marker}")


@app.command()
def restart(
    host: str = typer.Option("127.0.0.1", help="Bind address."),
    port: int = typer.Option(8765, help="Port to bind."),
    config: str = typer.Option(
        "daemon/config.json", help="Path to config JSON.",
    ),
    no_auth: bool = typer.Option(False, "--no-auth"),
    grace: float = typer.Option(5.0),
    wait: float = typer.Option(60.0),
) -> None:
    """Stop (if running) then start. Idempotent.

    Wait timeout defaults to 60s (was 10s pre-2026-05-11) for the
    same reason as ``xmclaw start`` — see that command's docstring.
    """
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
    resume: bool = typer.Option(
        False, "--resume", "-r",
        help="Pick a previous session to continue (interactive list).",
    ),
    last: bool = typer.Option(
        False, "--last", "-l",
        help="Resume the most recently active session.",
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

    Resume a previous conversation with ``--resume`` (interactive picker)
    or ``--last`` (most recent), or supply ``--session-id <id>`` directly.
    Persisted history survives daemon restarts.

    Start a daemon in another terminal first:

        xmclaw serve

    Then in this terminal:

        xmclaw chat
    """
    from xmclaw.cli.chat import pick_resume_session, run_chat
    from xmclaw.daemon.pairing import default_token_path

    if (resume or last) and session_id:
        typer.echo(
            "  [!]   --resume / --last conflict with --session-id; pick one.",
            err=True,
        )
        raise typer.Exit(code=2)

    if resume or last:
        chosen = pick_resume_session(prefer_last=last)
        if chosen is None:
            typer.echo(
                "  [!]   no saved sessions to resume yet -- start a fresh "
                "chat first.",
                err=True,
            )
            raise typer.Exit(code=2)
        session_id = chosen

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


@app.command("onboard")
def onboard(
    config: str = typer.Option(
        "daemon/config.json", "--config",
        help="Path to write the config JSON.",
    ),
    skip_smoke: bool = typer.Option(
        False, "--skip-smoke",
        help="Skip the connectivity smoke test.",
    ),
) -> None:
    """Interactive first-time setup wizard.

    Guides through provider selection, API key capture, workspace
    confirmation, tool enablement, and a smoke test.
    """
    code = run_onboard(config_path=config, skip_smoke=skip_smoke)
    raise typer.Exit(code=code)


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


# B-31: list of provider IDs the wizard accepts. Kept in sync with the
# router's _AVAILABLE_PROVIDERS catalogue + factory.py wiring. Adding a
# new provider here without wiring its factory branch is a no-op until
# the daemon is taught how to construct it.
_MEMORY_PROVIDER_IDS = ("sqlite_vec", "hindsight", "supermemory", "mem0", "none")


@memory_app.command("setup")
def memory_setup(
    path: str = typer.Option(
        "daemon/config.json", "--path",
        help="Config file to mutate (default: daemon/config.json).",
    ),
    provider: str = typer.Option(
        "", "--provider",
        help=(
            "Skip the interactive picker and choose directly. "
            f"One of: {', '.join(_MEMORY_PROVIDER_IDS)}."
        ),
    ),
    api_key: str = typer.Option(
        "", "--api-key",
        help="API key for the chosen cloud provider (hindsight/supermemory/mem0).",
    ),
    base_url: str = typer.Option(
        "", "--base-url",
        help="Override the provider's default base URL (cloud providers only).",
    ),
) -> None:
    """Interactive picker for the external long-term memory provider.

    Walks the user through choosing one of: ``sqlite_vec`` (local
    vector DB, default), ``hindsight`` / ``supermemory`` / ``mem0``
    (cloud knowledge-graph backends, need an API key), or ``none``
    (only the always-on builtin file provider runs).

    Writes ``evolution.memory.provider`` plus the per-provider
    sub-section to the config. The builtin file provider is
    non-removable — this wizard only configures the *external* slot.

    Daemon restart required for the swap to take effect; we print a
    reminder. Re-running the wizard against a configured backend is
    safe and lets the user rotate API keys.
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
        cfg = _json.loads(target.read_text(encoding="utf-8"))
    except _json.JSONDecodeError as exc:
        typer.echo(f"  [x]  {target} is not valid JSON: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    if not isinstance(cfg, dict):
        typer.echo(
            f"  [x]  {target} must have a JSON object at its root", err=True,
        )
        raise typer.Exit(code=1)

    current = (
        ((cfg.get("evolution") or {}).get("memory") or {}).get("provider")
        or "sqlite_vec"
    )

    typer.echo("xmclaw memory setup")
    typer.echo(f"  current external provider: {current}")
    typer.echo("")
    typer.echo("  available providers:")
    typer.echo("    1) sqlite_vec   — local vector DB (no external service)")
    typer.echo("    2) hindsight    — cloud knowledge graph (needs API key)")
    typer.echo("    3) supermemory  — cloud key-value memory (needs API key)")
    typer.echo("    4) mem0         — cloud agent memory (needs API key)")
    typer.echo("    5) none         — only the builtin file provider runs")
    typer.echo("")

    # Resolve provider choice — flag value first, then interactive prompt.
    # Passing --provider switches the wizard into non-interactive mode:
    # only flags supply per-provider fields (api_key, base_url). This
    # makes the command scriptable + safe under CI/typer.testing.
    non_interactive = bool(provider)
    chosen = (provider or "").strip().lower()
    if chosen and chosen not in _MEMORY_PROVIDER_IDS:
        typer.echo(
            f"  [x]  unknown --provider {provider!r}; expected one of: "
            f"{', '.join(_MEMORY_PROVIDER_IDS)}",
            err=True,
        )
        raise typer.Exit(code=2)
    if not chosen:
        raw = typer.prompt(
            "  pick provider (1-5 or name)", default=current,
            show_default=True,
        ).strip().lower()
        # Number → name mapping.
        by_num = {
            "1": "sqlite_vec", "2": "hindsight", "3": "supermemory",
            "4": "mem0", "5": "none",
        }
        chosen = by_num.get(raw, raw)
    if chosen not in _MEMORY_PROVIDER_IDS:
        typer.echo(
            f"  [x]  unknown provider {chosen!r}; expected one of: "
            f"{', '.join(_MEMORY_PROVIDER_IDS)}",
            err=True,
        )
        raise typer.Exit(code=2)

    # Per-provider extras.
    section: dict[str, Any] = {"provider": chosen}
    cloud_providers = {"hindsight", "supermemory", "mem0"}
    if chosen in cloud_providers:
        key = api_key
        url = base_url
        if not non_interactive:
            if not key:
                try:
                    import getpass as _gp
                    key = _gp.getpass(
                        f"  {chosen} api_key (input hidden, blank to skip): "
                    )
                except Exception:  # noqa: BLE001
                    key = typer.prompt(
                        f"  {chosen} api_key (blank to skip)", default="",
                    )
            if not url:
                url = typer.prompt(
                    f"  {chosen} base_url (blank for default)", default="",
                )
        sub: dict[str, Any] = {}
        if key:
            sub["api_key"] = key
        if url:
            sub["base_url"] = url
        section[chosen] = sub

    # Merge into config preserving siblings.
    evo = cfg.setdefault("evolution", {})
    if not isinstance(evo, dict):
        evo = {}
        cfg["evolution"] = evo
    mem = evo.setdefault("memory", {})
    if not isinstance(mem, dict):
        mem = {}
        evo["memory"] = mem
    mem["provider"] = chosen
    if chosen in cloud_providers and section.get(chosen):
        # Merge sub-section so existing keys (rate limits, future
        # tunables) survive.
        sub_existing = mem.get(chosen)
        if not isinstance(sub_existing, dict):
            sub_existing = {}
        sub_existing.update(section[chosen])
        mem[chosen] = sub_existing

    target.write_text(
        _json.dumps(cfg, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    typer.echo("")
    typer.echo(f"  [ok]  wrote {target}")
    typer.echo(f"        evolution.memory.provider = {chosen}")
    if chosen in cloud_providers:
        sub = mem.get(chosen) or {}
        if sub.get("api_key"):
            typer.echo(f"        evolution.memory.{chosen}.api_key = ***")
        else:
            typer.echo(
                f"        no api_key set — export {chosen.upper()}_API_KEY "
                "or rerun this wizard"
            )
    typer.echo("        next: restart the daemon — 'xmclaw restart'")


# ── module entry-point ────────────────────────────────────────────────
#
# B-343: restored after B-325. The B-325 monolith split (extracted
# ``_config_cmds`` etc to sibling files) accidentally truncated the
# ``# ── config subcommands ──`` heading right when the trailing
# ``if __name__ == "__main__": app()`` block lived. Without this
# block, ``python -m xmclaw.cli.main serve …`` (the exact command
# ``xmclaw.daemon.lifecycle.start_daemon`` spawns) imports the
# module and exits silently → ``xmclaw start`` reports "daemon
# exited before becoming healthy" with an empty daemon.log because
# nothing ever ran. The installed ``xmclaw`` console-script
# entry-point still worked (pyproject's ``xmclaw =
# xmclaw.cli.main:app`` calls ``app()`` explicitly) which masked
# the regression in any test that didn't invoke ``xmclaw start``.
if __name__ == "__main__":
    app()
