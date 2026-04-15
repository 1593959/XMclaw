"""Anthropic Claude client."""
from typing import AsyncIterator
from anthropic import AsyncAnthropic
from xmclaw.utils.log import logger


class AnthropicClient:
    def __init__(self, config: dict):
        self.client = AsyncAnthropic(
            api_key=config.get("api_key", ""),
            base_url=config.get("base_url", "https://api.anthropic.com"),
        )
        self.model = config.get("default_model", "claude-sonnet-4-6")

    async def stream(self, messages: list[dict]) -> AsyncIterator[str]:
        try:
            # Convert OpenAI format to Anthropic format
            system = ""
            anthropic_messages = []
            for msg in messages:
                if msg["role"] == "system":
                    system = msg["content"]
                else:
                    anthropic_messages.append({"role": msg["role"], "content": msg["content"]})

            async with self.client.messages.stream(
                model=self.model,
                max_tokens=4096,
                system=system,
                messages=anthropic_messages,
            ) as stream:
                async for text in stream.text_stream:
                    yield text
        except Exception as e:
            logger.error("anthropic_stream_error", error=str(e))
            yield f"[Anthropic Error: {e}]"

    async def complete(self, messages: list[dict]) -> str:
        system = ""
        anthropic_messages = []
        for msg in messages:
            if msg["role"] == "system":
                system = msg["content"]
            else:
                anthropic_messages.append({"role": msg["role"], "content": msg["content"]})

        response = await self.client.messages.create(
            model=self.model,
            max_tokens=4096,
            system=system,
            messages=anthropic_messages,
        )
        return response.content[0].text if response.content else ""
