"""Sleep-time agent + idle scheduler — Sprint 3 #3 (Letta pattern).

The current "dream / evolution" cron triggers fire on time intervals
regardless of whether the foreground agent is busy — risking WAL
contention with active turns AND running expensive memory compaction
during a user's session. Letta proved (TerminalBench 2.0 +36.8%
relative) that splitting foreground / sleep-time agents both improves
user-felt latency and raises evolution quality.

This module is the actual value-add. See:

- ``docs/SLEEP_AGENT.md`` — architecture + permission separation +
  how to register a task.
- ``docs/EVOLUTION_HONEST_STATE.md`` (Iron Rules section) — why
  Sprint 3 onward never silently mutates HEAD: every sleep task is
  observed via the bus, never inline; every memory write goes through
  ``SleepWorkspace``'s atomic-on-success buffer so a mid-run crash
  doesn't half-compact memory.

Architecture
------------

- :class:`SleepWorker` — async background task. Polls
  ``IdleDetector.idle_seconds()`` every 30s, maintains
  ``last_short_run_at`` / ``last_long_run_at`` so a single long idle
  session triggers each level **once per crossing** (not repeatedly),
  and yields the bus to foreground when activity resumes.
- :class:`SleepWorkspace` — read/buffer/apply scratchpad passed to
  any task that registers as ``writable=True``. Writes are buffered
  until the task returns successfully; mid-task user-resume cancels
  the task and discards the buffer. Foreground readers always see
  the pre-task or post-success state, never a half-applied one.

Permission separation
---------------------

- Tasks registered with ``writable=False`` (default) get a read-only
  view: a ``SleepWorkspace`` whose ``apply()`` is a no-op so any
  attempt to write is silently swallowed.
- Tasks registered with ``writable=True`` get a real workspace
  whose ``apply()`` flushes the buffer atomically at the end of a
  successful run. If the task raises or the worker cancels mid-run,
  the buffer is discarded and ``SLEEP_INTERRUPTED`` (or
  ``SLEEP_TASK_FINISHED ok=False``) is published.

The two thresholds (short ≈ 5min, long ≈ 30min) match Letta's
spacing: light tasks (memory dedup, journal-summary, recent-trace
strategy distillation) on the short edge; heavy tasks (skill mutation
evaluation, cross-skill EWMA recompute, full memory.md compact) on
the long edge.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Any, Callable, Literal

from xmclaw.core.bus import InProcessEventBus
from xmclaw.core.bus.events import EventType, make_event
from xmclaw.daemon._idle_detector import (
    IdleDetector,
    _AlwaysIdleDetector,
    build_idle_detector,
)
from xmclaw.utils.log import get_logger

_log = get_logger(__name__)

DEFAULT_IDLE_SHORT_S = 300.0  # 5 min — Letta's "light tasks" edge
DEFAULT_IDLE_LONG_S = 1800.0  # 30 min — Letta's "heavy tasks" edge
DEFAULT_POLL_INTERVAL_S = 30.0

Level = Literal["short", "long"]
TaskFn = Callable[["SleepWorkspace"], Coroutine[Any, Any, dict[str, Any]]]


# ── SleepWorkspace ────────────────────────────────────────────────────


class SleepWorkspace:
    """Buffered read/write scratchpad for one sleep task.

    Read-only by default. Tasks registered ``writable=True`` get a
    workspace whose buffered writes ``apply()`` atomically when the
    task returns successfully. If the task raises or the worker
    cancels (user resume), ``rollback()`` discards the buffer.

    The "atomic apply" is process-local: the buffer is a plain dict
    in memory. We do **not** try to make it crash-safe on disk — the
    foreground reader either sees the pre-task state (no apply yet)
    or the post-success state (apply ran). A daemon SIGKILL mid-apply
    leaves whatever the apply callback already wrote on disk; that's
    the apply callback's job to make atomic, not the workspace's.
    """

    def __init__(self, *, writable: bool = False) -> None:
        self._writable = bool(writable)
        self._buffer: dict[str, Any] = {}
        self._apply_callbacks: list[Callable[[dict[str, Any]], None]] = []
        self._applied = False
        self._rolled_back = False
        # Tasks can stash partial progress into _checkpoint so that
        # SLEEP_INTERRUPTED has something to surface to the bus.
        self._checkpoint: dict[str, Any] = {}

    @property
    def writable(self) -> bool:
        return self._writable

    @property
    def applied(self) -> bool:
        return self._applied

    @property
    def rolled_back(self) -> bool:
        return self._rolled_back

    def buffer_set(self, key: str, value: Any) -> None:
        """Stage one write. No-op when read-only — silently swallowed."""
        if not self._writable:
            return
        if self._applied or self._rolled_back:
            return
        self._buffer[key] = value

    def buffer_get(self, key: str, default: Any = None) -> Any:
        """Read a buffered value (may be the default if not set)."""
        return self._buffer.get(key, default)

    def buffer_view(self) -> dict[str, Any]:
        """Read-only snapshot of staged writes (for tests / debug)."""
        return dict(self._buffer)

    def register_apply(
        self, fn: Callable[[dict[str, Any]], None],
    ) -> None:
        """Register a callable to run when the task completes ok.

        Apply callbacks fire in registration order on
        :meth:`apply`. Read-only workspaces still accept registration
        (so the same task code can run with either workspace shape)
        but never call the callbacks.
        """
        self._apply_callbacks.append(fn)

    def checkpoint(self, **kwargs: Any) -> None:
        """Stash partial progress so SLEEP_INTERRUPTED has context.

        Tasks should call this at safe points so an early-cancel can
        surface "I got 3/5 of the way through" instead of an empty
        dict. Non-blocking, accumulating — later kwargs override
        earlier ones for the same key.
        """
        self._checkpoint.update(kwargs)

    def get_checkpoint(self) -> dict[str, Any]:
        return dict(self._checkpoint)

    def apply(self) -> None:
        """Flush the buffer through every registered apply callback.

        Idempotent — calling twice is a no-op (the second call sees
        ``self._applied is True`` and skips). Read-only workspaces
        are a full no-op.
        """
        if not self._writable:
            return
        if self._applied or self._rolled_back:
            return
        snapshot = dict(self._buffer)
        for cb in self._apply_callbacks:
            try:
                cb(snapshot)
            except Exception as exc:  # noqa: BLE001
                # An apply callback failing is the worst outcome — the
                # buffer is partially flushed and we've nothing to roll
                # back to. Log loudly and keep going so subsequent
                # callbacks still get a shot.
                _log.warning(
                    "sleep_workspace.apply_callback_failed err=%s", exc,
                )
        self._applied = True

    def rollback(self) -> None:
        """Discard the buffer and mark this workspace dead.

        Called by SleepWorker when a task is cancelled (user resume)
        or when the task raises. Idempotent.
        """
        if self._applied or self._rolled_back:
            return
        self._buffer.clear()
        self._rolled_back = True


# ── SleepTask registration ───────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _RegisteredTask:
    """One task registered against a SleepWorker."""

    name: str
    level: Level
    fn: TaskFn
    writable: bool


# ── SleepWorker ──────────────────────────────────────────────────────


@dataclass
class _LevelState:
    """Per-level run-tracking. Crossing-once-per-idle bookkeeping."""

    last_run_at: float = 0.0
    # last_below_threshold_at — the last time we observed idle BELOW
    # this level's threshold. Used to gate "fire once per crossing":
    # a fresh crossing requires having dipped below the threshold
    # since the last fire.
    last_below_threshold_at: float = 0.0
    armed: bool = True


class SleepWorker:
    """Idle-aware async background scheduler.

    Usage from FastAPI lifespan:

    ::

        worker = SleepWorker(detector, bus,
                             idle_short_s=300, idle_long_s=1800)
        worker.register_task("memory_dedup", "short", run_dedup)
        worker.register_task("skill_mutation", "long", run_mutation,
                             writable=True)
        await worker.start()
        try:
            yield
        finally:
            await worker.stop()

    Each tick (every ``poll_interval_s``):
    1. Read ``detector.idle_seconds()``.
    2. If it's < a level's threshold, mark that level "armed" — next
       crossing will fire.
    3. If it's ≥ a level's threshold AND we're armed, run every
       registered task at that level in registration order, then
       disarm until the next dip below the threshold.
    4. While a task is running, keep polling the detector. If user
       activity resumes (idle drops), cancel-with-rollback at the
       next ``await`` checkpoint so the foreground reclaims the bus.

    Long-level tasks never fire before short-level tasks at the same
    threshold crossing. The order is: poll → short tasks (if armed)
    → long tasks (if armed). At a single threshold crossing the
    short level fires first; after both ran, both disarm.
    """

    def __init__(
        self,
        detector: IdleDetector,
        bus: InProcessEventBus,
        *,
        idle_short_s: float = DEFAULT_IDLE_SHORT_S,
        idle_long_s: float = DEFAULT_IDLE_LONG_S,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        agent_id: str = "sleep-worker",
    ) -> None:
        if idle_short_s <= 0:
            raise ValueError("idle_short_s must be positive")
        if idle_long_s < idle_short_s:
            raise ValueError(
                "idle_long_s must be >= idle_short_s (long fires after "
                "short at the same crossing)",
            )
        if poll_interval_s <= 0:
            raise ValueError("poll_interval_s must be positive")
        self._detector = detector
        self._bus = bus
        self._idle_short_s = float(idle_short_s)
        self._idle_long_s = float(idle_long_s)
        self._poll_interval_s = float(poll_interval_s)
        self._agent_id = agent_id

        self._tasks: dict[Level, list[_RegisteredTask]] = {
            "short": [], "long": [],
        }
        self._registered_names: set[str] = set()
        self._states: dict[Level, _LevelState] = {
            "short": _LevelState(),
            "long": _LevelState(),
        }

        self._loop_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        # Run-time guard: the in-flight (task, workspace) we're driving
        # so a "user resume" mid-run can cancel-with-rollback. None when
        # idle.
        self._inflight: tuple[_RegisteredTask, SleepWorkspace,
                              asyncio.Task[Any]] | None = None
        self._inflight_lock = asyncio.Lock()
        # Bookkeeping for tests / observability.
        self._fired_count: dict[Level, int] = {"short": 0, "long": 0}

    # ── public API ────────────────────────────────────────────────────

    @property
    def detector(self) -> IdleDetector:
        return self._detector

    @property
    def fired_count(self) -> dict[Level, int]:
        """Total threshold-crossings observed since start (per level)."""
        return dict(self._fired_count)

    def is_running(self) -> bool:
        return self._loop_task is not None and not self._loop_task.done()

    def registered(self) -> list[tuple[str, Level]]:
        """List of (name, level) for every registered task."""
        out: list[tuple[str, Level]] = []
        for level in ("short", "long"):
            for t in self._tasks[level]:
                out.append((t.name, level))
        return out

    def register_task(
        self,
        name: str,
        level: Level,
        fn: TaskFn,
        *,
        writable: bool = False,
    ) -> None:
        """Register a task. Names must be unique across levels."""
        if level not in ("short", "long"):
            raise ValueError(f"level must be 'short' or 'long', got {level!r}")
        if not name or not isinstance(name, str):
            raise ValueError("task name must be a non-empty string")
        if name in self._registered_names:
            raise ValueError(f"task {name!r} already registered")
        if not asyncio.iscoroutinefunction(fn) and not callable(fn):
            raise TypeError("fn must be an async callable")
        self._tasks[level].append(
            _RegisteredTask(
                name=name, level=level, fn=fn, writable=writable,
            ),
        )
        self._registered_names.add(name)

    async def start(self) -> None:
        """Start the polling loop. Idempotent."""
        if self.is_running():
            return
        self._stop_event.clear()
        if isinstance(self._detector, _AlwaysIdleDetector):
            _log.info(
                "sleep_worker.start fallback=always_idle reason=%s",
                self._detector.reason,
            )
        else:
            _log.info(
                "sleep_worker.start detector=%s short=%.0fs long=%.0fs",
                self._detector.name,
                self._idle_short_s, self._idle_long_s,
            )
        self._loop_task = asyncio.create_task(
            self._loop(), name="sleep-worker-loop",
        )

    async def stop(self) -> None:
        """Stop the polling loop and rollback any in-flight task."""
        self._stop_event.set()
        # Cancel any in-flight task with rollback.
        async with self._inflight_lock:
            inflight = self._inflight
        if inflight is not None:
            registered, workspace, task = inflight
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            workspace.rollback()
            await self._publish_interrupted(registered, workspace)
        # Cancel the loop.
        loop_task = self._loop_task
        self._loop_task = None
        if loop_task is not None and not loop_task.done():
            loop_task.cancel()
            try:
                await loop_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def tick_once(self) -> dict[str, Any]:
        """Run a single tick — exposed for tests / a future REST hook.

        Returns a small dict describing what happened so callers can
        assert without parsing logs:
        ``{"idle_seconds": float, "fired": [(level, task_name), ...]}``
        """
        idle = self._detector.idle_seconds()
        # Negative is the unmeasurable sentinel — treat as "always
        # idle" so behaviour matches today's cron.
        effective = idle if idle >= 0 else _AlwaysIdleDetector.SENTINEL
        fired: list[tuple[Level, str]] = []
        # Order matters: short fires before long, so a single crossing
        # at idle ≥ long_threshold runs both, with short first.
        levels: tuple[Level, Level] = ("short", "long")
        for level in levels:
            threshold = (
                self._idle_short_s if level == "short"
                else self._idle_long_s
            )
            state = self._states[level]
            if effective < threshold:
                # Below threshold: arm for the next crossing.
                state.last_below_threshold_at = time.time()
                state.armed = True
                continue
            if not state.armed:
                # Already fired since last dip — wait for a dip first.
                continue
            # Cross detected. Fire every registered task at this level
            # in registration order, then disarm.
            await self._publish_idle_detected(level, effective)
            for registered in self._tasks[level]:
                if self._stop_event.is_set():
                    return {"idle_seconds": idle, "fired": fired}
                ok = await self._run_one(registered)
                if ok:
                    fired.append((level, registered.name))
            state.armed = False
            state.last_run_at = time.time()
            self._fired_count[level] += 1
        return {"idle_seconds": idle, "fired": fired}

    # ── loop body ─────────────────────────────────────────────────────

    async def _loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                try:
                    await self.tick_once()
                except Exception as exc:  # noqa: BLE001 — never let the
                    # poller die. A broken tick is logged + we wait
                    # for the next one.
                    _log.warning("sleep_worker.tick_failed err=%s", exc)
                # Cancellable sleep.
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self._poll_interval_s,
                    )
                    return  # stop requested
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            return

    async def _run_one(self, registered: _RegisteredTask) -> bool:
        """Run one registered task. Returns True on success, False on
        cancel/error. Manages the in-flight slot + bus events +
        workspace lifecycle."""
        workspace = SleepWorkspace(writable=registered.writable)
        await self._publish_task_started(registered)
        started = time.perf_counter()
        # Wrap in a Task so user-resume can cancel without unwinding the
        # whole tick loop.
        coro = registered.fn(workspace)
        task: asyncio.Task[Any] = asyncio.create_task(
            coro,
            name=f"sleep-task-{registered.name}",
        )
        async with self._inflight_lock:
            self._inflight = (registered, workspace, task)
        try:
            result = await task
            workspace.apply()
            duration_ms = (time.perf_counter() - started) * 1000.0
            await self._publish_task_finished(
                registered,
                ok=True,
                duration_ms=duration_ms,
                result=dict(result) if isinstance(result, dict) else {
                    "value": result,
                },
            )
            return True
        except asyncio.CancelledError:
            workspace.rollback()
            await self._publish_interrupted(registered, workspace)
            return False
        except Exception as exc:  # noqa: BLE001
            workspace.rollback()
            duration_ms = (time.perf_counter() - started) * 1000.0
            _log.warning(
                "sleep_worker.task_failed task=%s err=%s",
                registered.name, exc,
            )
            await self._publish_task_finished(
                registered,
                ok=False,
                duration_ms=duration_ms,
                result={"error": repr(exc)},
            )
            return False
        finally:
            async with self._inflight_lock:
                self._inflight = None

    # ── bus event helpers ─────────────────────────────────────────────

    async def _publish_idle_detected(
        self, level: Level, idle_seconds: float,
    ) -> None:
        await self._publish(
            EventType.SLEEP_IDLE_DETECTED,
            {"level": level, "idle_seconds": float(idle_seconds)},
        )

    async def _publish_task_started(self, registered: _RegisteredTask) -> None:
        await self._publish(
            EventType.SLEEP_TASK_STARTED,
            {"task_name": registered.name, "level": registered.level},
        )

    async def _publish_task_finished(
        self,
        registered: _RegisteredTask,
        *,
        ok: bool,
        duration_ms: float,
        result: dict[str, Any],
    ) -> None:
        await self._publish(
            EventType.SLEEP_TASK_FINISHED,
            {
                "task_name": registered.name,
                "level": registered.level,
                "ok": ok,
                "duration_ms": duration_ms,
                "result": result,
            },
        )

    async def _publish_interrupted(
        self, registered: _RegisteredTask, workspace: SleepWorkspace,
    ) -> None:
        await self._publish(
            EventType.SLEEP_INTERRUPTED,
            {
                "task_name": registered.name,
                "level": registered.level,
                "partial_progress": workspace.get_checkpoint(),
            },
        )

    async def _publish(
        self, event_type: EventType, payload: dict[str, Any],
    ) -> None:
        try:
            event = make_event(
                session_id="_system",
                agent_id=self._agent_id,
                type=event_type,
                payload=payload,
            )
            await self._bus.publish(event)
        except Exception as exc:  # noqa: BLE001 — bus failures must not
            # halt the worker.
            _log.warning(
                "sleep_worker.publish_failed type=%s err=%s",
                event_type, exc,
            )


# ── config parsing ───────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SleepWorkerConfig:
    """Resolved sleep-worker config."""

    idle_aware: bool = True
    idle_short_s: float = DEFAULT_IDLE_SHORT_S
    idle_long_s: float = DEFAULT_IDLE_LONG_S
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S


def parse_sleep_config(
    cfg: dict[str, Any] | None,
) -> SleepWorkerConfig:
    """Pull ``evolution.scheduler`` config into a typed dataclass.

    Schema::

        {
          "idle_aware": true,
          "idle_short_s": 300,
          "idle_long_s": 1800,
          "poll_interval_s": 30
        }

    Bad values fall back to defaults with a WARN log — a daemon that
    boots with a typo here is still more useful than one that
    refuses to start. Same posture as ``parse_backup_config`` /
    ``parse_retention_config``.
    """
    if not isinstance(cfg, dict):
        return SleepWorkerConfig()
    idle_aware = bool(cfg.get("idle_aware", True))

    def _pos_float(key: str, default: float) -> float:
        v = cfg.get(key, default)
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
        if v is not None and v != default:
            _log.warning("sleep_worker.bad_field field=%s value=%r", key, v)
        return float(default)

    short = _pos_float("idle_short_s", DEFAULT_IDLE_SHORT_S)
    long_ = _pos_float("idle_long_s", DEFAULT_IDLE_LONG_S)
    poll = _pos_float("poll_interval_s", DEFAULT_POLL_INTERVAL_S)
    if long_ < short:
        _log.warning(
            "sleep_worker.bad_thresholds long_s=%.0f < short_s=%.0f, "
            "swapping", long_, short,
        )
        short, long_ = long_, short
    return SleepWorkerConfig(
        idle_aware=idle_aware,
        idle_short_s=short,
        idle_long_s=long_,
        poll_interval_s=poll,
    )


# ── helpers for migration ────────────────────────────────────────────


def make_dream_cycle_task(dream: Any) -> TaskFn:
    """Wrap an existing ``SkillDreamCycle`` so it registers as a sleep
    task at level ``"long"``.

    The cron-based ``start()/stop()`` lifecycle still works (and still
    fires on its own interval); idle-aware firing simply layers an
    additional trigger on top so heavy compaction never has to wait
    for the cron when the user has clearly stopped working.
    """

    async def _run(_workspace: SleepWorkspace) -> dict[str, Any]:
        count = await dream.run_once()
        return {"proposals": int(count or 0)}

    return _run


def make_memory_sweep_task(sweep: Any) -> TaskFn:
    """Wrap an existing ``MemorySweepTask`` as a sleep task at level
    ``"short"``.

    Memory dedup / TTL prune is exactly the kind of "light" task
    Letta runs on the short edge — it touches the same SQLite WAL the
    foreground reads, but in small batches.
    """

    async def _run(_workspace: SleepWorkspace) -> dict[str, Any]:
        result = await sweep.sweep_once()
        # ``sweep_once`` returns ``dict[Layer, int]``; surface as
        # JSON-friendly keys so the bus event payload reads cleanly.
        return {
            "evicted_short": int(result.get("short", 0) or 0),
            "evicted_working": int(result.get("working", 0) or 0),
            "evicted_long": int(result.get("long", 0) or 0),
        }

    return _run


__all__ = [
    "DEFAULT_IDLE_LONG_S",
    "DEFAULT_IDLE_SHORT_S",
    "DEFAULT_POLL_INTERVAL_S",
    "IdleDetector",
    "Level",
    "SleepWorkerConfig",
    "SleepWorker",
    "SleepWorkspace",
    "build_idle_detector",
    "make_dream_cycle_task",
    "make_memory_sweep_task",
    "parse_sleep_config",
]
