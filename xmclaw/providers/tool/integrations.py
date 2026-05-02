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


# ── B-144: 国内主流聊天工具 ─────────────────────────────────────


_FEISHU_SEND_SPEC = ToolSpec(
    name="feishu_send",
    description=(
        "Send a message to a Feishu (飞书 / Lark) group via custom "
        "bot webhook. Reads ``config.integrations.feishu.webhook_url`` "
        "(从飞书群里 添加自定义机器人 复制的 URL)。可选 ``secret`` "
        "(签名校验密钥，加了 secret 的机器人需要)。\n\n"
        "Use for daily standups, build alerts, anything you'd want to "
        "see in a Feishu group chat."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "msg_type": {
                "type": "string",
                "enum": ["text", "post"],
                "description": "Default 'text'. 'post' for rich content (not yet implemented).",
            },
        },
        "required": ["text"],
    },
)


_WECOM_SEND_SPEC = ToolSpec(
    name="wecom_send",
    description=(
        "Send to a WeChat Work (企业微信 / WeCom) group via bot "
        "webhook. Reads ``config.integrations.wecom.webhook_url`` "
        "(从企业微信群 添加群机器人 复制的 URL — 形如 "
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...)。\n\n"
        "注意：这是企业微信群机器人，不是个人微信。个人微信无官方 API。"
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "msg_type": {
                "type": "string",
                "enum": ["text", "markdown"],
                "description": "Default 'text'. 'markdown' renders headers/lists.",
            },
            "mentioned_list": {
                "type": "array",
                "items": {"type": "string"},
                "description": "userid 列表，'@all' = 全员。",
            },
        },
        "required": ["text"],
    },
)


_DINGTALK_SEND_SPEC = ToolSpec(
    name="dingtalk_send",
    description=(
        "Send a message to a DingTalk (钉钉) group via custom bot "
        "webhook. Reads ``config.integrations.dingtalk.webhook_url`` "
        "(从钉钉群里 自定义机器人 复制的 access_token URL)。可选 "
        "``secret`` 用于签名校验（钉钉机器人安全设置之一）。"
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "msg_type": {
                "type": "string",
                "enum": ["text", "markdown"],
                "description": "Default 'text'.",
            },
            "title": {
                "type": "string",
                "description": "Required when msg_type='markdown'. The card title.",
            },
            "at_mobiles": {
                "type": "array",
                "items": {"type": "string"},
                "description": "手机号列表，被 @ 的人。",
            },
            "at_all": {"type": "boolean", "description": "@ 所有人。"},
        },
        "required": ["text"],
    },
)


_QQ_SEND_SPEC = ToolSpec(
    name="qq_send",
    description=(
        "Send a QQ message via a OneBot v11 HTTP API endpoint "
        "(go-cqhttp / NapCat / Lagrange / Shamrock 等实现)。\n\n"
        "用户必须自己跑一个 OneBot 实现的 bot 客户端，把 HTTP API "
        "暴露给 daemon。读 ``config.integrations.qq.{base_url, "
        "access_token}``。``target_type`` = 'group' | 'private'，"
        "``target_id`` 是群号或好友 QQ 号。\n\n"
        "腾讯没有公开的个人 QQ API — 这条路依赖第三方 bot 框架，"
        "需要用户自己运维。"
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "target_type": {
                "type": "string",
                "enum": ["group", "private"],
            },
            "target_id": {
                "type": "string",
                "description": "群号 (group_id) 或 QQ 号 (user_id)。",
            },
        },
        "required": ["text", "target_type", "target_id"],
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

    @staticmethod
    def _has_real_value(d: dict[str, Any], *keys: str) -> bool:
        """One of ``keys`` is set to a non-stub value.

        Stub markers we've seen on disk:
          * empty string
          * ``"YOUR_*"`` placeholders the config wizard writes
          * literal ``"changeme"`` / ``"todo"``
        """
        for k in keys:
            v = d.get(k)
            if not isinstance(v, str):
                continue
            v = v.strip()
            if not v:
                continue
            low = v.lower()
            if low.startswith("your_") or low in ("changeme", "todo", "tbd"):
                continue
            return True
        return False

    def _is_enabled(self, section: str, *credential_keys: str) -> bool:
        """B-180: integration is exposed only when ``enabled: true``
        AND at least one credential key holds a real (non-stub) value.

        Pre-B-180 the provider unconditionally listed all 12 tools so
        the agent could "discover and try them." Real-data audit
        (events.db) showed the agent never tries unconfigured ones —
        it just routes around them. Net effect was 12 stubs cluttering
        the LLM's tool spec on every turn for zero benefit.
        """
        sec = self._section(section)
        if not sec:
            return False
        if not bool(sec.get("enabled", True)):
            # Explicit `enabled: false` hides regardless of creds.
            return False
        return self._has_real_value(sec, *credential_keys)

    def list_tools(self) -> list[ToolSpec]:
        """B-180: tool list is gated by configured credentials.

        Always-on (no per-service config required):
          * ``webhook_send`` — URL is always per-call; no auth needed
            in the tool itself (caller passes Authorization header
            verbatim if their endpoint wants one)
          * ``rss_fetch`` — URL per-call, anonymous fetch

        Config-gated (only listed when the user has actually
        configured the credentials):
          * email_send / slack_send / telegram_send / discord_send /
            github_create_issue / notion_create_page / feishu_send /
            wecom_send / dingtalk_send / qq_send
        """
        out: list[ToolSpec] = [
            _WEBHOOK_SEND_SPEC,
            _RSS_FETCH_SPEC,
        ]

        if self._is_enabled("email", "smtp_host"):
            out.append(_EMAIL_SEND_SPEC)
        if self._is_enabled("slack", "bot_token", "webhook_url"):
            out.append(_SLACK_SEND_SPEC)
        if self._is_enabled("telegram", "bot_token"):
            out.append(_TELEGRAM_SEND_SPEC)
        if self._is_enabled("discord", "bot_token", "webhook_url"):
            out.append(_DISCORD_SEND_SPEC)
        if self._is_enabled("github", "token"):
            out.append(_GITHUB_CREATE_ISSUE_SPEC)
        if self._is_enabled("notion", "api_key"):
            out.append(_NOTION_CREATE_PAGE_SPEC)
        # B-144: 国内主流聊天工具
        if self._is_enabled("feishu", "app_id", "webhook_url"):
            out.append(_FEISHU_SEND_SPEC)
        if self._is_enabled("wecom", "webhook_url", "corp_id"):
            out.append(_WECOM_SEND_SPEC)
        if self._is_enabled("dingtalk", "webhook_url", "access_token"):
            out.append(_DINGTALK_SEND_SPEC)
        if self._is_enabled("qq", "bot_url", "webhook_url"):
            out.append(_QQ_SEND_SPEC)
        return out

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
            # B-144: 国内主流聊天工具
            if name == "feishu_send":
                return await self._feishu(call, t0)
            if name == "wecom_send":
                return await self._wecom(call, t0)
            if name == "dingtalk_send":
                return await self._dingtalk(call, t0)
            if name == "qq_send":
                return await self._qq(call, t0)
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


    # ── B-144: 国内主流聊天工具 handlers ──────────────────────────

    async def _feishu(self, call: ToolCall, t0: float) -> ToolResult:
        cfg = self._section("feishu")
        url = cfg.get("webhook_url", "").strip()
        if not url:
            return _fail(call, t0, (
                "feishu (飞书) 未配置。在群里 添加自定义机器人 → "
                "复制 webhook URL 填到 config.integrations.feishu."
                "webhook_url。"
            ))
        args = call.args or {}
        text = args.get("text", "")
        body: dict[str, Any] = {
            "msg_type": "text",
            "content": {"text": text},
        }
        # Optional signature (机器人开了 '签名校验' 安全设置时必填)
        secret = cfg.get("secret", "").strip()
        if secret:
            import base64
            import hashlib
            import hmac
            ts = str(int(time.time()))
            string_to_sign = f"{ts}\n{secret}"
            sign = base64.b64encode(
                hmac.new(string_to_sign.encode("utf-8"), b"",
                         digestmod=hashlib.sha256).digest()
            ).decode("utf-8")
            body["timestamp"] = ts
            body["sign"] = sign
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(url, json=body)
        data = r.json() if r.status_code < 500 else {}
        if data.get("code") != 0 and r.status_code >= 300:
            return _fail(call, t0, (
                f"feishu HTTP {r.status_code}: {r.text[:200]}"
            ))
        if data.get("code") not in (0, None):
            return _fail(call, t0, (
                f"feishu error code={data.get('code')} "
                f"msg={data.get('msg', '')}"
            ))
        return _ok(call, t0, json.dumps({"ok": True}))

    async def _wecom(self, call: ToolCall, t0: float) -> ToolResult:
        cfg = self._section("wecom")
        url = cfg.get("webhook_url", "").strip()
        if not url:
            return _fail(call, t0, (
                "企业微信未配置。在群机器人设置里复制 webhook URL "
                "填到 config.integrations.wecom.webhook_url。"
                "（注意：个人微信无官方 API，此工具针对企业微信。）"
            ))
        args = call.args or {}
        msg_type = (args.get("msg_type") or "text").lower()
        if msg_type == "markdown":
            body = {
                "msgtype": "markdown",
                "markdown": {"content": args.get("text", "")},
            }
        else:
            payload: dict[str, Any] = {"content": args.get("text", "")}
            mentioned = args.get("mentioned_list")
            if isinstance(mentioned, list) and mentioned:
                payload["mentioned_list"] = mentioned
            body = {"msgtype": "text", "text": payload}
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(url, json=body)
        data = r.json() if r.status_code < 500 else {}
        if data.get("errcode") not in (0, None):
            return _fail(call, t0, (
                f"wecom error errcode={data.get('errcode')} "
                f"errmsg={data.get('errmsg', '')}"
            ))
        return _ok(call, t0, json.dumps({"ok": True}))

    async def _dingtalk(self, call: ToolCall, t0: float) -> ToolResult:
        cfg = self._section("dingtalk")
        url = cfg.get("webhook_url", "").strip()
        if not url:
            return _fail(call, t0, (
                "钉钉未配置。在群里添加自定义机器人 → 复制 webhook URL "
                "填到 config.integrations.dingtalk.webhook_url。"
            ))
        args = call.args or {}
        msg_type = (args.get("msg_type") or "text").lower()
        if msg_type == "markdown":
            title = args.get("title", "").strip() or "通知"
            body: dict[str, Any] = {
                "msgtype": "markdown",
                "markdown": {"title": title, "text": args.get("text", "")},
            }
        else:
            body = {
                "msgtype": "text",
                "text": {"content": args.get("text", "")},
            }
        # @ 控制
        at_block: dict[str, Any] = {}
        if args.get("at_mobiles") and isinstance(args["at_mobiles"], list):
            at_block["atMobiles"] = args["at_mobiles"]
        if args.get("at_all"):
            at_block["isAtAll"] = True
        if at_block:
            body["at"] = at_block
        # Sign URL (机器人勾选 '加签' 安全设置时必需)
        secret = cfg.get("secret", "").strip()
        send_url = url
        if secret:
            import base64
            import hashlib
            import hmac
            from urllib.parse import quote_plus
            ts = str(round(time.time() * 1000))
            string_to_sign = f"{ts}\n{secret}"
            sign = quote_plus(base64.b64encode(
                hmac.new(secret.encode("utf-8"),
                         string_to_sign.encode("utf-8"),
                         digestmod=hashlib.sha256).digest()
            ).decode("utf-8"))
            sep = "&" if "?" in url else "?"
            send_url = f"{url}{sep}timestamp={ts}&sign={sign}"
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(send_url, json=body)
        data = r.json() if r.status_code < 500 else {}
        if data.get("errcode") not in (0, None):
            return _fail(call, t0, (
                f"dingtalk error errcode={data.get('errcode')} "
                f"errmsg={data.get('errmsg', '')}"
            ))
        return _ok(call, t0, json.dumps({"ok": True}))

    async def _qq(self, call: ToolCall, t0: float) -> ToolResult:
        cfg = self._section("qq")
        base_url = cfg.get("base_url", "").strip().rstrip("/")
        if not base_url:
            return _fail(call, t0, (
                "QQ 未配置。需要先跑一个 OneBot v11 兼容 bot "
                "(go-cqhttp / NapCat / Lagrange) 暴露 HTTP API，"
                "然后填 config.integrations.qq.{base_url, "
                "access_token}。腾讯没有公开个人 QQ API。"
            ))
        args = call.args or {}
        target_type = args.get("target_type")
        target_id = args.get("target_id")
        if target_type not in ("group", "private"):
            return _fail(call, t0, "target_type must be 'group' or 'private'")
        if not target_id:
            return _fail(call, t0, "target_id required")
        # OneBot v11: send_group_msg / send_private_msg
        endpoint = (
            "/send_group_msg" if target_type == "group"
            else "/send_private_msg"
        )
        body: dict[str, Any] = {"message": args.get("text", "")}
        if target_type == "group":
            body["group_id"] = int(target_id)
        else:
            body["user_id"] = int(target_id)
        headers = {}
        access_token = cfg.get("access_token", "").strip()
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{base_url}{endpoint}",
                json=body,
                headers=headers,
            )
        if r.status_code >= 300:
            return _fail(call, t0, (
                f"OneBot HTTP {r.status_code}: {r.text[:200]}"
            ))
        data = r.json()
        if data.get("status") == "failed":
            return _fail(call, t0, (
                f"OneBot retcode={data.get('retcode')} "
                f"wording={data.get('wording', '')}"
            ))
        return _ok(call, t0, json.dumps({
            "ok": True,
            "message_id": data.get("data", {}).get("message_id"),
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
