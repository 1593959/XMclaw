"""Integration manager — lifecycle & message routing."""
from __future__ import annotations
import asyncio
from typing import TYPE_CHECKING
from xmclaw.utils.log import logger
from .base import Integration
from .slack import SlackIntegration
from .discord import DiscordIntegration
from .telegram import TelegramIntegration
from .github import GitHubIntegration
from .notion import NotionIntegration

if TYPE_CHECKING:
    from xmclaw.core.orchestrator import AgentOrchestrator

_REGISTRY: dict[str, type[Integration]] = {
    "slack": SlackIntegration,
    "discord": DiscordIntegration,
    "telegram": TelegramIntegration,
    "github": GitHubIntegration,
    "notion": NotionIntegration,
}


class IntegrationManager:
    """Manages all external integrations: start, stop, routing."""

    def __init__(self, config: dict, orchestrator: "AgentOrchestrator | None" = None):
        self._config = config  # dict keyed by integration name
        self._orchestrator = orchestrator
        self._integrations: dict[str, Integration] = {}

    def _build_instances(self) -> None:
        for name, cls in _REGISTRY.items():
            cfg = self._config.get(name, {})
            if cfg.get("enabled", False):
                inst = cls(cfg)
                agent_id = cfg.get("agent_id", "default")
                inst.on_message(self._make_handler(name, agent_id))
                self._integrations[name] = inst

    def _make_handler(self, integration_name: str, agent_id: str):
        async def handler(source_id: str, text: str, metadata: dict) -> None:
            if not self._orchestrator:
                return
            logger.info(
                "integration_message_received",
                integration=integration_name,
                source=source_id,
                length=len(text),
            )
            try:
                agent = await self._orchestrator.get_or_create_agent(agent_id)
                # Prepend context so the agent knows the message origin
                prefixed = f"[来自 {integration_name.upper()} / {source_id}] {text}"
                # Run agent and collect results
                result_chunks = []
                async for chunk in agent.run(prefixed):
                    result_chunks.append(chunk)
                result = "".join(result_chunks)
                # Route reply back
                integration = self._integrations.get(integration_name)
                if integration and result:
                    reply_target = metadata.get("channel") or metadata.get("chat_id") or metadata.get("channel_id")
                    await integration.send(str(result), target=reply_target)
            except Exception as e:
                logger.error("integration_handler_error", integration=integration_name, error=str(e))
        return handler

    async def start(self) -> None:
        self._build_instances()
        for name, inst in self._integrations.items():
            try:
                await inst.connect()
                logger.info("integration_started", name=name)
            except Exception as e:
                logger.error("integration_start_failed", name=name, error=str(e))

    async def stop(self) -> None:
        for name, inst in self._integrations.items():
            try:
                await inst.disconnect()
                logger.info("integration_stopped", name=name)
            except Exception as e:
                logger.warning("integration_stop_error", name=name, error=str(e))
        self._integrations.clear()

    def get(self, name: str) -> Integration | None:
        return self._integrations.get(name)

    @property
    def status(self) -> dict[str, dict]:
        result = {}
        for name in _REGISTRY:
            cfg = self._config.get(name, {})
            inst = self._integrations.get(name)
            result[name] = {
                "enabled": cfg.get("enabled", False),
                "running": inst.is_running if inst else False,
                "configured": bool(cfg),
            }
        return result

    @classmethod
    def available_integrations(cls) -> list[str]:
        return list(_REGISTRY.keys())
