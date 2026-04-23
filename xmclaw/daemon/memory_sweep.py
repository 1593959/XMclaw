"""Periodic memory maintenance — Epic #5.

Runs ``memory.prune(layer)`` + ``memory.evict(layer, max_items=, max_bytes=)``
at a configurable interval so the sqlite-vec memory file does not grow
unbounded. Wired from the ``create_app`` lifespan hook when the config's
``memory.retention`` section is present.

Design notes:
  * We use an asyncio background task rather than OS cron so the loop
    participates in the same asyncio event loop as the daemon — no
    cross-process file-locking concerns.
  * Each sweep tick iterates all three layers. Eviction is a no-op when
    both ``max_items`` and ``max_bytes`` are ``None`` for a layer, so we
    can always call it — no need to branch per-layer in the caller.
  * A successful eviction emits ``MEMORY_EVICTED`` via the memory
    provider's bus hook; the sweep task itself doesn't publish events.
  * Failures inside a tick are caught and logged — one bad tick must
    not kill the daemon.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Iterable

from xmclaw.providers.memory.base import Layer
from xmclaw.utils.log import get_logger

_log = get_logger(__name__)

_LAYERS: tuple[Layer, ...] = ("short", "working", "long")


@dataclass(frozen=True, slots=True)
class LayerRetention:
    """Per-layer retention caps. ``None`` fields disable that axis."""

    max_items: int | None = None
    max_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """Resolved retention config. Fields are per-layer."""

    short: LayerRetention = LayerRetention()
    working: LayerRetention = LayerRetention()
    long: LayerRetention = LayerRetention()
    sweep_interval_s: float = 3600.0
    prune_by_ttl: bool = True

    def for_layer(self, layer: Layer) -> LayerRetention:
        return getattr(self, layer)

    def any_cap_set(self) -> bool:
        """True when at least one layer has a cap or ttl-prune is on."""
        if self.prune_by_ttl:
            return True
        for layer in _LAYERS:
            cap = self.for_layer(layer)
            if cap.max_items is not None or cap.max_bytes is not None:
                return True
        return False


def parse_retention_config(cfg: dict[str, Any] | None) -> RetentionPolicy:
    """Build a ``RetentionPolicy`` from ``cfg['memory']['retention']``.

    Missing or malformed returns the default (TTL-prune on, no hard
    caps, 1h interval). This function deliberately never raises — a
    daemon that starts with bad retention config is still more useful
    than one that refuses to boot. Warnings go to the log.
    """
    if not isinstance(cfg, dict):
        return RetentionPolicy()

    def _per_layer(name: str) -> dict[str, int | None]:
        raw = cfg.get(name)
        if not isinstance(raw, dict):
            return {}
        out: dict[str, int | None] = {}
        for layer in _LAYERS:
            v = raw.get(layer)
            if v is None:
                out[layer] = None
            elif isinstance(v, int) and v > 0:
                out[layer] = v
            else:
                _log.warning(
                    "memory_sweep.bad_cap",
                    field=f"{name}.{layer}",
                    value=repr(v),
                )
                out[layer] = None
        return out

    max_items = _per_layer("max_items")
    max_bytes = _per_layer("max_bytes")

    interval_raw = cfg.get("sweep_interval_s", 3600)
    if isinstance(interval_raw, (int, float)) and interval_raw > 0:
        interval = float(interval_raw)
    else:
        _log.warning(
            "memory_sweep.bad_interval",
            value=repr(interval_raw),
        )
        interval = 3600.0

    prune_by_ttl = bool(cfg.get("prune_by_ttl", True))

    per_layer = {
        layer: LayerRetention(
            max_items=max_items.get(layer),
            max_bytes=max_bytes.get(layer),
        )
        for layer in _LAYERS
    }
    return RetentionPolicy(
        short=per_layer["short"],
        working=per_layer["working"],
        long=per_layer["long"],
        sweep_interval_s=interval,
        prune_by_ttl=prune_by_ttl,
    )


class MemorySweepTask:
    """Background periodic sweep driver.

    Usage (from FastAPI lifespan):

    ::

        sweep = MemorySweepTask(memory, policy)
        await sweep.start()
        try:
            yield
        finally:
            await sweep.stop()

    In tests, call ``sweep_once()`` directly to exercise one tick
    without waiting real wallclock.
    """

    def __init__(
        self,
        memory: Any,
        policy: RetentionPolicy,
        *,
        layers: Iterable[Layer] = _LAYERS,
    ) -> None:
        self._memory = memory
        self._policy = policy
        self._layers = tuple(layers)
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    async def sweep_once(self) -> dict[Layer, int]:
        """Run one pass across all configured layers.

        Returns the evicted-count per layer for observability / tests.
        Failures are caught per-layer so a broken ``long`` tier doesn't
        stall the ``short`` / ``working`` sweep.
        """
        removed: dict[Layer, int] = {}
        for layer in self._layers:
            try:
                count = await self._sweep_layer(layer)
            except Exception as exc:  # noqa: BLE001 — a bad tick must not kill the daemon
                _log.warning(
                    "memory_sweep.layer_failed",
                    layer=layer,
                    error=repr(exc),
                )
                count = 0
            removed[layer] = count
        return removed

    async def _sweep_layer(self, layer: Layer) -> int:
        total = 0
        if self._policy.prune_by_ttl:
            total += await self._memory.prune(layer)
        cap = self._policy.for_layer(layer)
        if cap.max_items is not None or cap.max_bytes is not None:
            total += await self._memory.evict(
                layer,
                max_items=cap.max_items,
                max_bytes=cap.max_bytes,
            )
        return total

    async def start(self) -> None:
        if self._task is not None:
            return
        if not self._policy.any_cap_set():
            # Nothing to do — don't even start the loop.
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._loop(), name="memory_sweep")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop_event.set()
        task = self._task
        self._task = None
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    async def _loop(self) -> None:
        interval = self._policy.sweep_interval_s
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=interval,
                )
                return  # stop requested
            except asyncio.TimeoutError:
                pass
            await self.sweep_once()
