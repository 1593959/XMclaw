"""B-209: FeishuAdapter markdown→card routing.

Pre-B-209: every outbound message went out as ``msg_type=text``,
which feishu renders as raw chars — `**bold**` shows as literal
asterisks, `# heading` shows the hash. The agent's prose looked
ugly on feishu compared to web UI.

Fix: detect markdown in ``OutboundMessage.content``; when present
and under the 24k char card cap, send as ``msg_type=interactive``
with a ``markdown`` element. Plain text replies stay as ``text``
because cards add chrome that's overkill for "OK 收到" replies.

Tests pin the helper functions directly — the actual send() path
goes through the lark SDK (network) which is integration-test
territory. The helpers ARE the routing decision; pinning them
gives us all the coverage we need without lark-oapi mocks.
"""
from __future__ import annotations

import json

from xmclaw.providers.channel.feishu.adapter import (
    _CARD_MAX_CHARS,
    _build_lark_markdown_card,
    _looks_like_markdown,
)


# ── _looks_like_markdown ──────────────────────────────────────────


def test_md_detects_bold() -> None:
    assert _looks_like_markdown("This is **bold** text")
    assert _looks_like_markdown("**单独 bold 行**")


def test_md_detects_inline_code() -> None:
    assert _looks_like_markdown("Use `xmclaw start` to launch")


def test_md_detects_fenced_code() -> None:
    assert _looks_like_markdown("```python\nprint('hi')\n```")


def test_md_detects_heading_at_line_start() -> None:
    assert _looks_like_markdown("## 诊断结果\n\n看到了 X")
    assert _looks_like_markdown("# Title\nbody")


def test_md_detects_heading_only_at_line_start_not_inline() -> None:
    """`#` mid-line is a hashtag, not a heading. Should NOT trigger."""
    assert not _looks_like_markdown("Look at the #hashtag in this text")


def test_md_detects_bullet_list() -> None:
    assert _looks_like_markdown("Findings:\n- bug A\n- bug B")
    assert _looks_like_markdown("- single bullet")


def test_md_detects_ordered_list() -> None:
    assert _looks_like_markdown("Steps:\n1. do X\n2. do Y")


def test_md_detects_quote() -> None:
    assert _looks_like_markdown("> 引用一段话")


def test_md_detects_link() -> None:
    assert _looks_like_markdown("see [docs](https://example.com)")


def test_md_detects_table_row() -> None:
    assert _looks_like_markdown("| col | val |\n| --- | --- |")


def test_md_does_not_fire_on_plain_chinese() -> None:
    assert not _looks_like_markdown("好的哥,我已经收到了你的消息 🌸")
    assert not _looks_like_markdown("OK 收到")
    assert not _looks_like_markdown("")
    # A single asterisk is not bold (needs paired ** ... **).
    assert not _looks_like_markdown("multiplication sign: 3 * 4")


def test_md_does_not_fire_on_natural_punctuation() -> None:
    """Defensive: trailing dash on a line shouldn't read as bullet."""
    assert not _looks_like_markdown("总结一下 -")
    # Period after number is fine in prose: "Step 1." with no space-X.
    assert not _looks_like_markdown("Refer to step 1.")


def test_md_card_cap_threshold_exists() -> None:
    """B-209 caps card size; very long content falls back to text.
    Pin the cap so a future bump above ~30k (lark's known server
    limit) gets caught."""
    assert 10_000 <= _CARD_MAX_CHARS <= 30_000


# ── _build_lark_markdown_card ────────────────────────────────────


def test_card_payload_shape_basic() -> None:
    card = _build_lark_markdown_card("**hello**")
    assert "elements" in card
    assert len(card["elements"]) == 1
    el = card["elements"][0]
    assert el["tag"] == "markdown"
    assert el["content"] == "**hello**"


def test_card_payload_has_wide_screen_mode() -> None:
    """Wide-screen helps tabular tool output render readably."""
    card = _build_lark_markdown_card("any")
    assert card.get("config", {}).get("wide_screen_mode") is True


def test_card_payload_serialises_to_lark_format() -> None:
    """The adapter passes ``json.dumps(card)`` to lark's content
    field. The dict must round-trip cleanly."""
    card = _build_lark_markdown_card("# 标题\n\n- 项目 1\n- 项目 2")
    s = json.dumps(card, ensure_ascii=False)
    back = json.loads(s)
    assert back == card
    # No bytes-y fallback that would garble Chinese.
    assert "标题" in s
