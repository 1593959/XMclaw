"""OpenRouter model directory auto-discovery — B-387 gap closure.

XMclaw's context-length and pricing tables in ``_provider_profiles.py`` and
``cost.py`` are static. OpenRouter adds / removes / re-prices models weekly;
static tables are always stale. This module fetches the live OpenRouter
model directory, caches it locally, and exposes the discovered metadata so
that ``get_model_context_length`` and ``lookup_pricing`` can resolve
OpenRouter models accurately without manual table updates.

Design:
  * ``fetch_models()`` hits ``https://openrouter.ai/api/v1/models`` (no auth
    required for the directory endpoint) and writes a JSON cache to
    ``~/.xmclaw/cache/openrouter_models.json``.
  * TTL = 24 h. Stale cache is still served while a background refresh runs.
  * ``get_context_length(model_id)`` and ``get_pricing(model_id)`` read from
    the cache first, then fall back to the static tables.
  * ``OpenRouterLLM`` boot calls ``warm_cache()`` so the first user turn
    already has warm data.

Thread-safety: all public methods are sync and operate on an in-memory dict
that is atomically swapped after a refresh. No locks are needed for reads.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from xmclaw.utils.cost import Pricing
from xmclaw.utils.log import get_logger

_log = get_logger(__name__)

# OpenRouter public directory endpoint — no API key required.
_OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"

# Cache TTL in seconds (24 hours).
_CACHE_TTL_S = 24 * 3600

# Cache file path inside the user's XMclaw data dir.
_CACHE_FILENAME = "openrouter_models.json"


def _norm_name(s: str) -> str:
    """Normalize a model id for fuzzy matching: lowercase, drop every
    non-alphanumeric char. ``Qwen2.5-VL-7B`` and ``qwen2-5-vl-7b`` both
    collapse to ``qwen25vl7b``."""
    import re
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _cache_path() -> Path:
    from xmclaw.utils.paths import data_dir
    p = data_dir() / "cache"
    p.mkdir(parents=True, exist_ok=True)
    return p / _CACHE_FILENAME


def _default_xmclaw_dir() -> Path:
    from xmclaw.utils.paths import data_dir
    return data_dir()


class _OpenRouterCache:
    """In-memory snapshot of the OpenRouter model directory.

    The ``_data`` dict is atomically replaced on refresh, so readers never
    see a partially-updated structure.
    """

    def __init__(self) -> None:
        self._data: dict[str, dict[str, Any]] = {}
        self._last_refresh_ts: float = 0.0
        self._load_from_disk()

    # ── public read API ─────────────────────────────────────────────

    def get(self, model_id: str) -> dict[str, Any] | None:
        return self._data.get(model_id)

    def context_length(self, model_id: str) -> int | None:
        entry = self._data.get(model_id)
        if entry is None:
            return None
        # OpenRouter returns ``context_length`` as an integer.
        raw = entry.get("context_length")
        if isinstance(raw, int) and raw > 0:
            return raw
        return None

    def pricing(self, model_id: str) -> Pricing | None:
        entry = self._data.get(model_id)
        if entry is None:
            return None
        pricing_block = entry.get("pricing")
        if not isinstance(pricing_block, dict):
            return None
        # OpenRouter pricing is per-token (e.g. "0.000003" USD per token).
        # XMclaw Pricing is per-million-tokens, so multiply by 1_000_000.
        try:
            prompt = float(pricing_block.get("prompt", 0))
            completion = float(pricing_block.get("completion", 0))
            if prompt <= 0 and completion <= 0:
                return None
            return Pricing(
                input_per_mtok=round(prompt * 1_000_000, 6),
                output_per_mtok=round(completion * 1_000_000, 6),
            )
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _entry_vision(entry: dict[str, Any]) -> bool | None:
        """Read image-input support from an OpenRouter model entry.

        OpenRouter exposes ``architecture.input_modalities: [...]`` (newer)
        or ``architecture.modality: "text+image->text"`` (older). Returns
        ``None`` when the entry carries no modality info at all.
        """
        arch = entry.get("architecture")
        if not isinstance(arch, dict):
            return None
        ims = arch.get("input_modalities")
        if isinstance(ims, list):
            return any(str(x).lower() == "image" for x in ims)
        m = arch.get("modality")
        if isinstance(m, str):
            return "image" in m.lower()
        return None

    def vision_by_name(self, name: str) -> bool | None:
        """Look up image-input support by a (possibly prefix-less) model name.

        Third-party endpoints report bare model ids (``gpt-4o``,
        ``claude-3-5-sonnet``); OpenRouter ids carry a provider prefix
        (``openai/gpt-4o``). Match on the normalized short id (and full id)
        so a reseller's ``gpt-4o`` resolves to OpenRouter's authoritative
        modality. Returns ``None`` when no catalog entry matches.
        """
        if not name:
            return None
        norm = _norm_name(name)
        if not norm:
            return None
        for mid, entry in self._data.items():
            short = mid.rsplit("/", 1)[-1]
            if _norm_name(short) == norm or _norm_name(mid) == norm:
                v = self._entry_vision(entry)
                if v is not None:
                    return v
        return None

    def is_stale(self) -> bool:
        if not self._data:
            return True
        return (time.time() - self._last_refresh_ts) > _CACHE_TTL_S

    # ── refresh API ─────────────────────────────────────────────────

    def refresh(self) -> bool:
        """Synchronously fetch from OpenRouter and update the cache.

        Returns ``True`` when new data was successfully ingested.
        """
        try:
            raw = self._fetch()
        except Exception as exc:  # noqa: BLE001
            _log.warning("openrouter.fetch_failed", err=str(exc))
            return False

        parsed = self._parse(raw)
        if not parsed:
            _log.warning("openrouter.parse_empty")
            return False

        self._data = parsed
        self._last_refresh_ts = time.time()
        self._save_to_disk()
        _log.info("openrouter.cache_refreshed", models=len(parsed))
        return True

    def warm(self) -> None:
        """Ensure the cache is warm. If stale or empty, trigger a refresh."""
        if self.is_stale():
            ok = self.refresh()
            if not ok and not self._data:
                _log.warning("openrouter.warm_failed_no_fallback")

    # ── internal helpers ────────────────────────────────────────────

    def _fetch(self) -> dict[str, Any]:
        import urllib.request
        import urllib.error

        req = urllib.request.Request(
            _OPENROUTER_MODELS_URL,
            headers={"Accept": "application/json", "User-Agent": "XMclaw/2.0"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status != 200:
                raise urllib.error.HTTPError(
                    req.full_url, resp.status, None, None, None
                )
            return json.loads(resp.read().decode("utf-8"))

    def _parse(self, raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        data = raw.get("data")
        if not isinstance(data, list):
            return out
        for entry in data:
            if not isinstance(entry, dict):
                continue
            model_id = entry.get("id")
            if not isinstance(model_id, str) or not model_id:
                continue
            out[model_id] = entry
        return out

    def _save_to_disk(self) -> None:
        path = _cache_path()
        payload = {
            "fetched_at": self._last_refresh_ts,
            "models": self._data,
        }
        try:
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            _log.warning("openrouter.cache_save_failed", path=str(path), err=str(exc))

    def _load_from_disk(self) -> None:
        path = _cache_path()
        if not path.exists():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            models = payload.get("models")
            if isinstance(models, dict):
                self._data = models
                self._last_refresh_ts = payload.get("fetched_at", 0.0)
                _log.info("openrouter.cache_loaded", models=len(models))
        except (json.JSONDecodeError, OSError) as exc:
            _log.warning("openrouter.cache_load_failed", path=str(path), err=str(exc))


# Module-level singleton — safe because all methods are sync and the
# underlying dict is atomically replaced.
_CACHE = _OpenRouterCache()


# ── public API ────────────────────────────────────────────────────

def warm_cache() -> None:
    """Warm the OpenRouter model cache. Safe to call at boot time."""
    _CACHE.warm()


def get_context_length(model_id: str) -> int | None:
    """Return the discovered context length for an OpenRouter model id.

    Returns ``None`` when the model is not in the cache (caller should
    fall back to the static tables).
    """
    return _CACHE.context_length(model_id)


def get_pricing(model_id: str) -> Pricing | None:
    """Return the discovered pricing for an OpenRouter model id.

    Returns ``None`` when the model is not in the cache (caller should
    fall back to the static tables).
    """
    return _CACHE.pricing(model_id)


def refresh_cache() -> bool:
    """Force a synchronous refresh of the OpenRouter model cache.

    Returns ``True`` on success. This is safe to call from a background
    thread or an async task.
    """
    return _CACHE.refresh()


def is_cache_stale() -> bool:
    """Return ``True`` when the cache is empty or older than 24 h."""
    return _CACHE.is_stale()


def list_models() -> list[str]:
    """Return the ids of all models currently in the cache."""
    return list(_CACHE._data.keys())


def get_vision_by_name(model_name: str) -> bool | None:
    """Resolve image-input support for a (prefix-less) model name via the
    OpenRouter catalog. Returns ``None`` when no entry matches — caller
    should fall back to the name heuristic or a live probe."""
    return _CACHE.vision_by_name(model_name)
