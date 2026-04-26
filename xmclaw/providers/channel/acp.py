"""ACP (Agent Client Protocol) channel adapter — Hermes integration shim.

Direct port target: ``hermes-agent/acp_adapter/server.py:13-47`` and the
companion ``acp_registry/agent.json`` 9-line manifest. ACP is Zed
editor's open agent-spec — implementing it gets XMclaw plugged into
Zed, VSCode (via the ACP extension), and JetBrains' agent panel for
free.

Wire format imports come from the upstream ``acp`` Python package
(pip install acp). Lazy-imported so daemons without ACP enabled
don't need the dep.

Phase 6 ships the manifest + adapter shape; concrete request handling
lands in Phase 6.1 once we have the full Hermes acp_adapter file
mapped to our event-bus contract. The interesting work is the
translator: ACP's ``PromptResponse`` ↔ XMclaw's ``LLM_RESPONSE`` event.

Public API:
* :class:`ACPAdapter` (sketch) — implements :class:`ChannelAdapter`
* :data:`AGENT_MANIFEST` — what to write to ``acp_registry/agent.json``
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, ClassVar

from xmclaw.providers.channel.base import (
    ChannelAdapter,
    ChannelTarget,
    InboundMessage,
    OutboundMessage,
    PluginManifest,
)

_log = logging.getLogger(__name__)


# 9-line agent manifest that Zed / VSCode / JetBrains read to learn
# how to invoke us. Mirrors hermes acp_registry/agent.json shape.
AGENT_MANIFEST: dict[str, Any] = {
    "name": "xmclaw",
    "displayName": "XMclaw",
    "description": "Local-first self-evolving AI agent",
    "version": "0.2.0",
    "command": "xmclaw",
    "args": ["acp"],
    "env": {},
    "transport": "stdio",
}


MANIFEST = PluginManifest(
    id="acp",
    label="ACP (Zed / VSCode / JetBrains)",
    adapter_factory_path="xmclaw.providers.channel.acp:ACPAdapter",
    requires=("acp>=0.1.0",),
    needs_tunnel=False,  # stdio transport, no public IP
    config_schema={
        "agent_id": "string (which xmclaw agent profile to drive)",
    },
)


class ACPAdapter(ChannelAdapter):
    """ACP server adapter — sketch, full handler in Phase 6.1.

    The hermes ``acp_adapter/server.py:73`` ThreadPoolExecutor pattern
    is the right model: each ACP session pins a thread; XMclaw runs
    the actual ``AgentLoop.run_turn`` on that thread via asyncio.

    For now this class registers itself with the channel registry +
    manifest so the UI shows ACP as available; ``start()`` raises
    ``NotImplementedError`` if the user actually enables it. The full
    port follows the same shape as the other channels — once
    ``hermes-agent/acp_adapter/server.py`` is line-by-line ported into
    this file, the channel becomes usable.
    """

    name: ClassVar[str] = "acp"

    def __init__(self, *, agent_id: str = "main") -> None:
        self._agent_id = agent_id
        self._handler: Callable[[InboundMessage], Awaitable[None]] | None = None

    async def start(self) -> None:
        # Lazy-import to avoid forcing the dep at daemon boot.
        try:
            import acp  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "ACP channel needs the 'acp' package: "
                "pip install acp (or skip enabling this channel)"
            ) from exc
        # Phase 6.1: port hermes acp_adapter/server.py:13-150 into here.
        # The InitializeResponse / PromptResponse / SessionInfo types
        # come from the imported acp module above.
        raise NotImplementedError(
            "ACP adapter is registered (manifest exposed) but the "
            "request handler hasn't been ported yet. Track at "
            "docs/DEV_PLAN.md §2.1 'Hermes 独家'."
        )

    async def stop(self) -> None:
        pass

    async def send(
        self, target: ChannelTarget, payload: OutboundMessage
    ) -> str:
        raise NotImplementedError("ACP send pending Phase 6.1 port")

    def subscribe(
        self, handler: Callable[[InboundMessage], Awaitable[None]]
    ) -> None:
        self._handler = handler
