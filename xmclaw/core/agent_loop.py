"""Core agent loop: think -> act -> observe -> repeat."""
from typing import AsyncIterator
from xmclaw.llm.router import LLMRouter
from xmclaw.tools.registry import ToolRegistry
from xmclaw.memory.manager import MemoryManager
from xmclaw.core.prompt_builder import PromptBuilder
from xmclaw.utils.log import logger


class AgentLoop:
    def __init__(self, agent_id: str, llm_router: LLMRouter, tools: ToolRegistry, memory: MemoryManager):
        self.agent_id = agent_id
        self.llm = llm_router
        self.tools = tools
        self.memory = memory
        self.builder = PromptBuilder()
        self.max_turns = 50

    async def run(self, user_input: str) -> AsyncIterator[str]:
        """Run the agent loop with streaming output."""
        logger.info("agent_loop_start", agent_id=self.agent_id, input=user_input[:200])

        # Load context
        context = await self.memory.load_context(self.agent_id, user_input)
        messages = self.builder.build(user_input, context)

        turn = 0
        while turn < self.max_turns:
            turn += 1

            # Think
            full_response = ""
            async for chunk in self.llm.stream(messages):
                full_response += chunk
                yield chunk

            # Parse tool calls from response
            tool_calls = self._extract_tool_calls(full_response)

            if not tool_calls:
                # No more actions, we're done
                await self.memory.save_turn(self.agent_id, user_input, full_response, tool_calls)
                break

            # Act + Observe
            observations = []
            for call in tool_calls:
                result = await self.tools.execute(call["name"], call.get("arguments", {}))
                observations.append({"tool": call["name"], "result": result})
                yield f"\n[Tool: {call['name']}] {result}\n"

            # Append to messages for next turn
            messages.append({"role": "assistant", "content": full_response})
            messages.append({"role": "user", "content": self._format_observations(observations)})

            await self.memory.save_turn(self.agent_id, user_input, full_response, tool_calls)

        logger.info("agent_loop_end", agent_id=self.agent_id, turns=turn)

    def _extract_tool_calls(self, text: str) -> list[dict]:
        """Extract tool calls from LLM response.
        
        Expected format:
        <function>function_name</function>
        <arguments>{"key": "value"}</arguments>
        """
        import re
        calls = []
        pattern = r"<function>(.*?)</function>.*?\n<arguments>(.*?)\n</arguments>"
        for match in re.finditer(pattern, text, re.DOTALL):
            name = match.group(1).strip()
            args_raw = match.group(2).strip()
            try:
                import json
                args = json.loads(args_raw)
            except json.JSONDecodeError:
                args = {"raw": args_raw}
            calls.append({"name": name, "arguments": args})
        return calls

    def _format_observations(self, observations: list[dict]) -> str:
        lines = ["Observation results:"]
        for obs in observations:
            lines.append(f"- {obs['tool']}: {obs['result']}")
        return "\n".join(lines)
