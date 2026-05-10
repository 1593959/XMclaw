"""Percept-source adapters — Jarvis Phase 6 wiring follow-up A.

This module bridges the existing event producers (WS user messages,
:class:`xmclaw.cognition.file_watcher.FileWatcher`,
:class:`xmclaw.cognition.process_watcher.ProcessWatcher`, cron ticks,
internal-event hooks) into the new :class:`PerceptionBus`.

The producers themselves were written before PerceptionBus existed and
already have their own subscribe / callback hooks. Rather than mutate
those interfaces, this module wraps them: an adapter callback receives
the producer's native event, builds a :class:`Percept`, and forwards
it to the bus. Each ``make_*_percept`` helper is small and pure so
tests can verify them without spinning the producer.

Salience baselines below come from
``docs/JARVIS_PHASE_6_DESIGN.md`` §3.1 + §3.7:

==========================  ==========  ====================================
percept                      salience    rationale
==========================  ==========  ====================================
WS user message              0.85        user input is high-baseline urgent
file_modified/created/...    0.40        most edits are routine
cron_tick                    0.30        most firings are routine
process cpu_high/mem_high    0.70        threshold breach — worth a look
process zombie/exited        0.95        almost always needs attention
internal event (default)     0.50        caller can override per-event
==========================  ==========  ====================================

**Per-source opt-in.** Each ``attach_*`` is independent and only fires
when the calling lifespan decides to enable that source. The lifespan
block in :mod:`xmclaw.daemon.app` builds the producer (gated on its
own config flag) BEFORE handing it here, so a source that is off in
config never reaches the registry.

This module never imports from ``xmclaw.daemon.*`` — it only uses
duck-typed producer references handed in by callers. That keeps the
cognition package self-contained for unit testing and prevents an
import cycle when the lifespan wires things up.

See ``docs/JARVIS_PHASE_6_DESIGN.md`` §3.1.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

from xmclaw.cognition.perception_bus import Percept, PerceptionBus

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------- helpers


def _new_id() -> str:
    """Fresh percept id (uuid4 hex) — one helper so tests can monkeypatch."""
    return uuid.uuid4().hex


# ---------------------------------------------------------------- builders


def make_user_msg_percept(
    session_id: str,
    content: str,
    ultrathink: bool = False,
) -> Percept:
    """Build a :class:`Percept` from a WS user message.

    Source ``ws``, kind ``user_msg``, salience 0.85 (user input is the
    highest-baseline urgency channel — they're literally typing at us).
    ``correlation_id`` is the session id so downstream machinery can
    thread the percept back to the WebSocket session.
    """
    return Percept(
        id=_new_id(),
        source="ws",
        kind="user_msg",
        timestamp=time.time(),
        payload={
            "session_id": session_id,
            "content": content,
            "ultrathink": bool(ultrathink),
        },
        suggested_salience=0.85,
        correlation_id=session_id,
    )


# Map FileWatcher's PerceptionEventType -> Percept.kind. The watcher uses
# ``created / modified / deleted / moved``; we want the more explicit
# ``file_*`` form on the bus (Percept.source already says it's a file
# event, but downstream filters often grep on ``kind``).
_FILE_KIND_MAP: dict[str, str] = {
    "created": "file_created",
    "modified": "file_modified",
    "deleted": "file_deleted",
    "moved": "file_moved",
}


def make_file_event_percept(file_percept: Any) -> Percept:
    """Convert a :class:`xmclaw.cognition.file_watcher.FilePercept` to a bus
    :class:`Percept`.

    Duck-typed on purpose so tests can pass a fake with the same
    ``path / event_type / timestamp / is_directory / src_path`` shape
    without importing the real watcher. Salience baseline is 0.40 —
    most file edits in a watched tree are routine and the
    :class:`AttentionFilter` will rescore based on goal relevance.
    """
    event_type = str(getattr(file_percept, "event_type", "") or "")
    kind = _FILE_KIND_MAP.get(event_type, f"file_{event_type or 'unknown'}")
    payload: dict[str, Any] = {
        "path": getattr(file_percept, "path", None),
        "event_type": event_type,
        "is_directory": bool(getattr(file_percept, "is_directory", False)),
    }
    src_path = getattr(file_percept, "src_path", None)
    if src_path is not None:
        payload["src_path"] = src_path
    timestamp = float(getattr(file_percept, "timestamp", None) or time.time())
    return Percept(
        id=_new_id(),
        source="file",
        kind=kind,
        timestamp=timestamp,
        payload=payload,
        suggested_salience=0.4,
        correlation_id=None,
    )


def make_cron_tick_percept(
    job_id: str,
    job_name: str,
    fired_at: float,
) -> Percept:
    """Cron-job firing → :class:`Percept`.

    Source ``time``, kind ``cron_tick``. Most cron firings are scheduled
    routine work so the baseline is low (0.30); the AttentionFilter
    will boost when the job's payload mentions an active goal.
    ``correlation_id`` is the job id so a cron-spawned plan can be
    traced back to its trigger.
    """
    return Percept(
        id=_new_id(),
        source="time",
        kind="cron_tick",
        timestamp=float(fired_at),
        payload={
            "job_id": job_id,
            "job_name": job_name,
            "fired_at": float(fired_at),
        },
        suggested_salience=0.3,
        correlation_id=job_id,
    )


# Process alerts that almost always indicate a problem (so high baseline).
_PROCESS_HIGH_PRIORITY: frozenset[str] = frozenset({"zombie", "exited", "ghost"})


def make_process_alert_percept(alert: Any) -> Percept:
    """Convert a :class:`xmclaw.cognition.process_watcher.ProcessAlert` to a
    :class:`Percept`.

    Duck-typed on the alert's ``watch_id / pid / description / kind /
    timestamp / payload`` shape. Salience baseline:

    * ``cpu_high`` / ``memory_high`` → 0.70 (threshold breach, but the
      process is still alive)
    * ``zombie`` / ``exited`` / ``ghost`` → 0.95 (process is broken or
      gone — almost always needs attention)
    """
    kind = str(getattr(alert, "kind", "process_alert"))
    salience = 0.95 if kind in _PROCESS_HIGH_PRIORITY else 0.7
    base_payload = getattr(alert, "payload", None) or {}
    payload: dict[str, Any] = {
        "watch_id": getattr(alert, "watch_id", None),
        "pid": getattr(alert, "pid", None),
        "description": getattr(alert, "description", None),
    }
    if isinstance(base_payload, dict):
        payload.update(base_payload)
    timestamp = float(getattr(alert, "timestamp", None) or time.time())
    return Percept(
        id=_new_id(),
        source="process",
        kind=kind,
        timestamp=timestamp,
        payload=payload,
        suggested_salience=salience,
        correlation_id=str(getattr(alert, "watch_id", "") or "") or None,
    )


def make_internal_event_percept(
    event_kind: str,
    payload: dict[str, Any],
    *,
    suggested_salience: float = 0.5,
    correlation_id: str | None = None,
) -> Percept:
    """Internal-event → :class:`Percept`.

    Source ``internal``. ``event_kind`` is producer-supplied (typical:
    ``goal_completed``, ``experiment_finished``, ``skill_promoted``)
    and goes straight into ``Percept.kind``. Salience defaults to 0.5
    but callers can override per-event — promotion of a skill is more
    interesting than the routine completion of a maintenance goal.
    """
    return Percept(
        id=_new_id(),
        source="internal",
        kind=event_kind,
        timestamp=time.time(),
        payload=dict(payload or {}),
        suggested_salience=float(suggested_salience),
        correlation_id=correlation_id,
    )


# ------------------------------------------------------------------- registry


class _AlertToBusAdapter:
    """Tiny adapter exposing ``async push(alert)`` so a ProcessWatcher
    pointed at it ends up forwarding alerts to the real PerceptionBus
    as Percepts.

    Kept private — callers see only :class:`PerceptSourceRegistry`.
    """

    def __init__(self, bus: PerceptionBus) -> None:
        self._bus = bus

    async def push(self, alert: Any) -> None:
        # Defensive: bus.push receives whatever ProcessWatcher hands us.
        # If somebody points us at this adapter and pushes a non-alert
        # we still produce a percept rather than blowing up.
        try:
            percept = make_process_alert_percept(alert)
        except Exception:  # noqa: BLE001 — observability path; never crash
            logger.exception("PerceptSources: failed to coerce alert; dropping")
            return
        try:
            await self._bus.push(percept)
        except Exception:  # noqa: BLE001
            logger.exception("PerceptSources: bus.push failed; dropping")


class PerceptSourceRegistry:
    """Thin lifespan helper that attaches percept producers to a bus.

    The lifespan in :mod:`xmclaw.daemon.app` constructs each producer
    (gated on the producer's own config flag) and hands the live
    instance to the matching ``attach_*`` method. Each attach is a
    no-op when the producer is ``None`` — that's how we get the
    "per-source opt-in" property: producer is absent → percept-source
    silently skips.

    Detach happens on shutdown via :meth:`detach_all`. Detach restores
    the producer's original callback / bus where we replaced one, so a
    stop-then-start cycle doesn't double-fire.
    """

    def __init__(self, bus: PerceptionBus) -> None:
        self._bus = bus
        # Subscriber ids on the bus — kept so we can unsubscribe on detach
        # if a future producer is connected via ``bus.subscribe`` instead
        # of a callback swap.
        self._sub_ids: list[str] = []
        # File-watcher hooks: stash the original callback so we restore
        # it on detach. The watcher already supports a single callback.
        self._file_watcher_state: list[tuple[Any, Any]] = []
        # Process-watcher hooks: stash the original ``_bus`` attribute.
        self._process_watcher_state: list[tuple[Any, Any]] = []
        # AgentLoop user-message hook: stash the original perception_bus
        # so we can restore on detach.
        self._agent_loops: list[tuple[Any, Any]] = []
        # Cron runner hooks: stash callbacks we registered.
        self._cron_runners: list[tuple[Any, Any]] = []

    # ----------------------------------------------------------- file events

    async def attach_file_watcher(self, watcher: Any) -> None:
        """Subscribe ``watcher`` to forward FilePercept events to the bus.

        Wraps the watcher's existing ``callback`` field. If a callback
        was already set (defensive — currently the lifespan does not
        set one), we chain ours BEHIND it so neither wins exclusively.
        """
        if watcher is None:
            return
        original = getattr(watcher, "callback", None)
        bus = self._bus

        async def _adapter(file_percept: Any) -> None:
            # Forward to the original first if there was one — preserves
            # any existing user wiring. Then push the bus percept.
            if original is not None:
                try:
                    await original(file_percept)
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "PerceptSources: original file_watcher callback "
                        "raised; continuing to forward"
                    )
            try:
                await bus.push(make_file_event_percept(file_percept))
            except Exception:  # noqa: BLE001
                logger.exception("PerceptSources: file -> bus push failed")

        try:
            setattr(watcher, "callback", _adapter)
        except Exception:  # noqa: BLE001 — duck-typed; best effort
            logger.exception("PerceptSources: could not set file watcher callback")
            return
        self._file_watcher_state.append((watcher, original))

    # -------------------------------------------------------- process events

    async def attach_process_watcher(self, watcher: Any) -> None:
        """Point ``watcher`` at an alert→percept adapter.

        ProcessWatcher already pushes alerts via ``self._bus.push(alert)``
        on a wired bus. We swap that bus for an adapter that converts
        alerts to percepts and forwards to the real PerceptionBus. The
        original bus (commonly ``None``) is preserved on the registry so
        :meth:`detach_all` restores it.
        """
        if watcher is None:
            return
        original_bus = getattr(watcher, "_bus", None)
        try:
            setattr(watcher, "_bus", _AlertToBusAdapter(self._bus))
        except Exception:  # noqa: BLE001
            logger.exception(
                "PerceptSources: could not set process watcher bus"
            )
            return
        self._process_watcher_state.append((watcher, original_bus))

    # -------------------------------------------------------------- WS hook

    def attach_user_message_hook(self, agent_loop: Any) -> None:
        """Inject the bus into ``agent_loop`` so each ``run_turn`` pushes a
        ``user_msg`` percept.

        We DO NOT subclass / monkey-patch ``run_turn``. Instead we set
        the public attribute ``_perception_bus`` that ``AgentLoop``
        reads inside the existing ``run_turn`` method (see the matching
        edit in ``xmclaw/daemon/agent_loop.py``). When this hook is not
        attached, the attribute is missing / None and ``run_turn`` is
        the same code path as today.
        """
        if agent_loop is None:
            return
        original = getattr(agent_loop, "_perception_bus", None)
        try:
            setattr(agent_loop, "_perception_bus", self._bus)
        except Exception:  # noqa: BLE001 — duck-typed; best effort
            logger.exception(
                "PerceptSources: could not inject perception_bus on agent_loop"
            )
            return
        self._agent_loops.append((agent_loop, original))

    # ------------------------------------------------------------- cron hook

    def attach_cron_hook(self, cron_runner: Any) -> None:
        """Register a fire-callback on ``cron_runner`` that pushes a
        cron-tick percept.

        The cron runner is duck-typed on the
        ``add_fire_callback(callable)`` shape used elsewhere in the
        codebase. If the runner doesn't expose that, this attach is a
        no-op (most production runners do; tests pass a fake).
        """
        if cron_runner is None:
            return
        register = getattr(cron_runner, "add_fire_callback", None)
        if not callable(register):
            return
        bus = self._bus

        async def _on_fire(job_id: str, job_name: str, fired_at: float) -> None:
            try:
                await bus.push(
                    make_cron_tick_percept(job_id, job_name, fired_at)
                )
            except Exception:  # noqa: BLE001 — observability path
                logger.exception("PerceptSources: cron -> bus push failed")

        try:
            register(_on_fire)
        except Exception:  # noqa: BLE001
            logger.exception("PerceptSources: cron add_fire_callback raised")
            return
        self._cron_runners.append((cron_runner, _on_fire))

    # ------------------------------------------------------------ shutdown

    async def detach_all(self) -> None:
        """Restore any producer state we mutated. Idempotent."""
        # File watchers
        for watcher, original_cb in self._file_watcher_state:
            try:
                setattr(watcher, "callback", original_cb)
            except Exception:  # noqa: BLE001
                logger.exception("PerceptSources: file watcher detach failed")
        self._file_watcher_state.clear()

        # Process watchers
        for watcher, original_bus in self._process_watcher_state:
            try:
                setattr(watcher, "_bus", original_bus)
            except Exception:  # noqa: BLE001
                logger.exception("PerceptSources: process watcher detach failed")
        self._process_watcher_state.clear()

        # Agent loops
        for agent_loop, original_bus in self._agent_loops:
            try:
                setattr(agent_loop, "_perception_bus", original_bus)
            except Exception:  # noqa: BLE001
                logger.exception("PerceptSources: agent_loop detach failed")
        self._agent_loops.clear()

        # Cron runners — best-effort. Some runners don't expose remove.
        for runner, callback in self._cron_runners:
            remove = getattr(runner, "remove_fire_callback", None)
            if callable(remove):
                try:
                    remove(callback)
                except Exception:  # noqa: BLE001
                    logger.exception("PerceptSources: cron detach failed")
        self._cron_runners.clear()

        # Generic subscribers we may have registered on the bus directly.
        for sub_id in self._sub_ids:
            try:
                self._bus.unsubscribe(sub_id)
            except Exception:  # noqa: BLE001
                logger.exception("PerceptSources: bus.unsubscribe failed")
        self._sub_ids.clear()

    # Allow ``async with`` usage in tests / lifespan helpers without
    # forcing it on callers — a light convenience.
    async def __aenter__(self) -> "PerceptSourceRegistry":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.detach_all()


__all__ = [
    "make_user_msg_percept",
    "make_file_event_percept",
    "make_cron_tick_percept",
    "make_process_alert_percept",
    "make_internal_event_percept",
    "PerceptSourceRegistry",
]


# Silence "asyncio imported but unused" — kept in scope for type-hints
# and to make future async helpers in this module cheap to add without
# a churn on imports.
_ = asyncio
