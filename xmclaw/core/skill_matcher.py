"""Skill Matcher: actively finds and executes matching skills for a task.

This is Step 4 of the Agent Cognition Pipeline.
Unlike the old approach (text injection via genes), this actively loads skills,
scores them against the task, and executes the best matches.
"""
import json
from typing import TypedDict

from xmclaw.tools.registry import ToolRegistry
from xmclaw.memory.manager import MemoryManager
from xmclaw.core.task_classifier import TaskType
from xmclaw.core.info_gather import GatheredInfo
from xmclaw.core.event_bus import Event, EventType, get_event_bus
from xmclaw.utils.log import logger


class MatchedSkill(TypedDict):
    skill_id: str
    name: str
    description: str
    score: float           # 0-1 relevance score
    auto_execute: bool     # execute automatically or require confirmation


class SkillMatchResult(TypedDict):
    matched: list[MatchedSkill]
    auto_executed: list[dict]   # {skill_id, name, result}
    failed: list[dict]          # {skill_id, name, error}


class SkillMatcher:
    """Actively matches and executes skills for the current task."""

    def __init__(self, memory: MemoryManager):
        self.memory = memory
        self._bus = get_event_bus()

    async def match_and_execute(
        self,
        user_input: str,
        task_type: TaskType,
        capabilities_needed: list[str],
        info: GatheredInfo,
    ) -> SkillMatchResult:
        """
        Find relevant skills and execute them.
        Returns which skills matched, which were auto-executed, and their results.
        """
        registry = ToolRegistry.get_shared()
        if registry is None:
            return SkillMatchResult(matched=[], auto_executed=[], failed=[])

        # Load skill registry from DB
        skill_defs = self._load_skill_definitions()
        if not skill_defs:
            return SkillMatchResult(matched=[], auto_executed=[], failed=[])

        # Score each skill against current task context
        scored = []
        for skill in skill_defs:
            score = self._score_skill(skill, user_input, task_type,
                                       capabilities_needed, info)
            if score >= 0.4:  # Minimum threshold
                matched = MatchedSkill(
                    skill_id=skill.get("id", ""),
                    name=skill.get("name", ""),
                    description=skill.get("description", ""),
                    score=score,
                    auto_execute=score >= 0.75,
                )
                scored.append(matched)

        # Sort by score descending
        scored.sort(key=lambda x: x["score"], reverse=True)
        top_matches = scored[:5]  # Top 5 at most

        # Auto-execute high-confidence matches via ToolRegistry
        executed = []
        failed = []
        for skill in top_matches:
            if not skill["auto_execute"]:
                continue
            try:
                result = await self._execute_skill(skill, context={
                    "user_input": user_input,
                    "task_type": task_type.value,
                })
                executed.append({
                    "skill_id": skill["skill_id"],
                    "name": skill["name"],
                    "result": result,
                })
                logger.info("skill_auto_executed",
                             skill_id=skill["skill_id"], name=skill["name"],
                             score=skill["score"])
                await self._bus.publish(Event(
                    event_type=EventType.SKILL_EXECUTED,
                    source="skill_matcher",
                    payload={
                        "skill_id": skill["skill_id"],
                        "name": skill["name"],
                        "action": "auto_executed",
                        "score": skill["score"],
                        "result_preview": str(result)[:200],
                    },
                ))
            except Exception as e:
                logger.warning("skill_auto_execute_failed",
                                skill_id=skill["skill_id"], error=str(e))
                failed.append({
                    "skill_id": skill["skill_id"],
                    "name": skill["name"],
                    "error": str(e),
                })

        return SkillMatchResult(
            matched=top_matches,
            auto_executed=executed,
            failed=failed,
        )

    def _load_skill_definitions(self) -> list[dict]:
        """Load all skill definitions from the shared ToolRegistry.

        Skills are registered by ToolRegistry._load_generated_skills() at startup.
        Each skill is a Tool instance with name, description, parameters, execute().
        """
        try:
            registry = ToolRegistry.get_shared()
            if registry is None:
                return []
            skills = []
            for name, tool in registry._tools.items():
                if not name.startswith("skill_"):
                    continue
                skills.append({
                    "id": name,
                    "name": getattr(tool, "name", name),
                    "description": getattr(tool, "description", ""),
                    "parameters": getattr(tool, "parameters", {}),
                })
            return skills
        except Exception as e:
            logger.warning("skill_definitions_load_failed", error=str(e))
            return []

    def _score_skill(
        self,
        skill: dict,
        user_input: str,
        task_type: TaskType,
        capabilities: list[str],
        info: GatheredInfo,
    ) -> float:
        """Score a skill's relevance to the current task (0-1)."""
        score = 0.0
        text = user_input.lower()
        name = skill.get("name", "").lower()
        desc = skill.get("description", "").lower()
        skill_text = f"{name} {desc}".lower()

        # Direct keyword match
        for kw in text.split():
            if len(kw) < 2:
                continue
            if kw in skill_text:
                score += 0.3

        # Task type alignment
        type_keywords = {
            "code": ["code", "代码", "debug", "写代码", "function", "写个"],
            "search": ["搜索", "查找", "search", "web"],
            "learning": ["学习", "研究", "learn", "research"],
            "file_op": ["文件", "读取", "写入", "read", "write", "edit"],
            "system": ["系统", "配置", "重启", "config", "restart"],
            "creative": ["创作", "写", "generate", "design"],
        }
        for cap in capabilities:
            keywords = type_keywords.get(cap, [])
            for kw in keywords:
                if kw in skill_text:
                    score += 0.2

        # Intent matching from insight/context
        for insight in info.get("insights", [])[:3]:
            ins_text = (insight.get("title", "") + insight.get("description", "")).lower()
            for kw in text.split():
                if len(kw) < 3:
                    continue
                if kw in ins_text:
                    score += 0.05

        # Cap at 1.0
        return min(score, 1.0)

    async def _execute_skill(self, skill: MatchedSkill, context: dict) -> str:
        """Execute a skill via the shared ToolRegistry.

        Constructs skill kwargs from the skill's defined parameters, then calls
        ToolRegistry.execute() with the correct parameter names.
        """
        registry = ToolRegistry.get_shared()
        if registry is None:
            return "[ToolRegistry not available]"

        # Build kwargs from the skill's defined parameters
        skill_params = skill.get("parameters", {})
        kwargs: dict = {}
        # Extract parameter names from the parameters dict
        param_spec = skill_params.get("properties", skill_params)
        if isinstance(param_spec, dict):
            for param_name in param_spec.keys():
                if param_name in context:
                    kwargs[param_name] = context[param_name]

        # Fallback: if no params matched, pass the whole context
        if not kwargs and context:
            kwargs = dict(context)

        try:
            result = await registry.execute(skill["skill_id"], **kwargs)
            return str(result)
        except Exception as e:
            logger.warning("skill_execute_via_registry_failed",
                            skill_id=skill["skill_id"], error=str(e))
            return f"[执行失败] {e}"

    def format_skill_results(self, result: SkillMatchResult) -> str:
        """Format auto-executed skill results for prompt injection."""
        if not result["auto_executed"]:
            return ""
        lines = ["【技能执行结果】"]
        for item in result["auto_executed"]:
            lines.append(f"  [{item['name']}] {item['result'][:200]}")
        if result["failed"]:
            lines.append("【技能执行失败】")
            for item in result["failed"]:
                lines.append(f"  [{item['name']}] {item['error']}")
        return "\n".join(lines)
