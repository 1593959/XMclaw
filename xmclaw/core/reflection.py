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


class ReflectionStatus(str, Enum):
    """Outcome of a reflect() call.

    Empty-signal skips used to collapse into `{}`, making them indistinguishable
    from parse failures (bug M73/M74). Both were swallowed silently, which
    blinded the evolution layer — a "nothing to learn" turn is itself a signal.
    """
    OK = "ok"                          # reflection produced
    SKIPPED_NO_HISTORY = "skipped_no_history"
    SKIPPED_EMPTY_SIGNAL = "skipped_empty_signal"
    PARSE_FAILED = "parse_failed"
    ERROR = "error"


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
            **kwargs: Additional context (e.g., error_context for ERROR_OCCURRED,
                      artifact_health for the evolution feedback loop — a list of
                      snapshots from EvolutionJournal.snapshot_active_artifacts).
        """
        if not history:
            logger.info("reflection_skipped", agent_id=agent_id, reason="no_history")
            return {"status": ReflectionStatus.SKIPPED_NO_HISTORY.value}

        if self._is_empty_signal(history):
            # Every assistant turn is empty AND no tools ran — LLM would just
            # hallucinate. Log as a first-class signal for the journal.
            logger.info("reflection_skipped", agent_id=agent_id,
                        reason="empty_signal", turns=len(history))
            return {"status": ReflectionStatus.SKIPPED_EMPTY_SIGNAL.value}

        self._reflection_count += 1
        prompt_template = REFLECTION_PROMPTS.get(trigger, REFLECTION_PROMPTS[ReflectionTrigger.CONVERSATION_END])

        # Phase E6: enrich each turn with any 👍/👎 the user left on it so
        # the reflection prompt can account for human verdicts, not just
        # the LLM's self-assessment of its own work.
        history = self._annotate_with_user_feedback(agent_id, history)
        feedback_summary = self._summarize_user_feedback(history)

        history_text = self._format_history(history)

        # Prepare context for different triggers
        context = {"history": history_text}
        if feedback_summary:
            context["history"] = feedback_summary + "\n\n" + context["history"]
        if trigger == ReflectionTrigger.ERROR_OCCURRED:
            context["error_context"] = kwargs.get("error_context", "未知错误")

        prompt = prompt_template.format(**context)
        # Phase E4 feedback loop: if the caller passes artifact_health, prepend
        # a section so the LLM knows which evolution products already exist and
        # how they're doing. This biases the next insight toward fixing
        # suspects / deleting dead code instead of forging a near-duplicate.
        health_block = self._format_artifact_health(kwargs.get("artifact_health") or [])
        if health_block:
            prompt = health_block + "\n\n" + prompt
        messages = [
            {"role": "system", "content": "你是一个专业的 AI 行为反思引擎。"},
            {"role": "user", "content": prompt}
        ]

        try:
            # NOTE: we deliberately use .complete() instead of .stream() here.
            # llm.stream() yields JSON event envelopes (`{"type":"text","content":"…"}`),
            # not raw text — concatenating the chunks produced a non-JSON blob
            # that _extract_json could never parse, so reflection was silently
            # returning PARSE_FAILED on every call. See regression test
            # tests/test_reflection.py::test_reflect_uses_completion_not_event_stream.
            response = await self.llm.complete(messages)

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
                    "status": ReflectionStatus.OK.value,
                    "reflection": result,
                    "improvement": improvement,
                    "trigger": trigger.value,
                }
            else:
                logger.warning("reflection_parse_failed", agent_id=agent_id, raw=response[:500])
                return {"status": ReflectionStatus.PARSE_FAILED.value, "raw": response[:500]}
        except Exception as e:
            logger.error("reflection_error", agent_id=agent_id, trigger=trigger.value, error=str(e))
            return {"status": ReflectionStatus.ERROR.value, "error": str(e)}

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

    def _is_empty_signal(self, history: list[dict]) -> bool:
        """Return True if the history has nothing meaningful to reflect on.

        An 'empty signal' turn has (a) no assistant text AND (b) no tool_calls
        AND (c) no tool_observations. If EVERY turn looks like that, reflecting
        would just hallucinate — we'd rather log a skipped signal and let the
        evolution layer know 'nothing to learn here'.
        """
        for turn in history:
            if (turn.get("assistant") or "").strip():
                return False
            if turn.get("tool_calls"):
                return False
            if turn.get("tool_observations"):
                return False
        return True

    def _format_artifact_health(self, snapshots: list[dict]) -> str:
        """Render a compact view of active evolution artifacts for the
        reflection prompt. Empty list → empty string, so trigger templates
        that run without health context get the same prompt as before."""
        if not snapshots:
            return ""
        lines = ["## 当前活跃的进化产物（由元评估注入）"]
        lines.append("下面是已经生成并仍处于运行中的技能/基因，以及它们最近的使用情况。")
        lines.append("如果某个产物已经在解决这次的问题，请在反思中推荐 **修改** 而不是新建；")
        lines.append("如果某个产物是 `dead`（没人用）或 `suspect`（多次失败），也请指出。")
        lines.append("")
        for s in snapshots:
            aid = s.get("artifact_id", "?")
            kind = s.get("kind", "?")
            status = s.get("status", "?")
            verdict = s.get("verdict", "?")
            matched = s.get("matched", 0)
            helpful = s.get("helpful", 0)
            harmful = s.get("harmful", 0)
            lines.append(
                f"- [{verdict}] {aid} (kind={kind}, status={status}) "
                f"— matched={matched}, helpful={helpful}, harmful={harmful}"
            )
        return "\n".join(lines)

    def _format_history(self, history: list[dict]) -> str:
        lines = []
        for turn in history[-10:]:  # Last 10 turns
            user = turn.get("user", "")
            assistant = turn.get("assistant", "")
            tools = turn.get("tool_calls", [])
            lines.append(f"User: {user}")
            # Phase E6: surface any human feedback attached to this turn so
            # the reflection LLM sees it next to the agent's reply.
            feedback = turn.get("user_feedback")
            marker = ""
            if feedback:
                thumb = feedback.get("thumb")
                note = feedback.get("note") or ""
                if thumb == "up":
                    marker = " [👍 human approved]"
                elif thumb == "down":
                    marker = " [👎 human disapproved]"
                if note:
                    marker += f' — "{note[:200]}"'
            lines.append(f"Agent: {assistant[:500]}{marker}")
            if tools:
                for t in tools:
                    lines.append(f"Tool: {t.get('name', '')} -> {str(t.get('result', ''))[:200]}")
        return "\n".join(lines)

    def _annotate_with_user_feedback(
        self, agent_id: str, history: list[dict],
    ) -> list[dict]:
        """Join recent user_feedback rows onto the in-memory history so the
        reflection prompt can see 👍/👎 per turn. Best-effort: if the store is
        unavailable, returns history untouched so reflection still runs."""
        store = getattr(self.memory, "sqlite", None)
        if store is None:
            return history
        turn_ids = [t.get("turn_id") for t in history if t.get("turn_id")]
        if not turn_ids:
            return history
        try:
            fb_map = store.get_user_feedback_by_turns(agent_id, turn_ids)
        except Exception as e:
            logger.warning("reflection_feedback_join_failed",
                          agent_id=agent_id, error=str(e))
            return history
        if not fb_map:
            return history
        annotated: list[dict] = []
        for turn in history:
            tid = turn.get("turn_id")
            row = fb_map.get(tid) if tid else None
            if row is None:
                annotated.append(turn)
                continue
            # Copy so we don't mutate the caller's in-memory state.
            annotated.append({**turn, "user_feedback": {
                "thumb": row.get("thumb"),
                "note": row.get("note"),
            }})
        return annotated

    @staticmethod
    def _summarize_user_feedback(history: list[dict]) -> str:
        """Top-of-prompt summary so the LLM notices the aggregate human
        verdict before reading the turn-by-turn detail. Empty when no
        feedback is attached."""
        up = 0
        down = 0
        notes: list[str] = []
        for turn in history[-10:]:
            fb = turn.get("user_feedback") or {}
            thumb = fb.get("thumb")
            if thumb == "up":
                up += 1
            elif thumb == "down":
                down += 1
            note = fb.get("note")
            if note:
                notes.append(note[:200])
        if up == 0 and down == 0:
            return ""
        lines = [
            "## 人类反馈摘要（Plan v2 E6）",
            f"过去 10 轮中用户留下了 {up} 个 👍 / {down} 个 👎。",
        ]
        if down > up:
            lines.append("下行反馈居多 —— 优先分析 **哪个工具/技能正在拖累** 而不是新建能力。")
        elif up > down:
            lines.append("上行反馈居多 —— 保留并强化已经在用的产物，不要贸然退役。")
        if notes:
            lines.append("用户备注：")
            for n in notes:
                lines.append(f"- {n}")
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
