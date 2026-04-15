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

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings. Falls back to openai if default provider doesn't support it."""
        provider = self.config.llm.get("default_provider", "anthropic")
        client = self.clients.get(provider)
        if client and hasattr(client, "embed"):
            result = await client.embed(texts)
            if result:
                return result
        # Fallback to openai client for embeddings
        openai_client = self.clients.get("openai")
        if openai_client:
            return await openai_client.embed(texts)
        return []
