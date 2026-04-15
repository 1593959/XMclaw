"""Tool registry and execution dispatcher."""
import importlib.util
from pathlib import Path
from typing import Any

from xmclaw.tools.base import Tool
from xmclaw.tools.file_read import FileReadTool
from xmclaw.tools.file_write import FileWriteTool
from xmclaw.tools.file_edit import FileEditTool
from xmclaw.tools.bash import BashTool
from xmclaw.tools.web_search import WebSearchTool
from xmclaw.tools.web_fetch import WebFetchTool
from xmclaw.tools.browser import BrowserTool
from xmclaw.tools.todo import TodoTool
from xmclaw.tools.task_tool import TaskTool
from xmclaw.tools.glob_tool import GlobTool
from xmclaw.tools.grep_tool import GrepTool
from xmclaw.tools.ask_user import AskUserTool
from xmclaw.tools.agent_tool import AgentTool
from xmclaw.tools.skill_tool import SkillTool
from xmclaw.tools.memory_search import MemorySearchTool
from xmclaw.tools.git import GitTool
from xmclaw.tools.computer_use import ComputerUseTool
from xmclaw.tools.test_tool import TestTool
from xmclaw.tools.mcp_tool import MCPTool
from xmclaw.llm.router import LLMRouter
from xmclaw.utils.log import logger
from xmclaw.utils.paths import BASE_DIR


class ToolRegistry:
    def __init__(self, llm_router: LLMRouter | None = None):
        self._tools: dict[str, Tool] = {}
        self.llm = llm_router

    async def load_all(self) -> None:
        """Load all built-in tools and auto-generated skills."""
        tools = [
            FileReadTool(),
            FileWriteTool(),
            FileEditTool(),
            BashTool(),
            WebSearchTool(),
            WebFetchTool(),
            BrowserTool(),
            TodoTool(),
            TaskTool(),
            GlobTool(),
            GrepTool(),
            AskUserTool(),
            AgentTool(),
            SkillTool(),
            MemorySearchTool(),
            GitTool(),
            ComputerUseTool(),
            TestTool(llm_router=self.llm),
            MCPTool(),
        ]
        for tool in tools:
            self._tools[tool.name] = tool

        # Load auto-generated skills from shared/skills/
        await self._load_generated_skills()
        logger.info("tools_loaded", count=len(self._tools))

    async def _load_generated_skills(self) -> None:
        skills_dir = BASE_DIR / "shared" / "skills"
        if not skills_dir.exists():
            return
        for py_file in skills_dir.glob("skill_*.py"):
            try:
                tool = self._load_skill_module(py_file)
                if tool and tool.name:
                    self._tools[tool.name] = tool
                    logger.info("generated_skill_loaded", name=tool.name, path=str(py_file))
            except Exception as e:
                logger.warning("generated_skill_load_failed", path=str(py_file), error=str(e))

    def _load_skill_module(self, path: Path) -> Tool | None:
        module_name = path.stem
        spec = importlib.util.spec_from_file_location(module_name, str(path))
        if not spec or not spec.loader:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        # Find the Tool subclass
        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if isinstance(attr, type) and issubclass(attr, Tool) and attr is not Tool:
                return attr()
        return None

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
