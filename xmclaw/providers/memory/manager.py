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

    def __init__(self) -> None:
        self._providers: list[MemoryProvider] = []
        self._has_external: bool = False

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
        # Iterate external first, then builtin, so the EXTERNAL provider
        # gets writes if both are registered. The builtin is a fallback
        # when nothing else is wired.
        for p in self._iter_external_first():
            try:
                return await p.put(layer, item)
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
    ) -> list[MemoryItem]:
        """Query first provider that returns results. Returns empty
        list if all providers fail."""
        for p in self._iter_external_first():
            try:
                hits = await p.query(
                    layer, text=text, embedding=embedding, k=k, filters=filters,
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
        for p in self._providers:
            sync = getattr(p, "sync_turn", None)
            if sync is None:
                continue
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
