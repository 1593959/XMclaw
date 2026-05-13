"""ModelTierRouter — pick the cheapest LLM tier that can serve a turn.

Sprint 0 of the Jarvis roadmap. Companion to ``ModeRouter`` (Batch D)
which decides instant / thinking / agent / swarm. THIS module decides
which TIER of model to use within the chosen mode:

  * ``"fast"``     — single-shot chitchat / commands (Qwen 7B,
                    Haiku, GPT-4o-mini). Latency target < 2s.
  * ``"balanced"`` — default for most turns (Sonnet, GPT-4o,
                    Kimi K2.6). Latency 5-15s.
  * ``"strong"``   — long-chain reasoning (Opus 4.7, GPT-4.1).
  * ``"vision"``   — vision-grounded GUI work (Sonnet 4.6, GPT-4o,
                    UI-TARS). Required when the user attached an
                    image or the agent needs to read a screenshot.

Goals
=====

* **Speed** — no LLM call inside the router; pure regex + signal
  inspection. Adds < 1 ms per turn.
* **Honest** — when in doubt, escalate (cheaper to over-spend on
  Kimi K2.6 than to bonk a complex query into Qwen 7B).
* **Graceful fallback** — if requested tier is unconfigured in the
  registry, return a fallback chain the caller can walk.

This is the FOUNDATION for proactive / real-time work: without sub-
second response on chitchat we can't have an always-on assistant.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


# ── Signals ────────────────────────────────────────────────────────


# Single-line greetings / acks / yes-no — always fast tier.
_TRIVIAL_RE = re.compile(
    r"^\s*("
    r"hi|hello|hey|你好|嗨|早安|晚安|早上好|晚上好|"
    r"thanks?|thank\s+you|谢谢|多谢|"
    r"ok|okay|got\s+it|明白|好的|"
    r"yes|no|yeah|nope|是|否|对|不|"
    r"几点|什么时间|今天(是)?(几号|星期几)|"
    r"what\s+(time|date|day)\s+is\s+it|"
    r"who\s+are\s+you|你是谁|"
    r"good\s+(morning|afternoon|evening)|"
    r"how\s+are\s+you|你好吗"
    r")[\s!.?，。、]*$",
    re.IGNORECASE,
)

# Multi-step / reasoning markers — push to strong tier.
_STRONG_MARKERS_RE = re.compile(
    r"\b(first\b[\s\S]{1,200}?then|"
    r"step\s*1\b|"
    r"analy[sz]e|"
    r"compare\s+\w+\s+(and|with|to|vs|against)|"
    r"design|architect|refactor|"
    r"prove|derive|explain\s+in\s+detail|"
    r"plan|strategy|roadmap|"
    r"全面|深入|彻底|完整|"
    r"首先[\s\S]{1,200}?(然后|再|接着|最后)|"
    r"分析|对比|设计|架构|重构|"
    r"证明|推导|详细解释)",
    re.IGNORECASE,
)

# English tool cues — use \b for word boundary (works on ASCII).
_TOOL_CUES_EN_RE = re.compile(
    r"\b(search|find|read|write|edit|create|delete|run|execute|"
    r"install|deploy|fetch|download|upload|test|verify|grep|"
    r"file|directory|folder|repo(sitory)?|commit|push|pull|merge)\b",
    re.IGNORECASE,
)

# Chinese tool cues — no \b (Python \b doesn't work on CJK).
_TOOL_CUES_CN_RE = re.compile(
    r"(截图|点击|打开|关闭|发送|"
    r"查找|搜索|读取|写入|编辑|创建|删除|运行|执行|安装|部署|"
    r"下载|上传|测试|验证|文件|目录|文件夹|仓库|提交|推送)",
)


def _has_tool_cue(text: str) -> bool:
    if not text:
        return False
    return bool(_TOOL_CUES_EN_RE.search(text) or _TOOL_CUES_CN_RE.search(text))


@dataclass(frozen=True, slots=True)
class TierDecision:
    """Result of routing one turn to a tier.

    ``tier`` is the primary choice. ``fallback_chain`` is the ordered
    list of tiers to try if the primary isn't configured in the
    registry — caller passes this to ``LLMRegistry.pick_by_tier``.
    """

    tier: str
    fallback_chain: tuple[str, ...]
    reason: str
    # Diagnostic flags so observers / Analytics can see why we chose.
    has_images: bool = False
    has_tool_cues: bool = False
    is_trivial: bool = False
    is_complex: bool = False


class ModelTierRouter:
    """Decide which LLM tier serves a turn.

    Constructor params:

    * ``vision_when_images`` — if True (default), any image attachment
      auto-routes to vision tier. Set False if the user manually
      pinned a tier via UI.
    * ``trivial_max_chars`` — short-circuit length for the trivial
      classifier. Default 80.
    """

    def __init__(
        self,
        *,
        vision_when_images: bool = True,
        trivial_max_chars: int = 80,
    ) -> None:
        self._vision_when_images = bool(vision_when_images)
        self._trivial_max_chars = max(20, int(trivial_max_chars))

    def route(
        self,
        user_message: str,
        *,
        has_images: bool = False,
        forced_tier: str | None = None,
    ) -> TierDecision:
        """Pick a tier.

        ``forced_tier`` from the UI / config overrides everything.
        """
        if isinstance(forced_tier, str) and forced_tier.strip():
            t = forced_tier.strip().lower()
            if t in ("fast", "balanced", "strong", "vision"):
                return TierDecision(
                    tier=t,
                    fallback_chain=self._default_fallback(t),
                    reason="forced by caller",
                    has_images=has_images,
                )

        text = (user_message or "").strip() if isinstance(user_message, str) else ""

        # 0) Empty / non-string → balanced default. No need to run
        # regexes against nothing — and avoids classifying "" as
        # trivial chitchat (which would route empty/None to fast and
        # surprise callers).
        if not text:
            return TierDecision(
                tier="balanced",
                fallback_chain=("strong", "fast"),
                reason="empty message — default",
            )

        # 1) Vision attachment → vision tier (highest priority).
        if has_images and self._vision_when_images:
            return TierDecision(
                tier="vision",
                fallback_chain=("balanced", "strong"),
                reason="user attached image(s)",
                has_images=True,
                has_tool_cues=_has_tool_cue(text),
            )

        # 2) Strong-tier reasoning markers.
        if _STRONG_MARKERS_RE.search(text) or len(text) >= 400:
            return TierDecision(
                tier="strong",
                fallback_chain=("balanced",),
                reason="multi-step / long reasoning markers",
                is_complex=True,
                has_tool_cues=_has_tool_cue(text),
            )

        # 3) Tool cues — balanced minimum (small models fumble tool use).
        has_tools = _has_tool_cue(text)
        if has_tools:
            return TierDecision(
                tier="balanced",
                fallback_chain=("strong",),
                reason="tool-use cues — needs balanced/strong",
                has_tool_cues=True,
            )

        # 4) Trivial chitchat → fast tier. Require either the regex
        # to match OR a very short message (≤ 12 chars). The bare
        # length-<12 fallback catches CJK short messages the regex
        # may miss (it's tuned for explicit greeting phrases).
        if (
            len(text) < self._trivial_max_chars
            and (_TRIVIAL_RE.search(text) or len(text) < 12)
        ):
            return TierDecision(
                tier="fast",
                fallback_chain=("balanced",),
                reason="trivial chitchat / ack",
                is_trivial=True,
            )

        # 5) Default — balanced.
        return TierDecision(
            tier="balanced",
            fallback_chain=("strong", "fast"),
            reason="default — no specific signal",
        )

    @staticmethod
    def _default_fallback(tier: str) -> tuple[str, ...]:
        """Sensible fallback chains per tier."""
        return {
            "fast": ("balanced",),
            "balanced": ("strong", "fast"),
            "strong": ("balanced",),
            "vision": ("balanced", "strong"),
        }.get(tier, ("balanced",))


__all__ = ["ModelTierRouter", "TierDecision"]
