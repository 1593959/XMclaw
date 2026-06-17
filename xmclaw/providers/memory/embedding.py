"""EmbeddingProvider — pluggable embedding backend for the memory layer.

B-41 (the upstream agent parity). XMclaw's :class:`SqliteVecMemory` had storage
plus the cosine-search SQL but no way to actually COMPUTE embeddings
— the caller had to supply ``MemoryItem.embedding`` as a tuple of
floats. Without this provider, semantic search silently degraded to
keyword fallback.

This module supplies the missing piece:

* :class:`EmbeddingProvider` — the ABC. Any backend (OpenAI,
  DashScope, Ollama, BGE local) that exposes an OpenAI-compatible
  ``/v1/embeddings`` endpoint can plug in via :class:`OpenAIEmbeddingProvider`
  with a different ``base_url``.

Config (``evolution.memory.embedding`` section in daemon/config.json)::

    {
      "evolution": {
        "memory": {
          "embedding": {
            "provider": "openai",
            "api_key": "sk-...",
            "base_url": "https://api.openai.com/v1",
            "model": "text-embedding-3-small",
            "dimensions": 1536
          }
        }
      }
    }

Or via env: ``XMC_EMBEDDING_API_KEY`` / ``XMC_EMBEDDING_BASE_URL`` /
``XMC_EMBEDDING_MODEL`` / ``XMC_EMBEDDING_DIMENSIONS``.

When no provider is configured, :func:`build_embedding_provider`
returns ``None``; the indexer skips embedding (text-only entries
still go in, semantic search falls back to keyword).
"""
from __future__ import annotations

import abc
import asyncio
import os
from typing import Any

from xmclaw.utils.log import get_logger

_log = get_logger(__name__)


class EmbeddingProvider(abc.ABC):
    """Compute dense embeddings for one or more texts."""

    name: str = "abstract"
    dim: int = 0

    @abc.abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Return embeddings for each input text in order. Empty input
        list returns an empty list. Implementations should batch
        internally — XMclaw passes up to ``max_batch_size`` per call."""

    def is_available(self) -> bool:
        """Whether the backend is reachable / configured. Default True;
        cloud providers override."""
        return True

    @property
    def fingerprint(self) -> str:
        """Stable identity of the embedding space this provider produces.

        Switching the embedding model (or its output dimension) makes
        previously-stored vectors incompatible — cosine distances across
        two spaces are meaningless. Persisting this lets callers detect a
        change and rebuild the index instead of silently degrading recall
        to keyword search. Subclasses with a model id fold it in."""
        return f"{self.name}::{self.dim}"


class OpenAIEmbeddingProvider(EmbeddingProvider):
    """OpenAI-compatible embedding endpoint client.

    Works with any service that implements ``POST /v1/embeddings``:
    OpenAI proper, DashScope (Alibaba) ``text-embedding-v3``,
    Ollama's ``/v1/embeddings`` shim, vLLM, LiteLLM, etc.

    Falls back to ``urllib`` when ``httpx`` isn't installed — same
    pattern as :class:`HindsightMemoryProvider`. Failures return
    empty embeddings (the indexer treats them as "skip and retry
    later") rather than raising.
    """

    name = "openai"

    def __init__(
        self, *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str = "text-embedding-3-small",
        dimensions: int = 1536,
        max_batch_size: int = 16,
        timeout_s: float = 30.0,
    ) -> None:
        self._api_key = api_key or os.environ.get("XMC_EMBEDDING_API_KEY") or ""
        self._base_url = (
            (base_url or os.environ.get("XMC_EMBEDDING_BASE_URL")
             or "https://api.openai.com/v1").rstrip("/")
        )
        self._model = (
            model or os.environ.get("XMC_EMBEDDING_MODEL")
            or "text-embedding-3-small"
        )
        self.dim = int(dimensions)
        self._max_batch_size = max(1, int(max_batch_size))
        self._timeout_s = float(timeout_s)

    @property
    def model(self) -> str:
        return self._model

    @property
    def fingerprint(self) -> str:
        # Fold the model id in — same dim, different model = different space.
        return f"{self.name}:{self._model}:{self.dim}"

    @property
    def _is_local(self) -> bool:
        """True when base_url points at a local server (Ollama/vLLM/…).

        2026-06-08 ROOT-CAUSE FIX: a system proxy (Clash ``HTTP_PROXY=
        http://127.0.0.1:7897``) made httpx/urllib route ``localhost:11434``
        THROUGH the proxy, which can't forward to localhost → ConnectTimeout
        on every embed → recall silently fell back to keyword search (the
        ``召回全是关键词`` symptom). For a local endpoint we must connect
        DIRECTLY, never via proxy — independent of whether NO_PROXY happens
        to be set in the daemon's launch env."""
        host = self._base_url.lower()
        for local in ("://localhost", "://127.0.0.1", "://0.0.0.0", "://[::1]"):
            if local in host:
                return True
        return False

    def is_available(self) -> bool:
        # B-43: Ollama / vLLM / local servers don't require an API key.
        # Treat a localhost/127.0.0.1 base_url as auth-free — the
        # admin opted in by pointing at a private endpoint. Cloud
        # endpoints (OpenAI / DashScope etc) still need a key.
        if self._api_key:
            return True
        return self._is_local

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if not self.is_available():
            return [[] for _ in texts]

        out: list[list[float]] = []
        for i in range(0, len(texts), self._max_batch_size):
            batch = texts[i:i + self._max_batch_size]
            vecs = await self._embed_one_batch(batch)
            out.extend(vecs)
        return out

    async def _embed_one_batch(self, batch: list[str]) -> list[list[float]]:
        body: dict[str, Any] = {"model": self._model, "input": batch}
        # OpenAI's text-embedding-3-* honours ``dimensions`` to truncate;
        # legacy models / non-OpenAI shims may ignore it (harmless).
        if self.dim:
            body["dimensions"] = self.dim

        # B-197: retry transient failures. Pre-B-197 audit found 86%
        # of memory.db rows had has_embedding=0 — many because the
        # embedder hit single-attempt failures (Ollama briefly busy,
        # network blip, model warmup). 2 retries with linear backoff
        # captures the cheap recoveries; persistent failures still
        # return empty and the caller logs.
        import asyncio as _asyncio
        resp = None
        for attempt in range(3):
            resp = await self._post("/embeddings", body)
            if resp:
                break
            if attempt < 2:
                # 0.2s, 0.5s backoff. Total worst-case added latency
                # for a doomed call: 0.7s.
                await _asyncio.sleep(0.2 + 0.3 * attempt)
                _log.info(
                    "embedding.retry attempt=%d batch_size=%d model=%s",
                    attempt + 2, len(batch), self._model,
                )
        if not resp:
            _log.warning(
                "embedding.persistent_failure model=%s batch_size=%d "
                "after_retries=2",
                self._model, len(batch),
            )
            return [[] for _ in batch]
        data = resp.get("data") or []
        out: list[list[float]] = []
        for entry in data:
            if isinstance(entry, dict):
                vec = entry.get("embedding") or []
                if isinstance(vec, list):
                    out.append([float(v) for v in vec])
                    continue
            out.append([])
        # Pad with empties if the response was short for some reason.
        while len(out) < len(batch):
            out.append([])
        return out[:len(batch)]

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any] | None:
        url = f"{self._base_url}{path}"
        headers: dict[str, str] = {"Content-Type": "application/json"}
        # B-43: only set Authorization when we actually have a key —
        # Ollama tolerates Bearer "" but some self-hosted shims reject
        # malformed auth headers. Cleaner to omit.
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        try:
            import httpx
            # 本地端点强制直连(trust_env=False)→ 绕开系统代理,免 ConnectTimeout。
            async with httpx.AsyncClient(
                timeout=self._timeout_s,
                trust_env=not self._is_local,
            ) as client:
                resp = await client.post(url, headers=headers, json=body)
                if resp.status_code >= 400:
                    _log.warning(
                        "embedding.http_error path=%s status=%s",
                        path, resp.status_code,
                    )
                    return None
                return resp.json()
        except ImportError:
            pass
        except Exception as exc:  # noqa: BLE001
            _log.warning("embedding.request_failed path=%s err=%s", path, exc)
            return None

        # urllib fallback
        import json as _json
        import urllib.request as _ur
        import urllib.error as _ue

        # 本地端点用一个无代理 opener(ProxyHandler({}) 显式清空代理),
        # 与 httpx 直连保持一致;远程端点用默认 opener(尊重系统代理 + NO_PROXY)。
        _opener = (
            _ur.build_opener(_ur.ProxyHandler({})) if self._is_local
            else _ur.build_opener()
        )

        def _sync() -> dict[str, Any] | None:
            req = _ur.Request(
                url, method="POST", headers=headers,
                data=_json.dumps(body).encode("utf-8"),
            )
            try:
                with _opener.open(req, timeout=self._timeout_s) as r:
                    raw = r.read()
            except _ue.HTTPError as exc:
                _log.warning(
                    "embedding.http_error path=%s status=%s", path, exc.code,
                )
                return None
            except (OSError, TimeoutError) as exc:
                _log.warning("embedding.network_error path=%s err=%s", path, exc)
                return None
            try:
                return _json.loads(raw.decode("utf-8", errors="replace"))
            except _json.JSONDecodeError:
                return None

        return await asyncio.get_event_loop().run_in_executor(None, _sync)


def build_embedding_provider(cfg: dict | None) -> EmbeddingProvider | None:
    """Construct an embedding provider from the daemon config.

    Returns ``None`` when no provider is configured / reachable —
    indexer code should treat that as "embedding disabled, run
    text-only" rather than as an error.
    """
    if not cfg:
        return _from_env()
    sec = (((cfg.get("evolution") or {}).get("memory") or {}).get("embedding") or {})
    if not sec:
        # No explicit embedding block — fall back to an ``embedding``-tagged
        # LLM profile (cloud or a local Ollama/vLLM endpoint configured as a
        # profile). Without this, a user who added an embedding model still
        # got keyword-only recall because nothing consumed the profile.
        prof = _from_profiles(cfg)
        if prof is not None:
            return prof
        return _from_env()
    provider = str(sec.get("provider") or "openai").lower()
    if provider not in ("openai", ""):
        # Future: extend with "ollama_native", "bge_local", etc. For
        # now anything not openai-shape goes through the OpenAI client
        # (most ollama / vllm shims are OpenAI-compatible).
        provider = "openai"
    p = OpenAIEmbeddingProvider(
        api_key=sec.get("api_key"),
        base_url=sec.get("base_url"),
        model=sec.get("model") or "text-embedding-3-small",
        dimensions=int(sec.get("dimensions") or 1536),
        max_batch_size=int(sec.get("max_batch_size") or 16),
        timeout_s=float(sec.get("timeout_s") or 30.0),
    )
    if p.is_available():
        return p
    # Fall through to env vars
    return _from_env()


_EMBED_NAME_HINTS = (
    "embedding", "embed", "bge-", "bge_", "text-embedding", "m3e", "voyage",
    "nomic-embed", "gte-", "e5-", "jina-embed",
)


def _looks_like_embedding_model(model: str, caps: Any) -> bool:
    if isinstance(caps, list) and any(
        isinstance(c, str) and c.strip().lower() == "embedding" for c in caps
    ):
        return True
    ml = model.lower()
    return any(h in ml for h in _EMBED_NAME_HINTS)


def _from_profiles(cfg: dict) -> EmbeddingProvider | None:
    """Resolve an embedding provider from an ``embedding``-capability LLM
    profile (explicit ``capabilities`` tag or the name heuristic).

    Note: dimension is NOT guessed. We pass the profile's explicit
    ``dimensions`` when set, else leave the OpenAIEmbeddingProvider default
    — for OpenAI-shape models that honour the ``dimensions`` request param
    that's correct; for fixed-dim models the index must lazy-init its dim
    from the first real vector. The fingerprint guard catches a later
    mismatch loudly rather than letting recall degrade silently."""
    llm = cfg.get("llm") if isinstance(cfg, dict) else None
    profiles = llm.get("profiles") if isinstance(llm, dict) else None
    if not isinstance(profiles, list):
        return None
    for entry in profiles:
        if not isinstance(entry, dict) or entry.get("enabled") is False:
            continue
        model = str(entry.get("model") or entry.get("default_model") or "").strip()
        if not model or not _looks_like_embedding_model(model, entry.get("capabilities")):
            continue
        api_key = entry.get("api_key") if isinstance(entry.get("api_key"), str) else ""
        pid = str(entry.get("id") or "").strip()
        if not api_key.strip() and pid:
            try:
                from xmclaw.utils.secrets import get_secret
                api_key = get_secret(f"llm.profile.{pid}.api_key") or ""
            except Exception:  # noqa: BLE001
                api_key = ""
        base_url = entry.get("base_url") if isinstance(entry.get("base_url"), str) else None
        dims = entry.get("dimensions") or entry.get("embedding_dim")
        kwargs: dict[str, Any] = {
            "api_key": api_key.strip() or None,
            "base_url": base_url,
            "model": model,
        }
        if isinstance(dims, int) and dims > 0:
            kwargs["dimensions"] = dims
        p = OpenAIEmbeddingProvider(**kwargs)
        if p.is_available():
            _log.info(
                "embedding.from_profile model=%s base=%s",
                model, base_url or "openai",
            )
            return p
    return None


def _from_env() -> EmbeddingProvider | None:
    # B-43: allow an Ollama-style env config without a key — the
    # base_url being localhost is the user's signal that auth isn't
    # required.
    has_key = bool(os.environ.get("XMC_EMBEDDING_API_KEY"))
    has_local_url = bool(os.environ.get("XMC_EMBEDDING_BASE_URL"))
    if not has_key and not has_local_url:
        return None
    p = OpenAIEmbeddingProvider(
        dimensions=int(os.environ.get("XMC_EMBEDDING_DIMENSIONS") or 1536),
    )
    return p if p.is_available() else None
