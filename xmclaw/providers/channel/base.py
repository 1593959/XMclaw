"""ChannelAdapter ABC + plugin-manifest shape (OpenClaw port)."""
from __future__ import annotations

import abc
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar


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


@dataclass(frozen=True, slots=True)
class PluginManifest:
    """OpenClaw-style bundled-channel plugin descriptor.

    Direct port of ``defineBundledChannelEntry`` shape from
    ``openclaw/extensions/<channel>/index.ts:4-24``. Each Python channel
    package exposes a module-level ``MANIFEST: PluginManifest`` so the
    daemon can discover + introspect available channels without
    importing the full adapter (which may pull heavy SDKs like
    ``lark-oapi`` only when the channel is actually enabled).

    Required protocols are documented per peer:

    * **Outbound**: ``send(target, payload) -> message_id``
    * **Inbound**: ``subscribe(handler)`` with at-least-once delivery
    * **Allowlist**: per-sender authorization gate (port of OpenClaw's
      ``security-contract-api`` — keeps multi-tenant agents from
      accepting commands from any random group-chat member). Phase 4+.
    * **Pairing**: webhook URL surface for inbound (Phase 5+ when we
      wire actual SDKs).

    Fields:

    * ``id`` — short slug (``"feishu"``, ``"dingtalk"``)
    * ``label`` — human-readable display name
    * ``adapter_factory_path`` — dotted path importable to instantiate
      the actual adapter class. ``"xmclaw.providers.channel.feishu:FeishuAdapter"``
    * ``requires`` — pip extras name(s) the user needs (``["lark-oapi"]``)
    * ``needs_tunnel`` — when True, daemon auto-starts cloudflared
      (mirrors QwenPaw's tunnel/cloudflare.py wiring)
    * ``config_schema`` — JSON-shape for required config keys (e.g.
      ``{"app_id": "string", "app_secret": "secret"}``)
    * ``implementation_status`` — B-38 honesty flag. ``"ready"`` =
      ``adapter_factory_path`` actually resolves to a working class;
      ``"scaffold"`` = manifest exists but the adapter module is a
      stub waiting on SDK choice / credentials. UI grays scaffold
      entries; ``discover()`` filters to ready-only by default.
      Without this flag the registry was advertising 5 phantom
      Chinese-IM channels (feishu/dingtalk/wecom/weixin/telegram —
      all manifest-only as of B-37) as if they were ready to enable.
    """
    id: str
    label: str
    adapter_factory_path: str
    requires: tuple[str, ...] = ()
    needs_tunnel: bool = False
    config_schema: dict[str, Any] = field(default_factory=dict)
    implementation_status: str = "ready"
