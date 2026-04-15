"""Reflection system for XMclaw Agent."""
import json
from typing import AsyncIterator

from xmclaw.llm.router import LLMRouter
from xmclaw.memory.manager import MemoryManager
from xmclaw.utils.log import logger


REFLECTION_PROMPT = """\
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
"""


class ReflectionEngine:
    def __init__(self, llm_router: LLMRouter, memory: MemoryManager):
        self.llm = llm_router
        self.memory = memory

    async def reflect(self, agent_id: str, history: list[dict]) -> dict:
        """Run reflection on a completed conversation."""
        if not history:
            return {}

        history_text = self._format_history(history)
        prompt = REFLECTION_PROMPT.format(history=history_text)
        messages = [{"role": "system", "content": "你是一个专业的 AI 行为反思引擎。"}, {"role": "user", "content": prompt}]

        try:
            response = ""
            async for chunk in self.llm.stream(messages):
                response += chunk

            # Try to extract JSON
            result = self._extract_json(response)
            if result:
                logger.info("reflection_completed", agent_id=agent_id, summary=result.get("summary", ""))
                await self._save_reflection(agent_id, result)
                return result
            else:
                logger.warning("reflection_parse_failed", agent_id=agent_id, raw=response[:500])
                return {}
        except Exception as e:
            logger.error("reflection_error", agent_id=agent_id, error=str(e))
            return {}

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

    async def _save_reflection(self, agent_id: str, result: dict) -> None:
        """Save reflection results to memory."""
        if not self.memory:
            return

        # Save as insight
        insight = {
            "title": result.get("summary", "Reflection"),
            "description": json.dumps(result, ensure_ascii=False),
            "source": "reflection",
            "type": "lesson",
        }
        self.memory.save_insight(agent_id, insight)

        # Also add to vector store for retrieval
        content = (
            f"Reflection: {result.get('summary', '')}\n"
            f"Problems: {', '.join(result.get('problems', []))}\n"
            f"Lessons: {', '.join(result.get('lessons', []))}\n"
            f"Improvements: {', '.join(result.get('improvements', []))}"
        )
        await self.memory.add_memory(agent_id, content, source="reflection")
