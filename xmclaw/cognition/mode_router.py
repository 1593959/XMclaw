"""ModeRouter — pick the right run mode for an incoming user turn.

Background
==========

Kimi K2.6 exposes four chat modes:

* **instant** — single-shot reply, no thinking, no tools. Latency: ~1s.
* **thinking** — visible CoT, no tools.
* **agent** — tools enabled, hop loop, up to ~200 steps.
* **swarm** — tools + fanout (the K2.6 "Agent Swarm" UI button), up to
  ~300 sub-agents.

The user picks the mode in the UI. But under the hood K2.6's runtime
ALSO routes — if you ask "what time is it" in agent mode, it still
single-shots (no tool needed); if you ask "search the web and
summarise" in instant mode, it kicks up to thinking automatically.

That auto-routing trick is model-independent: a tiny classifier picks
the cheapest mode that can answer the question, and the AgentLoop
honours it. XMclaw can do the same — saves money + latency on the
trivial-question majority of traffic.

How this composes with PlanFirstMode + GoalAnchor
=================================================

* ``instant`` mode → skip PlanFirst, skip GoalAnchor (no hops), skip
  StepValidator. Just call LLM once.
* ``thinking`` mode → skip PlanFirst+GoalAnchor (no tools to chain),
  but allow the system prompt to invite extended-thinking.
* ``agent`` mode (default) → all the Batch A-C goodness fires.
* ``swarm`` mode → agent + ``parallel_subagents`` tool actively
  encouraged in the system prompt.

This module is **pure routing logic** — no IO, no LLM call. It just
classifies and returns a ``RunMode`` enum. The AgentLoop reads the
enum and configures itself for the turn.

Heuristics
==========

We bucket on the user message:

1. **instant** if it's a one-liner factoid / greeting / chit-chat and
   has no signs of needing tools.
2. **swarm** if there are explicit fanout cues ("compare A B C",
   "summarise each of these N files", "analyse … for each").
3. **agent** is the default for everything else.
4. **thinking** is rarely auto-picked — most "thinking" use cases also
   want tools eventually, so agent is the safer default. Reserved
   for ``user explicitly asks to think / explain / reason``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class RunMode(str, Enum):
    INSTANT = "instant"
    THINKING = "thinking"
    AGENT = "agent"
    SWARM = "swarm"


# ── Heuristic signal regexes ─────────────────────────────────────


# Trivial / one-liner cues — questions answerable without tools or
# multi-step reasoning. Conservative — false negatives (routing to
# agent when instant would do) are way cheaper than false positives
# (routing to instant when tools were actually needed).
_GREETING_RE = re.compile(
    r"^\s*(hi|hello|hey|你好|嗨|早上好|晚上好|good\s+(morning|afternoon|evening))[\s!.?]*$",
    re.IGNORECASE,
)

_TRIVIAL_QUESTIONS_RE = re.compile(
    r"^\s*(what\s+(time|date|day)\s+is\s+it|"
    r"(现在)?(几点|什么时间|是几点)|"
    r"今天(是)?(星期几|几号)|"
    r"thanks?|thank\s+you|谢谢|多谢|"
    r"ok|okay|got\s+it|明白|好的|"
    r"yes|no|yeah|nope|是|否|不|对|"
    r"who\s+are\s+you|你是谁|你叫什么)[\s!.?]*$",
    re.IGNORECASE,
)

# Swarm cues — phrases that imply parallelisable independent slices.
_SWARM_CUES_RE = re.compile(
    r"\b(compare\s+\w+\s+(and|with|to|vs|against)\s+\w+|"
    r"summari[sz]e\s+each|"
    r"analy[sz]e\s+each|"
    r"for\s+each\s+(of\s+\w+\s+)?(following|these|file|item|ticket|option|module|function|module|repo)|"
    r"in\s+parallel|"
    r"side[\s-]by[\s-]side|"
    r"compare\s+these|"
    r"分别(总结|分析|检查|对比)|"
    r"对比[\s\S]{0,30}(和|与|及|跟)|"
    r"并行|"
    r"逐一(分析|总结|检查))",
    re.IGNORECASE,
)

# Explicit thinking cues — user wants reasoning shown.
_THINKING_CUES_RE = re.compile(
    r"\b(think\s+(out\s+loud|step[\s-]by[\s-]step|carefully|deeply)|"
    r"reason\s+(through|about)|"
    r"explain\s+your\s+(reasoning|thinking)|"
    r"walk\s+me\s+through\s+(your\s+)?(thinking|reasoning|logic)|"
    r"深入思考|逐步思考|仔细思考|"
    r"解释(你的)?(思路|推理|逻辑))",
    re.IGNORECASE,
)

# Tool-likely cues — anything that smells like it'll need tools.
_TOOL_CUES_RE = re.compile(
    r"\b(search|find|read|write|edit|create|delete|run|execute|install|"
    r"deploy|fetch|download|upload|test|verify|grep|file|directory|"
    r"folder|repo(sitory)?|commit|push|pull|merge|"
    r"查找|搜索|读取|写入|编辑|创建|删除|运行|执行|安装|部署|"
    r"下载|上传|测试|验证|文件|目录|文件夹|仓库|提交|推送)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class RouteDecision:
    """Result of routing a single user turn."""

    mode: RunMode
    reason: str
    # Whether the user explicitly forced this mode (UI toggle).
    forced: bool = False


class ModeRouter:
    """Pure-logic classifier — picks the cheapest mode that can serve
    the turn.

    Constructor params:

    * ``default_mode`` — fallback when no heuristic fires. Default
      ``agent`` (preserves current XMclaw behaviour).
    * ``enable_instant`` — if False, every turn routes to at least
      ``agent`` (no instant cost-saving). Default True.
    * ``enable_swarm`` — if False, swarm cues route to ``agent``
      instead. Default False (swarm is opt-in via config).
    * ``min_chars_for_agent`` — messages shorter than this AND
      matching instant cues stay instant. Default 200 — long
      messages even with chit-chat cues route to agent because the
      user probably attached context.
    """

    def __init__(
        self,
        *,
        default_mode: RunMode = RunMode.AGENT,
        enable_instant: bool = True,
        enable_swarm: bool = False,
        min_chars_for_agent: int = 200,
    ) -> None:
        self._default = default_mode
        self._enable_instant = bool(enable_instant)
        self._enable_swarm = bool(enable_swarm)
        self._min_chars_for_agent = max(50, int(min_chars_for_agent))

    def route(
        self,
        user_message: str,
        *,
        forced_mode: RunMode | str | None = None,
    ) -> RouteDecision:
        """Pick the run mode for ``user_message``.

        If ``forced_mode`` is provided (from the UI toggle) we honour
        it unconditionally — user knows their own intent better than
        our heuristic. Otherwise we apply the score logic.
        """
        if forced_mode is not None:
            mode = (
                forced_mode
                if isinstance(forced_mode, RunMode)
                else _coerce_mode(str(forced_mode))
            )
            if mode is not None:
                return RouteDecision(
                    mode=mode, reason="forced by caller", forced=True,
                )

        if not isinstance(user_message, str) or not user_message.strip():
            return RouteDecision(
                mode=self._default, reason="empty message — default",
            )

        text = user_message.strip()
        text_len = len(text)

        # Swarm cues win over everything else (most expensive but
        # most useful when the request really is fanout-shaped).
        if self._enable_swarm and _SWARM_CUES_RE.search(text):
            return RouteDecision(
                mode=RunMode.SWARM,
                reason="swarm cues detected (parallel/each/compare)",
            )

        # Explicit thinking cues.
        if _THINKING_CUES_RE.search(text):
            return RouteDecision(
                mode=RunMode.THINKING,
                reason="user requested visible reasoning",
            )

        # Instant for trivial questions / greetings — but only if the
        # message is short. Long messages with greeting prefixes
        # usually carry a real task in the body.
        if self._enable_instant and text_len < self._min_chars_for_agent:
            if _GREETING_RE.search(text) or _TRIVIAL_QUESTIONS_RE.search(text):
                if not _TOOL_CUES_RE.search(text):
                    return RouteDecision(
                        mode=RunMode.INSTANT,
                        reason="trivial greeting / factoid, no tool cues",
                    )

        return RouteDecision(
            mode=self._default,
            reason="default — no instant/swarm/thinking signal",
        )


def _coerce_mode(s: str) -> RunMode | None:
    s = s.strip().lower()
    for m in RunMode:
        if m.value == s:
            return m
    # Common aliases.
    if s in ("chat", "quick", "fast"):
        return RunMode.INSTANT
    if s in ("think", "cot", "reasoning"):
        return RunMode.THINKING
    if s in ("default", "tool", "tools"):
        return RunMode.AGENT
    if s in ("fanout", "parallel", "multi"):
        return RunMode.SWARM
    return None


__all__ = ["ModeRouter", "RouteDecision", "RunMode"]
