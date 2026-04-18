"""Tool registry and execution dispatcher with plugin support.

Thread Safety:
- Uses asyncio.Lock to protect concurrent access to _tools dict
- Prevents race conditions during hot reload and concurrent execution
"""
import asyncio
import importlib.util
import threading
from pathlib import Path
from typing import Any

from xmclaw.core.error_recovery import get_error_recovery, CircuitBreaker
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

    Thread Safety:
    - Uses asyncio.Lock to protect concurrent access
    - Safe for concurrent tool execution and hot reload
    """
    _shared: "ToolRegistry | None" = None

    def __init__(self, llm_router: LLMRouter | None = None):
        self._tools: dict[str, Tool] = {}
        self._plugin_errors: dict[str, str] = {}
        self._lock = asyncio.Lock()  # For async operations (load_all, hot_reload, execute)
        self._sync_lock = threading.Lock()  # For sync operations (register, unregister)
        self._tool_breakers: dict[str, CircuitBreaker] = {}
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
        """Load built-in tools, generated skills, and plugin tools.

        Thread-safe: uses lock to prevent concurrent modification during load.
        """
        async with self._lock:
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

            # Lazy-load SkillTool to avoid circular import
            # (skill_tool.py imports ToolRegistry; we avoid the module-level cycle here)
            try:
                from xmclaw.tools.skill_tool import SkillTool as _ST
                self._tools["skill"] = _ST()
            except Exception as e:
                logger.warning("skill_tool_load_failed", error=str(e))

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

        Thread-safe: uses synchronous lock to prevent concurrent modification.
        Usage:
            registry = ToolRegistry.get_shared()
            registry.register(MyCustomTool())
        """
        with self._sync_lock:
            self._tools[tool.name] = tool
            # Initialize circuit breaker for new tool
            if tool.name not in self._tool_breakers:
                self._tool_breakers[tool.name] = CircuitBreaker(failure_threshold=5, recovery_timeout=30.0)
        logger.info("tool_registered", name=tool.name)

    def unregister(self, name: str) -> bool:
        """Remove a tool by name. Returns True if removed.

        Thread-safe: uses synchronous lock to prevent concurrent modification.
        """
        with self._sync_lock:
            if name in self._tools:
                del self._tools[name]
                # Remove circuit breaker
                if name in self._tool_breakers:
                    del self._tool_breakers[name]
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
        """Execute a tool with error recovery and circuit breaker protection.

        Thread-safe: uses lock for reading tool from registry.
        Each tool has its own circuit breaker to prevent cascading failures.
        Transient failures (timeouts, network) are retried automatically.
        """
        # Get tool reference under lock (lock is released after lookup)
        async with self._lock:
            tool = self._tools.get(name)
            if not tool:
                available = ", ".join(sorted(self._tools.keys()))
                return f"[Error: Tool '{name}' not found]\nAvailable: {available}"

        # Get or create circuit breaker for this tool
        if name not in self._tool_breakers:
            self._tool_breakers[name] = CircuitBreaker(failure_threshold=5, recovery_timeout=30.0)

        breaker = self._tool_breakers[name]

        # Check circuit breaker
        if not await breaker.can_execute():
            logger.warning("tool_circuit_breaker_open", tool=name)
            return f"[Error: Tool '{name}' temporarily unavailable due to repeated failures. Please try again later.]"

        # Execute with retry logic (outside lock for better concurrency)
        max_retries = 2
        last_error: Exception | None = None

        for attempt in range(max_retries + 1):
            try:
                result = await tool.execute(**arguments)
                await breaker.record_success()
                return result
            except Exception as e:
                last_error = e
                logger.warning("tool_execution_error",
                             tool=name, attempt=attempt + 1, error=str(e)[:80])

                # Record failure for circuit breaker
                await breaker.record_failure()

                # Only retry on first attempts
                if attempt < max_retries:
                    # Brief backoff
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue

        # All retries exhausted
        logger.error("tool_execution_failed_all_retries", tool=name, error=str(last_error))
        return f"[Error executing {name}: {last_error}]"

    async def hot_reload(self, name: str) -> bool:
        """Hot-reload a single skill by name (from shared/skills/ or plugins/).

        Thread-safe: uses lock to prevent concurrent modification.
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

        # Update registry under lock
        async with self._lock:
            self._tools[name] = target
            # Reset circuit breaker for reloaded tool
            if name in self._tool_breakers:
                self._tool_breakers[name] = CircuitBreaker(failure_threshold=5, recovery_timeout=30.0)

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
