"""MemoryExtractor — Phase B of "agent 自己用记忆" (2026-05-10).

Phase A (``agent_loop._unified_memory.query`` on turn start) made the
agent **read** the unified memory store.  Phase B closes the loop:
after each turn, decide whether the conversation produced something
worth persisting and ``put()`` it back.

Design constraints
==================

* **Cheap by default.** A naive "extract every turn" doubles every
  turn's LLM cost. Most turns are routine ("what's the weather", "show
  me the diff") and produce no durable fact. The extractor MUST gate
  itself with cheap heuristics first; LLM call only fires when the
  heuristic flags a candidate signal.
* **Best-effort, never fails a turn.** Same posture as the recall
  side: any extraction error gets logged + swallowed. The LLM extract
  call also has a hard wall-clock cap.
* **Honest semantics.** Don't store the entire turn (creates noise);
  store ONLY the distilled fact the LLM identified — otherwise
  recall surface fills with conversational chaff and signal-to-noise
  drops.

Trigger heuristics (gates the LLM call)
========================================

In rough order of "cheapest signal first" — first match wins:

  1. **User declared a fact about themselves.** Phrases like "我叫"
     / "my name is" / "I prefer" / "I work at" / "我习惯". These
     are durable preferences worth keeping.
  2. **Decision was made.** Assistant text contains "决定" / "我们
     就用" / "let's go with" / "decided to". Captures architectural /
     planning decisions the user will refer back to later.
  3. **Task completion.** Assistant said "完成" / "done" / "shipped"
     / "merged" + the prior user message was a request. Captures
     "the X feature got shipped" milestones.
  4. **Explicit "remember this".** User says "记住" / "remember" /
     "save this". Direct instruction.

If any trigger fires → make ONE LLM call to extract the durable fact
in JSON format. The LLM is asked to return either ``null`` (false
positive — the heuristic was over-eager) or a single ``{text,
node_type, layer}`` envelope.

Why one fact, not many: keeps the JSON schema trivial, makes the LLM
call short (= cheap + fast), and avoids the "extract a wall of
generic facts" failure mode that one-shot multi-bucket extractors
fall into. If a turn has multiple durable facts, the next turn that
references them will trigger again on the relevant fragment.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Literal

logger = logging.getLogger(__name__)


# ── Trigger phrases ──────────────────────────────────────────────


# Each pattern is matched against the user message OR the assistant
# response (whichever is appropriate per kind). Patterns are
# CASE-INSENSITIVE (re.IGNORECASE). CN + EN coverage.
_USER_FACT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"我叫\s*\S+", re.IGNORECASE),
    re.compile(r"我是\s*\S+", re.IGNORECASE),
    re.compile(r"我习惯", re.IGNORECASE),
    re.compile(r"我喜欢", re.IGNORECASE),
    re.compile(r"我不喜欢", re.IGNORECASE),
    re.compile(r"我偏好", re.IGNORECASE),
    re.compile(r"my name is\b", re.IGNORECASE),
    re.compile(r"\bi prefer\b", re.IGNORECASE),
    re.compile(r"\bi work (at|on|for)\b", re.IGNORECASE),
    re.compile(r"\bi'?m a(n)?\s+\w+", re.IGNORECASE),
    re.compile(r"\bcall me\b", re.IGNORECASE),
)

_DECISION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"决定(用|采用|实现|做)", re.IGNORECASE),
    re.compile(r"我们就用", re.IGNORECASE),
    re.compile(r"就(选|定|用)\s*\S+", re.IGNORECASE),
    re.compile(r"\blet's go with\b", re.IGNORECASE),
    re.compile(r"\bdecided to\b", re.IGNORECASE),
    re.compile(r"\bgoing with\b", re.IGNORECASE),
)

_COMPLETION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"已完成|已实现|已合并|已部署", re.IGNORECASE),
    re.compile(r"\b(done|shipped|merged|deployed|completed)\b", re.IGNORECASE),
)

_REMEMBER_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"记住|记一下|留个底", re.IGNORECASE),
    re.compile(r"\b(remember|save) (this|that)\b", re.IGNORECASE),
    re.compile(r"\bnote (this|that) down\b", re.IGNORECASE),
)

# Wave 26 fix-4: assistant-side memorisation claims. Triggered when the
# agent says "我记下了 X" / "I'll remember Y" / "noted" — the user's
# pain point was that these claims were lies (no actual write
# happened). Now they force-fire the LLM extractor so the durable
# memory ACTUALLY persists the thing the agent promised to keep.
_ASSISTANT_REMEMBER_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Chinese: 记下了 / 记住了 / 已记录 / 已记住 / 会记得 / 帮你记住了
    # — "我" prefix is OPTIONAL because the agent often skips it
    # ("哥，记住了！" / "ok，已记录" — same intent without the
    # pronoun). Fix #2 (post-Wave 26 fix-4): the user's first real
    # test had "哥，记住了！" which my too-strict r"我..." pattern
    # missed → no extraction → no memo. Loosen the "我" anchor and
    # the patterns trigger on every natural Chinese memorisation
    # claim the agent emits.
    re.compile(r"(?:我(?:已经?)?)?记(?:下|住)了", re.IGNORECASE),
    re.compile(r"已记(?:录|住|下)", re.IGNORECASE),
    re.compile(r"(?:我)?会记得", re.IGNORECASE),
    re.compile(r"(?:我)?帮你记(?:下|住)了?", re.IGNORECASE),
    re.compile(r"(?:已)?收到[，,!！.。]?\s*(?:记下了|记住了)", re.IGNORECASE),
    # English: I'll remember / I've noted / noted! / got it, I'll remember
    re.compile(r"\bI(?:'ll| will) remember\b", re.IGNORECASE),
    re.compile(r"\bI(?:'ve| have) noted\b", re.IGNORECASE),
    re.compile(r"\bI(?:'ll| will) note\b", re.IGNORECASE),
    re.compile(r"\bnoted[.!]", re.IGNORECASE),
    re.compile(r"\bgot it,? I('?ll| will) remember\b", re.IGNORECASE),
)


TriggerKind = Literal[
    "user_fact",
    "decision",
    "completion",
    "remember",
    "assistant_remember",  # Wave 26 fix-4
]


def _detect_trigger(
    user_message: str,
    assistant_response: str,
) -> TriggerKind | None:
    """Cheap pattern-match gate. Returns the FIRST trigger kind that
    matches; ``None`` means the extractor should NOT fire (saves an
    LLM call on routine turns)."""
    user = user_message or ""
    asst = assistant_response or ""
    # Order matters — explicit "remember" beats heuristics.
    if any(p.search(user) for p in _REMEMBER_PATTERNS):
        return "remember"
    # Wave 26 fix-4: assistant-claimed memorisation is the #1 silent
    # failure the user complained about ("说他记住了，一压缩啥都不知道"
    # — closing the gap means we treat the claim as a commitment to
    # write).
    if any(p.search(asst) for p in _ASSISTANT_REMEMBER_PATTERNS):
        return "assistant_remember"
    if any(p.search(user) for p in _USER_FACT_PATTERNS):
        return "user_fact"
    if any(p.search(asst) for p in _DECISION_PATTERNS):
        return "decision"
    if any(p.search(asst) for p in _COMPLETION_PATTERNS):
        return "completion"
    return None


# ── Result type ─────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ExtractedFact:
    """One durable memory entry the extractor decided is worth a put.

    Maps 1:1 to ``UnifiedMemorySystem.put`` parameters. Frozen so the
    AgentLoop can't mutate it before persistence.
    """
    text: str
    node_type: Literal["event", "entity", "state", "intent"]
    layer: Literal["working", "short_term", "long_term", "procedural"]
    reason: str   # human-readable why we kept it (for the UI timeline)


# ── Prompt ───────────────────────────────────────────────────────


_EXTRACT_PROMPT = """\
你是一个记忆萃取助手。任务：从下面这一轮对话里萃取**最多一条**值得长期\
保留的事实/决定/状态。如果没有，返回 null。

触发原因：{trigger_reason}

用户消息：
{user_message}

助手回复：
{assistant_response}

输出严格 JSON，二选一：

null

或

{{"text": "...", "node_type": "event|entity|state|intent", \
"layer": "working|short_term|long_term|procedural", \
"reason": "为什么值得记 (一句话)"}}

判断准则：
- text 必须是**事实陈述**（"用户偏好深色主题"），不是对话片段（"用户问\
'你喜欢深色还是浅色'"）。
- node_type: entity=人/项目/工具；event=已发生的事；state=当前情况；\
intent=未来计划。
- layer: long_term=持久偏好/事实；short_term=本周/本项目；working=本会话；\
procedural=技能/方法。
- 不确定时返回 null，不要硬编。
- 输出 JSON 之外不要任何字符（包括代码块标记）。
"""


_TRIGGER_REASONS = {
    "user_fact":           "用户陈述了关于自己的事实/偏好",
    "decision":            "助手或对话中做出了决定",
    "completion":          "助手报告任务完成",
    "remember":            "用户明确要求记住",
    "assistant_remember":  "助手声称已记住 — 必须实际持久化以兑现承诺",
}


# ── Extractor ────────────────────────────────────────────────────


class MemoryExtractor:
    """LLM-driven extractor with heuristic gating.

    Lifecycle: one instance per AgentLoop, shared across sessions.
    Stateless — every ``extract`` call is independent.

    Args:
        llm: any object exposing ``async complete(messages, tools=None) ->
            LLMResponse``. ``LLMResponse.content`` is parsed as JSON.
            (NOT ``complete_streaming`` — extract is fire-and-forget,
            we don't need streaming UI for this background job.)
        timeout_s: hard wall-clock cap on the LLM call. Wave 26 fix-4
            bumped to 30s (was 8s — too tight for Kimi K2 / Sonnet
            which routinely take 5-15s). The call is background so a
            longer cap doesn't hurt UX; what hurt was the 8s timeout
            silently dropping every extraction attempt, which is why
            "I remember X" claims never landed in storage.
        log: logger instance for failures (defaults to module logger).
    """

    def __init__(
        self,
        llm: Any,
        *,
        timeout_s: float = 30.0,
        log: logging.Logger | None = None,
    ) -> None:
        self._llm = llm
        self._timeout_s = max(1.0, float(timeout_s))
        self._log = log or logger

    async def extract(
        self,
        *,
        user_message: str,
        assistant_response: str,
    ) -> ExtractedFact | None:
        """Run the heuristic gate, then (if triggered) the LLM extract.

        Returns:
            ``ExtractedFact`` when the LLM identifies something durable,
            ``None`` when:
              * heuristic didn't trigger (saved the LLM call)
              * LLM returned ``null`` (false-positive heuristic)
              * LLM call timed out / failed (best-effort)
              * LLM output was unparseable / shape-invalid
        """
        trigger = _detect_trigger(user_message, assistant_response)
        if trigger is None:
            return None

        prompt = _EXTRACT_PROMPT.format(
            trigger_reason=_TRIGGER_REASONS[trigger],
            user_message=user_message[:2000],  # cap to avoid runaway
            assistant_response=assistant_response[:2000],
        )

        try:
            from xmclaw.providers.llm.base import Message
            t0 = time.perf_counter()
            resp = await asyncio.wait_for(
                self._llm.complete([
                    Message(role="user", content=prompt),
                ]),
                timeout=self._timeout_s,
            )
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
        except asyncio.TimeoutError:
            self._log.warning(
                "memory_extractor.timeout trigger=%s elapsed_ms=%.0f",
                trigger, self._timeout_s * 1000.0,
            )
            return None
        except Exception as exc:  # noqa: BLE001
            self._log.warning(
                "memory_extractor.llm_failed trigger=%s err=%s",
                trigger, exc,
            )
            return None

        content = (getattr(resp, "content", "") or "").strip()
        # Strip markdown code fences if the LLM wrapped despite our
        # instruction to not do that.
        content = re.sub(r"^```(?:json)?\s*", "", content)
        content = re.sub(r"\s*```$", "", content)

        # ``null`` return = LLM agreed nothing is worth keeping.
        if content.lower() in ("null", "none", "{}"):
            self._log.debug(
                "memory_extractor.no_fact trigger=%s elapsed_ms=%.0f",
                trigger, elapsed_ms,
            )
            return None

        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            self._log.warning(
                "memory_extractor.bad_json trigger=%s preview=%r",
                trigger, content[:200],
            )
            return None

        if not isinstance(data, dict):
            return None

        text = data.get("text")
        node_type = data.get("node_type", "event")
        layer = data.get("layer", "long_term")
        reason = data.get("reason", _TRIGGER_REASONS[trigger])

        # Shape validation. Reject malformed extracts rather than
        # storing garbage.
        if not isinstance(text, str) or not text.strip():
            return None
        if node_type not in ("event", "entity", "state", "intent"):
            node_type = "event"
        if layer not in ("working", "short_term", "long_term", "procedural"):
            layer = "long_term"
        if not isinstance(reason, str) or not reason.strip():
            reason = _TRIGGER_REASONS[trigger]

        return ExtractedFact(
            text=text.strip(),
            node_type=node_type,  # type: ignore[arg-type]
            layer=layer,  # type: ignore[arg-type]
            reason=reason.strip(),
        )


__all__ = [
    "ExtractedFact",
    "MemoryExtractor",
    "TriggerKind",
]
