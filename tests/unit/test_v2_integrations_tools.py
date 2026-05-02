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


def test_list_tools_no_config_only_universals() -> None:
    """B-180: with no config the LLM only sees the always-on
    universals (webhook_send + rss_fetch). The 10 service-specific
    tools stay hidden — joint audit (events.db) showed agent
    never tries unconfigured ones, so listing them was just
    spec-bloat."""
    names = {s.name for s in IntegrationsTools().list_tools()}
    assert names == {"webhook_send", "rss_fetch"}


def test_list_tools_exposes_only_configured_services() -> None:
    """Configure slack + telegram → those two appear, others stay
    hidden. github stays hidden because token is unset."""
    cfg = {
        "slack": {"enabled": True, "bot_token": "xoxb-real"},
        "telegram": {"enabled": True, "bot_token": "12345:abc"},
        "github": {"enabled": True, "token": ""},  # empty == not configured
        "discord": {"enabled": True},  # no creds at all
    }
    names = {s.name for s in IntegrationsTools(cfg).list_tools()}
    assert "slack_send" in names
    assert "telegram_send" in names
    assert "github_create_issue" not in names
    assert "discord_send" not in names
    # Universals always there.
    assert "webhook_send" in names
    assert "rss_fetch" in names


def test_list_tools_treats_stub_values_as_unconfigured() -> None:
    """Config wizard writes ``YOUR_TOKEN_HERE`` placeholders. Those
    must NOT count as 'configured' — would re-bloat the spec for
    fresh installs that never edited config."""
    cfg = {
        "slack": {"enabled": True, "bot_token": "YOUR_BOT_TOKEN"},
        "telegram": {"enabled": True, "bot_token": "changeme"},
        "github": {"enabled": True, "token": "tbd"},
        "notion": {"enabled": True, "api_key": "TODO"},
    }
    names = {s.name for s in IntegrationsTools(cfg).list_tools()}
    assert "slack_send" not in names
    assert "telegram_send" not in names
    assert "github_create_issue" not in names
    assert "notion_create_page" not in names


def test_list_tools_explicit_disabled_hides_even_with_creds() -> None:
    """Explicit ``enabled: false`` overrides credential presence —
    user might keep tokens for later but want the tool off now."""
    cfg = {
        "slack": {"enabled": False, "bot_token": "xoxb-real-token"},
    }
    names = {s.name for s in IntegrationsTools(cfg).list_tools()}
    assert "slack_send" not in names


def test_list_tools_email_gated_on_smtp_host() -> None:
    cfg_off = {"email": {"enabled": True, "smtp_host": ""}}
    assert "email_send" not in {s.name for s in IntegrationsTools(cfg_off).list_tools()}
    cfg_on = {"email": {"enabled": True, "smtp_host": "smtp.gmail.com"}}
    assert "email_send" in {s.name for s in IntegrationsTools(cfg_on).list_tools()}


def test_list_tools_real_user_config_exposes_zero_integrations() -> None:
    """Reproduce the user's actual config from joint audit: 5 sections
    with `enabled` + stub values (bot_token: '' etc). Pre-B-180 this
    would advertise 12 tools; post-B-180 advertises 0 service tools
    (only webhook + rss universals)."""
    cfg = {
        "slack":    {"enabled": True, "bot_token": "", "channel": ""},
        "discord":  {"enabled": True, "bot_token": "", "channel_id": ""},
        "telegram": {"enabled": True, "bot_token": "", "chat_id": ""},
        "github":   {"enabled": True, "token": "", "repo": ""},
        "notion":   {"enabled": True, "api_key": "", "database_id": ""},
    }
    names = {s.name for s in IntegrationsTools(cfg).list_tools()}
    service_tools = names - {"webhook_send", "rss_fetch"}
    assert service_tools == set(), (
        f"unconfigured services leaked into tool list: {service_tools}"
    )


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


# ── B-144: 国内主流聊天工具 ──────────────────────────────────────


@pytest.mark.asyncio
async def test_feishu_unconfigured() -> None:
    r = await IntegrationsTools().invoke(_call("feishu_send", {"text": "x"}))
    assert r.ok is False
    assert "飞书" in (r.error or "") or "feishu" in (r.error or "").lower()


@pytest.mark.asyncio
async def test_wecom_unconfigured() -> None:
    r = await IntegrationsTools().invoke(_call("wecom_send", {"text": "x"}))
    assert r.ok is False
    assert "企业微信" in (r.error or "") or "wecom" in (r.error or "").lower()


@pytest.mark.asyncio
async def test_dingtalk_unconfigured() -> None:
    r = await IntegrationsTools().invoke(_call("dingtalk_send", {"text": "x"}))
    assert r.ok is False
    assert "钉钉" in (r.error or "") or "dingtalk" in (r.error or "").lower()


@pytest.mark.asyncio
async def test_qq_unconfigured() -> None:
    r = await IntegrationsTools().invoke(_call("qq_send", {
        "text": "x", "target_type": "group", "target_id": "1",
    }))
    assert r.ok is False
    assert "QQ" in (r.error or "") or "OneBot" in (r.error or "")


@pytest.mark.asyncio
async def test_feishu_send_text_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify feishu_send POSTs the right shape: msg_type=text, content.text=...,
    no signature when secret not set."""
    captured: dict[str, Any] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"code": 0, "msg": "ok"})

    transport = httpx.MockTransport(_handler)
    real_client = httpx.AsyncClient
    def patched(*a, **kw):
        kw["transport"] = transport
        return real_client(*a, **kw)
    monkeypatch.setattr("httpx.AsyncClient", patched)

    tools = IntegrationsTools({
        "feishu": {"webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/abc"},
    })
    r = await tools.invoke(_call("feishu_send", {"text": "hello"}))
    assert r.ok is True
    assert captured["body"]["msg_type"] == "text"
    assert captured["body"]["content"]["text"] == "hello"
    # No secret → no timestamp/sign fields
    assert "sign" not in captured["body"]


@pytest.mark.asyncio
async def test_wecom_markdown_msg_type(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})

    transport = httpx.MockTransport(_handler)
    real_client = httpx.AsyncClient
    def patched(*a, **kw):
        kw["transport"] = transport
        return real_client(*a, **kw)
    monkeypatch.setattr("httpx.AsyncClient", patched)

    tools = IntegrationsTools({
        "wecom": {"webhook_url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=x"},
    })
    r = await tools.invoke(_call("wecom_send", {
        "text": "# 头", "msg_type": "markdown",
    }))
    assert r.ok is True
    assert captured["body"]["msgtype"] == "markdown"
    assert captured["body"]["markdown"]["content"] == "# 头"


@pytest.mark.asyncio
async def test_dingtalk_signs_url_when_secret_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """加签设置：钉钉机器人有 'secret' 时，webhook URL 必须 append
    timestamp + sign query params。"""
    captured: dict[str, Any] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})

    transport = httpx.MockTransport(_handler)
    real_client = httpx.AsyncClient
    def patched(*a, **kw):
        kw["transport"] = transport
        return real_client(*a, **kw)
    monkeypatch.setattr("httpx.AsyncClient", patched)

    tools = IntegrationsTools({
        "dingtalk": {
            "webhook_url": "https://oapi.dingtalk.com/robot/send?access_token=abc",
            "secret": "SEC123",
        },
    })
    r = await tools.invoke(_call("dingtalk_send", {"text": "hi"}))
    assert r.ok is True
    assert "timestamp=" in captured["url"]
    assert "sign=" in captured["url"]
    # Original access_token must still be there
    assert "access_token=abc" in captured["url"]


@pytest.mark.asyncio
async def test_qq_send_routes_group(monkeypatch: pytest.MonkeyPatch) -> None:
    """OneBot v11: send_group_msg endpoint, group_id as int, message body."""
    captured: dict[str, Any] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={
            "status": "ok", "retcode": 0, "data": {"message_id": 99},
        })

    transport = httpx.MockTransport(_handler)
    real_client = httpx.AsyncClient
    def patched(*a, **kw):
        kw["transport"] = transport
        return real_client(*a, **kw)
    monkeypatch.setattr("httpx.AsyncClient", patched)

    tools = IntegrationsTools({
        "qq": {"base_url": "http://127.0.0.1:5700", "access_token": "T1"},
    })
    r = await tools.invoke(_call("qq_send", {
        "text": "hi", "target_type": "group", "target_id": "12345",
    }))
    assert r.ok is True
    assert captured["url"].endswith("/send_group_msg")
    assert captured["body"]["group_id"] == 12345
    assert captured["body"]["message"] == "hi"
    assert captured["auth"] == "Bearer T1"


@pytest.mark.asyncio
async def test_qq_send_routes_private(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"status": "ok", "data": {"message_id": 1}})

    transport = httpx.MockTransport(_handler)
    real_client = httpx.AsyncClient
    def patched(*a, **kw):
        kw["transport"] = transport
        return real_client(*a, **kw)
    monkeypatch.setattr("httpx.AsyncClient", patched)

    tools = IntegrationsTools({
        "qq": {"base_url": "http://127.0.0.1:5700"},
    })
    r = await tools.invoke(_call("qq_send", {
        "text": "私聊", "target_type": "private", "target_id": "999",
    }))
    assert r.ok is True
    assert captured["url"].endswith("/send_private_msg")
    assert captured["body"]["user_id"] == 999
