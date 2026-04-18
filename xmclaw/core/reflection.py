"""Reflection system for XMclaw Agent with multi-trigger support.

Supports multiple reflection triggers:
- CONVERSATION_END: After a conversation completes
- ERROR_OCCURRED: When an error or failure is detected
- USER_REQUEST: When user explicitly asks for reflection
- PERIODIC: Regular intervals during long conversations
"""
import json
from enum import Enum
from typing import AsyncIterator

from xmclaw.llm.router import LLMRouter
from xmclaw.memory.manager import MemoryManager
from xmclaw.utils.log import logger
from xmclaw.evolution.auto_improver import AutoImprover


class ReflectionTrigger(Enum):
    """Reflection trigger types."""
    CONVERSATION_END = "conversation_end"
    ERROR_OCCURRED = "error_occurred"
    USER_REQUEST = "user_request"
    PERIODIC = "periodic"


REFLECTION_PROMPTS = {
    ReflectionTrigger.CONVERSATION_END: """\
你是一位严格的 AI 工程师评审员。请回顾刚才的对话和工具调用历史，进行深度反思。

对话历史:
{history}

请用 JSON 格式输出反思结果（不要包含任何 markdown 代码块标记）:
{{
  "success": true/false,
  "summary": "一句话总结这次交互",
  "problems": ["问题1", "问题2"],
  "lessons": ["教训1", "教训2"],
  "improvements": ["改进建议1", "改进建议2"]
}}
""",
    ReflectionTrigger.ERROR_OCCURRED: """\
你是一位 AI 故障分析专家。刚才的对话中出现了一个错误或失败。请分析：

对话历史:
{history}

错误信息:
{error_context}

请分析：
1. 错误发生的根本原因
2. 如何避免类似错误
3. 需要的改进

请用 JSON 格式输出（不要包含任何 markdown 代码块标记）:
{{
  "success": true/false,
  "root_cause": "根本原因分析",
  "prevention": ["预防措施1", "预防措施2"],
  "improvements": ["改进建议1", "改进建议2"]
}}
""",
    ReflectionTrigger.USER_REQUEST: """\
用户要求进行反思。请回顾以下对话历史，给出深度分析：

对话历史:
{history}

请用 JSON 格式输出（不要包含任何 markdown 代码块标记）:
{{
  "success": true/false,
  "summary": "对话总结",
  "strengths": ["优点1", "优点2"],
  "weaknesses": ["不足1", "不足2"],
  "improvements": ["改进建议1", "改进建议2"]
}}
""",
    ReflectionTrigger.PERIODIC: """\
这是一个定期反思。请回顾最近的对话历史，总结当前状态：

对话历史:
{history}

请用 JSON 格式输出（不要包含任何 markdown 代码块标记）:
{{
  "success": true/false,
  "summary": "当前状态总结",
  "ongoing_issues": ["持续问题1", "持续问题2"],
  "patterns": ["观察到的模式1", "模式2"],
  "recommendations": ["建议1", "建议2"]
}}
""",
}


class ReflectionEngine:
    """Enhanced reflection engine with multi-trigger support."""

    def __init__(self, llm_router: LLMRouter, memory: MemoryManager):
        self.llm = llm_router
        self.memory = memory
        # Track reflection history for periodic triggers
        self._reflection_count = 0
        self._last_reflection_turn = 0

    async def reflect(
        self,
        agent_id: str,
        history: list[dict],
        trigger: ReflectionTrigger = ReflectionTrigger.CONVERSATION_END,
        **kwargs
    ) -> dict:
        """Run reflection with specified trigger.

        Args:
            agent_id: Agent identifier
            history: Conversation history
            trigger: Type of reflection trigger
            **kwargs: Additional context (e.g., error_context for ERROR_OCCURRED)
        """
        if not history:
            return {}

        self._reflection_count += 1
        prompt_template = REFLECTION_PROMPTS.get(trigger, REFLECTION_PROMPTS[ReflectionTrigger.CONVERSATION_END])

        history_text = self._format_history(history)

        # Prepare context for different triggers
        context = {"history": history_text}
        if trigger == ReflectionTrigger.ERROR_OCCURRED:
            context["error_context"] = kwargs.get("error_context", "未知错误")

        prompt = prompt_template.format(**context)
        messages = [
            {"role": "system", "content": "你是一个专业的 AI 行为反思引擎。"},
            {"role": "user", "content": prompt}
        ]

        try:
            response = ""
            async for chunk in self.llm.stream(messages):
                response += chunk

            # Try to extract JSON
            result = self._extract_json(response)
            if result:
                result["trigger"] = trigger.value
                logger.info("reflection_completed",
                          agent_id=agent_id,
                          trigger=trigger.value,
                          summary=result.get("summary", ""))
                improvement = await self._save_reflection(agent_id, result, trigger)
                return {
                    "reflection": result,
                    "improvement": improvement,
                    "trigger": trigger.value,
                }
            else:
                logger.warning("reflection_parse_failed", agent_id=agent_id, raw=response[:500])
                return {}
        except Exception as e:
            logger.error("reflection_error", agent_id=agent_id, trigger=trigger.value, error=str(e))
            return {}

    async def reflect_on_error(
        self,
        agent_id: str,
        history: list[dict],
        error: Exception | str,
        tool_name: str = "",
    ) -> dict:
        """Reflect specifically on an error that occurred.

        Args:
            agent_id: Agent identifier
            history: Conversation history leading to error
            error: The error that occurred
            tool_name: Which tool caused the error (if applicable)
        """
        error_context = f"工具: {tool_name}\n错误: {str(error)}"
        return await self.reflect(
            agent_id,
            history,
            trigger=ReflectionTrigger.ERROR_OCCURRED,
            error_context=error_context,
        )

    def should_reflect_periodically(self, current_turn: int, interval: int = 10) -> bool:
        """Check if a periodic reflection should be triggered.

        Args:
            current_turn: Current conversation turn number
            interval: Turns between reflections

        Returns:
            True if reflection should be triggered
        """
        if current_turn - self._last_reflection_turn >= interval:
            self._last_reflection_turn = current_turn
            return True
        return False

    def _format_history(self, history: list[dict]) -> str:
        lines = []
        for turn in history[-10:]:  # Last 10 turns
            user = turn.get("user", "")
            assistant = turn.get("assistant", "")
            tools = turn.get("tool_calls", [])
            lines.append(f"User: {user}")
            lines.append(f"Agent: {assistant[:500]}")
            if tools:
                for t in tools:
                    lines.append(f"Tool: {t.get('name', '')} -> {str(t.get('result', ''))[:200]}")
        return "\n".join(lines)

    def _extract_json(self, text: str) -> dict | None:
        # First try direct parse
        try:
            return json.loads(text.strip())
        except json.JSONDecodeError:
            pass
        # Try to extract from markdown code block
        import re
        match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
        if match:
            try:
                return json.loads(match.group(1).strip())
            except json.JSONDecodeError:
                pass
        # Try to find first { and last }
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start:end+1])
            except json.JSONDecodeError:
                pass
        return None

    async def _save_reflection(self, agent_id: str, result: dict, trigger: ReflectionTrigger = ReflectionTrigger.CONVERSATION_END) -> dict:
        """Save reflection results to memory and trigger auto-improvement."""
        if not self.memory:
            return {}

        # Build insight with trigger information
        trigger_label = {
            ReflectionTrigger.CONVERSATION_END: "对话结束反思",
            ReflectionTrigger.ERROR_OCCURRED: "错误分析反思",
            ReflectionTrigger.USER_REQUEST: "用户请求反思",
            ReflectionTrigger.PERIODIC: "定期反思",
        }.get(trigger, "反思")

        # Save as insight
        insight = {
            "title": result.get("summary", "Reflection"),
            "description": json.dumps(result, ensure_ascii=False),
            "source": f"reflection:{trigger.value}",
            "type": "lesson",
            "trigger": trigger.value,
        }
        self.memory.save_insight(agent_id, insight)

        # Also add to vector store for retrieval
        content_parts = [
            f"[{trigger_label}] Reflection: {result.get('summary', '')}",
        ]

        # Add trigger-specific fields
        if "problems" in result:
            content_parts.append(f"Problems: {', '.join(result.get('problems', []))}")
        if "lessons" in result:
            content_parts.append(f"Lessons: {', '.join(result.get('lessons', []))}")
        if "improvements" in result:
            content_parts.append(f"Improvements: {', '.join(result.get('improvements', []))}")
        if "root_cause" in result:
            content_parts.append(f"Root Cause: {result.get('root_cause', '')}")
        if "prevention" in result:
            content_parts.append(f"Prevention: {', '.join(result.get('prevention', []))}")

        content = "\n".join(content_parts)
        await self.memory.add_memory(agent_id, content, source=f"reflection:{trigger.value}")

        # Trigger auto-improvement pipeline
        try:
            improver = AutoImprover()
            improvement_result = await improver.improve_from_reflection(agent_id, result)
            logger.info("auto_improvement_triggered", agent_id=agent_id, result=improvement_result)
            return improvement_result
        except Exception as e:
            logger.error("auto_improvement_failed", agent_id=agent_id, error=str(e))
            return {"status": "error", "error": str(e)}
