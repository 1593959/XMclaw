"""ConfigFileWatcher — hot-reload daemon/config.json (B-109).

The daemon's in-memory ``app.state.config`` is loaded once during
lifespan startup. Until B-109 the only way to push a change into
the running process was to either:

  (a) restart the daemon, or
  (b) round-trip through ``PUT /api/v2/config`` (UI ConfigPage save)
      which both writes the file AND mutates the dict.

Hand-edits via terminal / IDE were invisible to the running daemon.
The Memory page's "向量索引启动失败" symptom (B-87) was a direct
consequence: user edits config.json, daemon never sees it, lifespan-
built indexer keeps using the stale snapshot.

This watcher polls the config file's mtime every ``poll_interval_s``
seconds. On change it re-parses, diffs against the in-memory dict,
swaps the contents, and publishes a CONFIG_RELOADED event with the
list of mutated dotted keys so subscribers (UI, Doctor) can react.

Important caveat: only **values read at runtime** take effect live.
Lifespan-bound subsystems (LLM provider, memory store, indexer,
dream cron, multi-agent registry) are constructed once at startup —
changing their config sections won't rebuild them. The event payload
flags ``restart_required: true`` when one of those sections changed,
so the UI can advise the user.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from xmclaw.utils.log import get_logger

_log = get_logger(__name__)


# Top-level config sections that are RUNTIME-readable — changes to
# these take effect on the next read without a restart. Subscribers
# read the live dict directly so a reload is enough.
_RUNTIME_KEYS: frozenset[str] = frozenset({
    "tools",
    "security",
    "evolution",   # most evolution.* knobs read live (thresholds etc.)
    "backup",      # auto_daily polled by BackupScheduler each tick
    "_comment",
})

# Top-level sections whose change requires a daemon restart. The
# CONFIG_RELOADED event payload's ``restart_required`` flag flips on
# when any of these moved.
_RESTART_BOUND_KEYS: frozenset[str] = frozenset({
    "llm",
    "memory",
    "gateway",
    "runtime",
    "mcp_servers",
    "integrations",
})


def _diff_keys(old: dict[str, Any], new: dict[str, Any], prefix: str = "") -> list[str]:
    """Return dotted keys that differ between two config dicts.

    Recurses into nested dicts; reports leaf-level paths. Lists and
    scalars are compared by JSON equality so identical-but-reordered
    lists count as equal.
    """
    out: list[str] = []
    keys = set(old.keys()) | set(new.keys())
    for k in keys:
        path = f"{prefix}.{k}" if prefix else k
        ov = old.get(k)
        nv = new.get(k)
        if isinstance(ov, dict) and isinstance(nv, dict):
            out.extend(_diff_keys(ov, nv, path))
        elif json.dumps(ov, sort_keys=True) != json.dumps(nv, sort_keys=True):
            out.append(path)
    return out


class ConfigFileWatcher:
    """Background asyncio task that polls the config file mtime."""

    def __init__(
        self,
        *,
        config_path: Path,
        cfg: dict[str, Any],
        bus: "Any | None" = None,
        poll_interval_s: float = 5.0,
    ) -> None:
        self._config_path = Path(config_path)
        self._cfg = cfg
        self._bus = bus
        self._poll_s = max(1.0, float(poll_interval_s))
        self._task: asyncio.Task[Any] | None = None
        self._stopped = asyncio.Event()
        try:
            self._last_mtime = (
                self._config_path.stat().st_mtime
                if self._config_path.is_file() else 0.0
            )
        except OSError:
            self._last_mtime = 0.0

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.is_running:
            return
        self._stopped.clear()
        self._task = asyncio.create_task(
            self._loop(), name="config-file-watcher",
        )

    async def stop(self) -> None:
        if not self.is_running:
            return
        self._stopped.set()
        try:
            await asyncio.wait_for(self._task, timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self._task.cancel()
        self._task = None

    async def _loop(self) -> None:
        while not self._stopped.is_set():
            try:
                await self.tick()
            except Exception as exc:  # noqa: BLE001 — never break the loop
                _log.warning("config_watcher.tick_failed err=%s", exc)
            try:
                await asyncio.wait_for(
                    self._stopped.wait(), timeout=self._poll_s,
                )
                return  # stopped event triggered
            except asyncio.TimeoutError:
                continue

    async def tick(self) -> dict[str, Any] | None:
        """One poll cycle. Returns the change-summary dict if anything
        was reloaded, otherwise ``None``."""
        if not self._config_path.is_file():
            return None
        try:
            mtime = self._config_path.stat().st_mtime
        except OSError:
            return None
        if abs(mtime - self._last_mtime) < 1e-6:
            return None  # unchanged
        try:
            text = self._config_path.read_text(encoding="utf-8")
            new_cfg = json.loads(text)
        except (OSError, json.JSONDecodeError) as exc:
            # Mid-write or syntax error — skip this tick, try again
            # next poll. We DO advance the mtime so we don't keep
            # re-trying the same broken state every tick.
            _log.debug("config_watcher.parse_skip err=%s", exc)
            self._last_mtime = mtime
            return None
        if not isinstance(new_cfg, dict):
            self._last_mtime = mtime
            return None

        changed = _diff_keys(self._cfg, new_cfg)
        if not changed:
            self._last_mtime = mtime
            return None

        # Mutate in place so all the closures / references holding the
        # original dict object see the new values. Same pattern used
        # by ``PUT /api/v2/config``.
        self._cfg.clear()
        self._cfg.update(new_cfg)
        self._last_mtime = mtime

        top_changed = {k.split(".", 1)[0] for k in changed}
        restart_required = bool(top_changed & _RESTART_BOUND_KEYS)
        runtime_only = top_changed.issubset(_RUNTIME_KEYS) and not restart_required

        summary = {
            "changed_keys": changed,
            "top_changed": sorted(top_changed),
            "restart_required": restart_required,
            "runtime_only": runtime_only,
            "mtime": mtime,
        }

        if self._bus is not None:
            try:
                from xmclaw.core.bus import EventType, make_event
                ev = make_event(
                    session_id="_system",
                    agent_id="daemon",
                    type=EventType.CONFIG_RELOADED,
                    payload=summary,
                )
                await self._bus.publish(ev)
            except Exception:  # noqa: BLE001 — telemetry path
                pass
        _log.info(
            "config_watcher.reloaded restart_required=%s changed=%s",
            restart_required, changed[:5],
        )
        return summary
