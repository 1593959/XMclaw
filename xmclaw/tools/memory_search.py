"""Memory search tool using ChromaDB."""
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
        memory = MemoryManager()
        await memory.initialize()
        try:
            results = await memory.search(query, top_k=top_k)
            if not results:
                return "No relevant memories found."
            lines = []
            for i, r in enumerate(results, 1):
                lines.append(f"{i}. [{r.get('source', 'unknown')}] {r.get('content', '')[:300]}")
            return "\n".join(lines)
        finally:
            await memory.close()
