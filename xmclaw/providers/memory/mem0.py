"""Mem0MemoryProvider — cloud agent memory plugin.

B-31. Talks to a Mem0-compatible REST API:

  POST {base_url}/v1/memories             body: {messages, user_id, metadata?}
  POST {base_url}/v1/memories/search      body: {query, user_id, limit}
  DELETE {base_url}/v1/memories/{id}

Auth: ``Authorization: Token {api_key}``. Mem0 (https://mem0.ai)
ships an SDK but we go HTTP-direct so XMclaw doesn't pull a heavy
dep just to support one provider — same approach as Hindsight and
Supermemory.

Config (daemon/config.json):

    {
      "evolution": {
        "memory": {
          "provider": "mem0",
          "mem0": {
            "api_key": "m0-...",
            "base_url": "https://api.mem0.ai",
            "user_id": "xmclaw-default",
            "timeout_s": 8
          }
        }
      }
    }

Or via env: ``MEM0_API_KEY`` + ``MEM0_BASE_URL`` + ``MEM0_USER_ID``.

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


class Mem0MemoryProvider(MemoryProvider):
    """Cloud agent-memory provider (mem0.ai)."""

    name = "mem0"

    def __init__(
        self, *, api_key: str | None = None, base_url: str | None = None,
        user_id: str | None = None, timeout_s: float = 8.0,
    ) -> None:
        self._api_key = api_key or os.environ.get("MEM0_API_KEY") or ""
        self._base_url = (
            (base_url or os.environ.get("MEM0_BASE_URL")
             or "https://api.mem0.ai").rstrip("/")
        )
        # Mem0 scopes everything by user_id; default to a stable
        # XMclaw-wide bucket so the user doesn't need to set this for
        # the single-user case.
        self._user_id = (
            user_id or os.environ.get("MEM0_USER_ID") or "xmclaw-default"
        )
        self._timeout_s = float(timeout_s)
        self._prefetch_cache: dict[tuple[str, str], str] = {}
        self._inflight: set[tuple[str, str]] = set()
        # B-69: hold strong refs to fire-and-forget prefetch tasks
        # so asyncio's weak-ref tracking can't GC them mid-flight.
        import asyncio as _asyncio_init
        self._bg_tasks: set[_asyncio_init.Task] = set()

    def is_available(self) -> bool:
        return bool(self._api_key)

    # ── HTTP plumbing ────────────────────────────────────────────

    async def _request(
        self, method: str, path: str, body: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        url = f"{self._base_url}{path}"
        # Mem0 uses ``Token`` not ``Bearer``.
        headers = {
            "Authorization": f"Token {self._api_key}",
            "Content-Type": "application/json",
        }
        try:
            import httpx
            async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                resp = await client.request(method, url, headers=headers, json=body)
                if resp.status_code >= 400:
                    _log.warning(
                        "mem0.http_error path=%s status=%s",
                        path, resp.status_code,
                    )
                    return None
                return resp.json() if resp.content else {}
        except ImportError:
            pass
        except Exception as exc:  # noqa: BLE001
            _log.warning("mem0.request_failed path=%s err=%s", path, exc)
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
                    "mem0.http_error path=%s status=%s", path, exc.code,
                )
                return None
            except (OSError, TimeoutError) as exc:
                _log.warning("mem0.network_error path=%s err=%s", path, exc)
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
        # Mem0 ingests "messages" rather than raw text; wrap a single
        # turn as a synthetic [{role, content}] pair. user_id scopes.
        body = {
            "messages": [{"role": "system", "content": item.text}],
            "user_id": self._user_id,
            "metadata": {**(item.metadata or {}), "layer": layer, "ts": item.ts},
        }
        resp = await self._request("POST", "/v1/memories", body)
        if resp and isinstance(resp.get("id"), str):
            return resp["id"]
        # Mem0 also returns ``results: [{id, ...}]`` on bulk add.
        results = (resp or {}).get("results")
        if isinstance(results, list) and results:
            first = results[0]
            if isinstance(first, dict) and first.get("id"):
                return str(first["id"])
        return item.id or uuid.uuid4().hex

    async def query(
        self, layer: Layer, *,
        text: str | None = None, embedding: list[float] | None = None,
        k: int = 10, filters: dict[str, Any] | None = None,
    ) -> list[MemoryItem]:
        if not text:
            return []
        body: dict[str, Any] = {
            "query": text,
            "user_id": self._user_id,
            "limit": int(k),
        }
        if filters:
            body["filters"] = filters
        resp = await self._request("POST", "/v1/memories/search", body)
        if not resp:
            return []
        rows = (
            resp.get("results")
            or resp.get("memories")
            or resp.get("matches")
            or []
        )
        out: list[MemoryItem] = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            out.append(MemoryItem(
                id=str(r.get("id") or uuid.uuid4().hex),
                layer=layer,
                text=str(r.get("memory") or r.get("text") or r.get("content") or ""),
                metadata=dict(r.get("metadata") or {}),
                ts=float(r.get("ts") or r.get("created_at_ts") or 0),
            ))
        return out

    async def forget(self, item_id: str) -> None:
        await self._request("DELETE", f"/v1/memories/{item_id}")

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
                    block = "Relevant memories from Mem0:\n" + "\n".join(
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

        # B-69: hold strong ref + auto-cleanup on done.
        bg = _asyncio.create_task(_bg(), name=f"mem0-prefetch-{session_id[:8]}")
        self._bg_tasks.add(bg)
        bg.add_done_callback(self._bg_tasks.discard)

    def on_pre_compress(self, messages: list) -> str:
        if not messages:
            return ""
        return (
            f"_(Mem0 long-term memory is active — "
            f"{len(messages)} earlier messages already retained)_"
        )

    def system_prompt_block(self) -> str:
        return (
            "Long-term memory is backed by Mem0 (cloud agent memory). "
            "When the user references something from a past conversation, "
            "prefer the ``recall_memory`` tool over guessing."
        )

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        if not self.is_available():
            return []
        return [
            {
                "name": "recall_memory",
                "description": (
                    "Search Mem0 long-term memory across past sessions. "
                    "Returns the top-k most relevant memories."
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
        return f"mem0 tool {tool_name!r} not implemented"


def _hash(s: str) -> str:
    import hashlib
    return hashlib.blake2s(s.encode("utf-8"), digest_size=8).hexdigest()
