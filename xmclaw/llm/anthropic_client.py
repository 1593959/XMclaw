"""Anthropic Claude client."""
from typing import AsyncIterator
from anthropic import AsyncAnthropic
from xmclaw.utils.log import logger


class AnthropicClient:
    def __init__(self, config: dict):
        self.api_key = config.get("api_key", "")
        self.client = AsyncAnthropic(
            api_key=self.api_key,
            base_url=config.get("base_url", "https://api.anthropic.com"),
        )
        self.model = config.get("default_model", "")

    async def stream(self, messages: list[dict]) -> AsyncIterator[str]:
        if not self.api_key:
            logger.error("anthropic_api_key_missing")
            yield "[错误：未配置 Anthropic API Key，请前往「设置 → LLM」填写。]"
            return
        if not self.model:
            logger.error("anthropic_model_missing")
            yield "[错误：未配置 Anthropic 模型名称，请前往「设置 → LLM」填写，例如 claude-opus-4-5 或 claude-sonnet-4-5。]"
            return
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
        if not self.api_key:
            logger.error("anthropic_api_key_missing")
            return "[错误：未配置 Anthropic API Key，请前往「设置 → LLM」填写。]"
        if not self.model:
            logger.error("anthropic_model_missing")
            return "[错误：未配置 Anthropic 模型名称，请前往「设置 → LLM」填写。]"
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
        # Handle both TextBlock and ThinkingBlock
        for block in response.content:
            if hasattr(block, "text"):
                return block.text
        return ""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        logger.error("anthropic_embed_not_supported")
        return []
