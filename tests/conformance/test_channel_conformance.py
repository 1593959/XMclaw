"""Parametric channel conformance suite — anti-req #7, CI-3.

Every ``ChannelAdapter`` implementation MUST pass every test in this
file before it ships. Adding a new channel = register a fixture that
yields ``(adapter, make_client)`` and every test automatically runs
against it.

The test matrix covers the contract claims the scheduler + daemon rely
on. It does NOT include channel-specific quirks (Slack's threading,
Telegram's markdown flavors, etc.) — those live in per-channel test
files. Anything the ABC *promises* should be verifiable here.

Phase 2.3 registers the WS adapter. Later phases add Slack, Discord,
Telegram, Feishu, etc. — each needs a fixture that stands up the
adapter and provides a client capable of sending and recv'ing on it.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol

import pytest
import pytest_asyncio

from xmclaw.providers.channel import (
    ChannelAdapter,
    ChannelTarget,
    InboundMessage,
    OutboundMessage,
)
from xmclaw.providers.channel.ws import WSChannelAdapter


# ── protocol every channel fixture must satisfy ──────────────────────────


class _ChannelClient(Protocol):
    """A test client that can send to the adapter and recv replies from it."""

    ref: str

    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def send(self, content: str) -> None: ...
    async def recv(self, timeout: float = 2.0) -> dict[str, Any]: ...


@dataclass
class _ChannelFixture:
    adapter: ChannelAdapter
    make_client: Any
    name: str


# ── WS-specific client + fixture ─────────────────────────────────────────


class _WSClient:
    def __init__(self, url: str) -> None:
        self.url = url
        self.ref: str = ""
        self._ws: Any = None

    async def connect(self) -> None:
        import websockets
        self._ws = await websockets.connect(self.url, open_timeout=5.0)

    async def disconnect(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
            self._ws = None

    async def send(self, content: str) -> None:
        assert self._ws is not None, "call connect() first"
        await self._ws.send(json.dumps({"type": "user", "content": content}))

    async def recv(self, timeout: float = 2.0) -> dict[str, Any]:
        assert self._ws is not None
        raw = await asyncio.wait_for(self._ws.recv(), timeout=timeout)
        return json.loads(raw)


async def _build_ws_fixture() -> _ChannelFixture:
    adapter = WSChannelAdapter(host="127.0.0.1", port=0)
    await adapter.start()
    url = f"ws://{adapter.host}:{adapter.port}"

    def make_client() -> _WSClient:
        return _WSClient(url)

    return _ChannelFixture(adapter=adapter, make_client=make_client, name="ws")


# Registry: (id, async-builder). Adding a new channel = append one line.
_CHANNEL_BUILDERS: list[tuple[str, Any]] = [
    ("ws", _build_ws_fixture),
]


@pytest_asyncio.fixture(
    params=[b for _, b in _CHANNEL_BUILDERS],
    ids=[i for i, _ in _CHANNEL_BUILDERS],
)
async def channel(request: Any) -> AsyncIterator[_ChannelFixture]:
    builder = request.param
    f = await builder()
    try:
        yield f
    finally:
        await f.adapter.stop()


# ── conformance tests ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_recv_roundtrip(channel: _ChannelFixture) -> None:
    """Inbound messages reach subscribers; outbound reaches the addressed client."""
    received: list[InboundMessage] = []

    async def handler(msg: InboundMessage) -> None:
        received.append(msg)

    channel.adapter.subscribe(handler)

    client = channel.make_client()
    await client.connect()
    try:
        await client.send("hello from client")
        await asyncio.sleep(0.1)
        assert len(received) == 1
        assert received[0].content == "hello from client"

        await channel.adapter.send(
            received[0].target,
            OutboundMessage(content="hello from server"),
        )
        frame = await client.recv()
        assert frame["type"] == "assistant"
        assert frame["content"] == "hello from server"
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_multiple_concurrent_clients_isolated(
    channel: _ChannelFixture,
) -> None:
    """Two clients connected simultaneously receive only their own replies."""
    received: list[InboundMessage] = []

    async def handler(msg: InboundMessage) -> None:
        received.append(msg)

    channel.adapter.subscribe(handler)

    a = channel.make_client()
    b = channel.make_client()
    await a.connect()
    await b.connect()
    try:
        await a.send("from A")
        await b.send("from B")
        await asyncio.sleep(0.1)
        assert len(received) == 2

        a_msg = next(r for r in received if r.content == "from A")
        await channel.adapter.send(a_msg.target, OutboundMessage(content="only for A"))
        frame_a = await a.recv()
        assert frame_a["content"] == "only for A"
        with pytest.raises(asyncio.TimeoutError):
            await b.recv(timeout=0.3)
    finally:
        await a.disconnect()
        await b.disconnect()


@pytest.mark.asyncio
async def test_broadcast_reaches_every_client(channel: _ChannelFixture) -> None:
    a = channel.make_client()
    b = channel.make_client()
    await a.connect()
    await b.connect()
    try:
        await asyncio.sleep(0.05)
        await channel.adapter.send(
            ChannelTarget(channel=channel.name, ref="*"),
            OutboundMessage(content="announcement"),
        )
        frame_a = await a.recv()
        frame_b = await b.recv()
        assert frame_a["content"] == "announcement"
        assert frame_b["content"] == "announcement"
    finally:
        await a.disconnect()
        await b.disconnect()


@pytest.mark.asyncio
async def test_subscriber_exception_isolates(channel: _ChannelFixture) -> None:
    """A broken subscriber must not block others or kill the connection."""
    good: list[InboundMessage] = []

    async def bad(_msg: InboundMessage) -> None:
        raise RuntimeError("intentional")

    async def good_h(msg: InboundMessage) -> None:
        good.append(msg)

    channel.adapter.subscribe(bad)
    channel.adapter.subscribe(good_h)

    client = channel.make_client()
    await client.connect()
    try:
        await client.send("x")
        await asyncio.sleep(0.1)
        assert len(good) == 1
        await client.send("y")
        await asyncio.sleep(0.1)
        assert len(good) == 2
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_malformed_frame_dropped_not_fatal(channel: _ChannelFixture) -> None:
    """Bad payload must not kill the connection; next valid message works."""
    received: list[InboundMessage] = []

    async def handler(msg: InboundMessage) -> None:
        received.append(msg)

    channel.adapter.subscribe(handler)

    client = channel.make_client()
    await client.connect()
    try:
        if channel.name == "ws":
            await client._ws.send("{not json")  # type: ignore[attr-defined]
        await asyncio.sleep(0.05)
        await client.send("recovery")
        await asyncio.sleep(0.1)
        assert [m.content for m in received] == ["recovery"]
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_send_to_unknown_ref_raises(channel: _ChannelFixture) -> None:
    """Sending to a nonexistent ref must fail loudly, not silently drop."""
    with pytest.raises((LookupError, KeyError, ValueError)):
        await channel.adapter.send(
            ChannelTarget(channel=channel.name, ref="definitely-does-not-exist"),
            OutboundMessage(content="nope"),
        )


@pytest.mark.asyncio
async def test_send_to_wrong_channel_name_raises(channel: _ChannelFixture) -> None:
    """An adapter must refuse ChannelTargets for a different channel."""
    with pytest.raises(ValueError):
        await channel.adapter.send(
            ChannelTarget(channel="definitely-not-this-channel", ref="x"),
            OutboundMessage(content="leak"),
        )


@pytest.mark.asyncio
async def test_stop_closes_all_connections(channel: _ChannelFixture) -> None:
    """After stop(), the client's next recv sees the connection closed."""
    client = channel.make_client()
    await client.connect()
    await asyncio.sleep(0.05)

    await channel.adapter.stop()
    await asyncio.sleep(0.05)

    with pytest.raises(Exception):  # noqa: B017 — any conn-closed flavor is OK
        await client.recv(timeout=0.5)
    await client.disconnect()
