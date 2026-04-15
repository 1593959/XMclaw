"""Tool registry and execution dispatcher."""
from typing import Any
from xmclaw.tools.base import Tool
from xmclaw.tools.file_read import FileReadTool
from xmclaw.tools.file_write import FileWriteTool
from xmclaw.tools.file_edit import FileEditTool
from xmclaw.tools.bash import BashTool
from xmclaw.tools.web_search import WebSearchTool
from xmclaw.tools.browser import BrowserTool
from xmclaw.tools.todo import TodoTool
from xmclaw.tools.memory_search import MemorySearchTool
from xmclaw.utils.log import logger


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}

    async def load_all(self) -> None:
        """Load all built-in tools."""
        tools = [
            FileReadTool(),
            FileWriteTool(),
            FileEditTool(),
            BashTool(),
            WebSearchTool(),
            BrowserTool(),
            TodoTool(),
            MemorySearchTool(),
        ]
        for tool in tools:
            self._tools[tool.name] = tool
        logger.info("tools_loaded", count=len(self._tools))

    def get_descriptions(self) -> str:
        lines = []
        for tool in self._tools.values():
            schema = tool.get_schema()
            lines.append(f"- {schema['name']}: {schema['description']}")
        return "\n".join(lines)

    async def execute(self, name: str, arguments: dict[str, Any]) -> str:
        tool = self._tools.get(name)
        if not tool:
            return f"[Error: Tool '{name}' not found]"
        try:
            return await tool.execute(**arguments)
        except Exception as e:
            logger.error("tool_execution_error", tool=name, error=str(e))
            return f"[Error executing {name}: {e}]"
