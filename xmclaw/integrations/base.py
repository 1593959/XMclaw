"""Integration base class."""
from __future__ import annotations
import asyncio
from abc import ABC, abstractmethod
from typing import Callable, Awaitable
from xmclaw.utils.log import logger


MessageCallback = Callable[[str, str, dict], Awaitable[None]]
"""Called with (source_id, text, metadata) when a message arrives."""


class Integration(ABC):
    """Base class for all external service integrations."""

    name: str = "base"

    def __init__(self, config: dict):
        self.config = config
        self.enabled = config.get("enabled", False)
        self._message_callbacks: list[MessageCallback] = []
        self._running = False

    def on_message(self, callback: MessageCallback) -> None:
        """Register a callback to handle incoming messages."""
        self._message_callbacks.append(callback)

    async def _dispatch(self, source_id: str, text: str, metadata: dict | None = None) -> None:
        """Dispatch an incoming message to all registered callbacks."""
        for cb in self._message_callbacks:
            try:
                await cb(source_id, text, metadata or {})
            except Exception as e:
                logger.error("integration_dispatch_error", integration=self.name, error=str(e))

    @abstractmethod
    async def connect(self) -> None:
        """Connect to the external service."""

    @abstractmethod
    async def disconnect(self) -> None:
        """Disconnect from the external service."""

    @abstractmethod
    async def send(self, text: str, target: str | None = None) -> None:
        """Send a message to the external service."""

    @property
    def is_running(self) -> bool:
        return self._running

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *_):
        await self.disconnect()
