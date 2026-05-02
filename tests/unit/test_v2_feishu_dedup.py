"""B-196: FeishuAdapter dedup unit test.

Lark's WS uses at-least-once delivery — same message_id can land twice
on reconnect. Without dedup the agent runs the turn N times and the
user sees duplicate replies (the screenshot complaint that triggered
this fix). The adapter keeps an LRU of seen message_ids and skips
re-deliveries.

We don't import lark here — the test feeds a duck-typed event into
``_handle_event`` directly, the same surface lark's dispatcher hits.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from xmclaw.providers.channel.feishu.adapter import FeishuAdapter


def _make_event(
    *,
    message_id: str,
    text: str = "你好",
    chat_id: str = "oc_chat_1",
    user_id: str = "ou_user_1",
    msg_type: str = "text",
):
    """Duck-type a lark P2ImMessageReceiveV1 event. Only the attribute
    paths the adapter actually reads need to exist; everything else
    can be missing and getattr's default kicks in."""
    return SimpleNamespace(
        event=SimpleNamespace(
            message=SimpleNamespace(
                message_id=message_id,
                message_type=msg_type,
                chat_id=chat_id,
                content=json.dumps({"text": text}, ensure_ascii=False),
            ),
            sender=SimpleNamespace(
                sender_id=SimpleNamespace(open_id=user_id, user_id=""),
            ),
        )
    )


def _build_adapter() -> FeishuAdapter:
    """Construct without start() — we only exercise _handle_event."""
    return FeishuAdapter({"app_id": "cli_test", "app_secret": "x"})


@pytest.mark.asyncio
async def test_duplicate_message_id_skipped() -> None:
    """B-196: same message_id delivered twice → handler called once."""
    adapter = _build_adapter()
    inbox: list[str] = []

    async def _handler(msg) -> None:  # type: ignore[no-untyped-def]
        inbox.append(msg.content)

    adapter.subscribe(_handler)

    ev = _make_event(message_id="om_dup_1", text="hello")
    await adapter._handle_event(ev)
    await adapter._handle_event(ev)  # exact same event again

    assert inbox == ["hello"], (
        f"expected one delivery, got {len(inbox)}: {inbox}"
    )


@pytest.mark.asyncio
async def test_distinct_message_ids_both_delivered() -> None:
    """Different message_ids → both processed (dedup must not over-fire)."""
    adapter = _build_adapter()
    inbox: list[str] = []

    async def _handler(msg) -> None:  # type: ignore[no-untyped-def]
        inbox.append(msg.content)

    adapter.subscribe(_handler)

    await adapter._handle_event(_make_event(message_id="om_a", text="one"))
    await adapter._handle_event(_make_event(message_id="om_b", text="two"))

    assert inbox == ["one", "two"]


@pytest.mark.asyncio
async def test_dedup_lru_caps_memory() -> None:
    """Cap=512 keeps memory bounded under high traffic. Pass 600
    distinct ids and verify the set size stays at the cap."""
    adapter = _build_adapter()
    adapter._seen_cap = 50  # tighten for the test

    async def _handler(msg) -> None:  # type: ignore[no-untyped-def]
        pass

    adapter.subscribe(_handler)

    for i in range(120):
        await adapter._handle_event(
            _make_event(message_id=f"om_{i:04d}", text=f"#{i}"),
        )

    assert len(adapter._seen_msg_ids) <= adapter._seen_cap


@pytest.mark.asyncio
async def test_non_text_message_not_added_to_dedup_set() -> None:
    """B-196: non-text messages are dropped before the dedup check.
    The dedup set should NOT pollute with their ids — otherwise a real
    text message with the same id (rare but possible across image+text
    threads) would be incorrectly suppressed."""
    adapter = _build_adapter()

    async def _handler(msg) -> None:  # type: ignore[no-untyped-def]
        pass

    adapter.subscribe(_handler)

    # An image / sticker message should be silently skipped without
    # leaving a footprint in the dedup set.
    await adapter._handle_event(
        _make_event(message_id="om_img_1", msg_type="image", text=""),
    )

    assert "om_img_1" not in adapter._seen_msg_ids


@pytest.mark.asyncio
async def test_empty_message_id_not_dedup_keyed() -> None:
    """Defensive: if Lark ever sends an event without message_id (or
    empty), don't crash the dedup logic. The handler should still run
    (the alternative — silent drop — would lose real messages)."""
    adapter = _build_adapter()
    inbox: list[str] = []

    async def _handler(msg) -> None:  # type: ignore[no-untyped-def]
        inbox.append(msg.content)

    adapter.subscribe(_handler)
    await adapter._handle_event(_make_event(message_id="", text="anonymous"))
    assert inbox == ["anonymous"]
