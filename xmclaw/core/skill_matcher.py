"""Skill Matcher: actively finds and executes matching skills for a task.

This is Step 4 of the Agent Cognition Pipeline.
Enhanced with:
- Multi-dimensional scoring (keyword, semantic, frequency, preference)
- User preference learning
- Frequency-based weighting
- Confidence-aware auto-execution
"""
import json
import re
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
    match_reasons: list[str]  # Why this skill matched


class SkillMatchResult(TypedDict):
    matched: list[MatchedSkill]
    auto_executed: list[dict]   # {skill_id, name, result}
    failed: list[dict]          # {skill_id, name, error}


# Expanded task type keywords for better matching
TASK_TYPE_KEYWORDS = {
    "code": [
        "code", "代码", "debug", "写代码", "function", "写个", "function",
        "class", "def ", "import ", "=>", "->", "fn ", "//", "python",
        "javascript", "java", "script", "coding", "编程", "程序", "函数",
        "bug", "fix", "error", "syntax", "compile", "refactor", "重构"
    ],
    "search": [
        "搜索", "查找", "search", "web", "google", "bing", "query",
        "lookup", "find", "find", "如何", "怎么", "what", "how to",
        "是什么", "哪里", "哪个", "latest", "recent", "top", "排行榜"
    ],
    "learning": [
        "学习", "研究", "learn", "research", "study", "teach",
        "教程", "课程", "课程", "guide", "understand", "explain",
        "解释", "理解", "教学", "培训"
    ],
    "file_op": [
        "文件", "读取", "写入", "read", "write", "edit", "文件",
        "folder", "directory", "path", "path", "save", "load",
        "open", "create", "delete", "remove", "copy", "move"
    ],
    "system": [
        "系统", "配置", "重启", "config", "restart", "setting",
        "setup", "install", "uninstall", "update", "upgrade",
        "服务", "启动", "停止", "status", "monitor"
    ],
    "creative": [
        "创作", "写", "generate", "design", "create", "创意",
        "写作", "文章", "story", "novel", "blog", "content",
        "营销", "文案", "copywriting", "marketing"
    ],
    "plan": [
        "规划", "计划", "analyze", "design", "strategy", "方案",
        "分析", "研究", "review", "evaluate", "assess"
    ],
    "qa": [
        "what", "why", "how", "when", "where", "who", "question",
        "answer", "解释", "说明", "定义", "概念", "meaning"
    ],
}


class SkillMatcher:
    """Actively matches and executes skills for the current task.

    Enhanced scoring dimensions:
    1. Keyword match (0.25 max)
    2. Task type alignment (0.25 max)
    3. Intent/semantic match (0.20 max)
    4. Frequency weighting (0.15 max)
    5. User preference (0.15 max)
    """

    # User preference cache: skill_name -> usage_count
    _skill_usage_cache: dict[str, int] = {}
    _skill_success_cache: dict[str, float] = {}  # skill_name -> success_rate

    def __init__(self, memory: MemoryManager):
        self.memory = memory
        self._bus = get_event_bus()
        self._load_skill_preferences()

    def _load_skill_preferences(self) -> None:
        """Load skill usage history from memory for preference weighting."""
        try:
            # Try to load from memory/skill_stats
            import sqlite3
            from xmclaw.utils.paths import BASE_DIR
            db_path = BASE_DIR / "shared" / "memory.db"
            if db_path.exists():
                conn = sqlite3.connect(db_path)
                cursor = conn.execute(
                    "SELECT name, usage_count, success_rate FROM skill_stats LIMIT 100"
                )
                for row in cursor.fetchall():
                    self._skill_usage_cache[row[0]] = row[1]
                    self._skill_success_cache[row[0]] = row[2] if row[2] else 0.8
                conn.close()
        except Exception as e:
            logger.debug("skill_preference_load_failed", error=str(e))

    def _record_skill_usage(self, skill_name: str, success: bool) -> None:
        """Record skill usage for future preference learning."""
        try:
            # Update cache
            self._skill_usage_cache[skill_name] = self._skill_usage_cache.get(skill_name, 0) + 1

            # Calculate rolling success rate
            prev_rate = self._skill_success_cache.get(skill_name, 0.8)
            new_rate = (prev_rate * 0.7) + (1.0 if success else 0.0) * 0.3
            self._skill_success_cache[skill_name] = new_rate

            # Persist to DB
            import sqlite3
            from xmclaw.utils.paths import BASE_DIR
            db_path = BASE_DIR / "shared" / "memory.db"
            if db_path.exists():
                conn = sqlite3.connect(db_path)
                conn.execute("""
                    INSERT OR REPLACE INTO skill_stats (name, usage_count, success_rate, last_used)
                    VALUES (?, ?, ?, datetime('now'))
                """, (skill_name, self._skill_usage_cache[skill_name],
                      self._skill_success_cache[skill_name]))
                conn.commit()
                conn.close()
        except Exception as e:
            logger.debug("skill_usage_record_failed", error=str(e))

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
            score_result = self._score_skill(skill, user_input, task_type,
                                              capabilities_needed, info)
            if score_result["score"] >= 0.3:  # Minimum threshold (lowered from 0.4)
                matched = MatchedSkill(
                    skill_id=skill.get("id", ""),
                    name=skill.get("name", ""),
                    description=skill.get("description", ""),
                    score=score_result["score"],
                    auto_execute=score_result["score"] >= 0.65,  # Lowered from 0.75
                    match_reasons=score_result["reasons"],
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
                self._record_skill_usage(skill["name"], success=True)
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
                self._record_skill_usage(skill["name"], success=False)

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
    ) -> dict:
        """Score a skill's relevance to the current task with multi-dimensional scoring.

        Returns dict with 'score' (0-1) and 'reasons' (list of match reasons).
        """
        score = 0.0
        reasons = []

        text = user_input.lower()
        # Extract meaningful tokens (2+ chars, remove punctuation)
        tokens = [t.strip('.,!?;:()[]{}') for t in text.split() if len(t) >= 2]

        name = skill.get("name", "").lower()
        desc = skill.get("description", "").lower()
        skill_text = f"{name} {desc}".lower()
        # Extract skill tokens
        skill_tokens = set(t.strip('.,!?;:()[]{}') for t in skill_text.split() if len(t) >= 2)

        # ── 1. Keyword Match (0.25 max) ─────────────────────────────────────────
        keyword_score = 0.0
        matched_keywords = []
        for kw in tokens:
            if kw in skill_tokens:
                keyword_score += 0.1
                matched_keywords.append(kw)
        keyword_score = min(keyword_score, 0.25)
        if matched_keywords:
            score += keyword_score
            reasons.append(f"关键词: {', '.join(matched_keywords[:3])}")

        # ── 2. Task Type Alignment (0.25 max) ──────────────────────────────────
        type_score = 0.0
        matched_types = []
        type_keywords = TASK_TYPE_KEYWORDS.get(task_type.value, [])

        # Check if any capability keywords match
        for cap in capabilities:
            cap_keywords = TASK_TYPE_KEYWORDS.get(cap, [])
            for kw in cap_keywords:
                if kw in skill_text:
                    type_score += 0.15
                    if cap not in matched_types:
                        matched_types.append(cap)
                    break

        # Also check task_type directly
        for kw in type_keywords:
            if kw in skill_text:
                type_score += 0.1
                if task_type.value not in matched_types:
                    matched_types.append(task_type.value)

        type_score = min(type_score, 0.25)
        if matched_types:
            score += type_score
            reasons.append(f"任务类型: {', '.join(matched_types)}")

        # ── 3. Intent/Semantic Match (0.20 max) ────────────────────────────────
        intent_score = 0.0
        # Match against insights
        for insight in info.get("insights", [])[:3]:
            ins_text = (insight.get("title", "") + insight.get("description", "")).lower()
            for kw in tokens:
                if len(kw) >= 3 and kw in ins_text:
                    intent_score += 0.05
                    break

        # Match against memories
        for mem in info.get("memories", [])[:3]:
            mem_text = mem.get("content", "").lower()
            for kw in tokens:
                if len(kw) >= 3 and kw in mem_text:
                    intent_score += 0.03
                    break

        intent_score = min(intent_score, 0.20)
        if intent_score > 0:
            score += intent_score
            reasons.append(f"意图匹配: {intent_score:.2f}")

        # ── 4. Frequency/Preference Weighting (0.15 max) ───────────────────────
        pref_score = 0.0
        skill_name = skill.get("name", "")
        usage_count = self._skill_usage_cache.get(skill_name, 0)
        success_rate = self._skill_success_cache.get(skill_name, 0.8)

        if usage_count > 0:
            # More usage = higher base score, capped at 0.1
            usage_weight = min(usage_count / 20, 1.0) * 0.05
            # Success rate adds up to 0.1
            success_weight = success_rate * 0.1
            pref_score = usage_weight + success_weight
            reasons.append(f"使用历史: {usage_count}次, 成功率{success_rate:.0%}")

        score += min(pref_score, 0.15)

        # ── 5. Fuzzy/Partial Match Bonus (0.15 max) ────────────────────────────
        fuzzy_score = 0.0

        # Check for partial word matches
        for kw in tokens:
            if len(kw) >= 4:
                for st in skill_tokens:
                    if len(st) >= 4:
                        # Check if one contains the other
                        if kw in st or st in kw:
                            fuzzy_score += 0.03
                            break
                        # Check common prefixes (3+ chars)
                        if kw[:3] == st[:3]:
                            fuzzy_score += 0.02
                            break

        fuzzy_score = min(fuzzy_score, 0.15)
        if fuzzy_score > 0:
            score += fuzzy_score
            reasons.append(f"模糊匹配: +{fuzzy_score:.2f}")

        # Cap at 1.0
        final_score = min(score, 1.0)

        return {
            "score": final_score,
            "reasons": reasons,
            "dimensions": {
                "keyword": keyword_score,
                "type": type_score,
                "intent": intent_score,
                "preference": pref_score,
                "fuzzy": fuzzy_score,
            }
        }

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
