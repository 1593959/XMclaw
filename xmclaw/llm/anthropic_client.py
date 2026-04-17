"""Anthropic Claude client with native tool calling support."""
import json
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

    async def stream(self, messages: list[dict], tools: list[dict] | None = None) -> AsyncIterator[str]:
        """Stream response from Claude, handling both text and tool calls.
        
        Yields:
            - Text chunks as {"type": "text", "content": ...}
            - Tool calls as {"type": "tool_call", "name": ..., "input": {...}}
            - Done signal as {"type": "done"}
        """
        if not self.api_key:
            logger.error("anthropic_api_key_missing")
            yield json.dumps({"type": "error", "content": "[错误：未配置 Anthropic API Key]"})
            return
        if not self.model:
            logger.error("anthropic_model_missing")
            yield json.dumps({"type": "error", "content": "[错误：未配置 Anthropic 模型]"})
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

            # Build tool list for Claude
            claude_tools = []
            if tools:
                for tool in tools:
                    if "name" in tool and "description" in tool:
                        claude_tools.append({
                            "name": tool["name"],
                            "description": tool.get("description", ""),
                            "input_schema": tool.get("parameters", {"type": "object", "properties": {}}),
                        })

            async with self.client.messages.stream(
                model=self.model,
                max_tokens=4096,
                system=system,
                messages=anthropic_messages,
                tools=claude_tools if claude_tools else None,
            ) as stream:
                _in_tool_block = False

                async for event in stream:
                    etype = event.type

                    if etype == "content_block_start":
                        cb = getattr(event, "content_block", None)
                        if cb and cb.type == "tool_use":
                            _in_tool_block = True
                            yield json.dumps({
                                "type": "tool_call_start",
                                "id": cb.id,
                                "name": cb.name,
                            })
                        else:
                            _in_tool_block = False

                    elif etype == "content_block_delta":
                        delta = getattr(event, "delta", None)
                        if delta:
                            if delta.type == "text_delta":
                                yield json.dumps({"type": "text", "content": delta.text})
                            elif delta.type == "input_json_delta":
                                yield json.dumps({
                                    "type": "tool_call_input",
                                    "input_delta": delta.partial_json,
                                })

                    elif etype == "content_block_stop":
                        # Only emit tool_call_end when a tool_use block ended, not text blocks
                        if _in_tool_block:
                            yield json.dumps({"type": "tool_call_end"})
                            _in_tool_block = False

        except Exception as e:
            logger.error("anthropic_stream_error", error=str(e))
            yield json.dumps({"type": "error", "content": f"[Anthropic Error: {e}]"})

    async def complete(self, messages: list[dict], tools: list[dict] | None = None) -> str:
        """Complete a request and return the response text."""
        if not self.api_key:
            logger.error("anthropic_api_key_missing")
            return "[错误：未配置 Anthropic API Key]"
        if not self.model:
            logger.error("anthropic_model_missing")
            return "[错误：未配置 Anthropic 模型]"
        
        system = ""
        anthropic_messages = []
        for msg in messages:
            if msg["role"] == "system":
                system = msg["content"]
            else:
                anthropic_messages.append({"role": msg["role"], "content": msg["content"]})

        claude_tools = []
        if tools:
            for tool in tools:
                if "name" in tool and "description" in tool:
                    claude_tools.append({
                        "name": tool["name"],
                        "description": tool.get("description", ""),
                        "input_schema": tool.get("parameters", {"type": "object", "properties": {}}),
                    })

        try:
            response = await self.client.messages.create(
                model=self.model,
                max_tokens=4096,
                system=system,
                messages=anthropic_messages,
                tools=claude_tools if claude_tools else None,
            )
            
            # Collect text and handle tool results
            text_parts = []
            tool_results = []
            stop_reason = None
            
            for block in response.content:
                if hasattr(block, "type"):
                    if block.type == "text":
                        text_parts.append(block.text)
                    elif block.type == "tool_use":
                        tool_results.append({
                            "type": "tool_use",
                            "id": block.id,
                            "name": block.name,
                            "input": block.input
                        })
            
            if hasattr(response, "stop_reason"):
                stop_reason = response.stop_reason
            
            # If there were tool calls, we need to handle them differently
            if tool_results:
                # Return structured data for tool calling
                return json.dumps({
                    "text": "\n".join(text_parts),
                    "tool_calls": tool_results,
                    "stop_reason": stop_reason
                })
            
            return "\n".join(text_parts)
            
        except Exception as e:
            logger.error("anthropic_complete_error", error=str(e))
            return f"[Anthropic Error: {e}]"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        logger.error("anthropic_embed_not_supported")
        return []
