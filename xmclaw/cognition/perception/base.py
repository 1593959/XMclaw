"""PerceptionSource ABC — common shape for the R4 multi-modal sources.

Each source manages an async loop polling its modality (screen /
window / clipboard / calendar) and pushes :class:`Percept` onto a
shared bus. The ABC normalises:

* lifecycle (``start`` / ``stop``)
* error containment (errors NEVER propagate out of the loop —
  one source going bad must not take the daemon down)
* ``available()`` capability check — sources whose optional deps
  aren't installed return False here, and the registry skips them
  without complaint
"""
from __future__ import annotations

import abc
import asyncio
import logging
import time
import uuid
from typing import Any

from xmclaw.cognition.perception_bus import Percept

logger = logging.getLogger(__name__)


class PerceptionSource(abc.ABC):
    """Base class for periodic perception sources.

    Subclasses implement:
      * :meth:`available` — runtime feature flag (e.g. import the
        optional native module; return False on ImportError).
      * :meth:`poll_once` — one tick of perception. Returns a list
        of :class:`Percept` to push (may be empty when nothing
        changed).
      * (optional) :meth:`name` — short identifier used in logs and
        the daemon's source registry.

    The base class handles the loop, sleep cadence, exception
    containment, and bus push.
    """

    def __init__(
        self,
        *,
        bus: Any | None = None,
        period_s: float = 5.0,
    ) -> None:
        self._bus = bus
        self._period_s = max(0.1, float(period_s))
        self._task: asyncio.Task[Any] | None = None
        self._running = False

    # ── Subclass contract ────────────────────────────────────────

    @property
    @abc.abstractmethod
    def name(self) -> str: ...

    @abc.abstractmethod
    def available(self) -> bool:
        """Whether the runtime-time deps for this source are
        installed + the platform supports it. Sources MUST NOT raise
        from here — return False when uncertain."""

    @abc.abstractmethod
    async def poll_once(self) -> list[Percept]:
        """Run one perception tick. Return percepts to push (may be
        empty). MAY raise; the base loop catches + logs."""

    # ── Lifecycle (idempotent) ───────────────────────────────────

    async def start(self) -> None:
        if self._running:
            return
        if not self.available():
            logger.info(
                "perception.%s.unavailable — skipping start", self.name,
            )
            return
        self._running = True
        self._task = asyncio.create_task(
            self._run_loop(), name=f"perception-{self.name}",
        )
        logger.info("perception.%s.started period_s=%.1f",
                    self.name, self._period_s)

    async def stop(self, timeout_s: float = 5.0) -> None:
        self._running = False
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=timeout_s)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass

    async def _run_loop(self) -> None:
        while self._running:
            try:
                percepts = await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "perception.%s.poll_failed err=%s", self.name, exc,
                )
                percepts = []

            for p in percepts:
                if self._bus is None:
                    continue
                try:
                    await self._bus.push(p)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "perception.%s.push_failed err=%s",
                        self.name, exc,
                    )

            try:
                await asyncio.sleep(self._period_s)
            except asyncio.CancelledError:
                raise

    # ── Helpers for subclasses ───────────────────────────────────

    @staticmethod
    def _make_percept(
        source: str, kind: str, payload: dict[str, Any],
        suggested_salience: float | None = None,
    ) -> Percept:
        return Percept(
            id=uuid.uuid4().hex,
            source=source,  # type: ignore[arg-type]
            kind=kind,
            timestamp=time.time(),
            payload=payload,
            suggested_salience=suggested_salience,
        )


__all__ = ["PerceptionSource"]
