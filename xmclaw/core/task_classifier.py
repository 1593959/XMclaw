"""Task Analyzer: classifies incoming user input into task types.

This is Step 1 of the Agent Cognition Pipeline.
It runs BEFORE the LLM generates any response, giving the system
structured awareness of what kind of work it needs to do.
"""
import json
from enum import Enum
from typing import TypedDict

from xmclaw.llm.router import LLMRouter
from xmclaw.utils.log import logger


class TaskType(str, Enum):
    QA = "qa"                    # 问答/解释
    CODE = "code"                # 写代码/调试/重构
    SEARCH = "search"            # 网络搜索/信息查找
    PLAN = "plan"                # 规划/分析
    CREATIVE = "creative"        # 写作/创意
    LEARNING = "learning"        # 学习/研究
    FILE_OP = "file_op"          # 文件操作
    SYSTEM = "system"            # 系统控制/配置
    GENERAL = "general"          # 通用对话


class Complexity(str, Enum):
    LOW = "low"      # 简单，直接回答或单步操作
    MEDIUM = "medium"  # 中等，需多步或多个工具
    HIGH = "high"    # 复杂，需拆解、规划、多次迭代


class ClassifierSource(str, Enum):
    """Which path produced a TaskProfile.

    Downstream code (reflection, gatherer, skill_matcher) can branch on this
    to know whether the classification is trustworthy — a "fallback" profile
    is structurally valid but semantically meaningless (classifier broke),
    so adaptive behavior should be disabled (bug M29).
    """
    FAST = "fast"          # heuristic shortcut matched
    LLM = "llm"            # LLM-assisted classification succeeded
    FALLBACK = "fallback"  # LLM failed / empty input — default applied
    EMPTY = "empty"        # blank input


class TaskProfile(TypedDict):
    type: TaskType
    complexity: Complexity
    capabilities_needed: list[str]     # ["web_search", "code", "file_read", ...]
    recommended_actions: list[str]     # ["search_web", "load_examples", "plan_steps", ...]
    reasoning: str                     # 为什么判定为这个类型
    subtasks: list[str]                # 分解出的子任务（如果复杂）
    source: ClassifierSource           # provenance — see ClassifierSource docstring


CLASSIFY_PROMPT = """\
分析以下用户输入，判断其任务类型并评估复杂度。

用户输入: {input}

请用 JSON 格式输出分析结果（不要 markdown 代码块）:
{{
  "type": "code|search|plan|creative|learning|file_op|system|qa|general",
  "complexity": "low|medium|high",
  "capabilities_needed": ["web_search", "file_read", ...],
  "recommended_actions": ["search_web", "load_examples", "plan_steps", ...],
  "reasoning": "判定理由",
  "subtasks": ["子任务1", "子任务2"]  // 仅当 complexity=high 时需要拆解
}}

capabilities_needed 可选: web_search, code_write, code_debug, file_read, file_write,
math, data_analysis, creative_writing, research, system_control, memory_search
recommended_actions 可选: search_web, load_memories, load_insights, plan_steps,
execute_subtasks, call_skill, end_response
"""


class TaskClassifier:
    """Analyzes and classifies user input before LLM processing."""

    def __init__(self, llm_router: LLMRouter):
        self.llm = llm_router

    async def classify(self, user_input: str) -> TaskProfile:
        """Classify a user input string into a structured TaskProfile."""
        if not user_input or not user_input.strip():
            return TaskProfile(
                type=TaskType.GENERAL,
                complexity=Complexity.LOW,
                capabilities_needed=[],
                recommended_actions=["end_response"],
                reasoning="空输入",
                subtasks=[],
                source=ClassifierSource.EMPTY,
            )

        # Fast-path: heuristic shortcuts for obvious patterns
        fast = self._fast_classify(user_input)
        if fast:
            logger.debug("task_classified_fast", type=fast["type"], input=user_input[:50])
            # Ensure type/complexity are proper enums, not plain strings
            fast["type"] = TaskType(fast.get("type", "general"))
            fast["complexity"] = Complexity(fast.get("complexity", "low"))
            fast["source"] = ClassifierSource.FAST
            return TaskProfile(**fast)

        # Slow-path: LLM-assisted classification for ambiguous inputs
        try:
            prompt = CLASSIFY_PROMPT.format(input=user_input)
            messages = [
                {"role": "system",
                 "content": "你是一个严格的任务分类引擎。只输出纯 JSON，不要任何额外文字。"},
                {"role": "user", "content": prompt},
            ]
            # NOTE: llm.stream() yields JSON event envelopes (see reflection.py
            # for the full story); use .complete() so we get the raw text the
            # model actually produced, otherwise _extract_json always fails
            # and every medium-ambiguity input silently falls through to the
            # FALLBACK TaskProfile.
            response = await self.llm.complete(messages)

            data = self._extract_json(response)
            if data:
                profile = TaskProfile(
                    type=TaskType(data.get("type", "general")),
                    complexity=Complexity(data.get("complexity", "low")),
                    capabilities_needed=data.get("capabilities_needed", []),
                    recommended_actions=data.get("recommended_actions", []),
                    reasoning=data.get("reasoning", ""),
                    subtasks=data.get("subtasks", []),
                    source=ClassifierSource.LLM,
                )
                logger.info("task_classified", type=profile["type"],
                             complexity=profile["complexity"], reasoning=profile["reasoning"])
                return profile
            logger.warning("task_classify_empty_response", raw=response[:200])
        except Exception as e:
            logger.warning("task_classify_failed", error=str(e))

        # Fallback: treat as general low complexity — source=FALLBACK marks
        # this as untrusted so the gatherer/skill_matcher can degrade safely.
        return TaskProfile(
            type=TaskType.GENERAL,
            complexity=Complexity.LOW,
            capabilities_needed=[],
            recommended_actions=["end_response"],
            reasoning="分类失败，使用默认分类",
            subtasks=[],
            source=ClassifierSource.FALLBACK,
        )

    def _fast_classify(self, user_input: str) -> dict | None:
        """Heuristic fast-path for obvious patterns. Returns dict or None."""
        text = user_input.strip().lower()

        # Coding patterns
        if any(kw in text for kw in ["写代码", "写个", "代码", "debug", "fix", "bug", "function", "def ", "class ", "import ", "=>", "->", "fn ", "//"]):
            return {
                "type": "code", "complexity": "medium",
                "capabilities_needed": ["code_write"],
                "recommended_actions": ["load_examples", "end_response"],
                "reasoning": "检测到代码相关关键词", "subtasks": [],
            }
        # Search patterns
        if any(kw in text for kw in ["搜索", "查找", "最新", "排行榜", "排名", "top ", "best ", "how to", "what is", "是什么", "怎么"]):
            return {
                "type": "search", "complexity": "low",
                "capabilities_needed": ["web_search", "memory_search"],
                "recommended_actions": ["search_web", "load_memories"],
                "reasoning": "检测到搜索/查询关键词", "subtasks": [],
            }
        # Planning patterns
        if any(kw in text for kw in ["规划", "计划", "如何做", "方案", "流程", "步骤", "分析", "分析一下"]):
            return {
                "type": "plan", "complexity": "high",
                "capabilities_needed": ["research", "memory_search"],
                "recommended_actions": ["plan_steps", "load_memories", "load_insights"],
                "reasoning": "检测到规划/分析关键词", "subtasks": [],
            }
        # Creative patterns
        if any(kw in text for kw in ["写一篇", "写个", "创作", "设计", "写小说", "生成"]):
            return {
                "type": "creative", "complexity": "medium",
                "capabilities_needed": ["creative_writing"],
                "recommended_actions": ["end_response"],
                "reasoning": "检测到创意写作关键词", "subtasks": [],
            }
        # File operation patterns
        if any(kw in text for kw in ["文件", "读取", "写入", "编辑", "修改", "read", "write", "edit", "open"]):
            return {
                "type": "file_op", "complexity": "low",
                "capabilities_needed": ["file_read", "file_write"],
                "recommended_actions": ["end_response"],
                "reasoning": "检测到文件操作关键词", "subtasks": [],
            }
        # System patterns
        if any(kw in text for kw in ["配置", "设置", "重启", "启动", "停止", "config", "setting", "restart"]):
            return {
                "type": "system", "complexity": "low",
                "capabilities_needed": ["system_control"],
                "recommended_actions": ["end_response"],
                "reasoning": "检测到系统控制关键词", "subtasks": [],
            }
        return None

    def _extract_json(self, text: str) -> dict | None:
        text = text.strip()
        # Try direct parse
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        # Try code block
        import re
        match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
        if match:
            try:
                return json.loads(match.group(1).strip())
            except json.JSONDecodeError:
                pass
        # Find first { ... last }
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start:end+1])
            except json.JSONDecodeError:
                pass
        return None
