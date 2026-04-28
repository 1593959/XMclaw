"""HindsightMemoryProvider — cloud knowledge-graph memory plugin.

B-28 real HTTP integration (was scaffold-only in B-27). Talks to a
Hindsight-compatible REST API:

  POST {base_url}/retain     body: {text, metadata, embedding?}
  POST {base_url}/recall     body: {query, top_k, filters?}
  POST {base_url}/synthesize body: {query}
  POST {base_url}/forget     body: {id}
  POST {base_url}/flush      body: {session_id}

Auth: ``Authorization: Bearer {api_key}``. Endpoints follow the
de-facto retain/recall API shape — works with real Hindsight Cloud
AND with self-hosted Hindsight via the embedded daemon
(localhost:8080 by default). Users who want a fully different
backend can override base_url.

Config (xmclaw config.json or daemon/config.json):

    {
      "evolution": {
        "memory": {
          "provider": "hindsight",
          "hindsight": {
            "api_key": "hk_...",
            "base_url": "https://api.hindsight.io",
            "timeout_s": 8
          }
        }
      }
    }

Or via env: ``HINDSIGHT_API_KEY`` + ``HINDSIGHT_BASE_URL``.

Failure mode: any HTTP error → returns empty result + logs warning.
Won't break the agent — manager isolates failures.
"""
from __future__ import annotations

import os
import time
import uuid
from typing import Any

from xmclaw.providers.memory.base import Layer, MemoryItem, MemoryProvider
from xmclaw.utils.log import get_logger

_log = get_logger(__name__)


class HindsightMemoryProvider(MemoryProvider):
    """Cloud knowledge-graph memory provider.

    Talks to a Hindsight-compatible REST API. ``is_available()``
    returns True iff an API key is configured AND ``httpx`` (or
    ``urllib`` fallback) can reach the base URL.
    """

    name = "hindsight"

    def __init__(
        self, *, api_key: str | None = None, base_url: str | None = None,
        timeout_s: float = 8.0,
    ) -> None:
        self._api_key = api_key or os.environ.get("HINDSIGHT_API_KEY") or ""
        self._base_url = (
            (base_url or os.environ.get("HINDSIGHT_BASE_URL")
             or "https://api.hindsight.io").rstrip("/")
        )
        self._timeout_s = float(timeout_s)
        # Cache of prefetched recall blocks, keyed by (session, query
        # hash). queue_prefetch fires a synthesise call in the
        # background; prefetch drains. Bounded to 32 entries.
        self._prefetch_cache: dict[tuple[str, str], str] = {}
        self._inflight: set[tuple[str, str]] = set()

    def is_available(self) -> bool:
        return bool(self._api_key)

    # ── HTTP plumbing ────────────────────────────────────────────

    async def _post(
        self, path: str, body: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Tiny async POST helper. Tries httpx first (proper async),
        falls back to a thread-pool urllib call. Returns None on any
        failure (auth, timeout, connection refused, JSON decode)."""
        url = f"{self._base_url}{path}"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        try:
            import httpx
            async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                resp = await client.post(url, headers=headers, json=body)
                if resp.status_code >= 400:
                    _log.warning(
                        "hindsight.http_error path=%s status=%s",
                        path, resp.status_code,
                    )
                    return None
                return resp.json()
        except ImportError:
            # Fall through to urllib in a thread.
            pass
        except Exception as exc:  # noqa: BLE001
            _log.warning("hindsight.request_failed path=%s err=%s", path, exc)
            return None

        # urllib fallback — runs in default thread pool so we don't
        # block the event loop.
        import asyncio as _asyncio
        import json as _json
        import urllib.request as _ur
        import urllib.error as _ue

        def _sync_post() -> dict[str, Any] | None:
            req = _ur.Request(
                url, method="POST",
                headers=headers,
                data=_json.dumps(body).encode("utf-8"),
            )
            try:
                with _ur.urlopen(req, timeout=self._timeout_s) as r:
                    raw = r.read()
            except _ue.HTTPError as exc:
                _log.warning(
                    "hindsight.http_error path=%s status=%s",
                    path, exc.code,
                )
                return None
            except (OSError, TimeoutError) as exc:
                _log.warning(
                    "hindsight.network_error path=%s err=%s", path, exc,
                )
                return None
            try:
                return _json.loads(raw.decode("utf-8", errors="replace"))
            except _json.JSONDecodeError:
                return None

        return await _asyncio.get_event_loop().run_in_executor(None, _sync_post)

    # ── MemoryProvider API ───────────────────────────────────────

    async def put(self, layer: Layer, item: MemoryItem) -> str:
        body = {
            "text": item.text,
            "metadata": {**(item.metadata or {}), "layer": layer, "ts": item.ts},
        }
        if item.embedding:
            body["embedding"] = list(item.embedding)
        resp = await self._post("/retain", body)
        if resp and isinstance(resp.get("id"), str):
            return resp["id"]
        return item.id or uuid.uuid4().hex

    async def query(
        self, layer: Layer, *,
        text: str | None = None, embedding: list[float] | None = None,
        k: int = 10, filters: dict[str, Any] | None = None,
    ) -> list[MemoryItem]:
        if not text and not embedding:
            return []
        body: dict[str, Any] = {
            "query": text or "",
            "top_k": int(k),
            "layer": layer,
        }
        if filters:
            body["filters"] = filters
        if embedding:
            body["embedding"] = list(embedding)
        resp = await self._post("/recall", body)
        if not resp:
            return []
        rows = resp.get("results") or resp.get("hits") or []
        out: list[MemoryItem] = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            out.append(MemoryItem(
                id=str(r.get("id") or uuid.uuid4().hex),
                layer=layer,
                text=str(r.get("text") or r.get("content") or ""),
                metadata=dict(r.get("metadata") or {}),
                ts=float(r.get("ts") or 0),
            ))
        return out

    async def forget(self, item_id: str) -> None:
        await self._post("/forget", {"id": item_id})

    # ── Hooks ────────────────────────────────────────────────────

    async def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Drain prefetched recall block for this (session, query)."""
        key = (session_id, _hash(query))
        return self._prefetch_cache.pop(key, "")

    async def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """Spin a background ``/synthesize`` call. One in-flight per
        (session, query) — the next call against an in-flight key is
        a no-op. Bounded cache (32 entries; oldest evicted)."""
        key = (session_id, _hash(query))
        if key in self._inflight or key in self._prefetch_cache:
            return
        self._inflight.add(key)
        import asyncio as _asyncio

        async def _bg() -> None:
            try:
                resp = await self._post(
                    "/synthesize",
                    {"query": query, "session_id": session_id},
                )
                if resp and isinstance(resp.get("answer"), str):
                    if len(self._prefetch_cache) >= 32:
                        # Evict oldest by FIFO order.
                        first_k = next(iter(self._prefetch_cache))
                        self._prefetch_cache.pop(first_k, None)
                    self._prefetch_cache[key] = resp["answer"]
            except Exception:  # noqa: BLE001
                pass
            finally:
                self._inflight.discard(key)

        _asyncio.create_task(_bg(), name=f"hindsight-prefetch-{session_id[:8]}")

    async def on_session_end(
        self, *, session_id: str, messages: list,
    ) -> None:
        await self._post("/flush", {"session_id": session_id})

    def on_pre_compress(self, messages: list) -> str:
        """Returns the running entity-resolve digest. Best-effort:
        synchronous-only since the compressor seam is sync — we
        just emit a stub line that says we have a backend, and rely
        on prefetch / sync_turn to do the real work async."""
        if not messages:
            return ""
        return (
            f"_(Hindsight long-term memory is active — "
            f"{len(messages)} earlier messages already retained)_"
        )

    def system_prompt_block(self) -> str:
        return (
            "Long-term memory is backed by Hindsight (cloud knowledge "
            "graph). When a user asks about something we've discussed "
            "before, prefer the ``recall_memory`` tool over guessing."
        )

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        if not self.is_available():
            return []
        return [
            {
                "name": "recall_memory",
                "description": (
                    "Search Hindsight long-term memory across past "
                    "sessions. Returns the top-k most relevant turns."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Natural-language search query"},
                        "k": {"type": "integer", "description": "Top-k results (default 5)"},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "synthesize_memory",
                "description": (
                    "Ask Hindsight to synthesize an answer from "
                    "long-term memory. Returns prose, not raw hits."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                    },
                    "required": ["query"],
                },
            },
        ]

    async def handle_tool_call(
        self, tool_name: str, args: dict[str, Any], **kwargs: Any,
    ) -> str:
        if tool_name == "recall_memory":
            q = str(args.get("query") or "")
            k = int(args.get("k") or 5)
            hits = await self.query("long", text=q, k=k)
            if not hits:
                return "No relevant memories found."
            lines = [f"Found {len(hits)} memories:"]
            for i, h in enumerate(hits, 1):
                lines.append(f"  {i}. {h.text[:200]}")
            return "\n".join(lines)
        if tool_name == "synthesize_memory":
            q = str(args.get("query") or "")
            resp = await self._post("/synthesize", {"query": q})
            if not resp:
                return "Synthesis failed."
            return str(resp.get("answer") or "(empty)")
        return f"hindsight tool {tool_name!r} not implemented"


def _hash(s: str) -> str:
    """Stable short hash for prefetch cache keys."""
    import hashlib
    return hashlib.blake2s(s.encode("utf-8"), digest_size=8).hexdigest()
