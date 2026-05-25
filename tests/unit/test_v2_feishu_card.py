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

import asyncio
import json
from unittest.mock import AsyncMock, patch

from xmclaw.providers.channel.feishu.adapter import (
    _CARD_MAX_CHARS,
    _build_lark_markdown_card,
    _extract_markdown_tables,
    _markdown_table_to_lark_table_element,
    _looks_like_markdown,
    FeishuAdapter,
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


# ── _extract_markdown_tables ─────────────────────────────────────


def test_extracts_simple_markdown_table() -> None:
    text = "before\n| A | B |\n|---|---|\n| 1 | 2 |\nafter"
    tables = _extract_markdown_tables(text)
    assert len(tables) == 1
    assert "| A | B |" in tables[0]["table_text"]
    assert "| 1 | 2 |" in tables[0]["table_text"]


def test_extracts_multiple_tables() -> None:
    text = (
        "| A | B |\n|---|---|\n| 1 | 2 |\n\n"
        "middle\n\n"
        "| X | Y |\n|---|---|\n| 3 | 4 |"
    )
    tables = _extract_markdown_tables(text)
    assert len(tables) == 2


def test_skips_non_table_pipe_text() -> None:
    text = "some | pipe | in a sentence\nand another line"
    tables = _extract_markdown_tables(text)
    assert len(tables) == 0


def test_skips_single_pipe_line() -> None:
    text = "| just one line with pipes |\nnext line"
    tables = _extract_markdown_tables(text)
    assert len(tables) == 0


# ── _markdown_table_to_lark_table_element ────────────────────────


def test_converts_md_table_to_lark_table_element() -> None:
    md = "| 方向 | 利润率 |\n|------|--------|\n| 工具 | 80%+ |"
    el = _markdown_table_to_lark_table_element(md)
    assert el["tag"] == "table"
    assert el["border"] is True
    assert len(el["columns"]) == 2
    assert el["columns"][0]["name"] == "方向"
    assert el["columns"][1]["name"] == "利润率"
    assert len(el["rows"]) == 1
    assert el["rows"][0]["col0"] == "工具"
    assert el["rows"][0]["col1"] == "80%+"
    assert el["header_style"]["background_style"] == "grey"


def test_table_with_emoji_cells() -> None:
    md = "| 评级 | 匹配度 |\n|------|--------|\n| ⭐⭐⭐⭐⭐ | 完美匹配 |"
    el = _markdown_table_to_lark_table_element(md)
    assert el["tag"] == "table"
    assert el["rows"][0]["col0"] == "⭐⭐⭐⭐⭐"
    assert el["rows"][0]["col1"] == "完美匹配"


def test_table_fallback_on_invalid_input() -> None:
    el = _markdown_table_to_lark_table_element("not a table")
    assert el["tag"] == "markdown"


def test_table_fallback_on_single_line() -> None:
    el = _markdown_table_to_lark_table_element("| only header |")
    assert el["tag"] == "markdown"


# ── FeishuAdapter tool-finished image caching ────────────────────


def test_tool_finished_caches_image_side_effects() -> None:
    adapter = FeishuAdapter({"app_id": "test", "app_secret": "test"})
    asyncio.run(adapter._handle_tool_finished("feishu:chat1", {
        "name": "browser_screenshot",
        "call_id": "abc",
        "ok": True,
        "expected_side_effects": [
            "C:\\Users\\test\\.xmclaw\\v2\\screenshots\\shot_123.png",
            "some_log.txt",
        ],
    }))
    assert adapter._session_tool_images.get("feishu:chat1") == [
        "C:\\Users\\test\\.xmclaw\\v2\\screenshots\\shot_123.png",
    ]


def test_tool_finished_skips_non_image_side_effects() -> None:
    adapter = FeishuAdapter({"app_id": "test", "app_secret": "test"})
    asyncio.run(adapter._handle_tool_finished("feishu:chat1", {
        "name": "browser_click",
        "call_id": "abc",
        "ok": True,
        "expected_side_effects": ["some_log.txt", "data.json"],
    }))
    assert "feishu:chat1" not in adapter._session_tool_images


def test_tool_finished_does_not_cache_on_failure() -> None:
    adapter = FeishuAdapter({"app_id": "test", "app_secret": "test"})
    asyncio.run(adapter._handle_tool_finished("feishu:chat1", {
        "name": "browser_screenshot",
        "call_id": "abc",
        "ok": False,
        "expected_side_effects": ["C:\\Users\\test\\shot.png"],
    }))
    assert "feishu:chat1" not in adapter._session_tool_images


# ── FeishuAdapter LLM_RESPONSE narration relay ───────────────────


async def test_llm_response_sends_mid_turn_narration() -> None:
    adapter = FeishuAdapter({"app_id": "test", "app_secret": "test"})
    with patch.object(adapter, "_send_text_to_chat", new_callable=AsyncMock) as mock_send:
        await adapter._handle_llm_response("feishu:chat1", {
            "content": "让我跳官网看看",
            "tool_calls_count": 2,
            "ok": True,
        })
        mock_send.assert_awaited_once_with("chat1", "让我跳官网看看")


async def test_llm_response_skips_terminal_reply() -> None:
    adapter = FeishuAdapter({"app_id": "test", "app_secret": "test"})
    with patch.object(adapter, "_send_text_to_chat", new_callable=AsyncMock) as mock_send:
        await adapter._handle_llm_response("feishu:chat1", {
            "content": "最终答案",
            "tool_calls_count": 0,
            "ok": True,
        })
        mock_send.assert_not_awaited()


async def test_llm_response_skips_empty_content() -> None:
    adapter = FeishuAdapter({"app_id": "test", "app_secret": "test"})
    with patch.object(adapter, "_send_text_to_chat", new_callable=AsyncMock) as mock_send:
        await adapter._handle_llm_response("feishu:chat1", {
            "content": "   ",
            "tool_calls_count": 1,
            "ok": True,
        })
        mock_send.assert_not_awaited()
