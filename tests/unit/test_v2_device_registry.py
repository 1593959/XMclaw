"""Tests for DeviceRegistry — M0/M1 Android Companion skeleton.

Covers: register/drop lifecycle, send_request/resolve pairing, timeout,
        single-device convenience get(None), and list().
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from xmclaw.daemon.device_registry import DeviceConn, DeviceDisconnected, DeviceRegistry


class _FakeWS:
    """Minimal mock WebSocket for DeviceConn."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.close_code: int | None = None

    async def send_json(self, data: dict[str, Any]) -> None:
        self.sent.append(data)


# ------------------------------------------------------------------
# DeviceConn
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_request_resolve() -> None:
    ws = _FakeWS()
    conn = DeviceConn("d-123", ws)
    task = asyncio.create_task(conn.send_request("cmd", {"ui": "tap", "x": 100, "y": 200}))
    # Allow the event loop to schedule send_request
    await asyncio.sleep(0)

    # Resolve the pending future
    assert len(ws.sent) == 1
    frame = ws.sent[0]
    assert frame["type"] == "cmd"
    assert frame["data"] == {"ui": "tap", "x": 100, "y": 200}
    req_id = frame["req_id"]
    conn.resolve(req_id, {"ok": True})

    result = await task
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_send_request_timeout() -> None:
    ws = _FakeWS()
    conn = DeviceConn("d-123", ws)
    with pytest.raises(asyncio.TimeoutError):
        await conn.send_request("cmd", {"ui": "tap"}, timeout=0.05)


@pytest.mark.asyncio
async def test_cancel_all_on_disconnect() -> None:
    ws = _FakeWS()
    conn = DeviceConn("d-123", ws)
    task = asyncio.create_task(conn.send_request("cmd", {"ui": "tap"}, timeout=10.0))
    await asyncio.sleep(0)
    conn.cancel_all(DeviceDisconnected("gone"))
    with pytest.raises(DeviceDisconnected):
        await task


@pytest.mark.asyncio
async def test_send_non_request_frame() -> None:
    ws = _FakeWS()
    conn = DeviceConn("d-123", ws)
    await conn.send({"v": 1, "type": "dev.welcome", "data": {}})
    assert len(ws.sent) == 1


# ------------------------------------------------------------------
# DeviceRegistry
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_register_drop() -> None:
    reg = DeviceRegistry()
    ws = _FakeWS()
    conn = reg.register("d-abc", ws)
    assert conn.device_id == "d-abc"
    assert "d-abc" in reg
    assert len(reg) == 1

    reg.drop("d-abc")
    assert "d-abc" not in reg
    assert len(reg) == 0


@pytest.mark.asyncio
async def test_get_single_device_convenience() -> None:
    reg = DeviceRegistry()
    ws = _FakeWS()
    reg.register("d-abc", ws)
    # get(None) returns the single device
    assert reg.get(None) is not None
    assert reg.get(None).device_id == "d-abc"
    # get with explicit id
    assert reg.get("d-abc") is not None
    # get missing id
    assert reg.get("d-missing") is None


@pytest.mark.asyncio
async def test_get_none_when_multiple_devices() -> None:
    reg = DeviceRegistry()
    reg.register("d-1", _FakeWS())
    reg.register("d-2", _FakeWS())
    # get(None) returns None when multiple devices
    assert reg.get(None) is None


@pytest.mark.asyncio
async def test_list_returns_summary() -> None:
    reg = DeviceRegistry()
    ws = _FakeWS()
    reg.register("d-abc", ws)
    items = reg.list()
    assert len(items) == 1
    assert items[0]["device_id"] == "d-abc"
    assert items[0]["is_open"] is True


@pytest.mark.asyncio
async def test_drop_cancels_pending_futures() -> None:
    reg = DeviceRegistry()
    ws = _FakeWS()
    conn = reg.register("d-abc", ws)
    task = asyncio.create_task(conn.send_request("cmd", {"ui": "tap"}, timeout=10.0))
    await asyncio.sleep(0)
    reg.drop("d-abc")
    with pytest.raises(DeviceDisconnected):
        await task
