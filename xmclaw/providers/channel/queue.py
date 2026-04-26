"""UnifiedInboundQueue — fan-in across all enabled channels.

Direct port of QwenPaw's ``app/channels/unified_queue_manager.py`` shape:
every channel adapter publishes into one queue, the AgentLoop consumer
pulls from the same queue regardless of source channel. This decouples
adapters from the dispatch logic — adding a new channel = registering
a new producer; the consumer side never changes.

The queue is bounded (``maxsize`` default 1000) so a runaway channel
can't OOM the daemon. Producers that hit the cap raise ``asyncio.QueueFull``
which the channel adapter is expected to log + drop (don't block the
adapter's read loop, it would freeze that platform's whole inbox).
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from xmclaw.providers.channel.base import InboundMessage

_log = logging.getLogger(__name__)


@dataclass
class _QueueItem:
    """Internal wrapper — adds a sequence number for ordering observability."""
    seq: int
    message: InboundMessage
    received_at: float = field(default_factory=lambda: __import__("time").time())


class UnifiedInboundQueue:
    """Single async queue all channels feed into.

    Args:
        maxsize: max pending messages. 0 = unbounded (not recommended).

    Producers:
        ``await queue.put(message)`` — channel adapter calls this when
        it gets an inbound message.

    Consumer:
        ``async for item in queue.drain():`` — typically the AgentLoop
        WS gateway. ``item.message`` is the :class:`InboundMessage`.
    """

    def __init__(self, *, maxsize: int = 1000) -> None:
        self._queue: asyncio.Queue[_QueueItem] = asyncio.Queue(maxsize=maxsize)
        self._seq = 0
        self._closed = False

    @property
    def size(self) -> int:
        return self._queue.qsize()

    @property
    def is_full(self) -> bool:
        return self._queue.full()

    async def put(self, message: InboundMessage) -> None:
        """Enqueue an inbound message. Raises QueueFull when at cap."""
        if self._closed:
            raise RuntimeError("queue is closed")
        self._seq += 1
        try:
            self._queue.put_nowait(_QueueItem(seq=self._seq, message=message))
        except asyncio.QueueFull:
            _log.warning(
                "channel.queue_full channel=%s ref=%s",
                message.target.channel,
                message.target.ref,
            )
            raise

    async def get(self) -> InboundMessage:
        """Block until one inbound message is available; return it.

        Returns the raw :class:`InboundMessage` (sequence number is
        only used for internal observability).
        """
        item = await self._queue.get()
        return item.message

    async def drain(self) -> AsyncIterator[InboundMessage]:
        """Async iterator — yields messages until the queue closes."""
        while not self._closed:
            try:
                item = await self._queue.get()
            except asyncio.CancelledError:
                return
            yield item.message

    async def close(self) -> None:
        """Mark closed; existing in-flight gets unblock on next await
        when the queue empties."""
        self._closed = True
