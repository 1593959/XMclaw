"""MemoryManager — orchestrates multiple MemoryProviders.

B-25 (Hermes parity). Hermes' ``agent/memory_manager.py`` ships a
two-tier model: a built-in file-backed provider (always present,
non-removable) + at most ONE external plugin provider (hindsight,
supermemory, mem0, etc.). The manager holds them both, dispatches
tool calls, fans out turn-end syncs, isolates failures.

We adopt that shape so XMclaw can grow the same plugin ecosystem
later (the abstraction comes first; concrete plugins land per-need).

Today's wiring:
  • ``BuiltinFileMemoryProvider`` — wraps the persona files
    (MEMORY.md / USER.md) so they're addressable through the same
    interface as everything else
  • Existing ``SqliteVecMemory`` — already implements
    :class:`MemoryProvider`; the manager just owns it as the second
    provider when configured

The agent_loop uses the manager for prefetch / sync without caring
which provider answered.
"""
from __future__ import annotations

from typing import Any, Iterable

from xmclaw.providers.memory.base import Layer, MemoryItem, MemoryProvider
from xmclaw.utils.log import get_logger

_log = get_logger(__name__)


class MemoryManager:
    """Holds 1 builtin + at most 1 external memory provider.

    Mirrors Hermes ``agent/memory_manager.MemoryManager``: builtin is
    non-removable, only one external provider permitted at a time
    (avoid tool-schema bloat + conflicting backends).
    """

    BUILTIN_NAME = "builtin"

    def __init__(self, *, bus: Any | None = None) -> None:
        self._providers: list[MemoryProvider] = []
        self._has_external: bool = False
        # B-27: emit MEMORY_OP events for observability so the Trace
        # page can show provider activity. ``bus`` is duck-typed —
        # anything with ``async publish(event)`` works. None = no
        # emission (used by tests / standalone code).
        self._bus = bus

    def attach_bus(self, bus: Any) -> None:
        """Wire a bus after construction. Tools that build the manager
        before the bus is available use this to upgrade later."""
        self._bus = bus

    async def _emit(
        self, op: str, *, provider: str, session_id: str | None = None,
        elapsed_ms: float = 0.0, k: int | None = None,
        hits: int | None = None, extra: dict | None = None,
    ) -> None:
        """Emit a MEMORY_OP event. Best-effort; never raises."""
        if self._bus is None:
            return
        try:
            from xmclaw.core.bus import EventType, make_event
            payload: dict = {
                "provider": provider, "op": op,
                "session_id": session_id, "elapsed_ms": elapsed_ms,
            }
            if k is not None:
                payload["k"] = k
            if hits is not None:
                payload["hits"] = hits
            if extra:
                payload.update(extra)
            ev = make_event(
                session_id=session_id or "_system",
                agent_id="memory",
                type=EventType.MEMORY_OP,
                payload=payload,
            )
            await self._bus.publish(ev)
        except Exception:  # noqa: BLE001 — observability must not break ops
            pass

    # ── registration ─────────────────────────────────────────────

    def add_provider(self, provider: MemoryProvider) -> bool:
        """Register a provider. Returns True if accepted, False if
        rejected (e.g. second external provider attempt)."""
        is_builtin = getattr(provider, "name", "") == self.BUILTIN_NAME
        if not is_builtin:
            if self._has_external:
                existing = next(
                    (getattr(p, "name", "?") for p in self._providers
                     if getattr(p, "name", "") != self.BUILTIN_NAME),
                    "?",
                )
                _log.warning(
                    "memory.duplicate_external_rejected new=%s existing=%s",
                    getattr(provider, "name", "?"), existing,
                )
                return False
            self._has_external = True
        self._providers.append(provider)
        return True

    @property
    def providers(self) -> list[MemoryProvider]:
        return list(self._providers)

    @property
    def is_empty(self) -> bool:
        return not self._providers

    # ── core dispatch (delegate to first provider that supports the op) ─

    async def put(self, layer: Layer, item: MemoryItem) -> str | None:
        """Write to the first provider that accepts. External provider
        first (it's the "active recall" surface), builtin last."""
        import time as _t
        # Iterate external first, then builtin, so the EXTERNAL provider
        # gets writes if both are registered. The builtin is a fallback
        # when nothing else is wired.
        for p in self._iter_external_first():
            t0 = _t.perf_counter()
            try:
                rid = await p.put(layer, item)
                await self._emit(
                    "put", provider=getattr(p, "name", "?"),
                    session_id=(item.metadata or {}).get("session_id"),
                    elapsed_ms=(_t.perf_counter() - t0) * 1000.0,
                    extra={"layer": layer},
                )
                return rid
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "memory.put_failed provider=%s err=%s",
                    getattr(p, "name", "?"), exc,
                )
                continue
        return None

    async def query(
        self,
        layer: Layer,
        *,
        text: str | None = None,
        embedding: list[float] | None = None,
        k: int = 10,
        filters: dict[str, Any] | None = None,
        hybrid: bool = False,
    ) -> list[MemoryItem]:
        """Query first provider that returns results. Returns empty
        list if all providers fail.

        B-50: when ``hybrid=True`` AND both ``text`` and ``embedding``
        are supplied AND the provider implements ``hybrid_query``,
        route through that path instead — Reciprocal Rank Fusion of
        the vector + keyword candidate lists. Falls back to plain
        ``query()`` when the provider doesn't support hybrid.
        """
        import time as _t
        sid = (filters or {}).get("session_id") if filters else None
        for p in self._iter_external_first():
            t0 = _t.perf_counter()
            try:
                use_hybrid = (
                    hybrid and text and embedding
                    and hasattr(p, "hybrid_query")
                )
                if use_hybrid:
                    hits = await p.hybrid_query(  # type: ignore[attr-defined]
                        layer, text=text, embedding=embedding, k=k, filters=filters,
                    )
                    op_label = "hybrid_query"
                else:
                    hits = await p.query(
                        layer, text=text, embedding=embedding, k=k, filters=filters,
                    )
                    op_label = "query"
                await self._emit(
                    op_label, provider=getattr(p, "name", "?"),
                    session_id=sid,
                    elapsed_ms=(_t.perf_counter() - t0) * 1000.0,
                    k=k, hits=len(hits),
                )
                if hits:
                    return hits
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "memory.query_failed provider=%s err=%s",
                    getattr(p, "name", "?"), exc,
                )
                continue
        return []

    async def forget(self, item_id: str) -> None:
        for p in self._providers:
            try:
                await p.forget(item_id)
            except Exception:  # noqa: BLE001
                pass

    # ── hooks (optional, providers override) ─────────────────────

    async def sync_turn(
        self, *, session_id: str, agent_id: str,
        user_content: str, assistant_content: str,
    ) -> None:
        """End-of-turn write-back. Each provider gets a chance; failures
        in one don't block others. Mirrors Hermes ``sync_all``."""
        import time as _t
        for p in self._providers:
            sync = getattr(p, "sync_turn", None)
            if sync is None:
                continue
            t0 = _t.perf_counter()
            try:
                if _is_async_method(sync):
                    await sync(
                        session_id=session_id, agent_id=agent_id,
                        user_content=user_content, assistant_content=assistant_content,
                    )
                else:
                    sync(
                        session_id=session_id, agent_id=agent_id,
                        user_content=user_content, assistant_content=assistant_content,
                    )
                # B-27: emit so the Trace page sees provider activity
                # even when the provider's sync_turn goes via its own
                # put (bypassing manager.put which has its own emit).
                await self._emit(
                    "sync_turn", provider=getattr(p, "name", "?"),
                    session_id=session_id,
                    elapsed_ms=(_t.perf_counter() - t0) * 1000.0,
                )
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "memory.sync_failed provider=%s err=%s",
                    getattr(p, "name", "?"), exc,
                )

    async def on_session_end(self, *, session_id: str, messages: list) -> None:
        """Session-end summary hook. Hermes uses this for fact extraction."""
        for p in self._providers:
            hook = getattr(p, "on_session_end", None)
            if hook is None:
                continue
            try:
                if _is_async_method(hook):
                    await hook(session_id=session_id, messages=messages)
                else:
                    hook(session_id=session_id, messages=messages)
            except Exception:  # noqa: BLE001
                pass

    async def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Aggregate prefetched recall blocks across providers.

        External provider returns first (its recall is usually
        higher-signal); builtin appends below. Empty providers are
        skipped silently. Failures are isolated per-provider.
        """
        parts: list[str] = []
        for p in self._iter_external_first():
            fn = getattr(p, "prefetch", None)
            if fn is None:
                continue
            try:
                blk = await fn(query, session_id=session_id) if _is_async_method(fn) \
                    else fn(query, session_id=session_id)
            except Exception:  # noqa: BLE001
                continue
            if blk:
                parts.append(str(blk))
        return "\n\n".join(parts)

    async def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """Fan-out: notify every provider to spin a background fetch
        for the NEXT turn. Best-effort, non-blocking."""
        for p in self._providers:
            fn = getattr(p, "queue_prefetch", None)
            if fn is None:
                continue
            try:
                if _is_async_method(fn):
                    await fn(query, session_id=session_id)
                else:
                    fn(query, session_id=session_id)
            except Exception:  # noqa: BLE001
                pass

    def on_pre_compress(self, messages: list) -> str:
        """Aggregate pre-compression text from all providers — used
        by future context compressor to preserve fact-extracted
        insights when older messages get dropped."""
        parts: list[str] = []
        for p in self._providers:
            fn = getattr(p, "on_pre_compress", None)
            if fn is None:
                continue
            try:
                blk = fn(messages)
            except Exception:  # noqa: BLE001
                continue
            if blk:
                parts.append(str(blk))
        return "\n\n".join(parts)

    def system_prompt_block(self) -> str:
        """Concatenate static prompt blocks from all providers.

        Each provider's ``system_prompt_block()`` returns markdown to
        splice into the system prompt. Used for the BuiltinFile
        provider's MEMORY.md / USER.md content; external providers
        often return "" (their value is dynamic prefetch, not static
        prompt prose)."""
        parts: list[str] = []
        for p in self._providers:
            fn = getattr(p, "system_prompt_block", None)
            if fn is None:
                continue
            try:
                blk = fn() if not _is_async_method(fn) else ""
            except Exception:  # noqa: BLE001
                continue
            if blk:
                parts.append(blk)
        return "\n\n".join(parts)

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        """Aggregate tool schemas across providers."""
        schemas: list[dict[str, Any]] = []
        for p in self._providers:
            fn = getattr(p, "get_tool_schemas", None)
            if fn is None:
                continue
            try:
                got = fn() or []
            except Exception:  # noqa: BLE001
                got = []
            schemas.extend(got)
        return schemas

    # ── helpers ─────────────────────────────────────────────────

    def _iter_external_first(self) -> Iterable[MemoryProvider]:
        external = [
            p for p in self._providers
            if getattr(p, "name", "") != self.BUILTIN_NAME
        ]
        builtin = [
            p for p in self._providers
            if getattr(p, "name", "") == self.BUILTIN_NAME
        ]
        return external + builtin


def _is_async_method(fn: Any) -> bool:
    import asyncio
    import inspect
    return inspect.iscoroutinefunction(fn) or asyncio.iscoroutinefunction(fn)
