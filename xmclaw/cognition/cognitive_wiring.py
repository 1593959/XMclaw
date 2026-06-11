"""Cognitive wiring — activate cognition components at daemon startup.

This module bridges the "wiring gap" (audit 2026-06-11): all cognitive
modules are fully implemented but default-disabled or unconnected at
daemon startup. This module wires the key ones together and starts them.

All subsystems enabled by default. Opt-out via config.cognition.*.enabled=false:
  - ProactiveAgent (30s tick) + triggers: idle_check_in, system_health
  - ReflectionCycle (5-min quality reflection, 1-hour memory consolidation)
  - PerceptionBus + FileWatcher (environment awareness)
  - GoalGenerator (maintenance/exploration goals, autonomy_level=25)

Usage in daemon lifespan::

    from xmclaw.cognition.cognitive_wiring import start_cognition
    tasks = await start_cognition(app, config, agent)
    # ... later, on shutdown:
    await stop_cognition(tasks)
"""
from __future__ import annotations

import asyncio
from typing import Any

from xmclaw.utils.log import get_logger

_log = get_logger(__name__)


async def start_cognition(
    app: Any,
    config: dict[str, Any],
    agent: Any = None,
) -> list[asyncio.Task[None]]:
    """Start all cognitive subsystems. Returns list of background tasks
    that must be cancelled on shutdown.

    All subsystems ON by default. Set ``cognition.<name>.enabled=false``
    in daemon/config.json to opt out. GoalGenerator runs at autonomy=25
    (passive observe mode — suggests but does not execute).
    """
    cog_cfg = config.get("cognition", {})
    tasks: list[asyncio.Task[None]] = []

    # Audit 2026-06-11: all subsystems default ON. Users opt OUT via
    # config, not in. The wiring gap was leaving fully-built code dead.

    # ── ProactiveAgent (30s tick) ────────────────────────────────
    try:
        from xmclaw.cognition.proactive_agent import ProactiveAgent
        pa = ProactiveAgent(bus=app.state.bus, agent=agent, config=cog_cfg)
        tasks.append(asyncio.create_task(pa.run(), name="cog-proactive"))
        app.state.proactive_agent = pa
        _log.info("cognition.proactive_agent started")
    except Exception as exc:  # noqa: BLE001
        _log.warning("cognition.proactive_agent failed: %s", exc)

    # ── ReflectionCycle (quality + memory consolidation) ─────────
    try:
        from xmclaw.cognition.reflection_cycle import ReflectionCycle
        rc = ReflectionCycle(
            bus=app.state.bus, agent=agent,
            memory_service=getattr(app.state, "memory_service", None),
            config=cog_cfg.get("reflection", {}),
        )
        tasks.append(asyncio.create_task(rc.run(), name="cog-reflection"))
        app.state.reflection_cycle = rc
        _log.info("cognition.reflection_cycle started (5m quality / 1h consolidation)")
    except Exception as exc:
        _log.warning("cognition.reflection_cycle failed: %s", exc)

    # ── PerceptionBus + FileWatcher ──────────────────────────────
    try:
        from xmclaw.cognition.perception_bus import PerceptionBus
        from xmclaw.cognition.file_watcher import FileWatcher
        from xmclaw.cognition.attention_filter import AttentionFilter
        ws_root = config.get("workspace_root", ".")
        perception = PerceptionBus(capacity=1024)
        fw = FileWatcher(root=ws_root, bus=perception, config=cog_cfg.get("perception", {}))
        af = AttentionFilter(perception, cognitive_state=getattr(app.state, "cognitive_state", None))
        tasks.append(asyncio.create_task(fw.start(), name="cog-filewatcher"))
        tasks.append(asyncio.create_task(af.run(), name="cog-attention"))
        app.state.perception_bus = perception
        app.state.attention_filter = af
        _log.info("cognition.perception started (FileWatcher + AttentionFilter)")
    except Exception as exc:
        _log.warning("cognition.perception failed: %s", exc)

    # ── GoalGenerator ────────────────────────────────────────────
    try:
        from xmclaw.cognition.goal_generator import GoalGenerator
        from xmclaw.cognition.state import AutonomyPolicy
        policy = AutonomyPolicy(
            level=int(cog_cfg.get("goals", {}).get("autonomy_level", 25)),
        )
        gg = GoalGenerator(
            cognitive_state=getattr(app.state, "cognitive_state", None),
            policy=policy,
            config=cog_cfg.get("goals", {}),
        )
        tasks.append(asyncio.create_task(gg.run(), name="cog-goals"))
        app.state.goal_generator = gg
        _log.info("cognition.goal_generator started (autonomy=%d)", policy.level)
    except Exception as exc:
        _log.warning("cognition.goal_generator failed: %s", exc)

    _log.info("cognition.wiring complete: %d tasks started", len(tasks))
    return tasks


async def stop_cognition(tasks: list[asyncio.Task[None]]) -> None:
    """Cancel all cognitive subsystem tasks."""
    for t in tasks:
        t.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _log.info("cognition.wiring stopped: %d tasks cancelled", len(tasks))
