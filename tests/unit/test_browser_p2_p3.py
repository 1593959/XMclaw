"""Tests for P2.8 (eval safety), P3.7 (composite wait),
P3.5 (network log), P2.4 (dialog pre-arm).
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from xmclaw.core.ir import ToolCall
from xmclaw.providers.tool.browser import BrowserTools


def _call(name: str, args: dict, sid: str = "s1") -> ToolCall:
    return ToolCall(
        name=name, args=args, provenance="synthetic", session_id=sid,
    )


# ─── P2.8: browser_eval safety toggle ──────────────────────────────


@pytest.mark.asyncio
async def test_eval_disabled_returns_structured_refusal():
    bt = BrowserTools(evaluate_enabled=False)
    r = await bt._eval(
        _call("browser_eval", {"expression": "1+1"}), 0.0,
    )
    assert r.ok is False
    assert "disabled" in r.error.lower()
    assert "evaluate_enabled" in r.error


@pytest.mark.asyncio
async def test_eval_enabled_by_default_doesnt_short_circuit(monkeypatch):
    bt = BrowserTools()  # evaluate_enabled defaults True
    fake_page = MagicMock()
    fake_page.url = "https://x"
    fake_page.evaluate = AsyncMock(return_value=42)
    monkeypatch.setattr(bt, "_page_for", AsyncMock(return_value=fake_page))
    r = await bt._eval(
        _call("browser_eval", {"expression": "21+21"}), 0.0,
    )
    assert r.ok is True
    assert r.content == 42


# ─── P3.7: composite browser_wait_for ──────────────────────────────


@pytest.mark.asyncio
async def test_wait_for_requires_at_least_one_condition():
    bt = BrowserTools()
    r = await bt._wait_for(_call("browser_wait_for", {}), 0.0)
    assert r.ok is False
    assert "condition" in r.error.lower()


@pytest.mark.asyncio
async def test_wait_for_load_state_only(monkeypatch):
    bt = BrowserTools()
    fake_page = MagicMock()
    fake_page.url = "https://x/dash"
    fake_page.wait_for_load_state = AsyncMock()
    monkeypatch.setattr(bt, "_page_for", AsyncMock(return_value=fake_page))
    r = await bt._wait_for(
        _call("browser_wait_for", {"load_state": "networkidle"}), 0.0,
    )
    assert r.ok is True
    assert "load_state" in r.content["satisfied"]
    fake_page.wait_for_load_state.assert_awaited_once_with(
        "networkidle", timeout=10000,
    )


@pytest.mark.asyncio
async def test_wait_for_url_glob(monkeypatch):
    bt = BrowserTools()
    fake_page = MagicMock()
    fake_page.url = "https://example.com/dashboard"
    fake_page.wait_for_url = AsyncMock()
    monkeypatch.setattr(bt, "_page_for", AsyncMock(return_value=fake_page))
    r = await bt._wait_for(
        _call("browser_wait_for", {"url_glob": "**/dashboard"}),
        0.0,
    )
    assert r.ok is True
    fake_page.wait_for_url.assert_awaited_once_with(
        "**/dashboard", timeout=10000,
    )


@pytest.mark.asyncio
async def test_wait_for_js_predicate_wraps_expression(monkeypatch):
    bt = BrowserTools()
    fake_page = MagicMock()
    fake_page.url = "https://x"
    fake_page.wait_for_function = AsyncMock()
    monkeypatch.setattr(bt, "_page_for", AsyncMock(return_value=fake_page))
    await bt._wait_for(
        _call(
            "browser_wait_for",
            {"js_predicate": "window.app && window.app.ready"},
        ),
        0.0,
    )
    args = fake_page.wait_for_function.call_args
    # Wrapped into a function expression so the agent doesn't have to.
    wrapped = args.args[0]
    assert wrapped.startswith("() => Boolean(")


@pytest.mark.asyncio
async def test_wait_for_combo_url_plus_load_state(monkeypatch):
    bt = BrowserTools()
    fake_page = MagicMock()
    fake_page.url = "https://x"
    fake_page.wait_for_url = AsyncMock()
    fake_page.wait_for_load_state = AsyncMock()
    monkeypatch.setattr(bt, "_page_for", AsyncMock(return_value=fake_page))
    r = await bt._wait_for(
        _call(
            "browser_wait_for",
            {"url_glob": "**/done", "load_state": "load"},
        ),
        0.0,
    )
    assert r.ok is True
    assert set(r.content["satisfied"]) == {"url_glob", "load_state"}


@pytest.mark.asyncio
async def test_wait_for_reports_satisfied_so_far_on_timeout(monkeypatch):
    bt = BrowserTools()
    fake_page = MagicMock()
    fake_page.url = "https://x"
    fake_page.wait_for_url = AsyncMock()  # url_glob succeeds first
    fake_page.wait_for_load_state = AsyncMock(side_effect=TimeoutError("timeout"))
    monkeypatch.setattr(bt, "_page_for", AsyncMock(return_value=fake_page))
    # Conditions are checked in registration order: url_glob runs
    # before load_state. url_glob passes, load_state timeouts —
    # error should report ['url_glob'] satisfied so far.
    r = await bt._wait_for(
        _call(
            "browser_wait_for",
            {"url_glob": "**/x", "load_state": "load"},
        ),
        0.0,
    )
    assert r.ok is False
    assert "Satisfied so far" in r.error
    assert "['url_glob']" in r.error


# ─── P3.5: network log ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_network_log_empty_buffer():
    bt = BrowserTools()
    r = await bt._network_log(_call("browser_network_log", {}), 0.0)
    assert r.ok is True
    assert r.content["entries"] == []
    assert r.content["total_in_buffer"] == 0


@pytest.mark.asyncio
async def test_network_log_url_glob_filter():
    bt = BrowserTools()
    bt._network_buffers["s1"] = [
        {
            "method": "GET", "url": "https://example.com/api/v2/login",
            "status": 200, "request_headers": {}, "response_headers": {},
            "ts": time.time(),
        },
        {
            "method": "GET", "url": "https://cdn.example.com/style.css",
            "status": 200, "request_headers": {}, "response_headers": {},
            "ts": time.time(),
        },
    ]
    r = await bt._network_log(
        _call(
            "browser_network_log",
            {"url_glob": "**/api/**"},
        ),
        0.0,
    )
    assert r.ok is True
    assert len(r.content["entries"]) == 1
    assert "/api/" in r.content["entries"][0]["url"]


@pytest.mark.asyncio
async def test_network_log_method_filter():
    bt = BrowserTools()
    bt._network_buffers["s1"] = [
        {"method": "POST", "url": "https://x/login", "status": 200,
         "request_headers": {}, "response_headers": {}, "ts": time.time()},
        {"method": "GET", "url": "https://x/login", "status": 200,
         "request_headers": {}, "response_headers": {}, "ts": time.time()},
    ]
    r = await bt._network_log(
        _call("browser_network_log", {"method": "post"}), 0.0,
    )
    assert len(r.content["entries"]) == 1
    assert r.content["entries"][0]["method"] == "POST"


@pytest.mark.asyncio
async def test_network_log_with_body_utf8():
    bt = BrowserTools()
    fake_resp = MagicMock()
    fake_resp.body = AsyncMock(return_value=b'{"token":"xyz"}')
    bt._network_buffers["s1"] = [
        {
            "method": "POST", "url": "https://x/auth", "status": 200,
            "request_headers": {}, "response_headers": {"content-type": "application/json"},
            "ts": time.time(), "_response_obj": fake_resp,
        },
    ]
    r = await bt._network_log(
        _call("browser_network_log", {"with_body": True}), 0.0,
    )
    assert r.ok is True
    assert r.content["entries"][0]["body"] == '{"token":"xyz"}'


@pytest.mark.asyncio
async def test_network_log_with_body_binary_falls_back_to_base64():
    bt = BrowserTools()
    fake_resp = MagicMock()
    fake_resp.body = AsyncMock(return_value=b"\x00\xff\xfe binary garbage")
    bt._network_buffers["s1"] = [
        {"method": "GET", "url": "https://x/img.png", "status": 200,
         "request_headers": {}, "response_headers": {}, "ts": time.time(),
         "_response_obj": fake_resp},
    ]
    r = await bt._network_log(
        _call("browser_network_log", {"with_body": True}), 0.0,
    )
    e = r.content["entries"][0]
    assert "body_base64" in e
    assert "body" not in e


@pytest.mark.asyncio
async def test_network_log_with_body_truncates_over_cap():
    bt = BrowserTools()
    big = b"a" * (80 * 1024)
    fake_resp = MagicMock()
    fake_resp.body = AsyncMock(return_value=big)
    bt._network_buffers["s1"] = [
        {"method": "GET", "url": "https://x/large", "status": 200,
         "request_headers": {}, "response_headers": {}, "ts": time.time(),
         "_response_obj": fake_resp},
    ]
    r = await bt._network_log(
        _call("browser_network_log", {"with_body": True}), 0.0,
    )
    e = r.content["entries"][0]
    assert e["body_truncated"] is True
    assert len(e["body"]) == 64 * 1024


@pytest.mark.asyncio
async def test_network_log_clear_empties_buffer():
    bt = BrowserTools()
    bt._network_buffers["s1"] = [
        {"method": "GET", "url": "https://x", "status": 200,
         "request_headers": {}, "response_headers": {}, "ts": time.time()},
    ]
    r = await bt._network_log(
        _call("browser_network_log", {"clear": True}), 0.0,
    )
    assert r.ok is True
    assert bt._network_buffers["s1"] == []


# ─── P2.4: dialog pre-arm ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_dialog_arm_sets_session_arm():
    bt = BrowserTools()
    r = await bt._dialog_arm(
        _call("browser_dialog_arm", {"action": "accept"}), 0.0,
    )
    assert r.ok is True
    assert bt._session_dialog_armed["s1"]["action"] == "accept"


@pytest.mark.asyncio
async def test_dialog_arm_clear_drops_arm():
    bt = BrowserTools()
    bt._session_dialog_armed["s1"] = {"action": "accept"}
    r = await bt._dialog_arm(
        _call("browser_dialog_arm", {"action": "clear"}), 0.0,
    )
    assert r.ok is True
    assert r.content["cleared"] is True
    assert "s1" not in bt._session_dialog_armed


@pytest.mark.asyncio
async def test_dialog_arm_bad_action():
    bt = BrowserTools()
    r = await bt._dialog_arm(
        _call("browser_dialog_arm", {"action": "panic"}), 0.0,
    )
    assert r.ok is False


# ─── close_session cleans up new state ─────────────────────────────


@pytest.mark.asyncio
async def test_close_session_clears_armed_and_network():
    bt = BrowserTools()
    bt._session_dialog_armed["s1"] = {"action": "accept"}
    bt._network_buffers["s1"] = [{"url": "x"}]

    await bt.close_session("s1")

    assert "s1" not in bt._session_dialog_armed
    assert "s1" not in bt._network_buffers


# ─── tool roster ───────────────────────────────────────────────────


def test_p2_p3_tools_registered():
    bt = BrowserTools()
    names = {t.name for t in bt.list_tools()}
    assert "browser_dialog_arm" in names
    assert "browser_network_log" in names
