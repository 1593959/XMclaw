"""Sprint 2 Wave 12 — Feishu image inbound tests.

We don't exercise the full lark-oapi WS path (would need a mocked
client); instead test the pure helper functions and the
ChannelDispatcher → AgentLoop image pass-through.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from xmclaw.providers.channel.base import (
    ChannelTarget,
    InboundMessage,
)
from xmclaw.providers.channel.feishu.adapter import (
    _flatten_post,
    _sniff_image_ext,
)


# ── _sniff_image_ext ─────────────────────────────────────────────


def test_sniff_png():
    data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    assert _sniff_image_ext(data) == ".png"


def test_sniff_jpeg():
    data = b"\xff\xd8\xff\xe0" + b"\x00" * 16
    assert _sniff_image_ext(data) == ".jpg"


def test_sniff_gif():
    assert _sniff_image_ext(b"GIF89a" + b"\x00" * 10) == ".gif"
    assert _sniff_image_ext(b"GIF87a" + b"\x00" * 10) == ".gif"


def test_sniff_webp():
    data = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 16
    assert _sniff_image_ext(data) == ".webp"


def test_sniff_bmp():
    assert _sniff_image_ext(b"BM" + b"\x00" * 30) == ".bmp"


def test_sniff_unknown_returns_none():
    assert _sniff_image_ext(b"garbage-data") is None
    assert _sniff_image_ext(b"") is None
    assert _sniff_image_ext(b"x") is None


# ── _flatten_post ────────────────────────────────────────────────


def test_flatten_post_extracts_text_spans():
    obj = {
        "title": "周报",
        "content": [
            [
                {"tag": "text", "text": "完成了 "},
                {"tag": "text", "text": "Wave 12"},
            ],
            [
                {"tag": "text", "text": "另起一行"},
            ],
        ],
    }
    text, images = _flatten_post(obj)
    assert "周报" in text
    assert "完成了" in text
    assert "Wave 12" in text
    assert "另起一行" in text
    assert images == []


def test_flatten_post_extracts_image_keys():
    obj = {
        "title": "",
        "content": [
            [
                {"tag": "text", "text": "看这个："},
                {"tag": "img", "image_key": "img_v3_aaa"},
            ],
            [
                {"tag": "img", "image_key": "img_v3_bbb"},
            ],
        ],
    }
    text, images = _flatten_post(obj)
    assert text == "看这个："
    assert images == ["img_v3_aaa", "img_v3_bbb"]


def test_flatten_post_handles_links():
    obj = {
        "title": "",
        "content": [
            [{"tag": "a", "text": "点这里", "href": "https://example.com"}],
        ],
    }
    text, _ = _flatten_post(obj)
    assert "点这里" in text


def test_flatten_post_tolerates_garbage():
    text, images = _flatten_post({})
    assert text == ""
    assert images == []
    text, images = _flatten_post({"content": "not-a-list"})
    assert images == []
    text, images = _flatten_post({"content": [["not-a-dict"]]})
    assert images == []


# ── ChannelDispatcher passes raw.images through ──────────────────


@pytest.fixture
def fake_agent() -> MagicMock:
    agent = MagicMock()
    agent.run_turn = AsyncMock()
    agent._histories = {}
    return agent


@pytest.mark.asyncio
async def test_dispatcher_passes_images_to_run_turn(
    fake_agent: MagicMock,
) -> None:
    from xmclaw.daemon.channel_dispatcher import ChannelDispatcher

    dispatcher = ChannelDispatcher(fake_agent, ack_delay_s=10.0)

    # Need an adapter registered so _handle_one finds it for ack/send
    fake_adapter = MagicMock()
    fake_adapter.name = "feishu"
    fake_adapter.send = AsyncMock()
    fake_adapter.subscribe = lambda h: None
    dispatcher._adapters.append(fake_adapter)

    inbound = InboundMessage(
        target=ChannelTarget(channel="feishu", ref="oc_chat"),
        user_ref="ou_user1",
        content="看一下这张图。",
        raw={
            "message_id": "om_msg",
            "msg_type": "image",
            "images": ["/tmp/foo.png", "/tmp/bar.jpg"],
        },
    )
    await dispatcher._on_inbound(inbound)

    # Give the ack-cancel cleanup a moment
    await asyncio.sleep(0)

    fake_agent.run_turn.assert_awaited_once()
    args, kwargs = fake_agent.run_turn.call_args
    assert kwargs.get("user_images") == ("/tmp/foo.png", "/tmp/bar.jpg")


@pytest.mark.asyncio
async def test_dispatcher_no_images_passes_none(
    fake_agent: MagicMock,
) -> None:
    from xmclaw.daemon.channel_dispatcher import ChannelDispatcher

    dispatcher = ChannelDispatcher(fake_agent, ack_delay_s=10.0)
    fake_adapter = MagicMock()
    fake_adapter.name = "feishu"
    fake_adapter.send = AsyncMock()
    fake_adapter.subscribe = lambda h: None
    dispatcher._adapters.append(fake_adapter)

    inbound = InboundMessage(
        target=ChannelTarget(channel="feishu", ref="oc_chat"),
        user_ref="ou_user1",
        content="纯文本消息",
        raw={"message_id": "om_msg", "msg_type": "text"},
    )
    await dispatcher._on_inbound(inbound)

    fake_agent.run_turn.assert_awaited_once()
    _, kwargs = fake_agent.run_turn.call_args
    assert kwargs.get("user_images") is None


@pytest.mark.asyncio
async def test_dispatcher_filters_non_string_image_entries(
    fake_agent: MagicMock,
) -> None:
    from xmclaw.daemon.channel_dispatcher import ChannelDispatcher

    dispatcher = ChannelDispatcher(fake_agent, ack_delay_s=10.0)
    fake_adapter = MagicMock()
    fake_adapter.name = "feishu"
    fake_adapter.send = AsyncMock()
    fake_adapter.subscribe = lambda h: None
    dispatcher._adapters.append(fake_adapter)

    # Garbage entries should be silently filtered.
    inbound = InboundMessage(
        target=ChannelTarget(channel="feishu", ref="oc_chat"),
        user_ref="ou_user1",
        content="x",
        raw={
            "message_id": "om_msg",
            "images": ["/tmp/good.png", 12345, None, "/tmp/also_good.jpg"],
        },
    )
    await dispatcher._on_inbound(inbound)

    fake_agent.run_turn.assert_awaited_once()
    _, kwargs = fake_agent.run_turn.call_args
    assert kwargs.get("user_images") == (
        "/tmp/good.png", "/tmp/also_good.jpg",
    )
