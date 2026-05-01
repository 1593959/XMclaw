"""B-143 — IntegrationsTools unit tests.

Pins:
  * 8 tools always advertised (don't hide on missing config; the LLM
    needs to see the affordance + get a 'configure first' error if
    it tries unconfigured services)
  * Each tool returns ok=False + actionable error when config keys
    are missing — never a stack trace
  * webhook_send happy path against an httpx mock
  * Slack / Telegram / Discord / GitHub / Notion routes hit the
    correct API URL with the correct auth header
  * email_send fails clearly when smtp_host is missing
"""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from xmclaw.core.ir import ToolCall
from xmclaw.providers.tool.integrations import IntegrationsTools


def _call(name: str, args: dict | None = None) -> ToolCall:
    return ToolCall(name=name, args=args or {}, provenance="synthetic")


# ── tool list ─────────────────────────────────────────────────────


def test_list_tools_always_advertises_eight() -> None:
    names = {s.name for s in IntegrationsTools().list_tools()}
    assert names == {
        "webhook_send", "email_send", "rss_fetch",
        "slack_send", "telegram_send", "discord_send",
        "github_create_issue", "notion_create_page",
    }


def test_list_tools_unchanged_when_config_present() -> None:
    """Hiding tools on missing config would make the LLM guess we
    don't have them and reach for clumsier paths. Always advertise."""
    cfg = {"slack": {"bot_token": "xoxb-fake"}}
    names_with    = {s.name for s in IntegrationsTools(cfg).list_tools()}
    names_without = {s.name for s in IntegrationsTools().list_tools()}
    assert names_with == names_without


# ── unconfigured services return actionable errors ──────────────


@pytest.mark.asyncio
async def test_slack_unconfigured() -> None:
    r = await IntegrationsTools().invoke(_call("slack_send", {"text": "hi"}))
    assert r.ok is False
    assert "slack" in (r.error or "").lower()
    assert "bot_token" in (r.error or "")


@pytest.mark.asyncio
async def test_telegram_unconfigured() -> None:
    r = await IntegrationsTools().invoke(_call("telegram_send", {"text": "x"}))
    assert r.ok is False
    assert "telegram" in (r.error or "").lower()


@pytest.mark.asyncio
async def test_email_unconfigured() -> None:
    r = await IntegrationsTools().invoke(_call("email_send", {
        "to": "x@y.com", "subject": "s", "body": "b",
    }))
    assert r.ok is False
    assert "smtp_host" in (r.error or "")


@pytest.mark.asyncio
async def test_github_unconfigured() -> None:
    r = await IntegrationsTools().invoke(_call("github_create_issue", {"title": "x"}))
    assert r.ok is False
    assert "github" in (r.error or "").lower()


@pytest.mark.asyncio
async def test_notion_unconfigured() -> None:
    r = await IntegrationsTools().invoke(_call("notion_create_page", {"title": "x"}))
    assert r.ok is False
    assert "notion" in (r.error or "").lower()


@pytest.mark.asyncio
async def test_discord_unconfigured() -> None:
    r = await IntegrationsTools().invoke(_call("discord_send", {"content": "x"}))
    assert r.ok is False
    assert "discord" in (r.error or "").lower()


# ── webhook_send round-trip ──────────────────────────────────────


@pytest.mark.asyncio
async def test_webhook_send_post_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifies the request goes out as POST with the JSON body
    + custom headers. Uses httpx MockTransport so no real network."""
    captured: dict[str, Any] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = request.content.decode("utf-8") if request.content else ""
        return httpx.Response(202, json={"received": True})

    transport = httpx.MockTransport(_handler)

    # Patch httpx.AsyncClient to use the mock transport
    real_client = httpx.AsyncClient
    def patched_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)
    monkeypatch.setattr("httpx.AsyncClient", patched_client)

    r = await IntegrationsTools().invoke(_call("webhook_send", {
        "url": "https://example.com/hook",
        "json": {"event": "test"},
        "headers": {"X-Custom": "yes"},
    }))
    assert r.ok is True
    payload = json.loads(r.content)
    assert payload["status_code"] == 202
    assert captured["method"] == "POST"
    assert captured["url"] == "https://example.com/hook"
    assert captured["headers"].get("x-custom") == "yes"


@pytest.mark.asyncio
async def test_webhook_requires_url() -> None:
    r = await IntegrationsTools().invoke(_call("webhook_send", {}))
    assert r.ok is False
    assert "url required" in (r.error or "")


# ── slack hits chat.postMessage with the bearer token ───────────


@pytest.mark.asyncio
async def test_slack_send_hits_correct_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "ok": True, "channel": "C123", "ts": "1700000.0001",
        })

    transport = httpx.MockTransport(_handler)
    real_client = httpx.AsyncClient
    def patched(*a, **kw):
        kw["transport"] = transport
        return real_client(*a, **kw)
    monkeypatch.setattr("httpx.AsyncClient", patched)

    tools = IntegrationsTools({
        "slack": {"bot_token": "xoxb-test", "channel": "#dev"},
    })
    r = await tools.invoke(_call("slack_send", {"text": "hi"}))
    assert r.ok is True
    assert "slack.com/api/chat.postMessage" in captured["url"]
    assert captured["auth"] == "Bearer xoxb-test"
    assert captured["body"]["channel"] == "#dev"
    assert captured["body"]["text"] == "hi"


# ── telegram hits Bot API URL with the right token ──────────────


@pytest.mark.asyncio
async def test_telegram_send_hits_bot_api(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "ok": True, "result": {"message_id": 42},
        })

    transport = httpx.MockTransport(_handler)
    real_client = httpx.AsyncClient
    def patched(*a, **kw):
        kw["transport"] = transport
        return real_client(*a, **kw)
    monkeypatch.setattr("httpx.AsyncClient", patched)

    tools = IntegrationsTools({
        "telegram": {"bot_token": "12345:abc", "chat_id": "999"},
    })
    r = await tools.invoke(_call("telegram_send", {"text": "hello"}))
    assert r.ok is True
    assert "/bot12345:abc/sendMessage" in captured["url"]
    assert captured["body"]["chat_id"] == "999"
    assert captured["body"]["text"] == "hello"


# ── github hits issues endpoint with bearer token ───────────────


@pytest.mark.asyncio
async def test_github_create_issue_hits_correct_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(201, json={
            "number": 42, "html_url": "https://github.com/x/y/issues/42",
        })

    transport = httpx.MockTransport(_handler)
    real_client = httpx.AsyncClient
    def patched(*a, **kw):
        kw["transport"] = transport
        return real_client(*a, **kw)
    monkeypatch.setattr("httpx.AsyncClient", patched)

    tools = IntegrationsTools({
        "github": {"token": "ghp_test", "repo": "octocat/spoon"},
    })
    r = await tools.invoke(_call("github_create_issue", {
        "title": "bug", "body": "broken",
    }))
    assert r.ok is True
    payload = json.loads(r.content)
    assert payload["number"] == 42
    assert "/repos/octocat/spoon/issues" in captured["url"]
    assert captured["auth"] == "Bearer ghp_test"


# ── unknown tool name ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unknown_tool_returns_structured_error() -> None:
    r = await IntegrationsTools().invoke(_call("nothing_here"))
    assert r.ok is False
    assert "unknown" in (r.error or "")
