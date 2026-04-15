"""Core agent loop: think -> act -> observe -> repeat."""
import json
import re
from typing import AsyncIterator

from xmclaw.llm.router import LLMRouter
from xmclaw.tools.registry import ToolRegistry
from xmclaw.memory.manager import MemoryManager
from xmclaw.core.prompt_builder import PromptBuilder
from xmclaw.core.cost_tracker import CostTracker
from xmclaw.utils.log import logger


class AgentLoop:
    def __init__(self, agent_id: str, llm_router: LLMRouter, tools: ToolRegistry, memory: MemoryManager):
        self.agent_id = agent_id
        self.llm = llm_router
        self.tools = tools
        self.memory = memory
        self.builder = PromptBuilder()
        self.cost_tracker = CostTracker()
        self.max_turns = 50

    async def run(self, user_input: str) -> AsyncIterator[str]:
        """Run the agent loop, yielding JSON-encoded events."""
        logger.info("agent_loop_start", agent_id=self.agent_id, input=user_input[:200])

        yield json.dumps({"type": "state", "state": "THINKING", "thought": "Analyzing request..."})

        context = await self.memory.load_context(self.agent_id, user_input)
        messages = self.builder.build(user_input, context)

        turn = 0
        while turn < self.max_turns:
            turn += 1

            # Stream thinking
            full_response = ""
            async for chunk in self.llm.stream(messages):
                full_response += chunk
                yield json.dumps({"type": "chunk", "content": chunk})

            # Parse tool calls
            tool_calls = self._extract_tool_calls(full_response)

            if not tool_calls:
                await self.memory.save_turn(self.agent_id, user_input, full_response, tool_calls)
                break

            # Execute tools
            observations = []
            for call in tool_calls:
                yield json.dumps({"type": "state", "state": "TOOL_CALL", "thought": f"Using {call['name']}..."})
                yield json.dumps({"type": "tool_call", "tool": call["name"], "args": call.get("arguments", {})})

                result = await self.tools.execute(call["name"], call.get("arguments", {}))
                observations.append({"tool": call["name"], "result": result})

                yield json.dumps({"type": "tool_result", "tool": call["name"], "result": result})

                # Detect self-modification (file operations on own codebase)
                await self._detect_self_mod(call, result)

            # Append to messages for next turn
            messages.append({"role": "assistant", "content": full_response})
            messages.append({"role": "user", "content": self._format_observations(observations)})

            await self.memory.save_turn(self.agent_id, user_input, full_response, tool_calls)

        # Cost summary
        cost_info = self.cost_tracker.estimate(messages)
        yield json.dumps({"type": "cost", "tokens": cost_info.get("tokens", 0), "cost": cost_info.get("cost", 0)})

        yield json.dumps({"type": "done"})
        logger.info("agent_loop_end", agent_id=self.agent_id, turns=turn)

    def _extract_tool_calls(self, text: str) -> list[dict]:
        """Extract tool calls from LLM response."""
        calls = []
        pattern = r"<function>(.*?)</function>.*?\n<arguments>(.*?)\n</arguments>"
        for match in re.finditer(pattern, text, re.DOTALL):
            name = match.group(1).strip()
            args_raw = match.group(2).strip()
            try:
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

    async def _detect_self_mod(self, call: dict, result: any) -> None:
        """Detect if the agent is modifying its own source code."""
        import json
        name = call.get("name", "")
        args = call.get("arguments", {})

        if name in ("file_write", "file_edit", "file_read"):
            path = args.get("file_path", args.get("path", ""))
            if "xmclaw" in path.lower() or "XMclaw" in path:
                action = "read" if name == "file_read" else "modify"
                # Emit self-mod event
                # This is yielded from run() via a side-channel pattern: we can't yield here directly,
                # but we can log it and the orchestrator/daemon can pick it up via an event bus in the future.
                # For now, we inject a special log marker that the server could intercept.
                logger.info("self_mod_detected", agent_id=self.agent_id, file=path, action=action)
