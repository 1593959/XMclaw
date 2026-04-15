"""LLM router: selects provider and handles requests."""
from typing import AsyncIterator
from xmclaw.llm.openai_client import OpenAIClient
from xmclaw.llm.anthropic_client import AnthropicClient
from xmclaw.daemon.config import DaemonConfig
from xmclaw.utils.log import logger


class LLMRouter:
    def __init__(self):
        self.config = DaemonConfig.load()
        self.clients = {
            "openai": OpenAIClient(self.config.llm["openai"]),
            "anthropic": AnthropicClient(self.config.llm["anthropic"]),
        }

    async def stream(self, messages: list[dict]) -> AsyncIterator[str]:
        provider = self.config.llm.get("default_provider", "anthropic")
        client = self.clients.get(provider)
        if not client:
            logger.error("unknown_llm_provider", provider=provider)
            yield f"[Error: Unknown provider {provider}]"
            return
        async for chunk in client.stream(messages):
            yield chunk

    async def complete(self, messages: list[dict]) -> str:
        provider = self.config.llm.get("default_provider", "anthropic")
        client = self.clients.get(provider)
        if not client:
            raise ValueError(f"Unknown provider: {provider}")
        return await client.complete(messages)
