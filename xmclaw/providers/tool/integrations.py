"""IntegrationsTools — make ``config.integrations.*`` actually do something.

B-143. Until now ``daemon/config.json::integrations`` had stub fields
(slack.bot_token / discord.bot_token / telegram.bot_token / github.token /
notion.api_key) but ZERO Python code read them. The user kept saying
"外部集成这里太少" — actually we had 5 stubs, 0 working integrations.

This provider closes that loop by:

  1. Adding tools that the agent can autonomously call when the user
     asks "send a message to slack" / "create a github issue" / etc.
  2. Reading credentials from ``config.integrations.<service>.*`` so
     the user configures once and every tool just works.
  3. Adding 3 universals (webhook_send / email_send / rss_fetch)
     because peers (Hermes / OpenClaw) all ship them and they're
     the catch-all path when the user's target service has no
     dedicated tool.

Layering: structurally implements ``ToolProvider`` (no inheritance) so
``providers/tool/`` doesn't grow a daemon dep. Composed into
``agent._tools`` in ``daemon/app.py`` post-construction (same pattern
as agent_inter / content / automation).

Trust model: outbound HTTP is unsandboxed — these tools send wherever
the URL points. The user authored the config that holds the auth
tokens; the agent acts on their behalf. No additional consent prompt;
the LLM is expected to confirm intent in its assistant text before
invoking a destructive integration tool.
"""
from __future__ import annotations

import json
import smtplib
import time
from email.message import EmailMessage
from typing import Any

import httpx

from xmclaw.core.ir import ToolCall, ToolResult, ToolSpec
from xmclaw.providers.tool.base import ToolProvider


# ── Specs ─────────────────────────────────────────────────────────


_WEBHOOK_SEND_SPEC = ToolSpec(
    name="webhook_send",
    description=(
        "Send an HTTP request to an arbitrary URL. The catch-all "
        "integration tool — covers IFTTT, Zapier, n8n, custom "
        "webhooks, and any service without a dedicated XMclaw tool.\n\n"
        "Use POST with a JSON body for most webhook handlers. Returns "
        "{status_code, headers, body} so the agent can confirm "
        "success or read the response."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Target URL (https recommended)."},
            "method": {"type": "string", "description": "GET/POST/PUT/PATCH/DELETE. Default POST."},
            "json": {"type": "object", "description": "JSON body (POST/PUT/PATCH)."},
            "headers": {"type": "object", "description": "Extra headers, e.g. {'Authorization': 'Bearer ...'}."},
            "timeout_s": {"type": "integer", "description": "1-60, default 15."},
        },
        "required": ["url"],
    },
)


_EMAIL_SEND_SPEC = ToolSpec(
    name="email_send",
    description=(
        "Send an email via SMTP. Reads ``config.integrations.email.*`` "
        "(smtp_host / smtp_port / username / password / from / use_tls). "
        "Use for breakthrough notifications, daily digests, error "
        "alerts the user should see in their inbox."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "to": {"type": "string", "description": "Recipient (or comma-separated list)."},
            "subject": {"type": "string"},
            "body": {"type": "string", "description": "Plain text body."},
            "cc": {"type": "string"},
            "bcc": {"type": "string"},
        },
        "required": ["to", "subject", "body"],
    },
)


_RSS_FETCH_SPEC = ToolSpec(
    name="rss_fetch",
    description=(
        "Fetch and parse an RSS / Atom feed. Returns up to 20 most "
        "recent entries with title / link / published / summary. "
        "Useful for blog/news monitoring jobs combined with cron_create. "
        "Requires ``feedparser`` (``pip install feedparser``)."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "limit": {"type": "integer", "description": "Max entries (1-50, default 20)."},
        },
        "required": ["url"],
    },
)


_SLACK_SEND_SPEC = ToolSpec(
    name="slack_send",
    description=(
        "Post a message to Slack. Uses ``config.integrations.slack."
        "bot_token`` (xoxb-...). Channel falls back to "
        "``integrations.slack.channel`` if not passed.\n\n"
        "For threaded replies pass ``thread_ts``. For DMs pass a user "
        "ID (U...) as the channel."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "channel": {"type": "string", "description": "Channel name (#dev) or user id."},
            "thread_ts": {"type": "string", "description": "Reply in thread."},
        },
        "required": ["text"],
    },
)


_TELEGRAM_SEND_SPEC = ToolSpec(
    name="telegram_send",
    description=(
        "Send a Telegram message via the Bot API. Reads "
        "``config.integrations.telegram.{bot_token, chat_id}``. "
        "Pass ``chat_id`` to override the default. Useful for personal "
        "alerts (build status, daily summary) on the user's phone."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "chat_id": {"type": "string", "description": "Override default chat. Optional."},
            "parse_mode": {
                "type": "string",
                "enum": ["MarkdownV2", "HTML", "Markdown", ""],
                "description": "Telegram parse mode. Default plain.",
            },
        },
        "required": ["text"],
    },
)


_DISCORD_SEND_SPEC = ToolSpec(
    name="discord_send",
    description=(
        "Send a Discord message via Bot API. Reads "
        "``config.integrations.discord.{bot_token, channel_id}``. "
        "Pass ``channel_id`` to override."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "content": {"type": "string"},
            "channel_id": {"type": "string", "description": "Override default."},
        },
        "required": ["content"],
    },
)


_GITHUB_CREATE_ISSUE_SPEC = ToolSpec(
    name="github_create_issue",
    description=(
        "Create a GitHub issue. Reads ``config.integrations.github."
        "{token, repo}`` (repo format: ``owner/name``). Returns the "
        "new issue's URL + number."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "body": {"type": "string"},
            "labels": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["title"],
    },
)


_NOTION_CREATE_PAGE_SPEC = ToolSpec(
    name="notion_create_page",
    description=(
        "Create a page in a Notion database. Reads "
        "``config.integrations.notion.{api_key, database_id}``. "
        "``properties`` should match the database schema "
        "(see Notion API docs); ``content`` is appended as a single "
        "paragraph block under the page."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Page title (assumes a 'Name'/'title' property)."},
            "content": {"type": "string", "description": "Body text. Optional."},
            "properties": {"type": "object", "description": "Extra Notion properties (override). Optional."},
        },
        "required": ["title"],
    },
)


# ── Provider ──────────────────────────────────────────────────────


class IntegrationsTools(ToolProvider):
    """Reads config.integrations.<service>.* and exposes one tool per
    service. Tools that lack credentials fail with a clear "configure
    in Settings → Integrations" message instead of silently no-op-ing.
    """

    def __init__(self, integrations_config: dict[str, Any] | None = None) -> None:
        self._cfg = integrations_config or {}

    def _section(self, key: str) -> dict[str, Any]:
        s = self._cfg.get(key)
        return s if isinstance(s, dict) else {}

    def list_tools(self) -> list[ToolSpec]:
        # Always advertise — even when the user hasn't configured a
        # service, the LLM gets a clear "configure first" error if it
        # tries. Hiding tools just makes the agent guess we don't have
        # them and reach for clumsier paths.
        return [
            _WEBHOOK_SEND_SPEC,
            _EMAIL_SEND_SPEC,
            _RSS_FETCH_SPEC,
            _SLACK_SEND_SPEC,
            _TELEGRAM_SEND_SPEC,
            _DISCORD_SEND_SPEC,
            _GITHUB_CREATE_ISSUE_SPEC,
            _NOTION_CREATE_PAGE_SPEC,
        ]

    async def invoke(self, call: ToolCall) -> ToolResult:
        t0 = time.perf_counter()
        try:
            name = call.name
            if name == "webhook_send":
                return await self._webhook(call, t0)
            if name == "email_send":
                return await self._email(call, t0)
            if name == "rss_fetch":
                return await self._rss(call, t0)
            if name == "slack_send":
                return await self._slack(call, t0)
            if name == "telegram_send":
                return await self._telegram(call, t0)
            if name == "discord_send":
                return await self._discord(call, t0)
            if name == "github_create_issue":
                return await self._github_issue(call, t0)
            if name == "notion_create_page":
                return await self._notion_page(call, t0)
        except Exception as exc:  # noqa: BLE001
            return _fail(call, t0, f"{type(exc).__name__}: {exc}")
        return _fail(call, t0, f"unknown tool: {call.name!r}")

    # ── handlers ─────────────────────────────────────────────────

    async def _webhook(self, call: ToolCall, t0: float) -> ToolResult:
        args = call.args or {}
        url = (args.get("url") or "").strip()
        if not url:
            return _fail(call, t0, "url required")
        method = (args.get("method") or "POST").upper()
        timeout_s = max(1, min(int(args.get("timeout_s", 15)), 60))
        headers = args.get("headers") if isinstance(args.get("headers"), dict) else None
        body = args.get("json")
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            r = await client.request(
                method, url,
                json=body if body is not None else None,
                headers=headers,
            )
        return _ok(call, t0, json.dumps({
            "status_code": r.status_code,
            "headers": dict(r.headers),
            "body": r.text[:8000],
        }, ensure_ascii=False))

    async def _email(self, call: ToolCall, t0: float) -> ToolResult:
        cfg = self._section("email")
        if not cfg.get("smtp_host"):
            return _fail(call, t0, (
                "email integration not configured. Set "
                "config.integrations.email.{smtp_host, smtp_port, "
                "username, password, from, use_tls}."
            ))
        args = call.args or {}
        msg = EmailMessage()
        msg["From"] = cfg.get("from") or cfg.get("username") or ""
        msg["To"] = args.get("to", "")
        msg["Subject"] = args.get("subject", "")
        if args.get("cc"):
            msg["Cc"] = args["cc"]
        if args.get("bcc"):
            msg["Bcc"] = args["bcc"]
        msg.set_content(args.get("body", ""))
        host = cfg["smtp_host"]
        port = int(cfg.get("smtp_port") or 587)
        use_tls = bool(cfg.get("use_tls", True))
        smtp_cls = smtplib.SMTP_SSL if port == 465 else smtplib.SMTP
        with smtp_cls(host, port, timeout=20) as srv:
            if use_tls and port != 465:
                srv.starttls()
            if cfg.get("username") and cfg.get("password"):
                srv.login(cfg["username"], cfg["password"])
            srv.send_message(msg)
        return _ok(call, t0, json.dumps({
            "ok": True, "to": args.get("to"), "subject": args.get("subject"),
        }, ensure_ascii=False))

    async def _rss(self, call: ToolCall, t0: float) -> ToolResult:
        try:
            import feedparser  # type: ignore
        except ImportError:
            return _fail(call, t0, (
                "rss_fetch needs ``feedparser``. "
                "Install with: pip install feedparser"
            ))
        args = call.args or {}
        url = (args.get("url") or "").strip()
        if not url:
            return _fail(call, t0, "url required")
        limit = max(1, min(int(args.get("limit", 20)), 50))
        feed = feedparser.parse(url)
        entries = []
        for e in feed.entries[:limit]:
            entries.append({
                "title": getattr(e, "title", ""),
                "link": getattr(e, "link", ""),
                "published": getattr(e, "published", "") or getattr(e, "updated", ""),
                "summary": (getattr(e, "summary", "") or "")[:600],
            })
        return _ok(call, t0, json.dumps({
            "feed_title": getattr(feed.feed, "title", ""),
            "count": len(entries),
            "entries": entries,
        }, ensure_ascii=False))

    async def _slack(self, call: ToolCall, t0: float) -> ToolResult:
        cfg = self._section("slack")
        token = cfg.get("bot_token", "").strip()
        if not token:
            return _fail(call, t0, (
                "slack not configured. Set "
                "config.integrations.slack.bot_token (xoxb-...)."
            ))
        args = call.args or {}
        channel = args.get("channel") or cfg.get("channel") or ""
        if not channel:
            return _fail(call, t0, "channel required (or set integrations.slack.channel)")
        payload = {"channel": channel, "text": args.get("text", "")}
        if args.get("thread_ts"):
            payload["thread_ts"] = args["thread_ts"]
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                "https://slack.com/api/chat.postMessage",
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
            )
        data = r.json()
        if not data.get("ok"):
            return _fail(call, t0, f"slack error: {data.get('error', 'unknown')}")
        return _ok(call, t0, json.dumps({
            "ok": True, "channel": data.get("channel"), "ts": data.get("ts"),
        }))

    async def _telegram(self, call: ToolCall, t0: float) -> ToolResult:
        cfg = self._section("telegram")
        token = cfg.get("bot_token", "").strip()
        if not token:
            return _fail(call, t0, (
                "telegram not configured. Set "
                "config.integrations.telegram.{bot_token, chat_id}."
            ))
        args = call.args or {}
        chat_id = args.get("chat_id") or cfg.get("chat_id") or ""
        if not chat_id:
            return _fail(call, t0, "chat_id required (or set integrations.telegram.chat_id)")
        payload = {"chat_id": chat_id, "text": args.get("text", "")}
        if args.get("parse_mode"):
            payload["parse_mode"] = args["parse_mode"]
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json=payload,
            )
        data = r.json()
        if not data.get("ok"):
            return _fail(call, t0, f"telegram error: {data.get('description', 'unknown')}")
        return _ok(call, t0, json.dumps({
            "ok": True, "message_id": data.get("result", {}).get("message_id"),
        }))

    async def _discord(self, call: ToolCall, t0: float) -> ToolResult:
        cfg = self._section("discord")
        token = cfg.get("bot_token", "").strip()
        if not token:
            return _fail(call, t0, (
                "discord not configured. Set "
                "config.integrations.discord.{bot_token, channel_id}."
            ))
        args = call.args or {}
        channel = args.get("channel_id") or cfg.get("channel_id") or ""
        if not channel:
            return _fail(call, t0, "channel_id required (or set integrations.discord.channel_id)")
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"https://discord.com/api/v10/channels/{channel}/messages",
                headers={"Authorization": f"Bot {token}"},
                json={"content": args.get("content", "")},
            )
        if r.status_code >= 300:
            return _fail(call, t0, f"discord HTTP {r.status_code}: {r.text[:200]}")
        msg_id = r.json().get("id")
        return _ok(call, t0, json.dumps({"ok": True, "message_id": msg_id}))

    async def _github_issue(self, call: ToolCall, t0: float) -> ToolResult:
        cfg = self._section("github")
        token = cfg.get("token", "").strip()
        repo = cfg.get("repo", "").strip()
        if not token or not repo:
            return _fail(call, t0, (
                "github not configured. Set "
                "config.integrations.github.{token, repo}."
            ))
        args = call.args or {}
        body = {"title": args.get("title", "")}
        if args.get("body"):
            body["body"] = args["body"]
        if args.get("labels") and isinstance(args["labels"], list):
            body["labels"] = args["labels"]
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"https://api.github.com/repos/{repo}/issues",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                },
                json=body,
            )
        if r.status_code >= 300:
            return _fail(call, t0, f"github HTTP {r.status_code}: {r.text[:200]}")
        d = r.json()
        return _ok(call, t0, json.dumps({
            "ok": True, "number": d.get("number"), "url": d.get("html_url"),
        }))

    async def _notion_page(self, call: ToolCall, t0: float) -> ToolResult:
        cfg = self._section("notion")
        api_key = cfg.get("api_key", "").strip()
        db_id = cfg.get("database_id", "").strip()
        if not api_key or not db_id:
            return _fail(call, t0, (
                "notion not configured. Set "
                "config.integrations.notion.{api_key, database_id}."
            ))
        args = call.args or {}
        title = args.get("title", "")
        properties = args.get("properties") if isinstance(args.get("properties"), dict) else {}
        if "Name" not in properties and "title" not in properties:
            properties["Name"] = {
                "title": [{"text": {"content": title}}],
            }
        body: dict[str, Any] = {
            "parent": {"database_id": db_id},
            "properties": properties,
        }
        if args.get("content"):
            body["children"] = [{
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [{"type": "text", "text": {"content": args["content"]}}],
                },
            }]
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                "https://api.notion.com/v1/pages",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Notion-Version": "2022-06-28",
                    "Content-Type": "application/json",
                },
                json=body,
            )
        if r.status_code >= 300:
            return _fail(call, t0, f"notion HTTP {r.status_code}: {r.text[:200]}")
        d = r.json()
        return _ok(call, t0, json.dumps({
            "ok": True, "page_id": d.get("id"), "url": d.get("url"),
        }))


# ── helpers ───────────────────────────────────────────────────────


def _ok(call: ToolCall, t0: float, content: Any) -> ToolResult:
    return ToolResult(
        call_id=call.id, ok=True, content=content,
        latency_ms=(time.perf_counter() - t0) * 1000.0,
    )


def _fail(call: ToolCall, t0: float, err: str) -> ToolResult:
    return ToolResult(
        call_id=call.id, ok=False, content=None, error=err,
        latency_ms=(time.perf_counter() - t0) * 1000.0,
    )
