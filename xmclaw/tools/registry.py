"""Tool registry and execution dispatcher with plugin support."""
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
try:
    from tests.test_tool import TestTool
    _has_test_tool = True
except ImportError:
    _has_test_tool = False
from xmclaw.tools.mcp_tool import MCPTool
from xmclaw.tools.github_tool import GitHubTool
from xmclaw.tools.vision import VisionTool
from xmclaw.tools.asr import ASRTool
from xmclaw.tools.tts import TTSTool
from xmclaw.tools.code_exec import CodeExecTool
from xmclaw.llm.router import LLMRouter
from xmclaw.utils.log import logger
from xmclaw.utils.paths import BASE_DIR

# Plugin discovery paths (can be configured)
_PLUGINS_DIR = BASE_DIR / "plugins" / "tools"
_BUILTIN_TOOLS: list[type[Tool]] = [
    FileReadTool,
    FileWriteTool,
    FileEditTool,
    BashTool,
    WebSearchTool,
    WebFetchTool,
    BrowserTool,
    TodoTool,
    TaskTool,
    GlobTool,
    GrepTool,
    AskUserTool,
    AgentTool,
    SkillTool,
    MemorySearchTool,
    GitTool,
    ComputerUseTool,
    MCPTool,
    GitHubTool,
    VisionTool,
    ASRTool,
    TTSTool,
    CodeExecTool,
]


class ToolRegistry:
    """Tool registry and execution dispatcher with hot-pluggable support.

    Supports:
    - Built-in tools (loaded once at startup)
    - Generated skills (shared/skills/*.py)
    - Plugin tools (plugins/tools/*.py — drop-in discovery)
    - Shared singleton (for evolution engine hot-reload)
    """
    _shared: "ToolRegistry | None" = None

    def __init__(self, llm_router: LLMRouter | None = None):
        self._tools: dict[str, Tool] = {}
        self._plugin_errors: dict[str, str] = {}
        self.llm = llm_router

    @classmethod
    def set_shared(cls, registry: "ToolRegistry") -> None:
        """Register the orchestrator's ToolRegistry as the shared instance."""
        cls._shared = registry

    @classmethod
    def get_shared(cls) -> "ToolRegistry | None":
        """Return the shared registry, or None if not yet set."""
        return cls._shared

    async def load_all(self) -> None:
        """Load built-in tools, generated skills, and plugin tools."""
        self._tools.clear()
        self._plugin_errors.clear()

        # Built-in tools
        for cls in _BUILTIN_TOOLS:
            try:
                tool = cls() if not _needs_llm_router(cls) else cls(llm_router=self.llm)
                self._tools[tool.name] = tool
            except Exception as e:
                logger.warning("builtin_tool_load_failed", tool=cls.__name__, error=str(e))

        if _has_test_tool:
            try:
                self._tools["test"] = TestTool(llm_router=self.llm)
            except Exception:
                pass

        # Generated skills
        await self._load_generated_skills()

        # Plugin tools (drop-in directory)
        await self._load_plugin_tools()

        logger.info("tools_loaded",
                    builtin=sum(1 for t in _BUILTIN_TOOLS),
                    registered=len(self._tools),
                    plugin_errors=len(self._plugin_errors))

    async def _load_plugin_tools(self) -> None:
        """Discover and load tools from the plugins/tools/ drop-in directory.

        Any Python file in plugins/tools/ that defines a Tool subclass is loaded.
        No registration needed — just drop a file and it appears.
        """
        if not _PLUGINS_DIR.exists():
            _PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
            # Write a README to guide users
            (_PLUGINS_DIR / "README.md").write_text(
                "# Drop custom tool files here\n"
                "# Each .py file defining a Tool subclass will be auto-loaded.\n"
                "# Example:\n"
                "#   from xmclaw.tools.base import BaseTool\n"
                "#   class MyTool(BaseTool): ...\n",
                encoding="utf-8"
            )
            return

        for py_file in sorted(_PLUGINS_DIR.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            try:
                tool = self._load_tool_module(py_file)
                if tool and tool.name:
                    self._tools[tool.name] = tool
                    logger.info("plugin_tool_loaded", name=tool.name, path=str(py_file))
            except Exception as e:
                self._plugin_errors[str(py_file)] = str(e)
                logger.warning("plugin_tool_load_failed", path=str(py_file), error=str(e))

    async def _load_generated_skills(self) -> None:
        skills_dir = BASE_DIR / "shared" / "skills"
        if not skills_dir.exists():
            return
        for py_file in skills_dir.glob("skill_*.py"):
            try:
                tool = self._load_tool_module(py_file)
                if tool and tool.name:
                    self._tools[tool.name] = tool
                    logger.info("generated_skill_loaded", name=tool.name, path=str(py_file))
            except Exception as e:
                logger.warning("generated_skill_load_failed", path=str(py_file), error=str(e))

    def _load_tool_module(self, path: Path) -> Tool | None:
        """Load a Tool subclass from a Python file.

        Supports both old-style Tool and new-style BaseTool.
        """
        module_name = path.stem
        spec = importlib.util.spec_from_file_location(module_name, str(path))
        if not spec or not spec.loader:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        # Find Tool subclass (check both old and new base)
        for attr_name in dir(module):
            attr = getattr(module, attr_name, None)
            if not isinstance(attr, type):
                continue
            if attr is Tool or attr.__name__ == "BaseTool":
                continue
            try:
                if issubclass(attr, (Tool, getattr(__import__("xmclaw.tools.base", fromlist=["BaseTool"]), "BaseTool", Tool))):
                    # Instantiate with llm_router if the tool accepts it
                    try:
                        return attr(llm_router=self.llm)
                    except TypeError:
                        return attr()
            except Exception:
                pass
        return None

    def register(self, tool: Tool) -> None:
        """Manually register a tool instance.

        Usage:
            registry = ToolRegistry.get_shared()
            registry.register(MyCustomTool())
        """
        self._tools[tool.name] = tool
        logger.info("tool_registered", name=tool.name)

    def unregister(self, name: str) -> bool:
        """Remove a tool by name. Returns True if removed."""
        if name in self._tools:
            del self._tools[name]
            logger.info("tool_unregistered", name=name)
            return True
        return False

    def list_tools(self) -> list[dict[str, str]]:
        """Return list of all registered tools with metadata."""
        return [
            {"name": t.name, "description": t.description}
            for t in self._tools.values()
        ]

    def get_descriptions(self) -> str:
        lines = []
        for tool in self._tools.values():
            schema = tool.get_schema()
            lines.append(f"- {schema['name']}: {schema['description']}")
        return "\n".join(lines)

    async def execute(self, name: str, arguments: dict[str, Any]) -> str:
        tool = self._tools.get(name)
        if not tool:
            available = ", ".join(sorted(self._tools.keys()))
            return f"[Error: Tool '{name}' not found]\nAvailable: {available}"
        try:
            return await tool.execute(**arguments)
        except Exception as e:
            logger.error("tool_execution_error", tool=name, error=str(e))
            return f"[Error executing {name}: {e}]"

    async def hot_reload(self, name: str) -> bool:
        """Hot-reload a single skill by name (from shared/skills/ or plugins/).

        Returns True if reload succeeded, False otherwise.
        """
        # Search generated skills
        skills_dir = BASE_DIR / "shared" / "skills"
        target = None
        if skills_dir.exists():
            for py_file in skills_dir.glob("skill_*.py"):
                try:
                    tool = self._load_tool_module(py_file)
                    if tool and tool.name == name:
                        target = tool
                        break
                except Exception:
                    pass

        # Search plugins
        if target is None and _PLUGINS_DIR.exists():
            for py_file in _PLUGINS_DIR.glob("*.py"):
                if py_file.name.startswith("_"):
                    continue
                try:
                    tool = self._load_tool_module(py_file)
                    if tool and tool.name == name:
                        target = tool
                        break
                except Exception:
                    pass

        # Fall back to built-in
        if target is None:
            builtins = {cls().name: cls for cls in _BUILTIN_TOOLS}
            if name in builtins:
                try:
                    target = builtins[name](llm_router=self.llm)
                except TypeError:
                    target = builtins[name]()
            else:
                return False

        self._tools[name] = target
        logger.info("tool_hot_reloaded", name=name)
        return True

    async def reload_all(self) -> int:
        """Hot-reload all tools. Returns count of reloaded tools."""
        await self.load_all()
        return len(self._tools)


def _needs_llm_router(cls: type[Tool]) -> bool:
    """Check if a Tool subclass accepts llm_router in its __init__."""
    import inspect
    try:
        sig = inspect.signature(cls.__init__)
        return "llm_router" in sig.parameters
    except Exception:
        return False
