"""Turn-context helpers for AgentLoop."""
import re as _re_mem
from typing import Any

_MEMORY_FENCE_BLOCK_RE = _re_mem.compile(
    r"<\s*memory-context\s*>[\s\S]*?</\s*memory-context\s*>",
    _re_mem.IGNORECASE,
)
_MEMORY_FENCE_TAG_RE = _re_mem.compile(
    r"</?\s*memory-context\s*>", _re_mem.IGNORECASE,
)
# B-93: strip the LLM-picked-files block from persisted history too —
# same reason as <memory-context>: the on-disk record should be what
# the user typed, not what the picker injected.
_MEMORY_FILES_BLOCK_RE = _re_mem.compile(
    r"<\s*recalled-memory-files\s*>[\s\S]*?</\s*recalled-memory-files\s*>",
    _re_mem.IGNORECASE,
)
_MEMORY_FILES_TAG_RE = _re_mem.compile(
    r"</?\s*recalled-memory-files\s*>", _re_mem.IGNORECASE,
)
_MEMORY_SYS_NOTE_RE = _re_mem.compile(
    r"\[\s*System\s+note:\s*The\s+following\s+is\s+recalled\s+memory\s+"
    r"context[^\]]*\]\s*",
    _re_mem.IGNORECASE,
)
# B-202: curriculum-edit hint also rides on the user message and
# must be stripped before persistence — otherwise the on-disk
# history records a "[System note: ...]" framing as if the user
# typed it, and worse, the next turn would re-recall the hint
# block as memory.
_CURRICULUM_HINT_BLOCK_RE = _re_mem.compile(
    r"<\s*curriculum-hint\s*>[\s\S]*?</\s*curriculum-hint\s*>",
    _re_mem.IGNORECASE,
)
_CURRICULUM_HINT_TAG_RE = _re_mem.compile(
    r"</?\s*curriculum-hint\s*>", _re_mem.IGNORECASE,
)
# Sprint 3 #6: same scrub pattern as curriculum-hint, applied before
# persistence so the strategy block doesn't leak into history (which
# would re-feed the agent's own retrieved strategies on every turn).
_CURRICULUM_STRATEGIES_BLOCK_RE = _re_mem.compile(
    r"<\s*curriculum-strategies\s*>[\s\S]*?</\s*curriculum-strategies\s*>",
    _re_mem.IGNORECASE,
)
_CURRICULUM_STRATEGIES_TAG_RE = _re_mem.compile(
    r"</?\s*curriculum-strategies\s*>", _re_mem.IGNORECASE,
)
# B-186: vague-continuation messages that should pin to the prior
# turn's topic rather than letting the LLM forage MEMORY.md for
# salient items. Curated short list, not a regex — these are the
# words that genuinely mean "keep going" rather than "do a new thing".
_CONTINUATION_TOKENS = frozenset({
    "继续", "接着", "下一步", "go on", "continue", "keep going",
    "go ahead", "proceed", "next", "and?", "so?", "ok",
})
def _is_vague_continuation(text: str) -> bool:
    """Short user message that reads as 'pick up where you left off'
    rather than introducing new work."""
    if not text:
        return False
    s = text.strip().lower()
    if not s:
        return False
    if len(s) > 12:
        return False
    return s in _CONTINUATION_TOKENS
def _prior_ended_without_synthesis(prior: list[Any]) -> bool:
    """True when the most recent assistant message in ``prior`` is a
    tool-calling turn with empty (or whitespace-only) text content.

    Walks back from the end skipping ``tool`` (tool-result) messages
    until it hits the assistant turn that originated them. That turn's
    content tells us whether the agent had time to summarise before
    the previous turn ended. If ``content`` is empty, the agent never
    closed the loop — the next user message should pin to that work.
    """
    for m in reversed(prior):
        role = getattr(m, "role", None)
        content = getattr(m, "content", "") or ""
        if role == "tool":
            continue
        if role == "assistant":
            if isinstance(content, list):
                # Some providers stream content as a list of
                # text/tool_use blocks. Concatenate text parts.
                text = "".join(
                    getattr(part, "text", "") or
                    (part.get("text", "") if isinstance(part, dict) else "")
                    for part in content
                )
            else:
                text = str(content)
            return not text.strip()
        # User / system message hit before assistant: prior assistant
        # already finished cleanly, no anchor needed.
        return False
    return False
def _continuation_anchor(prior: list[Any], user_message: str) -> str:
    """If the new user message is a vague continuation AND the prior
    assistant turn never synthesised a final answer, prepend a
    routing hint that tells the LLM to keep working on the same
    topic — not to forage MEMORY.md / system prompt for new tasks.
    Otherwise empty string (no-op).

    Frame matches the existing ``[System note: ...]`` style used
    by memory injection so the persistence sanitiser already
    strips it before it lands in long-term history.
    """
    if not _is_vague_continuation(user_message):
        return ""
    if not _prior_ended_without_synthesis(prior):
        return ""
    return (
        "[System note: your previous turn made tool calls but did "
        "NOT produce a final synthesis (LLM provider may have "
        "hung, or you ran out of hops). The user's '"
        + user_message.strip()
        + "' means CONTINUE THAT INVESTIGATION — read the tool "
        "results in your context above and produce the answer the "
        "user originally asked for. Do NOT pick up unrelated "
        "tasks from MEMORY.md or persona — those are background "
        "context, not active TODOs.]\n\n"
    )
# B-202: frustration / pushback markers in the user's current message.
# When detected we inject a one-shot system hint suggesting the agent
# call ``propose_curriculum_edit`` after resolving the immediate issue.
# Background:
#   probe_b200_v2 round B observed the agent identifying a perfect
#   curriculum-edit case (self_review_recent scenario) but never firing
#   the tool — the LLM forgets the existence of dormant evolution tools
#   when no contextual cue appears. Mirrors how memory_ctx_block fixed
#   "agent ignores past sessions" by surfacing relevant items at the
#   right moment.
#
# Coverage:
#   - Chinese: 为什么 (why), 别 / 不要 (don't), 你看看 (look at this),
#     不是这样 (that's not it), 错了 (wrong), 我没问 (I didn't ask),
#     我之前说过 (I already told you), 我都说了 (I already said),
#     你不要 (you shouldn't), 太离谱 (too absurd)
#   - English: why are you, i didn't ask, that's not, that is not,
#     that's wrong, you keep, you always, i told you, you don't listen,
#     stop doing
#
# Bias: false-positive on "为什么" is fine — it just makes the agent
# slightly more likely to crystallise a lesson. False-negative is
# costly (the original bug). Matched on lowercased text + raw text
# for Chinese.
_FRUSTRATION_MARKERS_EN = (
    "why are you",
    "why do you",
    "why did you",
    "i didn't ask",
    "i did not ask",
    "that's not it",
    "that is not it",
    "that's not what",
    "that is not what",
    "that's wrong",
    "you keep",
    "you always",
    "i told you",
    "i already told you",
    "i already said",
    "you don't listen",
    "you do not listen",
    "stop doing",
    "you shouldn't",
    "you should not",
)

_FRUSTRATION_MARKERS_CN = (
    "为什么", "别", "不要", "你看看", "不是这样", "错了",
    "我没问", "我之前说过", "我都说了", "你不要", "太离谱",
    "你怎么", "你又", "我说过", "你听不懂", "听不懂",
)
def _detect_frustration_signal(text: str) -> bool:
    """Heuristic: does the current user message read as pushback /
    frustration / correction?

    Used to decide whether to inject a one-shot system hint about
    ``propose_curriculum_edit``. False-positive cost is low (one
    extra hint string in one user message), false-negative cost is
    high (lost crystallisation opportunity), so the markers err on
    the inclusive side.
    """
    if not text:
        return False
    s = text.strip()
    if not s:
        return False
    low = s.lower()
    if any(m in low for m in _FRUSTRATION_MARKERS_EN):
        return True
    if any(m in s for m in _FRUSTRATION_MARKERS_CN):
        return True
    return False
def _sanitize_memory_context(text: str) -> str:
    """Remove ``<memory-context>...</memory-context>`` blocks and the
    "[System note: ...]" framing from a string. Used before persisting
    history so the on-disk record reflects what the user actually
    said, not the prefetched recall block."""
    if not text:
        return text
    out = _MEMORY_FENCE_BLOCK_RE.sub("", text)
    out = _MEMORY_FILES_BLOCK_RE.sub("", out)
    # B-202: drop curriculum-hint envelope from history too.
    out = _CURRICULUM_HINT_BLOCK_RE.sub("", out)
    # Sprint 3 #6: drop curriculum-strategies block from history too.
    # Without this, the bank-retrieved strategies would be re-fed to
    # the agent on every subsequent turn as if the user had typed
    # them, AND the next distill pass would see them as user content.
    out = _CURRICULUM_STRATEGIES_BLOCK_RE.sub("", out)
    # Catch orphaned tags (e.g. block was malformed and only one tag
    # made it through) and orphaned system notes.
    out = _MEMORY_FENCE_TAG_RE.sub("", out)
    out = _MEMORY_FILES_TAG_RE.sub("", out)
    out = _CURRICULUM_HINT_TAG_RE.sub("", out)
    out = _CURRICULUM_STRATEGIES_TAG_RE.sub("", out)
    out = _MEMORY_SYS_NOTE_RE.sub("", out)
    return out.rstrip()
