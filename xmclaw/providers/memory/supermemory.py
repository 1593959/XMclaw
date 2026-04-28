"""SupermemoryMemoryProvider — cloud key-value memory plugin.

B-31. Talks to a Supermemory-compatible REST API:

  POST {base_url}/v3/memories          body: {content, metadata?, container_tag?}
  POST {base_url}/v3/search            body: {q, limit, container_tag?}
  POST {base_url}/v3/memories/{id}/forget   (delete)

Auth: ``Authorization: Bearer {api_key}``. Endpoints follow the
Supermemory v3 shape — works with Supermemory Cloud and any
self-hosted Supermemory server that exposes the same API surface.
Users who want a different shape can override base_url + the
provider will fall back gracefully if the endpoints 404.

Config (daemon/config.json):

    {
      "evolution": {
        "memory": {
          "provider": "supermemory",
          "supermemory": {
            "api_key": "sm_...",
            "base_url": "https://api.supermemory.ai",
            "container_tag": "xmclaw",
            "timeout_s": 8
          }
        }
      }
    }

Or via env: ``SUPERMEMORY_API_KEY`` + ``SUPERMEMORY_BASE_URL``.

Failure mode: any HTTP error → returns empty result + logs warning.
Won't break the agent — manager isolates failures.
"""
from __future__ import annotations

import os
import uuid
from typing import Any

from xmclaw.providers.memory.base import Layer, MemoryItem, MemoryProvider
from xmclaw.utils.log import get_logger

_log = get_logger(__name__)


class SupermemoryMemoryProvider(MemoryProvider):
    """Cloud key-value memory provider.

    Shape mirrors :class:`HindsightMemoryProvider` — only the wire
    format differs. Same hooks: prefetch / queue_prefetch / sync_turn /
    on_session_end / system_prompt_block / get_tool_schemas.
    """

    name = "supermemory"

    def __init__(
        self, *, api_key: str | None = None, base_url: str | None = None,
        container_tag: str | None = None, timeout_s: float = 8.0,
    ) -> None:
        self._api_key = api_key or os.environ.get("SUPERMEMORY_API_KEY") or ""
        self._base_url = (
            (base_url or os.environ.get("SUPERMEMORY_BASE_URL")
             or "https://api.supermemory.ai").rstrip("/")
        )
        # container_tag scopes memories so multiple agents can share an
        # account without crosstalk. Defaults to "xmclaw".
        self._container_tag = (
            container_tag or os.environ.get("SUPERMEMORY_CONTAINER_TAG") or "xmclaw"
        )
        self._timeout_s = float(timeout_s)
        self._prefetch_cache: dict[tuple[str, str], str] = {}
        self._inflight: set[tuple[str, str]] = set()

    def is_available(self) -> bool:
        return bool(self._api_key)

    # ── HTTP plumbing ────────────────────────────────────────────

    async def _request(
        self, method: str, path: str, body: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        url = f"{self._base_url}{path}"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        try:
            import httpx
            async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                resp = await client.request(method, url, headers=headers, json=body)
                if resp.status_code >= 400:
                    _log.warning(
                        "supermemory.http_error path=%s status=%s",
                        path, resp.status_code,
                    )
                    return None
                return resp.json() if resp.content else {}
        except ImportError:
            pass
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "supermemory.request_failed path=%s err=%s", path, exc,
            )
            return None

        # urllib fallback
        import asyncio as _asyncio
        import json as _json
        import urllib.request as _ur
        import urllib.error as _ue

        def _sync() -> dict[str, Any] | None:
            data = _json.dumps(body or {}).encode("utf-8") if body is not None else None
            req = _ur.Request(url, method=method, headers=headers, data=data)
            try:
                with _ur.urlopen(req, timeout=self._timeout_s) as r:
                    raw = r.read()
            except _ue.HTTPError as exc:
                _log.warning(
                    "supermemory.http_error path=%s status=%s",
                    path, exc.code,
                )
                return None
            except (OSError, TimeoutError) as exc:
                _log.warning(
                    "supermemory.network_error path=%s err=%s", path, exc,
                )
                return None
            if not raw:
                return {}
            try:
                return _json.loads(raw.decode("utf-8", errors="replace"))
            except _json.JSONDecodeError:
                return None

        return await _asyncio.get_event_loop().run_in_executor(None, _sync)

    # ── MemoryProvider API ───────────────────────────────────────

    async def put(self, layer: Layer, item: MemoryItem) -> str:
        body = {
            "content": item.text,
            "container_tag": self._container_tag,
            "metadata": {**(item.metadata or {}), "layer": layer, "ts": item.ts},
        }
        resp = await self._request("POST", "/v3/memories", body)
        if resp and isinstance(resp.get("id"), str):
            return resp["id"]
        return item.id or uuid.uuid4().hex

    async def query(
        self, layer: Layer, *,
        text: str | None = None, embedding: list[float] | None = None,
        k: int = 10, filters: dict[str, Any] | None = None,
    ) -> list[MemoryItem]:
        if not text:
            return []
        body: dict[str, Any] = {
            "q": text,
            "limit": int(k),
            "container_tag": self._container_tag,
        }
        if filters:
            body["filters"] = filters
        resp = await self._request("POST", "/v3/search", body)
        if not resp:
            return []
        rows = resp.get("results") or resp.get("memories") or resp.get("hits") or []
        out: list[MemoryItem] = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            out.append(MemoryItem(
                id=str(r.get("id") or uuid.uuid4().hex),
                layer=layer,
                text=str(r.get("content") or r.get("text") or ""),
                metadata=dict(r.get("metadata") or {}),
                ts=float(r.get("ts") or r.get("created_at_ts") or 0),
            ))
        return out

    async def forget(self, item_id: str) -> None:
        await self._request("DELETE", f"/v3/memories/{item_id}")

    # ── Hooks ────────────────────────────────────────────────────

    async def prefetch(self, query: str, *, session_id: str = "") -> str:
        key = (session_id, _hash(query))
        return self._prefetch_cache.pop(key, "")

    async def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        key = (session_id, _hash(query))
        if key in self._inflight or key in self._prefetch_cache:
            return
        self._inflight.add(key)
        import asyncio as _asyncio

        async def _bg() -> None:
            try:
                hits = await self.query("long", text=query, k=3)
                if hits:
                    block = "Relevant memories from Supermemory:\n" + "\n".join(
                        f"  · {h.text[:200]}" for h in hits
                    )
                    if len(self._prefetch_cache) >= 32:
                        first_k = next(iter(self._prefetch_cache))
                        self._prefetch_cache.pop(first_k, None)
                    self._prefetch_cache[key] = block
            except Exception:  # noqa: BLE001
                pass
            finally:
                self._inflight.discard(key)

        _asyncio.create_task(_bg(), name=f"supermemory-prefetch-{session_id[:8]}")

    def on_pre_compress(self, messages: list) -> str:
        if not messages:
            return ""
        return (
            f"_(Supermemory long-term memory is active — "
            f"{len(messages)} earlier messages already retained)_"
        )

    def system_prompt_block(self) -> str:
        return (
            "Long-term memory is backed by Supermemory (cloud key-value "
            "store). When the user references something from a past "
            "conversation, prefer the ``recall_memory`` tool over guessing."
        )

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        if not self.is_available():
            return []
        return [
            {
                "name": "recall_memory",
                "description": (
                    "Search Supermemory long-term memory across past "
                    "sessions. Returns the top-k most relevant items."
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
        return f"supermemory tool {tool_name!r} not implemented"


def _hash(s: str) -> str:
    import hashlib
    return hashlib.blake2s(s.encode("utf-8"), digest_size=8).hexdigest()
