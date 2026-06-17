"""InlineThinkStreamFilter — strip inline <think>…</think> from a streaming
text channel without leaking the literal tag into the visible bubble.

2026-06-17 regression (user): Kimi K2.6 /coding emits reasoning then a LONE
</think> (no opener). The streaming filter only looked for the opener, so the
reasoning + the literal </think> leaked into the chat bubble while ALSO
appearing in the 思考过程 channel.
"""
from __future__ import annotations

from xmclaw.providers.llm.streaming_utils import InlineThinkStreamFilter


def _drain(chunks: list[str], hold_leading: bool = False) -> tuple[str, str]:
    f = InlineThinkStreamFilter(hold_leading_reasoning=hold_leading)
    vis_parts: list[str] = []
    thk_parts: list[str] = []
    for c in chunks:
        v, t = f.feed(c)
        vis_parts.append(v)
        thk_parts.append(t)
    v, t = f.flush()
    vis_parts.append(v)
    thk_parts.append(t)
    return "".join(vis_parts), "".join(thk_parts)


def test_plain_text_passes_through() -> None:
    vis, thk = _drain(["你好", "，世界"])
    assert vis == "你好，世界"
    assert thk == ""


def test_balanced_block() -> None:
    vis, thk = _drain(["<think>reasoning</think>answer"])
    assert vis == "answer"
    assert thk == "reasoning"


def test_bare_close_no_opener_routes_to_thinking() -> None:
    # The exact bug shape: reasoning, then a lone </think>, then the answer.
    vis, thk = _drain(["让我先读取对话历史确认上下文。</think>🐱 你是指…"])
    assert "</think>" not in vis  # literal tag must NOT leak
    assert vis == "🐱 你是指…"
    assert thk == "让我先读取对话历史确认上下文。"


def test_open_tag_split_across_chunks() -> None:
    vis, thk = _drain(["<thi", "nk>r</think>a"])
    assert vis == "a"
    assert thk == "r"


def test_default_mode_drops_bare_close_tag_even_if_reasoning_streamed() -> None:
    # Without hold_leading, leading reasoning streamed in prior chunks DOES
    # leak (streaming can't un-emit) — but the literal </think> tag must never
    # appear. This is the floor guarantee for non-Kimi endpoints.
    vis, _ = _drain(["让我", "想想", "。", "</think>", "好的"])
    assert "</think>" not in vis
    assert vis.endswith("好的")


# ── hold_leading mode (Kimi /coding): reasoning fully held off the bubble ──


def test_hold_leading_routes_token_streamed_reasoning_to_thinking() -> None:
    # The real Kimi shape: reasoning streamed token-by-token (no opener), then
    # a lone </think>, then the answer. hold_leading keeps the whole reasoning
    # OUT of the visible bubble (not just the tag).
    vis, thk = _drain(["让我", "想想", "。", "</think>", "好的"], hold_leading=True)
    assert vis == "好的"
    assert thk == "让我想想。"


def test_hold_leading_exact_screenshot_shape() -> None:
    vis, thk = _drain(
        ["让我先读取对话历史确认上下文。", "</think>", "🐱 你是指…"],
        hold_leading=True,
    )
    assert vis == "🐱 你是指…"
    assert "</think>" not in vis
    assert thk == "让我先读取对话历史确认上下文。"


def test_hold_leading_plain_answer_with_no_reasoning_still_shows() -> None:
    # A response with NO reasoning (no </think>) must still surface as visible
    # (flushed at end), never swallowed into the thinking channel.
    vis, thk = _drain(["直接", "回答你"], hold_leading=True)
    assert vis == "直接回答你"
    assert thk == ""


def test_hold_leading_real_open_tag_keeps_leading_visible() -> None:
    # If real visible text precedes a proper <think> block, it stays visible.
    vis, thk = _drain(["答案是 ", "<think>", "推理", "</think>", " 完"], hold_leading=True)
    assert vis == "答案是  完"
    assert thk == "推理"
