"""OpenAI-compatible client."""
from typing import AsyncIterator
from openai import AsyncOpenAI
from xmclaw.utils.log import logger


class OpenAIClient:
    def __init__(self, config: dict):
        self.client = AsyncOpenAI(
            api_key=config.get("api_key", ""),
            base_url=config.get("base_url", "https://api.openai.com/v1"),
        )
        self.model = config.get("default_model", "gpt-4.1")

    async def stream(self, messages: list[dict]) -> AsyncIterator[str]:
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                stream=True,
            )
            async for chunk in response:
                delta = chunk.choices[0].delta.content or ""
                yield delta
        except Exception as e:
            logger.error("openai_stream_error", error=str(e))
            yield f"[OpenAI Error: {e}]"

    async def complete(self, messages: list[dict]) -> str:
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            stream=False,
        )
        return response.choices[0].message.content or ""
