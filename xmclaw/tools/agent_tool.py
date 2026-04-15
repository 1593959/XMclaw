"""Sub-agent tool - spawn a child agent for delegated tasks."""
from xmclaw.tools.base import Tool
from xmclaw.utils.log import logger


class AgentTool(Tool):
    name = "agent"
    description = "Spawn a sub-agent to handle a delegated task with its own context."
    parameters = {
        "task": {
            "type": "string",
            "description": "The task description for the sub-agent.",
        },
        "context": {
            "type": "string",
            "description": "Optional additional context or constraints.",
        },
    }

    async def execute(self, task: str, context: str | None = None) -> str:
        logger.info("agent_tool_spawn", task=task)
        prompt = f"""You are a focused sub-agent. Complete the following task efficiently.
Do not ask the user questions unless absolutely necessary. Use tools as needed.

Task: {task}
"""
        if context:
            prompt += f"\nAdditional context: {context}\n"

        # Delayed imports to avoid circular dependency
        from xmclaw.core.agent_loop import AgentLoop
        from xmclaw.llm.router import LLMRouter
        from xmclaw.tools.registry import ToolRegistry
        from xmclaw.memory.manager import MemoryManager

        # Create a fresh sub-agent with its own isolated state
        llm = LLMRouter()
        tools = ToolRegistry()
        memory = MemoryManager()
        await tools.load_all()
        await memory.initialize()

        loop = AgentLoop(
            agent_id="subagent",
            llm_router=llm,
            tools=tools,
            memory=memory,
        )

        try:
            chunks = []
            async for chunk in loop.run(prompt):
                event = __import__("json").loads(chunk)
                if event.get("type") == "chunk":
                    chunks.append(event.get("content", ""))
                elif event.get("type") == "done":
                    break
            result = "".join(chunks)
            return f"[Sub-agent result]\n{result}"
        except Exception as e:
            return f"[Sub-agent error: {e}]"
        finally:
            await memory.close()
