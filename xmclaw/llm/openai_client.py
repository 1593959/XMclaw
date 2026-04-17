"""OpenAI-compatible client."""
from typing import AsyncIterator
from openai import AsyncOpenAI
from xmclaw.utils.log import logger


class OpenAIClient:
    def __init__(self, config: dict):
        self.api_key = config.get("api_key", "")
        self.client = AsyncOpenAI(
            api_key=self.api_key,
            base_url=config.get("base_url", "https://api.openai.com/v1"),
        )
        self.model = config.get("default_model", "")

    async def stream(self, messages: list[dict]) -> AsyncIterator[str]:
        if not self.api_key:
            logger.error("openai_api_key_missing")
            yield "[错误：未配置 OpenAI API Key，请前往「设置 → LLM」填写。]"
            return
        if not self.model:
            logger.error("openai_model_missing")
            yield "[错误：未配置 OpenAI 模型名称，请前往「设置 → LLM」填写，例如 gpt-4o 或 gpt-4.1。]"
            return
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
        if not self.api_key:
            logger.error("openai_api_key_missing")
            return "[错误：未配置 OpenAI API Key，请前往「设置 → LLM」填写。]"
        if not self.model:
            logger.error("openai_model_missing")
            return "[错误：未配置 OpenAI 模型名称，请前往「设置 → LLM」填写。]"
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            stream=False,
        )
        return response.choices[0].message.content or ""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if self.api_key:
            try:
                response = await self.client.embeddings.create(
                    model="text-embedding-3-small",
                    input=texts,
                )
                return [item.embedding for item in response.data]
            except Exception as e:
                logger.error("openai_embed_error", error=str(e))
        # Fallback to local Ollama embedding for testing
        return await self._ollama_embed(texts)

    async def _ollama_embed(self, texts: list[str]) -> list[list[float]]:
        import aiohttp
        url = "http://127.0.0.1:11434/api/embeddings"
        results = []
        async with aiohttp.ClientSession() as session:
            for text in texts:
                try:
                    async with session.post(url, json={"model": "qwen3-embedding:0.6b", "prompt": text}) as resp:
                        data = await resp.json()
                        results.append(data.get("embedding", []))
                except Exception as e:
                    logger.error("ollama_embed_error", error=str(e))
                    results.append([])
        return results
