"""ProcessWatcher — Jarvis Phase 6.6 PID monitoring.

Polls watched PIDs every ``poll_interval_s`` and emits :class:`ProcessAlert`
percepts whenever a watched process crosses a CPU / memory threshold,
becomes a zombie, or exits unexpectedly. Alerts are pushed to a duck-typed
``bus`` (anything exposing ``async def push(percept)`` — typically
:class:`xmclaw.cognition.perception_bus.PerceptionBus`).

``psutil`` is **lazy-imported** inside :meth:`ProcessWatcher.start` /
:meth:`ProcessWatcher._import_psutil` so importing this module never
forces the optional dependency. The pyproject extra is added in a
follow-up wiring ticket; until then a clear ``pip install psutil``
hint is raised on first ``start()``.

See ``docs/JARVIS_PHASE_6_DESIGN.md`` §3.7 for the design rationale.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

logger = logging.getLogger(__name__)


ProcessAlertKind = Literal[
    "cpu_high", "memory_high", "zombie", "exited", "ghost"
]


@dataclass(frozen=True, slots=True)
class ProcessWatchSpec:
    """A single PID's monitoring configuration.

    ``description`` is human-readable text echoed back into alerts so
    downstream UIs / planners can render "Training run X has been hot
    for 2 hours" without bookkeeping a separate label table.
    """

    pid: int
    description: str
    cpu_threshold: float = 90.0  # percent
    memory_threshold_mb: float = 2048.0
    alert_on_zombie: bool = True
    alert_on_exit: bool = True


@dataclass(frozen=True, slots=True)
class ProcessAlert:
    """One alert emitted by a poll pass."""

    watch_id: str
    pid: int
    description: str
    kind: ProcessAlertKind
    timestamp: float
    payload: dict[str, Any] = field(default_factory=dict)


class ProcessWatcher:
    """Polls watched PIDs and pushes :class:`ProcessAlert` to the bus.

    The bus is duck-typed — anything exposing ``async def push(percept)``
    works. ``psutil`` is imported lazily; constructing a watcher without
    psutil installed is safe, but :meth:`start` (or :meth:`_poll_once`)
    will raise :class:`ImportError` with a ``pip install psutil`` hint.
    """

    def __init__(
        self,
        bus: Any | None = None,
        poll_interval_s: float = 30.0,
    ) -> None:
        self._bus = bus
        self._poll_interval_s = poll_interval_s
        self._watches: dict[str, ProcessWatchSpec] = {}
        self._lock = asyncio.Lock()
        self._running = False
        self._task: asyncio.Task[Any] | None = None
        self._psutil: Any | None = None

    # ------------------------------------------------------------------ API

    async def watch(self, spec: ProcessWatchSpec) -> str:
        """Register a watch; returns the assigned ``watch_id``."""
        watch_id = uuid.uuid4().hex
        async with self._lock:
            self._watches[watch_id] = spec
        return watch_id

    async def unwatch(self, watch_id: str) -> bool:
        """Remove a watch; returns ``True`` if it existed."""
        async with self._lock:
            return self._watches.pop(watch_id, None) is not None

    async def list_watches(self) -> list[tuple[str, ProcessWatchSpec]]:
        """Return ``(watch_id, spec)`` pairs in insertion order."""
        async with self._lock:
            return list(self._watches.items())

    async def start(self) -> None:
        """Start the background poll loop.

        Imports ``psutil`` here so plain ``import`` of this module does
        not force the optional dependency. Idempotent: a second call
        while running is a no-op.
        """
        if self._running:
            return
        # Force the lazy import up front so the user gets the friendly
        # error message immediately, not on first poll tick.
        self._psutil = self._import_psutil()
        self._running = True
        self._task = asyncio.create_task(self._poll_loop(), name="process-watcher")

    async def stop(self) -> None:
        """Cancel the poll loop and wait for it to drain."""
        self._running = False
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 — defensive; task swallows already
                logger.exception("ProcessWatcher poll task raised on stop")

    # ------------------------------------------------------------- internal

    def _import_psutil(self) -> Any:
        """Lazy-import psutil with a clear remediation hint."""
        try:
            import psutil  # type: ignore[import-untyped,import-not-found,unused-ignore]
        except ImportError as exc:  # pragma: no cover — exercised via tests
            raise ImportError(
                "ProcessWatcher requires psutil. Install with: "
                "`pip install psutil` (an optional `cognition-process` "
                "extra will be added in a follow-up wiring ticket)."
            ) from exc
        return psutil

    async def _poll_loop(self) -> None:
        """Background loop — calls :meth:`_poll_once` on a fixed cadence."""
        while self._running:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — observability shouldn't crash
                logger.exception("ProcessWatcher poll pass failed")
            try:
                await asyncio.sleep(self._poll_interval_s)
            except asyncio.CancelledError:
                raise

    async def _poll_once(self) -> list[ProcessAlert]:
        """One pass over registered watches.

        Returns the alerts produced; also pushes each to ``self._bus``
        when wired. Public so tests can drive a single tick without
        spinning up the loop.
        """
        psutil = self._psutil if self._psutil is not None else self._import_psutil()
        # Cache so repeated calls in the same poll don't re-import.
        self._psutil = psutil

        async with self._lock:
            snapshot = list(self._watches.items())

        alerts: list[ProcessAlert] = []
        now = time.time()

        for watch_id, spec in snapshot:
            try:
                proc = psutil.Process(spec.pid)
            except psutil.NoSuchProcess:
                if spec.alert_on_exit:
                    alerts.append(
                        ProcessAlert(
                            watch_id=watch_id,
                            pid=spec.pid,
                            description=spec.description,
                            kind="exited",
                            timestamp=now,
                            payload={"reason": "no_such_process"},
                        )
                    )
                continue
            except psutil.AccessDenied:
                logger.debug(
                    "ProcessWatcher access denied for pid=%s; skipping", spec.pid
                )
                continue
            except Exception:  # noqa: BLE001 — psutil edge cases
                logger.exception(
                    "ProcessWatcher: unexpected error opening pid=%s", spec.pid
                )
                continue

            alerts.extend(self._evaluate(watch_id, spec, proc, now, psutil))

        if self._bus is not None:
            for alert in alerts:
                try:
                    await self._bus.push(alert)
                except Exception:  # noqa: BLE001 — bus must not crash poll
                    logger.exception(
                        "ProcessWatcher: bus.push failed for alert kind=%s", alert.kind
                    )

        return alerts

    def _evaluate(
        self,
        watch_id: str,
        spec: ProcessWatchSpec,
        proc: Any,
        now: float,
        psutil: Any,
    ) -> list[ProcessAlert]:
        """Apply thresholds + status checks to one process snapshot."""
        out: list[ProcessAlert] = []

        # Status — zombie / dead detection comes first because it can
        # short-circuit the rest of the metric reads.
        try:
            status = proc.status()
        except psutil.NoSuchProcess:
            if spec.alert_on_exit:
                out.append(
                    ProcessAlert(
                        watch_id=watch_id,
                        pid=spec.pid,
                        description=spec.description,
                        kind="exited",
                        timestamp=now,
                        payload={"reason": "no_such_process"},
                    )
                )
            return out
        except psutil.AccessDenied:
            logger.debug("ProcessWatcher: status() denied for pid=%s", spec.pid)
            status = None
        except Exception:  # noqa: BLE001
            logger.exception("ProcessWatcher: status() raised for pid=%s", spec.pid)
            status = None

        if spec.alert_on_zombie and status == getattr(
            psutil, "STATUS_ZOMBIE", "zombie"
        ):
            out.append(
                ProcessAlert(
                    watch_id=watch_id,
                    pid=spec.pid,
                    description=spec.description,
                    kind="zombie",
                    timestamp=now,
                    payload={"status": status},
                )
            )

        # CPU
        cpu: float | None = None
        try:
            cpu = float(proc.cpu_percent(interval=None))
        except psutil.NoSuchProcess:
            if spec.alert_on_exit:
                out.append(
                    ProcessAlert(
                        watch_id=watch_id,
                        pid=spec.pid,
                        description=spec.description,
                        kind="exited",
                        timestamp=now,
                        payload={"reason": "no_such_process"},
                    )
                )
            return out
        except psutil.AccessDenied:
            cpu = None
        except Exception:  # noqa: BLE001
            logger.exception("ProcessWatcher: cpu_percent failed pid=%s", spec.pid)
            cpu = None

        if cpu is not None and cpu >= spec.cpu_threshold:
            out.append(
                ProcessAlert(
                    watch_id=watch_id,
                    pid=spec.pid,
                    description=spec.description,
                    kind="cpu_high",
                    timestamp=now,
                    payload={"cpu_percent": cpu, "threshold": spec.cpu_threshold},
                )
            )

        # Memory
        rss_bytes: float | None = None
        try:
            mem_info = proc.memory_info()
            rss_bytes = float(mem_info.rss)
        except psutil.NoSuchProcess:
            if spec.alert_on_exit:
                out.append(
                    ProcessAlert(
                        watch_id=watch_id,
                        pid=spec.pid,
                        description=spec.description,
                        kind="exited",
                        timestamp=now,
                        payload={"reason": "no_such_process"},
                    )
                )
            return out
        except psutil.AccessDenied:
            rss_bytes = None
        except Exception:  # noqa: BLE001
            logger.exception("ProcessWatcher: memory_info failed pid=%s", spec.pid)
            rss_bytes = None

        if rss_bytes is not None:
            rss_mb = rss_bytes / 1024.0 / 1024.0
            if rss_mb >= spec.memory_threshold_mb:
                out.append(
                    ProcessAlert(
                        watch_id=watch_id,
                        pid=spec.pid,
                        description=spec.description,
                        kind="memory_high",
                        timestamp=now,
                        payload={
                            "memory_mb": rss_mb,
                            "threshold_mb": spec.memory_threshold_mb,
                        },
                    )
                )

        return out
