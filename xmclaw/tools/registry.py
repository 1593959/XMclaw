"""Tool registry and execution dispatcher with plugin support.

Thread Safety:
- Uses asyncio.Lock to protect concurrent access to _tools dict
- Prevents race conditions during hot reload and concurrent execution
"""
import asyncio
import importlib.util
import inspect
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


async def _record_skill_telemetry(
    tool_name: str,
    agent_id: str | None,
    outcome: str,
    result: str | None = None,
) -> None:
    """Increment lineage metrics for a generated skill after it runs.

    Telemetry is a side channel — it must NEVER raise back into the tool
    execution path. Any storage error is swallowed so a broken journal can't
    break the agent loop.

    Outcome semantics:
      * matched_count  — always incremented for skill invocations (the skill
                         was selected and executed at all).
      * helpful_count  — tool returned without raising. Note: the Tool base
                         may still return a string that *starts with*
                         `[Error ...]`; we treat that as harmful because the
                         agent sees failure downstream even if Python didn't.
      * harmful_count  — tool raised, OR returned an `[Error` sentinel.

    After a harmful outcome we poll `_maybe_rollback_skill`, which consults
    the configured thresholds (PR-E3-2). If it decides to roll back the
    skill, it deletes the active artifact, marks lineage rolled_back, and
    emits EVOLUTION_ROLLBACK so the Live panel updates.
    """
    if not tool_name.startswith("skill_"):
        return
    if not agent_id:
        return
    try:
        from xmclaw.evolution.journal import get_journal
        journal = get_journal(agent_id)
        await journal.increment_metric(tool_name, "matched_count", 1)
        effective = outcome
        if outcome == "helpful" and isinstance(result, str) and result.lstrip().startswith("[Error"):
            effective = "harmful"
        if effective == "helpful":
            await journal.increment_metric(tool_name, "helpful_count", 1)
        elif effective == "harmful":
            await journal.increment_metric(tool_name, "harmful_count", 1)
            await _maybe_rollback_skill(tool_name, agent_id, journal)
    except Exception as e:
        logger.debug("skill_telemetry_record_failed", tool=tool_name, error=str(e))


async def _maybe_rollback_skill(
    tool_name: str,
    agent_id: str,
    journal,
) -> None:
    """Retire a promoted skill if its harm metrics cross the configured
    threshold. The caller has already incremented harmful_count for this
    invocation, so we read the post-increment lineage row.

    Rollback conditions (either is sufficient; both require min_matches):
      1. absolute: harmful_count ≥ harmful_count_threshold
         AND     harmful_count > helpful_count
      2. ratio:   harmful_count / matched_count ≥ harmful_ratio_threshold
         AND     matched_count ≥ min_matches

    The lineage row flips to `rolled_back` (not `retired`) so audit can
    distinguish "rejected by validator before promotion" from
    "promoted, then failed in production". The active-dir file is deleted
    so the registry no longer exposes it on next reload.
    """
    try:
        from xmclaw.daemon.config import DaemonConfig
        from xmclaw.evolution.journal import STATUS_ROLLED_BACK
        from xmclaw.core.event_bus import Event, EventType, get_event_bus

        try:
            cfg = DaemonConfig.load()
            evo = cfg.evolution or {}
        except Exception:
            evo = {}
        if not evo.get("auto_rollback", True):
            return
        abs_threshold = int(evo.get("rollback_harmful_count_threshold", 3))
        ratio_threshold = float(evo.get("rollback_harmful_ratio_threshold", 0.5))
        min_matches = int(evo.get("rollback_min_matches", 4))

        row = await journal.get_artifact(tool_name)
        if not row:
            return
        # Only roll back skills that were actually promoted. Shadow / already-
        # retired / already-rolled-back rows are no-ops.
        if row.get("status") != "promoted":
            return

        harmful = int(row.get("harmful_count", 0) or 0)
        helpful = int(row.get("helpful_count", 0) or 0)
        matched = int(row.get("matched_count", 0) or 0)

        tripped_reason: str | None = None
        if harmful >= abs_threshold and harmful > helpful:
            tripped_reason = "harmful_count_threshold"
        elif matched >= min_matches and matched > 0 and (harmful / matched) >= ratio_threshold:
            tripped_reason = "harmful_ratio_threshold"
        if not tripped_reason:
            return

        # Perform the rollback: delete active artifact + flip lineage status.
        active_py = BASE_DIR / "shared" / "skills" / f"{tool_name}.py"
        active_meta = BASE_DIR / "shared" / "skills" / f"{tool_name}.json"
        for p in (active_py, active_meta):
            try:
                if p.exists():
                    p.unlink()
            except Exception as e:
                logger.warning("rollback_unlink_failed", path=str(p), error=str(e))
        await journal.update_artifact_status(tool_name, STATUS_ROLLED_BACK)

        # E7: if the promote was recorded as a git commit, produce a matching
        # revert commit so ``git log`` carries the full promote→rollback trail.
        # No-op when git tracking is disabled or the original sha is missing.
        try:
            from xmclaw.evolution.git_ops import revert_commit
            promote_sha = row.get("promote_commit_sha") or ""
            if promote_sha:
                revert_sha = revert_commit(promote_sha, reason=tripped_reason)
                if revert_sha:
                    await journal.set_commit_sha(
                        tool_name, "rollback_commit_sha", revert_sha,
                    )
        except Exception as e:
            logger.warning("rollback_commit_record_failed",
                           tool=tool_name, error=str(e))

        # Drop the tool from the shared registry so it stops being invokable
        # without waiting for the next full reload.
        shared = ToolRegistry.get_shared()
        if shared is not None:
            try:
                async with shared._lock:
                    shared._tools.pop(tool_name, None)
            except Exception as e:
                logger.warning("rollback_registry_evict_failed", tool=tool_name, error=str(e))

        logger.warning(
            "skill_auto_rolled_back",
            tool=tool_name,
            reason=tripped_reason,
            harmful=harmful,
            helpful=helpful,
            matched=matched,
        )
        try:
            await get_event_bus().publish(Event(
                event_type=EventType.EVOLUTION_ROLLBACK,
                source=agent_id,
                payload={
                    "artifact_id": tool_name,
                    "kind": "skill",
                    "reason": tripped_reason,
                    "metrics": {
                        "matched": matched,
                        "helpful": helpful,
                        "harmful": harmful,
                    },
                },
            ))
        except Exception as e:
            logger.debug("rollback_event_publish_failed", error=str(e))
    except Exception as e:
        logger.debug("rollback_check_failed", tool=tool_name, error=str(e))


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
        """Load only PROMOTED skills. Shadow/retired subdirs are NEVER loaded —
        broken artifacts under validation quarantine must stay invisible to
        the agent (fail-closed guard for bug M22)."""
        skills_dir = BASE_DIR / "shared" / "skills"
        if not skills_dir.exists():
            return
        for py_file in skills_dir.glob("skill_*.py"):
            # glob() is non-recursive so shadow/skill_*.py is already excluded,
            # but check is_file() explicitly to guard against any future
            # refactor that switches to rglob.
            if not py_file.is_file() or py_file.parent != skills_dir:
                continue
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

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        agent_id: str | None = None,
    ) -> str:
        """Execute a tool with error recovery and circuit breaker protection.

        Thread-safe: uses lock for reading tool from registry.
        Each tool has its own circuit breaker to prevent cascading failures.
        Transient failures (timeouts, network) are retried automatically.

        When `agent_id` is provided AND the tool is a generated skill (name
        starts with `skill_`), lineage telemetry is recorded so the evolution
        meta-evaluator can decide whether the skill is paying off (helpful),
        mute (never matched), or actively harming turns (harmful). See
        PR-E3-1 / Phase E3 — this is the canary signal that drives auto
        rollback.
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
            msg = f"[Error: Tool '{name}' temporarily unavailable due to repeated failures. Please try again later.]"
            # A tripped breaker is itself a harmful signal — the skill has
            # failed enough times to warrant protection, which is exactly
            # what auto-rollback should react to. Record telemetry here or
            # the rollback threshold can never trip once the breaker opens.
            await _record_skill_telemetry(name, agent_id, outcome="harmful", result=msg)
            return msg

        # Execute with retry logic (outside lock for better concurrency)
        max_retries = 2
        last_error: Exception | None = None

        # Inject agent_id into kwargs only if the tool's ``execute``
        # signature accepts it. Per-agent tools (task, todo, file_*)
        # need this so they don't hard-code "default"; other tools don't
        # have to care.
        call_args = arguments
        if agent_id is not None:
            try:
                sig = inspect.signature(tool.execute)
                if "agent_id" in sig.parameters and "agent_id" not in arguments:
                    call_args = {**arguments, "agent_id": agent_id}
            except (TypeError, ValueError):
                pass

        for attempt in range(max_retries + 1):
            try:
                result = await tool.execute(**call_args)
                await breaker.record_success()
                await _record_skill_telemetry(name, agent_id, outcome="helpful", result=result)
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
        await _record_skill_telemetry(name, agent_id, outcome="harmful", result=str(last_error))
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
