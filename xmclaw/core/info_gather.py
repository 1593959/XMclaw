"""Information Gatherer: actively searches memories, insights, and web.

This is Step 2 of the Agent Cognition Pipeline.
Runs BEFORE LLM generates response — proactively collects relevant context
instead of passively relying on the LLM to request it.
"""
import asyncio
import json
from typing import TypedDict

from xmclaw.llm.router import LLMRouter
from xmclaw.memory.manager import MemoryManager
from xmclaw.tools.registry import ToolRegistry
from xmclaw.core.task_classifier import TaskType
from xmclaw.utils.log import logger


class GatheredInfo(TypedDict):
    memories: list[dict]       # Vector search results
    insights: list[dict]        # Past reflection insights
    web_results: list[dict]     # Web search results (if applicable)
    skill_context: list[dict]   # Matched skills for this task
    reasoning: str              # What was gathered and why


class InfoGatherer:
    """Actively gathers information from all sources based on task type."""

    def __init__(self, llm_router: LLMRouter, memory: MemoryManager, agent_id: str = "default"):
        self.llm = llm_router
        self.memory = memory
        self._agent_id = agent_id

    async def gather(self, user_input: str, task_type: TaskType,
                     capabilities_needed: list[str]) -> GatheredInfo:
        """
        Run parallel information gathering tailored to task type.
        Returns all gathered info merged into a single dict.
        """
        results: dict = {
            "memories": [],
            "insights": [],
            "web_results": [],
            "skill_context": [],
            "reasoning": "",
        }

        # Always gather: memories + insights (parallel)
        gather_tasks = [
            self._search_memories(user_input),
            self._search_insights(user_input, agent_id=getattr(self, '_agent_id', 'default')),
        ]

        # Conditionally gather based on task type / capabilities
        if task_type in (TaskType.SEARCH, TaskType.LEARNING, TaskType.PLAN):
            gather_tasks.append(self._search_web(user_input))

        if "code" in capabilities_needed or task_type == TaskType.CODE:
            gather_tasks.append(self._search_code_examples(user_input))

        # Run all in parallel, collect results
        partial = await asyncio.gather(*gather_tasks, return_exceptions=True)

        idx = 0
        for result in partial:
            if isinstance(result, Exception):
                logger.warning("gather_task_failed", index=idx, error=str(result))
            elif isinstance(result, dict):
                if "memories" in result:
                    results["memories"] = result["memories"]
                elif "insights" in result:
                    results["insights"] = result["insights"]
                elif "web" in result:
                    results["web_results"] = result["web"]
                elif "code_examples" in result:
                    results["web_results"] = result["code_examples"]
            idx += 1

        # Build reasoning summary
        parts = []
        if results["memories"]:
            parts.append(f"记忆: {len(results['memories'])}条")
        if results["insights"]:
            parts.append(f"经验: {len(results['insights'])}条")
        if results["web_results"]:
            parts.append(f"网页: {len(results['web_results'])}条")
        results["reasoning"] = "; ".join(parts) if parts else "无额外信息"

        logger.info("info_gathered",
                     memories=len(results["memories"]),
                     insights=len(results["insights"]),
                     web=len(results["web_results"]))

        return GatheredInfo(**results)

    async def _search_memories(self, query: str) -> dict:
        """Vector semantic search over long-term memory."""
        try:
            memories = await self.memory.search(query, top_k=5)
            return {"memories": memories}
        except Exception as e:
            logger.warning("memory_search_failed", error=str(e))
            return {"memories": []}

    async def _search_insights(self, query: str, agent_id: str = "default") -> dict:
        """Surface recent reflection insights for the prompt.

        Previously filtered via whitespace-split keyword substring match.
        That filter silently dropped everything for Chinese queries (no word
        splitting) and for short queries — so the prompt's {insights}
        slot was almost always "None" even when the agent had dozens of
        stored lessons. The LLM is a better relevance filter than
        substring-contains; just hand it the top-N most recent insights
        and let it decide what matters.
        """
        try:
            insights = self.memory.get_insights(agent_id=agent_id, limit=5)
            return {"insights": insights}
        except Exception as e:
            logger.warning("insight_search_failed", error=str(e))
            return {"insights": []}

    async def _search_web(self, query: str) -> dict:
        """Actively search the web if the tool is available."""
        registry = ToolRegistry.get_shared()
        if registry is None:
            return {"web": []}

        # Check if web_search tool exists
        if "web_search" not in registry._tools:
            return {"web": []}

        try:
            result = await registry.execute("web_search", {
                "query": query,
                "top_k": 5,
            })
            # Parse result if it's JSON-like
            try:
                parsed = json.loads(str(result))
                if isinstance(parsed, list):
                    return {"web": parsed[:5]}
                if isinstance(parsed, dict) and "results" in parsed:
                    return {"web": parsed["results"][:5]}
            except (json.JSONDecodeError, TypeError):
                pass
            return {"web": [{"content": str(result)[:500]}]}
        except Exception as e:
            logger.warning("web_search_failed", error=str(e))
            return {"web": []}

    async def _search_code_examples(self, query: str) -> dict:
        """Search for code examples if web_search is available."""
        registry = ToolRegistry.get_shared()
        if registry is None or "web_search" not in registry._tools:
            return {"code_examples": []}

        try:
            result = await registry.execute("web_search", {
                "query": f"{query} site:github.com OR site:stackoverflow.com code example",
                "top_k": 3,
            })
            try:
                parsed = json.loads(str(result))
                return {"code_examples": parsed if isinstance(parsed, list) else []}
            except (json.JSONDecodeError, TypeError):
                return {"code_examples": [{"content": str(result)[:300]}]}
        except Exception as e:
            logger.warning("code_example_search_failed", error=str(e))
            return {"code_examples": []}

    def format_for_prompt(self, info: GatheredInfo) -> str:
        """Format gathered information into a readable string for the prompt."""
        sections = []

        if info["memories"]:
            lines = ["【相关记忆】"]
            for m in info["memories"][:5]:
                content = m.get("content", "")[:200]
                source = m.get("source", "memory")
                lines.append(f"  [{source}] {content}")
            sections.append("\n".join(lines))

        if info["insights"]:
            lines = ["【相关经验】"]
            for ins in info["insights"][:5]:
                title = ins.get("title", "经验")
                try:
                    desc = json.loads(ins.get("description", "{}"))
                    summary = desc.get("summary", "")
                    lessons = desc.get("lessons", [])[:2]
                    lines.append(f"  - {title}: {summary}")
                    for l in lessons:
                        lines.append(f"    → {l}")
                except (json.JSONDecodeError, TypeError):
                    lines.append(f"  - {title}")
            sections.append("\n".join(lines))

        if info["web_results"]:
            lines = ["【网络搜索结果】"]
            for r in info["web_results"][:5]:
                if isinstance(r, dict):
                    title = r.get("title", r.get("url", "结果"))
                    snippet = r.get("snippet", r.get("content", ""))[:150]
                    lines.append(f"  - {title}: {snippet}")
                else:
                    lines.append(f"  - {str(r)[:150]}")
            sections.append("\n".join(lines))

        return "\n\n".join(sections) if sections else ""
