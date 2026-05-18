"""5-layer feature-flag resolution engine.

Resolution priority on every lookup (first match wins):

  1. **env override** — ``XMC_FF_<NAME>`` env var. Highest priority
     so tests + ad-hoc debug sessions can pin a value without
     touching disk or remote services. Parsed as JSON when possible
     (``"true"`` / ``"false"`` / ``"1"`` / ``"42"`` / ``"[1,2]"``);
     otherwise treated as a raw string.

  2. **memory cache** — in-process dict updated by ``set(name, val)``
     and by successful remote pulls. Survives the request lifetime
     but evaporates on daemon restart.

  3. **disk cache** — ``~/.xmclaw/v2/features.json``. Survives daemon
     restarts so the user's flag state is durable. Written every time
     a flag is mutated; loaded on engine construction.

  4. **remote provider** — pluggable. The default is a no-op
     (``NoopRemoteProvider``) so XMclaw with no flag service
     configured just falls through to defaults. Plug a real
     GrowthBook / LaunchDarkly / Unleash client by passing it to
     the engine constructor.

  5. **registered default** — the ``FeatureFlag.default`` declared
     in ``registry.py``. When NOTHING above matched, this is the
     final answer.

Hot-path concern: ``is_enabled`` and ``variant`` are called from
inside loops (e.g. on every tool dispatch). Layer-1 (env) is a
``os.environ`` lookup (fast). Layer-2 (memory) is a dict get. The
expensive layers (3 disk, 4 remote) are only consulted on misses
of the memory cache. Disk is loaded once at boot.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Protocol

from xmclaw.core.feature_flags.flags import FeatureFlag, Variant
from xmclaw.utils.log import get_logger

_log = get_logger(__name__)


class RemoteProvider(Protocol):
    """Duck-typed remote flag source.

    Implementations only need a single sync ``lookup`` method that
    returns the raw variant or ``None`` (= "no opinion, fall through").
    Sync is intentional — the engine is called from hot paths and
    can't block on remote I/O. Real implementations should pull
    asynchronously on a refresh tick and answer ``lookup`` from a
    local snapshot.
    """

    def lookup(self, name: str) -> Variant | None: ...


class NoopRemoteProvider:
    """Default provider — always abstains. Use when you don't have a
    flag service wired."""

    def lookup(self, name: str) -> Variant | None:  # noqa: ARG002
        return None


def _parse_env_value(raw: str) -> Variant:
    """Convert an env-var string to a typed variant.

    JSON-decode first (catches ``"true"`` / ``"42"`` / ``"[1,2]"``);
    fall through to raw string when not JSON. Empty / whitespace =
    None (= "abstain", same as env not being set at all).
    """
    s = raw.strip()
    if not s:
        return None
    try:
        return json.loads(s)
    except (json.JSONDecodeError, ValueError):
        return s


class FeatureFlagEngine:
    """The 5-layer resolver. Construct once per daemon."""

    def __init__(
        self,
        *,
        disk_path: Path | str | None = None,
        remote: RemoteProvider | None = None,
    ) -> None:
        self._registry: dict[str, FeatureFlag] = {}
        self._memory: dict[str, Variant] = {}
        self._disk_path: Path | None = (
            Path(disk_path) if disk_path is not None else None
        )
        self._disk_cache: dict[str, Variant] = {}
        self._remote: RemoteProvider = remote or NoopRemoteProvider()
        self._lock = threading.RLock()
        self._last_refresh_ts: float = 0.0
        # Load disk on construction so day-1 lookups see persisted
        # values without an explicit ``refresh()``.
        self._load_disk()

    # ── Registration ─────────────────────────────────────────────

    def register(self, flag: FeatureFlag) -> None:
        """Declare a flag's schema. Idempotent — re-registering the
        same name replaces, useful for live config reloads."""
        with self._lock:
            self._registry[flag.name] = flag

    def register_many(self, flags: list[FeatureFlag]) -> None:
        for f in flags:
            self.register(f)

    def known_flags(self) -> list[FeatureFlag]:
        with self._lock:
            return list(self._registry.values())

    # ── Resolution ────────────────────────────────────────────────

    def variant(self, name: str, default: Variant = None) -> Variant:
        """Resolve a flag. Falls through the 5 layers in priority
        order. ``default`` is the caller-provided fallback used ONLY
        when the flag isn't in the registry — for registered flags
        the registered default is canonical."""
        # Layer 1: env override.
        env_name = "XMC_FF_" + name.upper().replace(".", "_").replace("-", "_")
        if env_name in os.environ:
            v = _parse_env_value(os.environ[env_name])
            if v is not None:
                return v
        with self._lock:
            # Layer 2: memory cache (set() writes land here).
            if name in self._memory:
                return self._memory[name]
            # Layer 3: disk cache (loaded at boot, refreshed by set()).
            if name in self._disk_cache:
                # Promote to memory so subsequent lookups are O(1)
                # dict-get without disk I/O.
                v = self._disk_cache[name]
                self._memory[name] = v
                return v
            # Layer 4: remote provider.
            try:
                v = self._remote.lookup(name)
            except Exception as exc:  # noqa: BLE001 — never fail a lookup
                _log.warning(
                    "feature_flag.remote_failed name=%s err=%s", name, exc,
                )
                v = None
            if v is not None:
                self._memory[name] = v
                return v
            # Layer 5: registered default, then caller default.
            flag = self._registry.get(name)
            if flag is not None:
                return flag.default
            return default

    def is_enabled(self, name: str, default: bool = False) -> bool:
        """Sugar for boolean flags. Truthy variant → True."""
        return bool(self.variant(name, default=default))

    # ── Mutation ─────────────────────────────────────────────────

    def set(
        self, name: str, value: Variant, *, persist: bool = True,
    ) -> None:
        """Write to the memory cache + optionally persist to disk.

        ``persist=False`` makes the write process-local — survives
        the current daemon run but evaporates on restart. Useful
        for transient overrides (e.g. "disable expensive evolution
        for this debug session").
        """
        with self._lock:
            self._memory[name] = value
            if persist:
                self._disk_cache[name] = value
                self._save_disk()

    def clear(self, name: str) -> None:
        """Forget a flag at all layers we own (memory + disk).
        Remote values come back on the next refresh."""
        with self._lock:
            self._memory.pop(name, None)
            self._disk_cache.pop(name, None)
            self._save_disk()

    # ── Refresh / introspection ──────────────────────────────────

    def refresh(self, names: list[str] | None = None) -> int:
        """Re-pull values from the remote provider for the named
        flags (or all registered flags when ``names=None``). Returns
        count refreshed. Memory cache is updated for every successful
        pull; disk is NOT touched (remote values are ephemeral; we
        only persist explicit ``set`` calls).
        """
        with self._lock:
            targets = names or [f.name for f in self._registry.values()]
        n = 0
        for name in targets:
            try:
                v = self._remote.lookup(name)
            except Exception:  # noqa: BLE001
                v = None
            if v is not None:
                with self._lock:
                    self._memory[name] = v
                n += 1
        with self._lock:
            self._last_refresh_ts = time.time()
        return n

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """Operator-facing view: every known flag + its effective
        value + the layer that resolved it. For the /api/v2/features
        endpoint and a future Web UI page."""
        out: dict[str, dict[str, Any]] = {}
        with self._lock:
            for name, flag in self._registry.items():
                env_name = (
                    "XMC_FF_"
                    + name.upper().replace(".", "_").replace("-", "_")
                )
                if env_name in os.environ:
                    layer = "env"
                    value = _parse_env_value(os.environ[env_name])
                elif name in self._memory:
                    layer = "memory"
                    value = self._memory[name]
                elif name in self._disk_cache:
                    layer = "disk"
                    value = self._disk_cache[name]
                else:
                    try:
                        rv = self._remote.lookup(name)
                    except Exception:  # noqa: BLE001
                        rv = None
                    if rv is not None:
                        layer = "remote"
                        value = rv
                    else:
                        layer = "default"
                        value = flag.default
                out[name] = {
                    "name": name,
                    "value": value,
                    "layer": layer,
                    "default": flag.default,
                    "description": flag.description,
                }
        return out

    # ── Disk persistence ─────────────────────────────────────────

    def _load_disk(self) -> None:
        if self._disk_path is None:
            return
        try:
            if self._disk_path.is_file():
                data = json.loads(
                    self._disk_path.read_text(encoding="utf-8"),
                )
                if isinstance(data, dict):
                    self._disk_cache = {
                        str(k): v for k, v in data.items()
                    }
        except (OSError, json.JSONDecodeError) as exc:
            _log.warning(
                "feature_flag.disk_load_failed path=%s err=%s",
                self._disk_path, exc,
            )

    def _save_disk(self) -> None:
        if self._disk_path is None:
            return
        try:
            self._disk_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._disk_path.with_suffix(
                self._disk_path.suffix + ".tmp",
            )
            tmp.write_text(
                json.dumps(self._disk_cache, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(tmp, self._disk_path)
        except OSError as exc:
            _log.warning(
                "feature_flag.disk_save_failed path=%s err=%s",
                self._disk_path, exc,
            )


__all__ = [
    "FeatureFlagEngine",
    "NoopRemoteProvider",
    "RemoteProvider",
]
