"""OpenAI-compatible client with native tool calling support."""
import json
from typing import AsyncIterator
from openai import AsyncOpenAI
from xmclaw.utils.log import logger


def _normalise_messages(messages: list[dict]) -> list[dict]:
    """Convert Anthropic-style tool_use/tool_result content blocks to OpenAI format.

    agent_loop uses Anthropic's native content-block format for multi-turn tool
    conversations.  OpenAI requires a different wire format:
      - assistant turn: {role:'assistant', tool_calls:[{id, type:'function', function:{name,arguments}}]}
      - tool result: {role:'tool', tool_call_id:..., content:...}
    """
    out: list[dict] = []
    for msg in messages:
        content = msg.get("content")
        role = msg.get("role", "")

        if not isinstance(content, list):
            out.append(msg)
            continue

        # Detect block types
        tool_use_blocks = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]
        tool_result_blocks = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]
        text_blocks = [b for b in content if isinstance(b, dict) and b.get("type") == "text"]

        if role == "assistant" and tool_use_blocks:
            text_content = " ".join(b.get("text", "") for b in text_blocks).strip() or None
            oai_msg: dict = {"role": "assistant", "content": text_content}
            oai_msg["tool_calls"] = [
                {
                    "id": b.get("id", f"call_{b.get('name','')}"),
                    "type": "function",
                    "function": {
                        "name": b.get("name", ""),
                        "arguments": json.dumps(b.get("input", {})),
                    },
                }
                for b in tool_use_blocks
            ]
            out.append(oai_msg)
        elif role == "user" and tool_result_blocks:
            # Expand each tool result into a separate 'tool' role message
            for b in tool_result_blocks:
                out.append({
                    "role": "tool",
                    "tool_call_id": b.get("tool_use_id", ""),
                    "content": b.get("content", ""),
                })
            # Append any plain-text blocks as a follow-up user message
            if text_blocks:
                out.append({"role": "user", "content": " ".join(b.get("text", "") for b in text_blocks)})
        else:
            # Plain list of text blocks — join into a single string
            out.append({"role": role, "content": " ".join(b.get("text", str(b)) for b in content)})

    return out


class OpenAIClient:
    def __init__(self, config: dict):
        self.api_key = config.get("api_key", "")
        self.client = AsyncOpenAI(
            api_key=self.api_key,
            base_url=config.get("base_url", "https://api.openai.com/v1"),
        )
        self.model = config.get("default_model", "")

    async def stream(self, messages: list[dict], tools: list[dict] | None = None) -> AsyncIterator[str]:
        """Stream response from OpenAI, yielding JSON event strings.

        Yields:
            - {"type": "text", "content": "..."}
            - {"type": "tool_call_start", "id": "...", "name": "..."}
            - {"type": "tool_call_input", "input_delta": "..."}
            - {"type": "tool_call_end"}
            - {"type": "error", "content": "..."}
        """
        if not self.api_key:
            logger.error("openai_api_key_missing")
            yield json.dumps({"type": "error", "content": "[错误：未配置 OpenAI API Key\n"
                "请运行 `xmclaw config init` 或编辑 daemon/config.json → llm.openai.api_key\n"
                "获取 Key: https://platform.openai.com/api-keys]"})
            return
        if not self.model:
            logger.error("openai_model_missing")
            yield json.dumps({"type": "error", "content": "[错误：未配置 OpenAI 模型\n"
                "请编辑 daemon/config.json → llm.openai.default_model，例如 gpt-4o 或 gpt-4o-mini]"})
            return
            return

        # Build OpenAI tool schemas
        oai_tools = []
        if tools:
            for t in tools:
                if "name" in t and "description" in t:
                    oai_tools.append({
                        "type": "function",
                        "function": {
                            "name": t["name"],
                            "description": t.get("description", ""),
                            "parameters": t.get("parameters", {"type": "object", "properties": {}}),
                        },
                    })

        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=_normalise_messages(messages),
                tools=oai_tools if oai_tools else None,
                tool_choice="auto" if oai_tools else None,
                stream=True,
                # Ask OpenAI to include a final chunk with `usage`. Without
                # this flag the SDK streams deltas only and we have no way
                # to bill the turn — cost_tracker.record() stays at zero.
                stream_options={"include_usage": True},
            )

            # Track per-index tool call accumulation
            active_tools: dict[int, dict] = {}

            async for chunk in response:
                # The final "usage" chunk arrives AFTER all choices; it has
                # no `choices` array but carries the token counts.
                chunk_usage = getattr(chunk, "usage", None)
                if chunk_usage is not None and not chunk.choices:
                    yield json.dumps({
                        "type": "usage",
                        "provider": "openai",
                        "model": self.model,
                        "prompt_tokens": getattr(chunk_usage, "prompt_tokens", 0) or 0,
                        "completion_tokens": getattr(chunk_usage, "completion_tokens", 0) or 0,
                    })
                    continue

                delta = chunk.choices[0].delta if chunk.choices else None
                if delta is None:
                    continue

                # Text content
                if delta.content:
                    yield json.dumps({"type": "text", "content": delta.content})

                # Tool call deltas
                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = tc.index
                        if idx not in active_tools:
                            # First chunk for this tool call — emit start
                            active_tools[idx] = {
                                "id": tc.id or "",
                                "name": tc.function.name if tc.function else "",
                            }
                            yield json.dumps({
                                "type": "tool_call_start",
                                "id": active_tools[idx]["id"],
                                "name": active_tools[idx]["name"],
                            })

                        # Accumulate function name (may arrive in pieces)
                        if tc.function and tc.function.name:
                            active_tools[idx]["name"] = tc.function.name

                        # Argument delta
                        if tc.function and tc.function.arguments:
                            yield json.dumps({
                                "type": "tool_call_input",
                                "input_delta": tc.function.arguments,
                            })

                # finish_reason == "tool_calls" → close all open tool calls
                if chunk.choices[0].finish_reason == "tool_calls":
                    for idx in active_tools:
                        yield json.dumps({"type": "tool_call_end"})
                    active_tools.clear()

        except Exception as e:
            logger.error("openai_stream_error", error=str(e))
            yield json.dumps({"type": "error", "content": f"[OpenAI Error: {e}]"})

    async def complete(self, messages: list[dict], tools: list[dict] | None = None) -> str:
        if not self.api_key:
            return "[错误：未配置 OpenAI API Key，请前往「设置 → LLM」填写。]"
        if not self.model:
            return "[错误：未配置 OpenAI 模型名称，请前往「设置 → LLM」填写。]"

        oai_tools = []
        if tools:
            for t in tools:
                if "name" in t and "description" in t:
                    oai_tools.append({
                        "type": "function",
                        "function": {
                            "name": t["name"],
                            "description": t.get("description", ""),
                            "parameters": t.get("parameters", {"type": "object", "properties": {}}),
                        },
                    })

        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=_normalise_messages(messages),
                tools=oai_tools if oai_tools else None,
                tool_choice="auto" if oai_tools else None,
                stream=False,
            )
            msg = response.choices[0].message
            # If there are tool calls, return structured JSON
            if msg.tool_calls:
                return json.dumps({
                    "text": msg.content or "",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "name": tc.function.name,
                            "input": json.loads(tc.function.arguments or "{}"),
                        }
                        for tc in msg.tool_calls
                    ],
                })
            return msg.content or ""
        except Exception as e:
            logger.error("openai_complete_error", error=str(e))
            return f"[OpenAI Error: {e}]"

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
