"""Memory search tool using vector store."""
from xmclaw.tools.base import Tool
from xmclaw.memory.manager import MemoryManager


class MemorySearchTool(Tool):
    name = "memory_search"
    description = "Search long-term memory for relevant past information."
    parameters = {
        "query": {
            "type": "string",
            "description": "Search query.",
        },
        "top_k": {
            "type": "integer",
            "description": "Number of results. Default 5.",
        },
    }

    async def execute(self, query: str, top_k: int = 5) -> str:
        # Tool execution context should receive an initialized memory manager
        # from the orchestrator. Fallback to a new instance if not available.
        from xmclaw.core.orchestrator import AgentOrchestrator
        memory = getattr(AgentOrchestrator, "_tool_memory", None)
        if memory is None:
            memory = MemoryManager()
            await memory.initialize()
            close_after = True
        else:
            close_after = False
        try:
            results = await memory.search(query, top_k=top_k)
            if not results:
                return "No relevant memories found."
            lines = []
            for i, r in enumerate(results, 1):
                content = r.get("content", "")
                source = r.get("source", "unknown")
                dist = r.get("distance", "")
                lines.append(f"{i}. [{source}] (score={dist}) {content[:300]}")
            return "\n".join(lines)
        finally:
            if close_after:
                await memory.close()
