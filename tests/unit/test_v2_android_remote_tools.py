"""Tests for AndroidRemoteToolProvider — M1 Android Companion.

Covers: tool list completeness, invoke → correct frame, screenshot attach,
        no-device downgrade, error pass-through, and key alias mapping.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from xmclaw.core.ir import ToolCall, ToolResult
from xmclaw.daemon.device_registry import DeviceRegistry
from xmclaw.providers.tool.android_remote import AndroidRemoteToolProvider


class _MockConn:
    """Mock DeviceConn that records requests and returns canned responses."""

    def __init__(self, responses: list[dict[str, Any]] | None = None) -> None:
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self._responses = iter(responses or [])

    async def send_request(self, type_: str, data: dict[str, Any], *, timeout: float = 15.0) -> dict[str, Any]:
        self.requests.append((type_, data))
        try:
            return next(self._responses)
        except StopIteration:
            return {"ok": True}


def _make_call(name: str, args: dict[str, Any] | None = None) -> ToolCall:
    return ToolCall(name=name, args=args or {}, provenance="synthetic")


# ------------------------------------------------------------------
# Tool list
# ------------------------------------------------------------------

def test_list_tools_names() -> None:
    reg = DeviceRegistry()
    p = AndroidRemoteToolProvider(reg)
    names = {t.name for t in p.list_tools()}
    expected = {
        "phone_open_app", "phone_click", "phone_tap", "phone_input",
        "phone_swipe", "phone_key", "phone_screenshot", "phone_ui_tree",
        "phone_notification", "phone_wait", "phone_clipboard_get", "phone_clipboard_set",
    }
    assert names == expected


# ------------------------------------------------------------------
# Invoke: no device → graceful fail
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_invoke_no_device() -> None:
    reg = DeviceRegistry()
    p = AndroidRemoteToolProvider(reg)
    call = _make_call("phone_tap", {"x": 100, "y": 200})
    result = await p.invoke(call)
    assert result.ok is False
    assert "no paired phone" in result.error.lower()


# ------------------------------------------------------------------
# Invoke: each tool sends correct frame
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_phone_tap_frame() -> None:
    reg = DeviceRegistry()
    mock_conn = _MockConn()
    reg._conns["d-1"] = mock_conn  # type: ignore[attr-defined]
    p = AndroidRemoteToolProvider(reg)
    call = _make_call("phone_tap", {"x": 100, "y": 200, "device_id": "d-1"})
    result = await p.invoke(call)
    assert result.ok is True
    assert len(mock_conn.requests) == 1
    type_, data = mock_conn.requests[0]
    assert type_ == "cmd"
    assert data == {"ui": "tap", "x": 100, "y": 200}


@pytest.mark.asyncio
async def test_phone_click_frame() -> None:
    reg = DeviceRegistry()
    mock_conn = _MockConn()
    reg._conns["d-1"] = mock_conn  # type: ignore[attr-defined]
    p = AndroidRemoteToolProvider(reg)
    call = _make_call("phone_click", {"target": {"text": "搜索"}, "device_id": "d-1"})
    result = await p.invoke(call)
    assert result.ok is True
    _, data = mock_conn.requests[0]
    assert data == {"ui": "click", "target": {"text": "搜索"}}


@pytest.mark.asyncio
async def test_phone_open_app_frame() -> None:
    reg = DeviceRegistry()
    mock_conn = _MockConn()
    reg._conns["d-1"] = mock_conn  # type: ignore[attr-defined]
    p = AndroidRemoteToolProvider(reg)
    call = _make_call("phone_open_app", {"package_name": "com.android.settings", "device_id": "d-1"})
    result = await p.invoke(call)
    assert result.ok is True
    _, data = mock_conn.requests[0]
    assert data == {"ui": "open_app", "package_name": "com.android.settings"}


@pytest.mark.asyncio
async def test_phone_input_frame() -> None:
    reg = DeviceRegistry()
    mock_conn = _MockConn()
    reg._conns["d-1"] = mock_conn  # type: ignore[attr-defined]
    p = AndroidRemoteToolProvider(reg)
    call = _make_call("phone_input", {"text": "下午好", "index": 1, "device_id": "d-1"})
    result = await p.invoke(call)
    assert result.ok is True
    _, data = mock_conn.requests[0]
    assert data == {"ui": "input", "text": "下午好", "index": 1}


@pytest.mark.asyncio
async def test_phone_swipe_frame() -> None:
    reg = DeviceRegistry()
    mock_conn = _MockConn()
    reg._conns["d-1"] = mock_conn  # type: ignore[attr-defined]
    p = AndroidRemoteToolProvider(reg)
    call = _make_call("phone_swipe", {"x1": 0, "y1": 1000, "x2": 0, "y2": 200, "ms": 500, "device_id": "d-1"})
    result = await p.invoke(call)
    assert result.ok is True
    _, data = mock_conn.requests[0]
    assert data == {"ui": "swipe", "x1": 0, "y1": 1000, "x2": 0, "y2": 200, "ms": 500}


@pytest.mark.asyncio
async def test_phone_key_aliases() -> None:
    reg = DeviceRegistry()
    mock_conn = _MockConn()
    reg._conns["d-1"] = mock_conn  # type: ignore[attr-defined]
    p = AndroidRemoteToolProvider(reg)

    for friendly, expected in (
        ("back", "KEYCODE_BACK"),
        ("home", "KEYCODE_HOME"),
        ("recents", "KEYCODE_APP_SWITCH"),
        ("enter", "KEYCODE_ENTER"),
        ("delete", "KEYCODE_DEL"),
        ("del", "KEYCODE_DEL"),
    ):
        mock_conn.requests.clear()
        call = _make_call("phone_key", {"key": friendly, "device_id": "d-1"})
        result = await p.invoke(call)
        assert result.ok is True, f"failed for {friendly}"
        _, data = mock_conn.requests[0]
        assert data["key"] == expected, f"{friendly} should map to {expected}"


@pytest.mark.asyncio
async def test_phone_key_raw_keycode() -> None:
    reg = DeviceRegistry()
    mock_conn = _MockConn()
    reg._conns["d-1"] = mock_conn  # type: ignore[attr-defined]
    p = AndroidRemoteToolProvider(reg)
    call = _make_call("phone_key", {"key": "KEYCODE_VOLUME_UP", "device_id": "d-1"})
    result = await p.invoke(call)
    assert result.ok is True
    _, data = mock_conn.requests[0]
    assert data["key"] == "KEYCODE_VOLUME_UP"


@pytest.mark.asyncio
async def test_phone_screenshot_attach_image_url() -> None:
    reg = DeviceRegistry()
    mock_conn = _MockConn([{"url": "https://cdn.example.com/s.png", "w": 1080, "h": 2400}])
    reg._conns["d-1"] = mock_conn  # type: ignore[attr-defined]
    p = AndroidRemoteToolProvider(reg)
    call = _make_call("phone_screenshot", {"device_id": "d-1"})
    result = await p.invoke(call)
    assert result.ok is True
    assert result.metadata.get("attach_image_url") == "https://cdn.example.com/s.png"
    _, data = mock_conn.requests[0]
    assert data == {"ui": "screenshot"}


@pytest.mark.asyncio
async def test_phone_ui_tree_tree_kind() -> None:
    reg = DeviceRegistry()
    mock_conn = _MockConn([{"nodes": [{"id": "n0", "text": "WLAN"}], "pkg": "com.android.settings"}])
    reg._conns["d-1"] = mock_conn  # type: ignore[attr-defined]
    p = AndroidRemoteToolProvider(reg)
    call = _make_call("phone_ui_tree", {"clickable_only": True, "device_id": "d-1"})
    result = await p.invoke(call)
    assert result.ok is True
    parsed = json.loads(result.content)
    assert parsed["nodes"][0]["text"] == "WLAN"


@pytest.mark.asyncio
async def test_phone_wait_frame() -> None:
    reg = DeviceRegistry()
    mock_conn = _MockConn([{"ok": True, "found": True}])
    reg._conns["d-1"] = mock_conn  # type: ignore[attr-defined]
    p = AndroidRemoteToolProvider(reg)
    call = _make_call("phone_wait", {"event": "exists", "target": {"text": "完成"}, "timeout_ms": 3000, "device_id": "d-1"})
    result = await p.invoke(call)
    assert result.ok is True
    _, data = mock_conn.requests[0]
    assert data == {"ui": "wait", "event": "exists", "target": {"text": "完成"}, "timeout_ms": 3000}


@pytest.mark.asyncio
async def test_phone_clipboard_get() -> None:
    reg = DeviceRegistry()
    mock_conn = _MockConn([{"text": "hello"}])
    reg._conns["d-1"] = mock_conn  # type: ignore[attr-defined]
    p = AndroidRemoteToolProvider(reg)
    call = _make_call("phone_clipboard_get", {"device_id": "d-1"})
    result = await p.invoke(call)
    assert result.ok is True
    _, data = mock_conn.requests[0]
    assert data == {"clipboard_cmd": "get_clipboard"}


@pytest.mark.asyncio
async def test_phone_clipboard_set() -> None:
    reg = DeviceRegistry()
    mock_conn = _MockConn()
    reg._conns["d-1"] = mock_conn  # type: ignore[attr-defined]
    p = AndroidRemoteToolProvider(reg)
    call = _make_call("phone_clipboard_set", {"text": "copied", "device_id": "d-1"})
    result = await p.invoke(call)
    assert result.ok is True
    _, data = mock_conn.requests[0]
    assert data == {"clipboard_cmd": "set_clipboard", "text": "copied"}


# ------------------------------------------------------------------
# Error pass-through
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_act_result_error_passed_through() -> None:
    reg = DeviceRegistry()
    mock_conn = _MockConn([{"ok": False, "error": "blocked: sensitive app com.bank.app"}])
    reg._conns["d-1"] = mock_conn  # type: ignore[attr-defined]
    p = AndroidRemoteToolProvider(reg)
    call = _make_call("phone_tap", {"x": 100, "y": 200, "device_id": "d-1"})
    result = await p.invoke(call)
    assert result.ok is False
    assert "blocked: sensitive app" in result.error


@pytest.mark.asyncio
async def test_timeout_error() -> None:
    reg = DeviceRegistry()

    class _SlowConn:
        async def send_request(self, type_: str, data: dict[str, Any], *, timeout: float = 15.0) -> dict[str, Any]:
            raise asyncio.TimeoutError()

    reg._conns["d-1"] = _SlowConn()  # type: ignore[attr-defined]
    p = AndroidRemoteToolProvider(reg)
    call = _make_call("phone_tap", {"x": 100, "y": 200, "device_id": "d-1"})
    result = await p.invoke(call)
    assert result.ok is False
    assert "device timeout" in result.error.lower()
