"""HindsightMemoryProvider — scaffold for the Hindsight cloud plugin.

B-27 SCAFFOLD ONLY. Does not actually call the Hindsight API — that
needs an API key + their SDK + integration testing. This module
exists to:

  1. Demonstrate the SHAPE a plugin author should follow.
  2. Provide a working class users can flip on (returns no-op-but-
     valid responses) to verify their config flow before swapping in
     the real API client.
  3. Document the boundaries: what hooks Hindsight maps onto, which
     XMclaw events feed which Hindsight calls.

Real Hindsight integration would require (per
https://docs.hindsight.io / Hermes ``plugins/memory/hindsight/``):

  * pip install hindsight-client (their SDK)
  * config: HINDSIGHT_API_KEY in ~/.xmclaw/.env or
    ``evolution.memory.hindsight.api_key``
  * map ``put`` → client.retain(text, metadata)
  * map ``query`` → client.recall(query, top_k) / client.search()
  * map ``sync_turn`` → batched retain at session boundaries
  * map ``get_tool_schemas`` → expose ``recall_memory`` /
    ``synthesize_memory`` so the LLM can call Hindsight directly

For now: a stub that NEVER reports ``is_available=True`` so the
factory skips it. To hand-test: set
``evolution.memory.hindsight.enabled=true`` (also a no-op gate
today), then implement the TODO sections below.
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
    """Cloud-backed knowledge-graph memory provider.

    Currently a NO-OP scaffold. Each method is a TODO placeholder
    that documents what the real implementation should do. ``is_
    available()`` returns False until a developer wires the SDK +
    credentials, so the factory's "skip on init failure" path keeps
    the agent functional even with the import present.
    """

    name = "hindsight"

    def __init__(self, *, api_key: str | None = None, base_url: str | None = None) -> None:
        self._api_key = api_key or os.environ.get("HINDSIGHT_API_KEY") or ""
        self._base_url = base_url or os.environ.get("HINDSIGHT_BASE_URL") or "https://api.hindsight.io"
        self._client: Any = None
        # Cache of prefetched recall blocks keyed by (session_id,
        # query_hash). Filled by queue_prefetch's background task,
        # drained by prefetch.
        self._prefetch_cache: dict[tuple[str, str], str] = {}

    def is_available(self) -> bool:
        """Return False until the SDK is installed AND credentials are
        present. Hooks the factory's skip-on-failure path."""
        if not self._api_key:
            return False
        try:
            # TODO: ``import hindsight`` (real SDK). Today the import
            # is wrapped in a try so the whole module loads cleanly
            # without the dep.
            return False
        except ImportError:
            return False

    # ── MemoryProvider API ───────────────────────────────────────

    async def put(self, layer: Layer, item: MemoryItem) -> str:
        """TODO: client.retain(item.text, metadata=item.metadata)."""
        if not self._client:
            return item.id or uuid.uuid4().hex
        # Real implementation:
        #   resp = await self._client.retain(
        #       text=item.text,
        #       metadata={**item.metadata, "ts": item.ts},
        #       embedding=item.embedding,
        #   )
        #   return resp.id
        return item.id or uuid.uuid4().hex

    async def query(
        self, layer: Layer, *,
        text: str | None = None, embedding: list[float] | None = None,
        k: int = 10, filters: dict[str, Any] | None = None,
    ) -> list[MemoryItem]:
        """TODO: client.recall(query=text, top_k=k, filters=filters)."""
        if not self._client:
            return []
        # Real implementation:
        #   hits = await self._client.recall(
        #       query=text or "",
        #       top_k=k,
        #       filters=filters or {},
        #   )
        #   return [MemoryItem(...) for h in hits]
        return []

    async def forget(self, item_id: str) -> None:
        """TODO: client.delete(item_id)."""

    # ── Hooks ────────────────────────────────────────────────────

    async def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Drain any cached prefetch block for this session/query.

        Hindsight has its own knowledge-graph synthesis; the natural
        thing is to call client.synthesize() in the background after
        each turn (queue_prefetch) and serve the cached result here.
        """
        key = (session_id, _hash(query))
        return self._prefetch_cache.pop(key, "")

    async def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """TODO: spin a background task that calls client.synthesize()
        and stores the result in self._prefetch_cache. Limit one
        in-flight per session."""

    async def on_session_end(
        self, *, session_id: str, messages: list,
    ) -> None:
        """TODO: client.flush(session_id) so any deferred
        retain-batches commit before we forget the conversation."""

    def on_pre_compress(self, messages: list) -> str:
        """TODO: return a digest from client.entity_resolve(messages)
        so the compressor preserves named entities + decisions."""
        return ""

    def system_prompt_block(self) -> str:
        """TODO: return a one-line note like 'Hindsight memory is
        active — use the recall_memory / synthesize_memory tools to
        explicitly query.' Empty by default."""
        return ""

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        """TODO: expose ``recall_memory`` / ``synthesize_memory``
        tools so the LLM can call Hindsight directly. Format:

            {
              "name": "recall_memory",
              "description": "Search Hindsight long-term memory ...",
              "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}, "k": {"type": "integer"}},
                "required": ["query"]
              }
            }

        Empty until is_available() is True.
        """
        return []

    async def handle_tool_call(
        self, tool_name: str, args: dict[str, Any], **kwargs: Any,
    ) -> str:
        """TODO: dispatch recall_memory / synthesize_memory to the
        client. Return a JSON string per Hermes contract."""
        return f"hindsight tool {tool_name!r} not yet implemented"


def _hash(s: str) -> str:
    """Stable short hash for prefetch cache keys."""
    import hashlib
    return hashlib.blake2s(s.encode("utf-8"), digest_size=8).hexdigest()
