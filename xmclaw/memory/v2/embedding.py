"""EmbeddingService — text → vector with LRU cache + retry (Phase 1b).

Wraps the existing :class:`xmclaw.providers.memory.embedding.EmbeddingProvider`
(the OpenAI-compat client) with three additions the v2 memory pipeline
needs:

1. **LRU cache by content hash** — same text never re-embeds. Most
   facts get written 2-3 times (initial + dedup re-extraction); caching
   cuts those API calls.
2. **Retry with exponential backoff** — the underlying provider
   returns ``[]`` on failure (silently degrades). For v2 writes we
   want strict: retry, then surface as exception so the caller can
   decide. Default 3 attempts, 0.5/1.5/4.5s backoff.
3. **Single-text convenience API** — ``embed(text)`` returns a
   tuple suitable for ``Fact.embedding`` directly, no list indexing.

Construction:

    >>> from xmclaw.memory.v2.embedding import EmbeddingService
    >>> svc = EmbeddingService.from_config({"api_key": "sk-..."})
    >>> vec = await svc.embed("网店 example.com")
    >>> # vec is tuple[float, ...] of length svc.dim

For tests, pass a ``StubEmbedder`` that returns deterministic vectors
based on text length / hash. Module also exposes a default global
singleton via ``get_default_embedding_service()`` for callers that
don't want to thread the service through.
"""
from __future__ import annotations

import asyncio
import hashlib
from collections import OrderedDict
from typing import Any, Protocol

from xmclaw.utils.log import get_logger

_log = get_logger(__name__)


# ── Provider Protocol (matches existing EmbeddingProvider shape) ──


class EmbeddingProviderLike(Protocol):
    """Minimal contract the EmbeddingService needs from a backend."""

    name: str
    dim: int

    async def embed(self, texts: list[str]) -> list[list[float]]:
        ...

    def is_available(self) -> bool:
        ...


# ── Stub for tests ────────────────────────────────────────────────


class StubEmbedder:
    """Deterministic test embedder. Returns vectors derived from
    the text's bytes — same text ⇒ same vector, different text ⇒
    different vector (within rounding). Not semantically meaningful;
    just used to test wiring."""

    name = "stub"

    def __init__(self, dim: int = 4) -> None:
        self.dim = dim

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for t in texts:
            h = hashlib.sha256(t.encode("utf-8")).digest()
            # Convert first N bytes to floats in [-1, 1]
            vec = [
                ((h[i] - 128) / 128.0) for i in range(self.dim)
            ]
            out.append(vec)
        return out

    def is_available(self) -> bool:
        return True


# ── EmbeddingService ─────────────────────────────────────────────


class EmbeddingFailure(Exception):
    """Raised when all retry attempts exhausted. Caller decides whether
    to fail the write or proceed with embedding=None."""


class EmbeddingService:
    """LRU-cached + retry-wrapped embedding service.

    Args:
        provider: any object matching :class:`EmbeddingProviderLike`.
        cache_capacity: max number of (text_hash → vec) entries.
            LRU eviction. 0 disables caching.
        retry_attempts: total tries including first. ≥ 1.
        retry_backoff_s: base delay; doubles each retry. 0 disables.
        cache_path: optional path to a gzipped JSON file for persistent
            cache. Loaded on init, saved periodically (every 50 puts).
    """

    def __init__(
        self,
        provider: EmbeddingProviderLike,
        *,
        cache_capacity: int = 8192,
        retry_attempts: int = 3,
        retry_backoff_s: float = 0.5,
        circuit_breaker_threshold: int = 5,
        circuit_breaker_cooldown_s: float = 300.0,
        cache_path: str | None = None,
    ) -> None:
        self._provider = provider
        self._cache: OrderedDict[str, tuple[float, ...]] = OrderedDict()
        self._cache_capacity = max(0, int(cache_capacity))
        self._retry_attempts = max(1, int(retry_attempts))
        self._retry_backoff_s = max(0.0, float(retry_backoff_s))
        self._cache_path = cache_path
        self._cache_save_counter = 0
        # Stats so tests + observability can see hit rate.
        self.cache_hits = 0
        self.cache_misses = 0
        self.failures = 0
        # Epic #27 sweep #8 (2026-05-19) — circuit breaker.
        self._cb_threshold = max(1, int(circuit_breaker_threshold))
        self._cb_cooldown_s = max(0.0, float(circuit_breaker_cooldown_s))
        self._cb_consecutive_failures = 0
        self._cb_open_until: float = 0.0  # monotonic ts; 0 = closed
        # Load persisted cache if available.
        if self._cache_path:
            self._load_cache()

    @property
    def dim(self) -> int:
        return self._provider.dim

    @property
    def name(self) -> str:
        return self._provider.name

    def is_available(self) -> bool:
        return self._provider.is_available()

    # ── Public surface ──────────────────────────────────────────

    async def embed(self, text: str) -> tuple[float, ...]:
        """Embed one text. Returns tuple suitable for ``Fact.embedding``.

        Raises :class:`EmbeddingFailure` if all retries exhausted.
        """
        if not text or not text.strip():
            raise EmbeddingFailure("cannot embed empty text")
        key = self._cache_key(text)
        cached = self._cache.get(key)
        if cached is not None:
            # Move to most-recently-used end.
            self._cache.move_to_end(key)
            self.cache_hits += 1
            return cached
        self.cache_misses += 1
        vec = await self._embed_with_retry([text])
        result = tuple(vec[0])
        self._cache_put(key, result)
        return result

    async def embed_query(self, text: str) -> tuple[float, ...]:
        """Embed a **search query** (vs a stored document).

        2026-06-08: Qwen3-Embedding is an *asymmetric* retriever — queries
        are meant to carry an instruction prefix (``Instruct: …\\nQuery: …``)
        while documents are embedded raw. Embedding both sides identically
        (as the old recall path did) throws away the model's retrieval
        signal. We add the prefix ONLY for qwen-family models; other models
        (OpenAI text-embedding-3, etc.) are symmetric, so the query text is
        embedded unchanged. The prefix makes the cache key differ from the
        same text stored as a document — correct, they SHOULD differ.
        """
        return await self.embed(self._query_instruct(text))

    def _query_instruct(self, text: str) -> str:
        model = str(getattr(self._provider, "model", "") or "").lower()
        if "qwen" in model:
            return (
                "Instruct: 给定用户当前的对话或问题，检索语义上最相关的长期记忆。\n"
                f"Query: {text}"
            )
        return text

    async def embed_batch(
        self, texts: list[str],
    ) -> list[tuple[float, ...]]:
        """Embed many texts. Returns vectors in same order as input.

        Cached entries skip the API call; missing ones are batched.
        Raises :class:`EmbeddingFailure` if any non-cached text fails.
        """
        if not texts:
            return []

        results: list[tuple[float, ...] | None] = [None] * len(texts)
        misses_idx: list[int] = []
        misses_text: list[str] = []

        # Check cache first.
        for i, t in enumerate(texts):
            if not t or not t.strip():
                raise EmbeddingFailure(
                    f"cannot embed empty text at index {i}",
                )
            key = self._cache_key(t)
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                self.cache_hits += 1
                results[i] = cached
            else:
                self.cache_misses += 1
                misses_idx.append(i)
                misses_text.append(t)

        # Embed misses in one batch.
        if misses_text:
            vecs = await self._embed_with_retry(misses_text)
            for j, idx in enumerate(misses_idx):
                v = tuple(vecs[j])
                results[idx] = v
                self._cache_put(self._cache_key(misses_text[j]), v)

        # All slots filled at this point.
        return [r for r in results if r is not None]

    # ── Internals ───────────────────────────────────────────────

    def _cache_key(self, text: str) -> str:
        # SHA-1 plenty for cache key; not used as security primitive.
        normalised = " ".join(text.split())
        return hashlib.sha1(normalised.encode("utf-8")).hexdigest()

    def _cache_put(self, key: str, vec: tuple[float, ...]) -> None:
        if self._cache_capacity <= 0:
            return
        self._cache[key] = vec
        self._cache.move_to_end(key)
        while len(self._cache) > self._cache_capacity:
            self._cache.popitem(last=False)
        # Persist every 50 writes.
        if self._cache_path:
            self._cache_save_counter += 1
            if self._cache_save_counter >= 50:
                self._cache_save_counter = 0
                self._save_cache()

    # ── Cache persistence ─────────────────────────────────────────

    def _load_cache(self) -> None:
        import gzip, json, os
        if not self._cache_path or not os.path.exists(self._cache_path):
            return
        try:
            with gzip.open(self._cache_path, "rt", encoding="utf-8") as fh:
                data = json.load(fh)
            if data.get("version") != 2:
                return
            if data.get("provider_name") != self._provider.name:
                return
            if data.get("dim") != self._provider.dim:
                return
            entries = data.get("entries", {})
            # Decode base64-encoded float32 vectors.
            import base64, struct
            dim = self._provider.dim
            fmt = f"<{dim}f"
            loaded = 0
            for key, b64 in entries.items():
                try:
                    raw = base64.b64decode(b64)
                    vec = struct.unpack(fmt, raw)
                    self._cache[key] = tuple(vec)
                    loaded += 1
                except Exception:
                    continue
            _log.info(
                "embedding_cache.loaded path=%s entries=%d/%d",
                self._cache_path, loaded, len(entries),
            )
        except Exception as exc:
            _log.warning("embedding_cache.load_failed err=%s", exc)

    def _save_cache(self) -> None:
        import base64, gzip, json, struct
        if not self._cache_path:
            return
        try:
            dim = self._provider.dim
            fmt = f"<{dim}f"
            entries: dict[str, str] = {}
            for key, vec in self._cache.items():
                try:
                    raw = struct.pack(fmt, *vec)
                    entries[key] = base64.b64encode(raw).decode("ascii")
                except Exception:
                    continue
            data = {
                "version": 2,
                "provider_name": self._provider.name,
                "dim": dim,
                "entries": entries,
            }
            # Atomic write: temp file then rename.
            import os
            tmp = self._cache_path + ".tmp"
            with gzip.open(tmp, "wt", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(tmp, self._cache_path)
            _log.debug(
                "embedding_cache.saved path=%s entries=%d",
                self._cache_path, len(entries),
            )
        except Exception as exc:
            _log.warning("embedding_cache.save_failed err=%s", exc)

    def persist(self) -> None:
        """Explicit save — call on graceful shutdown."""
        self._save_cache()

    async def _embed_with_retry(
        self, texts: list[str],
    ) -> list[list[float]]:
        # Epic #27 sweep #8: circuit-breaker open check. If we recently
        # tripped the breaker, refuse immediately instead of hammering
        # the broken provider. Caller catches EmbeddingFailure same
        # as any other failure mode — including the legitimate
        # "embedding off" path via service.recall keyword-only.
        import time as _time
        now = _time.monotonic()
        if self._cb_open_until and now < self._cb_open_until:
            self.failures += 1
            cooldown_left = self._cb_open_until - now
            raise EmbeddingFailure(
                f"embedding circuit breaker OPEN "
                f"(retry in {cooldown_left:.0f}s) — "
                f"too many consecutive failures, calls suppressed to "
                f"avoid hammering the upstream provider."
            )
        last_err: Exception | None = None
        delay = self._retry_backoff_s
        for attempt in range(1, self._retry_attempts + 1):
            try:
                vecs = await self._provider.embed(texts)
            except Exception as exc:  # noqa: BLE001 — wrap below
                last_err = exc
                _log.warning(
                    "embedding_service.attempt_failed n=%d/%d err=%s",
                    attempt, self._retry_attempts, exc,
                )
            else:
                # Provider returned but may contain empty rows (its
                # convention for "this one failed inside batch"). Treat
                # any empty row as a failure of the whole batch — the
                # consumer (Fact write) needs ALL or none.
                if all(v for v in vecs):
                    # Success — reset breaker counter.
                    self._cb_consecutive_failures = 0
                    return vecs
                last_err = EmbeddingFailure(
                    f"provider returned empty rows: "
                    f"{sum(1 for v in vecs if not v)}/{len(vecs)} empty",
                )
                _log.warning(
                    "embedding_service.partial_empty n=%d/%d",
                    attempt, self._retry_attempts,
                )

            if attempt < self._retry_attempts and delay > 0:
                await asyncio.sleep(delay)
                delay *= 3.0  # 0.5 → 1.5 → 4.5 by default

        # All retries failed. Bump the breaker counter; if we cross
        # the threshold, open the breaker for cooldown_s.
        self.failures += 1
        self._cb_consecutive_failures += 1
        if self._cb_consecutive_failures >= self._cb_threshold:
            self._cb_open_until = (
                _time.monotonic() + self._cb_cooldown_s
            )
            _log.warning(
                "embedding_service.circuit_breaker_open "
                "consecutive_failures=%d cooldown_s=%.0f",
                self._cb_consecutive_failures, self._cb_cooldown_s,
            )
        raise EmbeddingFailure(
            f"embedding failed after {self._retry_attempts} attempts: {last_err}",
        )

    # ── Diagnostics ─────────────────────────────────────────────

    def stats(self) -> dict[str, Any]:
        import time as _time
        total = self.cache_hits + self.cache_misses
        hit_rate = self.cache_hits / total if total else 0.0
        now = _time.monotonic()
        cb_open = bool(self._cb_open_until and now < self._cb_open_until)
        cb_cooldown_remaining = (
            max(0.0, self._cb_open_until - now) if cb_open else 0.0
        )
        return {
            "provider": self.name,
            "dim": self.dim,
            "cache_size": len(self._cache),
            "cache_capacity": self._cache_capacity,
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "cache_hit_rate": hit_rate,
            "failures": self.failures,
            "circuit_breaker_open": cb_open,
            "circuit_breaker_cooldown_remaining_s": cb_cooldown_remaining,
            "circuit_breaker_consecutive_failures": (
                self._cb_consecutive_failures
            ),
        }


# ── Construction helpers ─────────────────────────────────────────


def build_embedding_service(
    *,
    cfg: dict[str, Any] | None = None,
    cache_capacity: int = 8192,
    retry_attempts: int = 3,
    retry_backoff_s: float = 0.5,
) -> EmbeddingService | None:
    """Build an EmbeddingService from the daemon config dict.

    Reads ``cfg['evolution']['memory']['embedding']`` (the same block
    the legacy ``build_embedding_provider`` reads). Returns None when
    no provider is configured / available — caller falls back to
    keyword-only search for that session.

    Config override::

        {
          "evolution": {
            "memory": {
              "embedding": {
                "cache_capacity": 16384   # default 8192
              }
            }
          }
        }
    """
    from xmclaw.providers.memory.embedding import build_embedding_provider
    provider = build_embedding_provider(cfg=cfg)
    if provider is None or not provider.is_available():
        _log.info("embedding_service.no_provider — falling back to text-only mode")
        return None
    # Allow config-driven cache capacity override.
    if cfg:
        sec = (((cfg.get("evolution") or {}).get("memory") or {}).get("embedding") or {})
        if isinstance(sec, dict):
            cfg_cap = sec.get("cache_capacity")
            if isinstance(cfg_cap, int) and cfg_cap > 0:
                cache_capacity = cfg_cap
    # Default persistent cache path.
    _cache_path = None
    try:
        import os
        _data_dir = os.path.expanduser("~/.xmclaw/v2")
        os.makedirs(_data_dir, exist_ok=True)
        _cache_path = os.path.join(
            _data_dir,
            f"embedding_cache_{provider.name}_{provider.dim}.json.gz",
        )
    except Exception:
        pass
    return EmbeddingService(
        provider,
        cache_capacity=cache_capacity,
        retry_attempts=retry_attempts,
        retry_backoff_s=retry_backoff_s,
        cache_path=_cache_path,
    )


__all__ = [
    "EmbeddingFailure",
    "EmbeddingProviderLike",
    "EmbeddingService",
    "StubEmbedder",
    "build_embedding_service",
]
