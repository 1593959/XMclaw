"""Detect when a user message is correcting a previously-captured fact.

Background
==========

Until 2026-05-26 the agent's only memory mutator was append: when the
user said "I'm not 张伟", the extractor wrote ``user is not 张伟`` as
a NEW fact next to the original ``user is 张伟``, leaving the system
prompt with a contradiction. The user surfaced this as: "他没有修剪
机制，纠正后又添加了一条，已经这种错误的他都没有能力修剪".

We now have ``memory_forget`` / ``memory_correct`` tools that
properly supersede the bad fact. Job of this module: notice when
the user message LOOKS like a correction so we can nudge the LLM to
call those tools instead of just appending.

Strategy: cheap regex patterns. We do NOT auto-fire the tools from
this layer — false-positive risk is high (idioms, role-play, jokes).
Instead, we return a short hint string that the agent_loop appends
to the user prompt as a ``[correction-detected]`` marker. The LLM
then decides whether to call ``memory_correct``/``memory_forget`` on
this turn. Soft nudge + hard tool surface beats either alone.
"""
from __future__ import annotations

import re
from typing import Final


# Each pattern fires a hint with a brief explanation of what kind of
# correction was detected. The hint gets appended to the user
# message so the LLM has BOTH the user's words and our perception
# that a correction is happening on the same turn — without having
# to thread a separate context entry.
_PATTERNS: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    # Chinese — explicit denial of identity / attribute.
    (
        re.compile(r"我不(?:是|叫|姓|住|属于)\s*[一-鿿\w]"),
        "user denied a previously-attributed identity / attribute",
    ),
    (
        re.compile(r"(?:你|您)(?:别|不要)(?:再)?(?:说|叫|认为|记)\s*我"),
        "user told the agent to stop attributing something",
    ),
    (
        re.compile(r"(?:不是|不叫)\s*[一-鿿\w]{1,20}(?:[,，。]|\s)*(?:是|叫|应该是)"),
        "user corrected with 'not X but Y' pattern",
    ),
    (
        re.compile(r"(?:你|你之前|之前的)?(?:说|记|认为|以为)?\s*错了"),
        "user explicitly flagged a prior claim as wrong",
    ),
    (
        re.compile(r"(?:忘[掉了]|删[掉了]|去掉|去除|清除)\s*(?:那|这|刚才|关于)"),
        "user asked agent to forget / remove a prior fact",
    ),
    # English — common phrasings.
    (
        re.compile(r"\b(?:i'?m|i\s+am)\s+not\s+[A-Z一-鿿]", re.IGNORECASE),
        "user denied a previously-attributed identity (en)",
    ),
    (
        re.compile(r"\b(?:actually|in\s+fact)\s+(?:i'?m|i\s+am|it'?s|my)", re.IGNORECASE),
        "user opened with an 'actually...' correction (en)",
    ),
    (
        re.compile(r"\b(?:that\s+(?:was|is)\s+wrong|you'?re\s+wrong\s+about)\b", re.IGNORECASE),
        "user explicitly flagged a prior claim as wrong (en)",
    ),
    (
        re.compile(r"\b(?:forget|disregard|ignore)\s+(?:that|what\s+i|the\s+previous)", re.IGNORECASE),
        "user asked agent to forget / disregard a prior fact (en)",
    ),
    (
        re.compile(r"\bi\s+(?:never|didn'?t)\s+(?:say|said|tell|told|mention(?:ed)?)\b", re.IGNORECASE),
        "user denied ever asserting something (en)",
    ),
)


def detect_correction(user_message: str) -> str | None:
    """If ``user_message`` looks like a correction, return a one-line
    hint the caller can splice into the prompt. Otherwise return
    None.

    The hint format is stable (tests pin it): one
    ``[correction-detected: <reason>]`` line. The caller appends it
    AFTER the original message body so the model reads the user's
    words first, then our annotation.
    """
    if not user_message or not user_message.strip():
        return None
    for rx, reason in _PATTERNS:
        if rx.search(user_message):
            return (
                f"\n\n[correction-detected: {reason}. If a related "
                f"fact is in memory, call ``memory_correct`` with the "
                f"corrected value, or ``memory_forget`` if the user "
                f"didn't supply a replacement. Do NOT just append a "
                f"contradiction with ``remember`` — that leaves the "
                f"wrong fact visible in future turns.]"
            )
    return None


__all__ = ["detect_correction"]
