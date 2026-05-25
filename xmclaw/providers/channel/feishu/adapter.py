"""FeishuAdapter — bidirectional 飞书 / Lark channel.

B-145. Implements the scaffolded :file:`__init__.py` MANIFEST as a
working :class:`ChannelAdapter`. Uses ``lark-oapi`` WebSocket long-poll
mode (``Client.ws.start``) so the daemon doesn't need a public IP /
cloudflared tunnel — feishu's open-platform pushes events to us
through their existing WS.

Inbound flow
------------

  飞书群里 @机器人 → lark 推 P2ImMessageReceiveV1 →
  EventDispatcherHandler 把 event 投到 _on_message →
  我们包成 InboundMessage 喂给 subscriber (典型 = ChannelDispatcher)
  → dispatcher 转给 AgentLoop.run_turn(session_id=feishu:<chat_id>) →
  AgentLoop 触发 LLM_RESPONSE 事件 → ChannelDispatcher 把 reply text
  通过 adapter.send() 回到飞书群

Outbound flow
-------------

  ``adapter.send(target, payload)`` → ReplyMessageRequest（带
  reply_to=msg_id 时引用回复，否则 SendMessageRequest 单聊群）→
  飞书 OpenAPI POST /im/v1/messages/{msg_id}/reply

Config (read from config.integrations.feishu_channel.{...})
-----------------------------------------------------------

  app_id      : 'cli_xxx' — 飞书开放平台应用 ID
  app_secret  : 应用 secret
  encrypt_key : (可选) 事件加密 key，开了 '事件加密' 才填
  verify_token: (可选) 旧版校验 token，长连模式可不填

The adapter starts a background task that runs ``client.ws.start``
forever; ``stop`` cancels it. Failures inside the WS loop log + retry
via lark-oapi's own reconnect machinery.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re as _re_md
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any

from xmclaw.providers.channel.base import (
    ChannelAdapter,
    ChannelTarget,
    InboundMessage,
    OutboundMessage,
)


_log = logging.getLogger(__name__)


# B-209: detect markdown in outbound text. When present, send as
# msg_type=interactive (card with markdown element) so feishu actually
# RENDERS bold / lists / code blocks instead of showing the raw chars.
# Plain text replies stay msg_type=text — cards add chrome that's
# overkill for "OK 收到" one-liners.
#
# Heuristic: any of these markers triggers card-mode.
#   **bold**, *italic*, __underline__, _italic_
#   `code`, ```fence```
#   # heading (line start)
#   - bullet, * bullet, 1. ordered (line start)
#   > quote (line start)
#   [text](url) link
#   --- horizontal rule (line start)
#   | table | row |
_MARKDOWN_MARKERS = _re_md.compile(
    r"(\*\*[^\n*]+\*\*"          # **bold**
    r"|`[^\n`]+`"                # `inline code`
    r"|```"                      # fenced code block
    r"|^\s*#{1,6}\s+\S"          # # heading
    r"|^\s*[-*]\s+\S"            # - bullet  / * bullet
    r"|^\s*\d+\.\s+\S"           # 1. ordered list
    r"|^\s*>\s+\S"               # > quote
    r"|\[[^\]\n]+\]\([^)\n]+\)"  # [link](url)
    r"|^\s*-{3,}\s*$"            # --- hr
    r"|^\s*\|.+\|\s*$)",         # | table | row |
    _re_md.MULTILINE,
)

# Lark interactive cards have a server-side size cap (~30k chars in
# practice). Stay well under so big tool dumps still go through as
# plain text rather than fail the card POST.
_CARD_MAX_CHARS = 24_000

# Wave-33: max elements per card before we split into multiple cards.
_CARD_MAX_ELEMENTS = 30


def _looks_like_markdown(text: str) -> bool:
    """B-209: True when ``text`` has at least one common markdown
    marker. Used to route outbound replies between text and card."""
    if not text:
        return False
    return bool(_MARKDOWN_MARKERS.search(text))


def _build_lark_markdown_card(content: str) -> dict[str, Any]:
    """Wrap markdown text in a Lark interactive-card payload.

    Card schema reference:
      https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/feishu-cards/card-content-component/markdown
    """
    return {
        "config": {
            "wide_screen_mode": True,
        },
        "elements": [
            {
                "tag": "markdown",
                "content": content,
                "text_align": "left",
            },
        ],
    }


def _build_lark_rich_card(
    elements: list[dict[str, Any]],
    *,
    header: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a Lark interactive card with header + arbitrary elements."""
    card: dict[str, Any] = {
        "config": {"wide_screen_mode": True},
        "elements": list(elements),
    }
    if header is not None:
        card["header"] = header
    return card


def _build_process_card(tool_name: str, status: str) -> dict[str, Any]:
    """Wave-33: compact process indicator card for tool invocation."""
    icon = "🛠️"
    text = f"**{tool_name}**"
    if status == "running":
        icon = "🛠️"
        text = f"🛠️ 正在调用 **{tool_name}** …"
    elif status == "ok":
        icon = "✅"
        text = f"✅ **{tool_name}** 完成"
    elif status == "fail":
        icon = "❌"
        text = f"❌ **{tool_name}** 失败"
    return {
        "config": {"wide_screen_mode": True},
        "elements": [
            {
                "tag": "markdown",
                "content": text,
            },
        ],
    }


def _build_canvas_code_card(title: str, kind: str, content: str) -> dict[str, Any]:
    """Wave-33: render a canvas artifact as a collapsible code-block card."""
    # Truncate very large content to avoid card size cap.
    max_content = 8000
    display = content if len(content) <= max_content else content[:max_content] + "\n\n…（内容已截断）"
    return {
        "config": {"wide_screen_mode": True},
        "elements": [
            {
                "tag": "markdown",
                "content": f"**📎 {title}**  `{kind}`",
            },
            {"tag": "hr"},
            {
                "tag": "markdown",
                "content": f"```{kind}\n{display}\n```",
            },
        ],
    }


def _table_json_to_markdown(content: str) -> str:
    """Parse JSON table content and render as markdown table.

    Expected shapes:
      {"headers": ["A","B"], "rows": [[1,2],[3,4]]}
      {"rows": [{"A":1,"B":2},{"A":3,"B":4}]}
    """
    try:
        obj = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return content

    # Shape 1: {headers, rows} where rows is list of lists
    headers = obj.get("headers") if isinstance(obj, dict) else None
    rows = obj.get("rows") if isinstance(obj, dict) else None

    if not isinstance(rows, list) or not rows:
        return content

    lines: list[str] = []

    # rows is list of dicts
    if isinstance(rows[0], dict):
        keys = list(rows[0].keys())
        lines.append("| " + " | ".join(keys) + " |")
        lines.append("| " + " | ".join(["---"] * len(keys)) + " |")
        for row in rows:
            vals = [str(row.get(k, "")) for k in keys]
            lines.append("| " + " | ".join(vals) + " |")
        return "\n".join(lines)

    # rows is list of lists
    if isinstance(rows[0], list):
        if headers and isinstance(headers, list):
            lines.append("| " + " | ".join(str(h) for h in headers) + " |")
            lines.append("| " + " | ".join(["---"] * len(headers)) + " |")
        for row in rows:
            lines.append("| " + " | ".join(str(c) for c in row) + " |")
        return "\n".join(lines)

    return content


def _build_canvas_table_card(title: str, content: str) -> dict[str, Any]:
    """Wave-33: render a canvas table artifact as a Lark markdown-table card."""
    md_table = _table_json_to_markdown(content)
    return {
        "config": {"wide_screen_mode": True},
        "elements": [
            {
                "tag": "markdown",
                "content": f"**📊 {title}**",
            },
            {"tag": "hr"},
            {
                "tag": "markdown",
                "content": md_table,
            },
        ],
    }


_CODE_FENCE_RE = _re_md.compile(
    r"```(\w*)\n(.*?)```",
    _re_md.DOTALL,
)


def _extract_code_blocks(text: str) -> list[dict[str, str]]:
    """Wave-33: extract fenced code blocks from text.

    Returns list of {"lang": str, "code": str, "start": int, "end": int}.
    Used by ``_split_and_send`` to pull code blocks into their own
    collapsible card sections."""
    out: list[dict[str, str]] = []
    for m in _CODE_FENCE_RE.finditer(text):
        out.append({
            "lang": m.group(1) or "",
            "code": m.group(2),
            "start": str(m.start()),
            "end": str(m.end()),
        })
    return out


def _extract_markdown_tables(text: str) -> list[dict[str, Any]]:
    """Extract markdown tables from text with character positions.

    Expected format::

        | Header1 | Header2 |
        |---------|---------|
        | cell1   | cell2   |

    Returns a list of dicts::

        {"table_text": str, "start": int, "end": int}
    """
    tables: list[dict[str, Any]] = []
    lines = text.split("\n")
    i = 0
    char_pos = 0

    while i < len(lines):
        line = lines[i]
        # Line without | can't be part of a markdown table
        if "|" not in line:
            char_pos += len(line) + 1  # +1 for \n
            i += 1
            continue

        # Potential table start — look ahead
        start_line = i
        start_pos = char_pos
        table_lines: list[str] = []

        while i < len(lines) and "|" in lines[i]:
            table_lines.append(lines[i])
            char_pos += len(lines[i]) + 1
            i += 1

        # Validate: at least 2 lines, 2nd line is separator
        if len(table_lines) >= 2:
            sep_content = table_lines[1].replace("|", "").strip()
            if sep_content and all(c in "-=:| " for c in sep_content):
                header_cells = [c.strip() for c in table_lines[0].split("|") if c.strip()]
                if header_cells:
                    end_pos = char_pos - 1  # exclude trailing \n
                    tables.append({
                        "table_text": "\n".join(table_lines),
                        "start": start_pos,
                        "end": end_pos,
                    })
                    continue

        # Not a valid table — reset cursor to just after the first line
        char_pos = start_pos + len(lines[start_line]) + 1
        i = start_line + 1

    return tables


def _markdown_table_to_lark_table_element(table_text: str) -> dict[str, Any]:
    """Convert a markdown table into a Lark interactive-card ``table`` element.

    Falls back to a plain ``markdown`` element if parsing fails.
    """
    lines = [line.strip() for line in table_text.strip().split("\n") if line.strip()]
    if len(lines) < 2:
        return {"tag": "markdown", "content": table_text}

    header_cells = [cell.strip() for cell in lines[0].split("|") if cell.strip()]
    if not header_cells:
        return {"tag": "markdown", "content": table_text}

    columns = [
        {
            "data_index": f"col{idx}",
            "name": name,
            "width": "auto",
            "horizontal_align": "left",
        }
        for idx, name in enumerate(header_cells)
    ]

    rows: list[dict[str, str]] = []
    for line in lines[2:]:
        cells = [cell.strip() for cell in line.split("|") if cell.strip()]
        if not cells:
            continue
        row: dict[str, str] = {}
        for idx, cell in enumerate(cells):
            if idx < len(header_cells):
                row[f"col{idx}"] = cell
        if row:
            rows.append(row)

    return {
        "tag": "table",
        "columns": columns,
        "rows": rows,
        "border": True,
        "header_style": {
            "background_style": "grey",
            "text_size": "normal",
            "text_color": "default",
            "text_align": "center",
        },
        "row_style": {
            "text_size": "normal",
            "text_color": "default",
            "text_align": "left",
        },
    }


def _partition_text(text: str) -> list[dict[str, Any]]:
    """Wave-33: split assistant reply into structured sections.

    Returns a list of section dicts:
      {"type": "text", "content": str}
      {"type": "code", "lang": str, "content": str}
      {"type": "table", "content": str}  # markdown table detected inline
    """
    # Collect all special blocks with positions
    blocks: list[dict[str, Any]] = []

    # Code blocks
    for cb in _extract_code_blocks(text):
        blocks.append({
            "start": int(cb["start"]),
            "end": int(cb["end"]),
            "type": "code",
            "lang": cb["lang"],
            "content": cb["code"],
        })

    # Markdown tables (skip those inside code blocks)
    for tb in _extract_markdown_tables(text):
        inside_code = any(
            b["type"] == "code" and b["start"] <= tb["start"] < b["end"]
            for b in blocks
        )
        if not inside_code:
            blocks.append({
                "start": tb["start"],
                "end": tb["end"],
                "type": "table",
                "content": tb["table_text"],
            })

    blocks.sort(key=lambda b: b["start"])

    sections: list[dict[str, Any]] = []
    cursor = 0
    for b in blocks:
        if b["start"] > cursor:
            pre = text[cursor:b["start"]]
            if pre.strip():
                sections.append({"type": "text", "content": pre.strip()})
        if b["type"] == "code":
            sections.append({"type": "code", "lang": b["lang"], "content": b["content"]})
        else:
            sections.append({"type": "table", "content": b["content"]})
        cursor = b["end"]

    if cursor < len(text):
        post = text[cursor:]
        if post.strip():
            sections.append({"type": "text", "content": post.strip()})

    return sections


class FeishuAdapter(ChannelAdapter):
    """飞书 / Lark channel adapter.

    Args:
        config: dict with at minimum ``app_id`` + ``app_secret``.
                Optional ``encrypt_key`` / ``verify_token`` if the
                user enabled event encryption in the open-platform
                console.
        bus: optional InProcessEventBus. When wired, the adapter
             subscribes to TOOL_INVOCATION_* and CANVAS_ARTIFACT_*
             events so 飞书 users see live progress cards and
             inline canvas artifacts.
    """

    name = "feishu"

    def __init__(
        self,
        config: dict[str, Any],
        bus: Any | None = None,
    ) -> None:
        self._cfg = config or {}
        self._app_id = (self._cfg.get("app_id") or "").strip()
        self._app_secret = (self._cfg.get("app_secret") or "").strip()
        self._encrypt_key = (self._cfg.get("encrypt_key") or "").strip() or None
        self._verify_token = (self._cfg.get("verify_token") or "").strip() or None
        if not self._app_id or not self._app_secret:
            raise ValueError(
                "飞书 adapter 需要 config.integrations.feishu_channel."
                "{app_id, app_secret}"
            )
        # Lazy: build inside start() so the heavy lark-oapi import
        # doesn't fire until the user actually enables this channel.
        self._client: Any = None
        self._ws_task: asyncio.Task[Any] | None = None
        self._handlers: list[Callable[[InboundMessage], Awaitable[None]]] = []
        # B-196: Lark's WS uses at-least-once event delivery.
        self._seen_msg_ids: OrderedDict[str, float] = OrderedDict()
        self._seen_cap = 512

        # Wave-33: optional event-bus wiring for live cards.
        self._bus = bus
        self._event_subs: list[Any] = []
        self._event_task: asyncio.Task[Any] | None = None
        self._enable_live_cards = bool(
            self._cfg.get("enable_live_cards", True)
        )
        # session_id (feishu:<chat_id>) → {tool_call_id: message_id}
        self._session_tool_msgs: dict[str, dict[str, str]] = {}
        # session_id → {artifact_id: message_id}
        self._session_artifact_msgs: dict[str, dict[str, str]] = {}
        # session_id → list of image paths produced by tools in this turn
        self._session_tool_images: dict[str, list[str]] = {}

    # ── internal helpers ────────────────────────────────────────

    @staticmethod
    def _import_lark_modules() -> tuple[Any, Any]:
        """Heavy ``lark_oapi`` import isolated so ``start()`` can
        offload it via ``asyncio.to_thread``. The cascade triggers
        ``pkg_resources.declare_namespace`` which is ~3.75s on cold
        module cache — far too slow for the daemon's main event loop.
        Module cache is process-wide so subsequent calls are free."""
        import lark_oapi as lark
        from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
        return lark, P2ImMessageReceiveV1

    # ── public API ──────────────────────────────────────────────

    def subscribe(
        self, handler: Callable[[InboundMessage], Awaitable[None]],
    ) -> None:
        self._handlers.append(handler)

    async def start(self) -> None:
        if self._ws_task is not None:
            return  # idempotent
        lark, P2ImMessageReceiveV1 = await asyncio.to_thread(
            self._import_lark_modules,
        )

        self._client = (
            lark.Client.builder()
            .app_id(self._app_id)
            .app_secret(self._app_secret)
            .build()
        )

        loop = asyncio.get_running_loop()

        def _on_im_message(event: P2ImMessageReceiveV1) -> None:
            try:
                asyncio.run_coroutine_threadsafe(
                    self._handle_event(event), loop,
                ).result(timeout=10)
            except Exception as exc:  # noqa: BLE001
                _log.warning("feishu.dispatch_failed err=%s", exc)

        dispatcher_builder = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(_on_im_message)
        )
        if self._encrypt_key:
            dispatcher_builder = lark.EventDispatcherHandler.builder(
                self._encrypt_key, self._verify_token or "",
            ).register_p2_im_message_receive_v1(_on_im_message)
        dispatcher = dispatcher_builder.build()

        def _build_ws_client() -> Any:
            return lark.ws.Client(
                self._app_id, self._app_secret,
                event_handler=dispatcher,
                log_level=lark.LogLevel.WARNING,
            )

        ws_client_holder: dict[str, Any] = {"client": None}

        def _start_in_thread(client: Any) -> None:
            import asyncio as _asyncio
            new_loop = _asyncio.new_event_loop()
            _asyncio.set_event_loop(new_loop)
            try:
                import lark_oapi.ws.client as _lark_ws_client_mod
                _lark_ws_client_mod.loop = new_loop
            except ImportError:
                pass
            client.start()

        async def _runner() -> None:
            backoff_s = 1.0
            backoff_max_s = 60.0
            while True:
                client = _build_ws_client()
                ws_client_holder["client"] = client
                started_at = time.monotonic()
                try:
                    await asyncio.to_thread(_start_in_thread, client)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    _log.warning(
                        "feishu.ws_loop_failed err=%s — will reconnect", exc,
                    )
                else:
                    _log.warning(
                        "feishu.ws_loop_returned — connection dropped, "
                        "will reconnect",
                    )
                uptime_s = time.monotonic() - started_at
                if uptime_s >= 60:
                    backoff_s = 1.0
                _log.info(
                    "feishu.reconnecting in=%.1fs uptime=%.1fs",
                    backoff_s, uptime_s,
                )
                try:
                    await asyncio.sleep(backoff_s)
                except asyncio.CancelledError:
                    raise
                backoff_s = min(backoff_s * 2.0, backoff_max_s)

        self._ws_task = loop.create_task(_runner(), name="feishu-ws")
        self._ws_client_holder = ws_client_holder  # type: ignore[attr-defined]

        # Wave-33: start event-bus listener for live progress + canvas.
        if self._bus is not None and self._enable_live_cards:
            self._event_task = loop.create_task(
                self._event_listener(), name="feishu-events",
            )

        _log.info("feishu.started app_id=%s", self._app_id[:8] + "***")

    async def stop(self) -> None:
        if self._ws_task is not None:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._ws_task = None

        # Wave-33: stop event listener.
        if self._event_task is not None:
            self._event_task.cancel()
            try:
                await self._event_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._event_task = None
        for sub in list(self._event_subs):
            try:
                sub.cancel()
            except Exception:  # noqa: BLE001
                pass
        self._event_subs.clear()

        _log.info("feishu.stopped")


    # ── Wave-33: event-bus listener for live cards ──────────────

    async def _event_listener(self) -> None:
        """Subscribe to bus events and fan out live cards to Feishu.

        Runs forever until cancelled. Each event is handled in a
        fire-and-forget task so a slow send doesn't block the listener."""
        from xmclaw.core.bus import EventType

        if self._bus is None:
            return

        handled_types = {
            EventType.TOOL_INVOCATION_STARTED,
            EventType.TOOL_INVOCATION_FINISHED,
            EventType.CANVAS_ARTIFACT_CREATED,
            EventType.CANVAS_ARTIFACT_UPDATED,
            EventType.CANVAS_ARTIFACT_CLOSED,
            EventType.LLM_RESPONSE,
        }

        def _predicate(event: Any) -> bool:
            return getattr(event, "type", None) in handled_types

        # Queue-based delivery: bus fan-out spawns tasks that put
        # events into our own asyncio.Queue so we can control
        # back-pressure and ordering.
        queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=256)

        async def _handler(event: Any) -> None:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                _log.debug("feishu.event_queue_full dropping event")

        sub = self._bus.subscribe(_predicate, _handler)
        self._event_subs.append(sub)

        while True:
            try:
                event = await queue.get()
            except asyncio.CancelledError:
                raise
            try:
                await self._on_bus_event(event)
            except Exception as exc:  # noqa: BLE001
                _log.debug("feishu.event_handle_failed err=%s", exc)

    async def _on_bus_event(self, event: Any) -> None:
        """Dispatch a single bus event to the appropriate handler."""
        from xmclaw.core.bus import EventType

        etype = getattr(event, "type", None)
        session_id = getattr(event, "session_id", "") or ""
        payload = getattr(event, "payload", {}) or {}

        # Only handle sessions that belong to this channel.
        if not session_id.startswith("feishu:"):
            return

        if etype == EventType.TOOL_INVOCATION_STARTED:
            await self._handle_tool_started(session_id, payload)
        elif etype == EventType.TOOL_INVOCATION_FINISHED:
            await self._handle_tool_finished(session_id, payload)
        elif etype == EventType.CANVAS_ARTIFACT_CREATED:
            await self._handle_canvas_created(session_id, payload)
        elif etype == EventType.CANVAS_ARTIFACT_UPDATED:
            await self._handle_canvas_updated(session_id, payload)
        elif etype == EventType.CANVAS_ARTIFACT_CLOSED:
            await self._handle_canvas_closed(session_id, payload)
        elif etype == EventType.LLM_RESPONSE:
            await self._handle_llm_response(session_id, payload)

    async def _chat_id_from_session(self, session_id: str) -> str | None:
        """Parse chat_id from ``feishu:<chat_id>`` session id."""
        if session_id.startswith("feishu:"):
            return session_id[len("feishu:"):]
        return None

    async def _handle_tool_started(
        self, session_id: str, payload: dict[str, Any],
    ) -> None:
        chat_id = await self._chat_id_from_session(session_id)
        if not chat_id:
            return
        tool_name = payload.get("tool_name") or payload.get("name") or "tool"
        call_id = payload.get("call_id") or tool_name
        card = _build_process_card(tool_name, "running")
        msg_id = await self._send_card_to_chat(chat_id, card)
        if msg_id:
            self._session_tool_msgs.setdefault(session_id, {})[call_id] = msg_id

    async def _handle_tool_finished(
        self, session_id: str, payload: dict[str, Any],
    ) -> None:
        chat_id = await self._chat_id_from_session(session_id)
        if not chat_id:
            return
        tool_name = payload.get("tool_name") or payload.get("name") or "tool"
        call_id = payload.get("call_id") or tool_name
        ok = payload.get("ok", True)
        status = "ok" if ok else "fail"
        card = _build_process_card(tool_name, status)
        msg_id = self._session_tool_msgs.get(session_id, {}).pop(call_id, None)
        if msg_id:
            await self._patch_message(msg_id, card)
        else:
            # No prior start card — send a finish-only card.
            await self._send_card_to_chat(chat_id, card)

        # Harvest image paths from tool side-effects for later delivery.
        if ok:
            for path in payload.get("expected_side_effects", ()):
                if (
                    isinstance(path, str)
                    and path.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"))
                ):
                    self._session_tool_images.setdefault(session_id, []).append(path)

    async def _handle_canvas_created(
        self, session_id: str, payload: dict[str, Any],
    ) -> None:
        chat_id = await self._chat_id_from_session(session_id)
        if not chat_id:
            return
        kind = payload.get("kind", "")
        title = payload.get("title", "")
        content = payload.get("content", "")
        artifact_id = payload.get("artifact_id", "")

        if kind == "table":
            card = _build_canvas_table_card(title, content)
        else:
            card = _build_canvas_code_card(title, kind, content)

        msg_id = await self._send_card_to_chat(chat_id, card)
        if msg_id and artifact_id:
            self._session_artifact_msgs.setdefault(session_id, {})[artifact_id] = msg_id

    async def _handle_canvas_updated(
        self, session_id: str, payload: dict[str, Any],
    ) -> None:
        artifact_id = payload.get("artifact_id", "")
        msg_id = self._session_artifact_msgs.get(session_id, {}).get(artifact_id)
        if not msg_id:
            return
        content = payload.get("content", "")
        # Re-fetch the stored artifact metadata (kind, title) from the
        # existing card?  We don't have it here — the bus event only
        # carries content.  Rebuild a generic code card with the new
        # content.  The title/kind are preserved in the card header
        # but PATCH replaces the whole card.
        card = {
            "config": {"wide_screen_mode": True},
            "elements": [
                {
                    "tag": "markdown",
                    "content": f"**📎 Artifact updated**\n\n```\n{content[:8000]}\n```",
                },
            ],
        }
        await self._patch_message(msg_id, card)

    async def _handle_canvas_closed(
        self, session_id: str, payload: dict[str, Any],
    ) -> None:
        artifact_id = payload.get("artifact_id", "")
        msg_id = self._session_artifact_msgs.get(session_id, {}).pop(artifact_id, None)
        if not msg_id:
            return
        # Collapse / mark as closed.
        card = {
            "config": {"wide_screen_mode": True},
            "elements": [
                {
                    "tag": "markdown",
                    "content": "~~已关闭~~",
                },
            ],
        }
        await self._patch_message(msg_id, card)

    async def _handle_llm_response(
        self, session_id: str, payload: dict[str, Any],
    ) -> None:
        """Wave-33 follow-up: relay mid-turn narration to Feishu so
        users see the agent's reasoning between tool calls."""
        chat_id = await self._chat_id_from_session(session_id)
        if not chat_id:
            return
        content = payload.get("content", "")
        tool_calls_count = payload.get("tool_calls_count", 0)
        ok = payload.get("ok", True)

        if not isinstance(content, str) or not content.strip():
            return

        # Skip terminal replies — ChannelDispatcher sends the final
        # assistant message after run_turn() returns.
        if tool_calls_count == 0 and ok:
            return

        await self._send_text_to_chat(chat_id, content.strip())

    async def _send_card_to_chat(
        self, chat_id: str, card: dict[str, Any],
    ) -> str:
        """Send an interactive card to a chat. Returns message_id or ''."""
        if self._client is None:
            return ""
        from lark_oapi.api.im.v1 import (
            CreateMessageRequest, CreateMessageRequestBody,
        )
        content_str = json.dumps(card, ensure_ascii=False)
        req = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .content(content_str)
                .msg_type("interactive")
                .build()
            )
            .build()
        )
        try:
            resp = await asyncio.to_thread(
                self._client.im.v1.message.create, req,
            )
            if resp.success() and getattr(resp, "data", None) is not None:
                return (
                    getattr(resp.data, "message_id", "")
                    or ""
                )
        except Exception as exc:  # noqa: BLE001
            _log.debug("feishu.send_card_failed err=%s", exc)
        return ""

    async def _patch_message(
        self, message_id: str, card: dict[str, Any],
    ) -> bool:
        """PATCH an existing interactive card message."""
        if self._client is None:
            return False
        try:
            from lark_oapi.api.im.v1 import (
                PatchMessageRequest, PatchMessageRequestBody,
            )
        except ImportError:
            return False
        content_str = json.dumps(card, ensure_ascii=False)
        req = (
            PatchMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                PatchMessageRequestBody.builder()
                .content(content_str)
                .build()
            )
            .build()
        )
        try:
            resp = await asyncio.to_thread(
                self._client.im.v1.message.patch, req,
            )
            return bool(resp.success())
        except Exception as exc:  # noqa: BLE001
            _log.debug("feishu.patch_message_failed msg_id=%s err=%s", message_id, exc)
        return False

    # ── send() — enhanced for Wave-33 ───────────────────────────

    async def send(
        self, target: ChannelTarget, payload: OutboundMessage,
    ) -> str:
        if self._client is None:
            raise RuntimeError("feishu adapter not started")
        from lark_oapi.api.im.v1 import (
            CreateMessageRequest, CreateMessageRequestBody,
            ReplyMessageRequest, ReplyMessageRequestBody,
        )

        reply_to = payload.reply_to
        chat_id = target.ref

        # Merge tool-generated images collected during the turn.
        session_id = f"feishu:{chat_id}"
        extra_images = self._session_tool_images.pop(session_id, [])
        seen = set(payload.attachments or ())
        all_attachments = list(payload.attachments or ()) + [
            p for p in extra_images if p not in seen
        ]

        # B-199: image attachments first.
        last_msg_id = ""
        for att in all_attachments:
            try:
                image_key = await self._upload_image(att)
            except (FileNotFoundError, RuntimeError) as exc:
                _log.warning("feishu.image_upload_failed path=%s err=%s", att, exc)
                continue
            img_content = json.dumps({"image_key": image_key}, ensure_ascii=False)
            img_req = (
                CreateMessageRequest.builder()
                .receive_id_type("chat_id")
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(chat_id)
                    .content(img_content)
                    .msg_type("image")
                    .build()
                )
                .build()
            )
            try:
                img_resp = await asyncio.to_thread(
                    self._client.im.v1.message.create, img_req,
                )
                if img_resp.success() and getattr(img_resp, "data", None) is not None:
                    last_msg_id = (
                        getattr(img_resp.data, "message_id", "") or last_msg_id
                    )
            except Exception as exc:  # noqa: BLE001
                _log.warning("feishu.image_send_failed key=%s err=%s", image_key, exc)

        if not payload.content.strip() and last_msg_id:
            return last_msg_id

        # Wave-33: if payload.extra carries a pre-built Lark card, use it.
        extra_card = (payload.extra or {}).get("card")
        if extra_card is not None and isinstance(extra_card, dict):
            return await self._send_or_reply_card(
                chat_id, reply_to, extra_card,
            )

        # Wave-33: rich-card mode for markdown replies.
        # Partition text into sections (text / code / table) and build
        # a multi-section interactive card.
        if _looks_like_markdown(payload.content):
            return await self._send_rich_card(chat_id, reply_to, payload.content)

        # Fallback: plain text.
        content_str = json.dumps(
            {"text": payload.content}, ensure_ascii=False,
        )
        if reply_to:
            req = (
                ReplyMessageRequest.builder()
                .message_id(reply_to)
                .request_body(
                    ReplyMessageRequestBody.builder()
                    .content(content_str)
                    .msg_type("text")
                    .build()
                )
                .build()
            )
            resp = await asyncio.to_thread(
                self._client.im.v1.message.reply, req,
            )
        else:
            req = (
                CreateMessageRequest.builder()
                .receive_id_type("chat_id")
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(chat_id)
                    .content(content_str)
                    .msg_type("text")
                    .build()
                )
                .build()
            )
            resp = await asyncio.to_thread(
                self._client.im.v1.message.create, req,
            )
        if not resp.success():
            raise RuntimeError(
                f"feishu send failed: code={resp.code} msg={resp.msg}"
            )
        msg_id = ""
        if getattr(resp, "data", None) is not None:
            msg_id = (
                getattr(resp.data, "message_id", None)
                or getattr(getattr(resp.data, "message", None), "message_id", "")
                or ""
            )
        return msg_id or f"feishu:{int(time.time())}"

    async def _send_or_reply_card(
        self, chat_id: str, reply_to: str | None, card: dict[str, Any],
    ) -> str:
        """Send or reply with a pre-built interactive card."""
        from lark_oapi.api.im.v1 import (
            CreateMessageRequest, CreateMessageRequestBody,
            ReplyMessageRequest, ReplyMessageRequestBody,
        )
        content_str = json.dumps(card, ensure_ascii=False)
        if reply_to:
            req = (
                ReplyMessageRequest.builder()
                .message_id(reply_to)
                .request_body(
                    ReplyMessageRequestBody.builder()
                    .content(content_str)
                    .msg_type("interactive")
                    .build()
                )
                .build()
            )
            resp = await asyncio.to_thread(
                self._client.im.v1.message.reply, req,
            )
        else:
            req = (
                CreateMessageRequest.builder()
                .receive_id_type("chat_id")
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(chat_id)
                    .content(content_str)
                    .msg_type("interactive")
                    .build()
                )
                .build()
            )
            resp = await asyncio.to_thread(
                self._client.im.v1.message.create, req,
            )
        if not resp.success():
            _log.warning("feishu.card_send_failed code=%s msg=%s", resp.code, resp.msg)
            return ""
        msg_id = ""
        if getattr(resp, "data", None) is not None:
            msg_id = getattr(resp.data, "message_id", "") or ""
        return msg_id

    async def _send_text_to_chat(
        self, chat_id: str, text: str, reply_to: str | None = None,
    ) -> str:
        """Send a plain text message to a chat."""
        if self._client is None:
            return ""
        from lark_oapi.api.im.v1 import (
            CreateMessageRequest, CreateMessageRequestBody,
            ReplyMessageRequest, ReplyMessageRequestBody,
        )
        content_str = json.dumps({"text": text}, ensure_ascii=False)
        if reply_to:
            req = (
                ReplyMessageRequest.builder()
                .message_id(reply_to)
                .request_body(
                    ReplyMessageRequestBody.builder()
                    .content(content_str)
                    .msg_type("text")
                    .build()
                )
                .build()
            )
            resp = await asyncio.to_thread(
                self._client.im.v1.message.reply, req,
            )
        else:
            req = (
                CreateMessageRequest.builder()
                .receive_id_type("chat_id")
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(chat_id)
                    .content(content_str)
                    .msg_type("text")
                    .build()
                )
                .build()
            )
            resp = await asyncio.to_thread(
                self._client.im.v1.message.create, req,
            )
        if not resp.success():
            return ""
        msg_id = ""
        if getattr(resp, "data", None) is not None:
            msg_id = getattr(resp.data, "message_id", "") or ""
        return msg_id

    async def _send_rich_card(
        self, chat_id: str, reply_to: str | None, text: str,
    ) -> str:
        """Wave-33: partition text into sections and send as a rich
        interactive card with collapsible code blocks and native tables."""
        sections = _partition_text(text)
        elements: list[dict[str, Any]] = []

        for sec in sections:
            if sec["type"] == "text":
                # Split long text sections if they exceed card char cap.
                chunks = _chunk_text(sec["content"], _CARD_MAX_CHARS // 2)
                for chunk in chunks:
                    elements.append({
                        "tag": "markdown",
                        "content": chunk,
                    })
            elif sec["type"] == "code":
                lang = sec.get("lang", "")
                title = lang or "代码"
                # Collapsible code block using Lark's fold-like pattern
                # (markdown inside a container with a header).
                code_display = sec["content"]
                if len(code_display) > 4000:
                    code_display = code_display[:4000] + "\n\n…（已截断）"
                elements.append({
                    "tag": "markdown",
                    "content": f"**{title}**\n```\n{code_display}\n```",
                })
            elif sec["type"] == "table":
                elements.append(_markdown_table_to_lark_table_element(sec["content"]))

            # If we're nearing the element cap, flush as a card and
            # start a new one.
            if len(elements) >= _CARD_MAX_ELEMENTS:
                card = _build_lark_rich_card(elements)
                await self._send_or_reply_card(chat_id, reply_to, card)
                elements = []
                # Subsequent cards in this turn are direct sends (no
                # reply_to) so they appear as follow-up messages.
                reply_to = None

        if elements:
            card = _build_lark_rich_card(elements)
            return await self._send_or_reply_card(chat_id, reply_to, card)
        return ""

    # ── internal helpers (images, downloads) ────────────────────

    async def _download_message_resource(
        self, message_id: str, file_key: str, *, kind: str = "image",
    ) -> bytes | None:
        if self._client is None:
            return None
        try:
            from lark_oapi.api.im.v1 import GetMessageResourceRequest
        except ImportError:
            return None

        def _do_get() -> Any:
            req = (
                GetMessageResourceRequest.builder()
                .message_id(message_id)
                .file_key(file_key)
                .type(kind)
                .build()
            )
            return self._client.im.v1.message_resource.get(req)

        try:
            resp = await asyncio.to_thread(_do_get)
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "feishu.download_resource_failed msg_id=%s key=%s err=%s",
                message_id, file_key, exc,
            )
            return None
        if not getattr(resp, "success", lambda: False)():
            _log.warning(
                "feishu.download_resource_unsuccessful msg_id=%s key=%s "
                "code=%s msg=%s",
                message_id, file_key,
                getattr(resp, "code", "?"), getattr(resp, "msg", "?"),
            )
            return None
        candidates = (
            getattr(resp, "file", None),
            getattr(getattr(resp, "raw", None), "content", None),
        )
        for c in candidates:
            if isinstance(c, bytes):
                return c
            if hasattr(c, "read"):
                try:
                    return c.read()
                except Exception:  # noqa: BLE001
                    continue
        _log.warning(
            "feishu.download_resource_no_body msg_id=%s key=%s",
            message_id, file_key,
        )
        return None

    async def _upload_image(self, image_path: str) -> str:
        if self._client is None:
            raise RuntimeError("feishu adapter not started")
        from lark_oapi.api.im.v1 import (
            CreateImageRequest, CreateImageRequestBody,
        )
        from pathlib import Path

        path = Path(image_path)
        if not path.is_file():
            raise FileNotFoundError(f"image not found: {image_path}")

        def _do_upload() -> Any:
            with path.open("rb") as f:
                req = (
                    CreateImageRequest.builder()
                    .request_body(
                        CreateImageRequestBody.builder()
                        .image_type("message")
                        .image(f)
                        .build()
                    )
                    .build()
                )
                return self._client.im.v1.image.create(req)

        resp = await asyncio.to_thread(_do_upload)
        if not resp.success():
            raise RuntimeError(
                f"feishu image upload failed: code={resp.code} msg={resp.msg}"
            )
        image_key = getattr(getattr(resp, "data", None), "image_key", "") or ""
        if not image_key:
            raise RuntimeError(
                f"feishu image upload returned no image_key: {resp!r}"
            )
        return image_key

    # ── inbound event handling ──────────────────────────────────

    async def _handle_event(self, event: Any) -> None:
        """Translate lark P2ImMessageReceiveV1 → InboundMessage and
        fan out to subscribers."""
        try:
            msg = event.event.message
            sender = event.event.sender
        except AttributeError:
            return
        msg_type = getattr(msg, "message_type", "") or ""
        if msg_type not in ("text", "image", "post"):
            _log.debug("feishu.skip_unsupported_type type=%s", msg_type)
            return
        text = ""
        image_keys: list[str] = []
        try:
            content_obj = json.loads(getattr(msg, "content", "") or "{}")
        except (json.JSONDecodeError, TypeError, ValueError):
            return
        if msg_type == "text":
            text = (content_obj.get("text") or "").strip()
            text = _strip_at_mentions(text)
        elif msg_type == "image":
            key = content_obj.get("image_key")
            if isinstance(key, str) and key:
                image_keys.append(key)
        elif msg_type == "post":
            text, image_keys = _flatten_post(content_obj)
            text = _strip_at_mentions(text)
        if not text and not image_keys:
            return

        chat_id = getattr(msg, "chat_id", "") or ""
        msg_id = getattr(msg, "message_id", "") or ""
        if msg_id and msg_id in self._seen_msg_ids:
            _log.info("feishu.duplicate_skipped msg_id=%s", msg_id)
            return
        if msg_id:
            self._seen_msg_ids[msg_id] = time.time()
            while len(self._seen_msg_ids) > self._seen_cap:
                self._seen_msg_ids.popitem(last=False)
        user_id = (
            getattr(getattr(sender, "sender_id", None), "open_id", "")
            or getattr(getattr(sender, "sender_id", None), "user_id", "")
            or "unknown"
        )

        try:
            from xmclaw.security import (
                PolicyMode,
                SOURCE_CHANNEL,
                apply_policy,
            )
            policy_str = str(self._cfg.get("injection_policy", "detect_only")).lower()
            try:
                policy = PolicyMode(policy_str)
            except ValueError:
                policy = PolicyMode.DETECT_ONLY
            decision = apply_policy(
                text,
                policy=policy,
                source=SOURCE_CHANNEL,
                extra={
                    "channel": "feishu",
                    "chat_id": chat_id,
                    "user_ref": user_id,
                    "message_id": msg_id,
                },
            )
            if decision.blocked:
                _log.warning(
                    "feishu.inbound_blocked chat_id=%s msg_id=%s "
                    "findings=%s",
                    chat_id, msg_id,
                    [f.pattern_id for f in decision.scan.findings][:5],
                )
                return
            text = decision.content
        except Exception as exc:  # noqa: BLE001
            _log.debug("feishu.scan_skipped err=%s", exc)

        try:
            from xmclaw.security import (
                PolicyMode,
                SOURCE_CHANNEL,
                apply_policy,
            )
            allowed_users = self._cfg.get("allowed_user_refs")
            if isinstance(allowed_users, list) and allowed_users:
                allowed_set = {str(u).strip() for u in allowed_users if str(u).strip()}
                if user_id not in allowed_set:
                    _log.warning(
                        "feishu.inbound_dropped_unauthorized "
                        "chat_id=%s msg_id=%s user_ref=%s "
                        "allowlist_size=%d",
                        chat_id, msg_id, user_id, len(allowed_set),
                    )
                    return
        except Exception:  # noqa: BLE001
            pass

        image_paths: list[str] = []
        if image_keys and msg_id:
            image_paths = await self._fetch_and_save_images(
                msg_id, image_keys,
            )
        if not text and not image_paths:
            _log.info(
                "feishu.inbound_empty_after_fetch msg_id=%s",
                msg_id,
            )
            return

        if not text and image_paths:
            text = "看一下这张图。"

        inbound = InboundMessage(
            target=ChannelTarget(channel="feishu", ref=chat_id),
            user_ref=user_id,
            content=text,
            raw={
                "message_id": msg_id,
                "msg_type": msg_type,
                "images": image_paths,
            },
        )
        for h in list(self._handlers):
            try:
                await h(inbound)
            except Exception as exc:  # noqa: BLE001
                _log.warning("feishu.handler_failed err=%s", exc)

    async def _fetch_and_save_images(
        self, message_id: str, image_keys: list[str],
    ) -> list[str]:
        from xmclaw.utils.paths import data_dir
        uploads_dir = data_dir() / "v2" / "uploads"
        try:
            uploads_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "feishu.uploads_dir_mkdir_failed err=%s", exc,
            )
            return []
        out: list[str] = []
        for i, key in enumerate(image_keys[:4]):
            data = await self._download_message_resource(
                message_id, key, kind="image",
            )
            if not data:
                continue
            ext = _sniff_image_ext(data) or ".jpg"
            safe_msg_id = "".join(
                c if c.isalnum() else "_" for c in message_id
            )[:32]
            out_path = uploads_dir / f"feishu_{safe_msg_id}_{i}{ext}"
            try:
                out_path.write_bytes(data)
                out.append(str(out_path))
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "feishu.image_write_failed path=%s err=%s",
                    out_path, exc,
                )
        return out


# ── module-level helpers ──────────────────────────────────────

def _strip_at_mentions(text: str) -> str:
    import re
    cleaned = re.sub(r"@_user_\d+\s*", "", text)
    return cleaned.strip()


def _flatten_post(content_obj: dict) -> tuple[str, list[str]]:
    texts: list[str] = []
    images: list[str] = []
    title = content_obj.get("title")
    if isinstance(title, str) and title.strip():
        texts.append(title.strip())
    content = content_obj.get("content")
    if not isinstance(content, list):
        return " ".join(texts).strip(), images
    for line in content:
        if not isinstance(line, list):
            continue
        for span in line:
            if not isinstance(span, dict):
                continue
            tag = span.get("tag")
            if tag == "text":
                t = span.get("text")
                if isinstance(t, str):
                    texts.append(t)
            elif tag == "a":
                t = span.get("text") or span.get("href") or ""
                if isinstance(t, str):
                    texts.append(t)
            elif tag == "img":
                key = span.get("image_key")
                if isinstance(key, str) and key:
                    images.append(key)
    return " ".join(texts).strip(), images


def _sniff_image_ext(data: bytes) -> str | None:
    if not data or len(data) < 4:
        return None
    if data.startswith(b"\x89PNG"):
        return ".png"
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[:4] == b"RIFF" and len(data) >= 12 and data[8:12] == b"WEBP":
        return ".webp"
    if data[:2] == b"BM":
        return ".bmp"
    return None


def _chunk_text(text: str, max_chars: int) -> list[str]:
    """Split text into chunks without breaking markdown blocks.

    Tries to break at paragraph boundaries first, then line boundaries."""
    if len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    paragraphs = text.split("\n\n")
    current = ""
    for para in paragraphs:
        if len(current) + len(para) + 2 <= max_chars:
            current = (current + "\n\n" + para).strip() if current else para
        else:
            if current:
                chunks.append(current)
            if len(para) > max_chars:
                # Hard break inside a long paragraph.
                for i in range(0, len(para), max_chars):
                    chunks.append(para[i:i + max_chars])
            else:
                current = para
    if current:
        chunks.append(current)
    return chunks
