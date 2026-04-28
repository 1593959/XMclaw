"""MemoryProvider ABC + extended Hermes-parity hooks (B-26)."""
from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Any, Literal

Layer = Literal["short", "working", "long"]


@dataclass(frozen=True, slots=True)
class MemoryItem:
    id: str
    layer: Layer
    text: str
    metadata: dict[str, Any]
    embedding: tuple[float, ...] | None = None
    ts: float = 0.0


class MemoryProvider(abc.ABC):
    """Pluggable cross-session memory backend.

    Required: ``put`` / ``query`` / ``forget``.

    Optional Hermes-parity hooks (override to opt in):

      ``prefetch(query, *, session_id) -> str``
          Return cached prefetch result for the upcoming turn. Should
          be FAST — long ops happen in the background. Returns an
          empty string when no fresh recall is queued.

      ``queue_prefetch(query, *, session_id) -> None``
          Triggered after a turn completes; provider can spin a
          background fetch whose result lands in the next ``prefetch``
          call. Default no-op.

      ``sync_turn(*, session_id, agent_id, user_content,
                  assistant_content) -> None``
          End-of-turn write-back. Provider's chance to ingest the
          completed (user, assistant) pair. Default impl calls put().

      ``on_session_end(*, session_id, messages) -> None``
          Session boundary. Use for fact extraction / summarisation.
          Default no-op.

      ``on_pre_compress(messages) -> str``
          Called before context compression discards old messages.
          Return text the compressor should preserve in its summary.
          Default empty.

      ``system_prompt_block() -> str``
          Static text to splice into the system prompt. Default empty.

      ``get_tool_schemas() -> list[dict]``
          Tool schemas the provider exposes to the LLM. Default empty.
    """

    name: str = "abstract"  # subclasses override

    @abc.abstractmethod
    async def put(self, layer: Layer, item: MemoryItem) -> str: ...

    @abc.abstractmethod
    async def query(
        self,
        layer: Layer,
        *,
        text: str | None = None,
        embedding: list[float] | None = None,
        k: int = 10,
        filters: dict[str, Any] | None = None,
    ) -> list[MemoryItem]: ...

    @abc.abstractmethod
    async def forget(self, item_id: str) -> None: ...

    # ── optional hooks (override to opt in) ──────────────────────

    async def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Return prefetched recall text for the upcoming turn.

        Default: empty (no async prefetch). Providers that maintain a
        background queue should override this to drain the queue.
        """
        return ""

    async def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """Hint that the next turn might need ``query``-related recall.

        Default no-op. Providers spin a background task here so
        ``prefetch`` returns immediately when called.
        """

    async def sync_turn(
        self, *, session_id: str, agent_id: str,
        user_content: str, assistant_content: str,
    ) -> None:
        """End-of-turn ingest. Default: store the (user, assistant)
        exchange via ``put`` so simple providers don't need to override."""
        import time as _t
        import uuid as _uuid
        text = f"User: {user_content}\nAssistant: {assistant_content}"
        try:
            await self.put(
                "long",
                MemoryItem(
                    id=_uuid.uuid4().hex, layer="long", text=text,
                    metadata={
                        "session_id": session_id,
                        "agent_id": agent_id,
                        "kind": "turn",
                    },
                    ts=_t.time(),
                ),
            )
        except Exception:  # noqa: BLE001 — manager isolates failures
            pass

    async def on_session_end(
        self, *, session_id: str, messages: list,
    ) -> None:
        """Session-end summary hook. Default no-op."""

    def on_pre_compress(self, messages: list) -> str:
        """Pre-compression fact extraction. Default: empty contribution."""
        return ""

    def system_prompt_block(self) -> str:
        """Static system-prompt block. Default: empty."""
        return ""

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        """Tool schemas this provider exposes. Default: none."""
        return []
