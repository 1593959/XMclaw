"""ChannelAdapter ABC."""
from __future__ import annotations

import abc
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import ClassVar


@dataclass(frozen=True, slots=True)
class ChannelTarget:
    channel: str   # e.g. "slack", "telegram"
    ref: str       # external channel id / room id / phone


@dataclass(frozen=True, slots=True)
class InboundMessage:
    target: ChannelTarget
    user_ref: str
    content: str
    raw: dict | None = None


@dataclass(frozen=True, slots=True)
class OutboundMessage:
    content: str
    reply_to: str | None = None
    attachments: tuple[str, ...] = ()


class ChannelAdapter(abc.ABC):
    name: ClassVar[str]

    @abc.abstractmethod
    async def start(self) -> None: ...

    @abc.abstractmethod
    async def stop(self) -> None: ...

    @abc.abstractmethod
    async def send(self, target: ChannelTarget, payload: OutboundMessage) -> str: ...

    @abc.abstractmethod
    def subscribe(
        self, handler: Callable[[InboundMessage], Awaitable[None]]
    ) -> None: ...
