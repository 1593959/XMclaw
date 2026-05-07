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
            # wide_screen_mode True = use full conversation width;
            # better for tabular tool output.
            "wide_screen_mode": True,
        },
        "elements": [
            {
                "tag": "markdown",
                "content": content,
                # text_align defaults left; explicit so future Lark
                # changes don't surprise us.
                "text_align": "left",
            },
        ],
    }


class FeishuAdapter(ChannelAdapter):
    """飞书 / Lark channel adapter.

    Args:
        config: dict with at minimum ``app_id`` + ``app_secret``.
                Optional ``encrypt_key`` / ``verify_token`` if the
                user enabled event encryption in the open-platform
                console.
    """

    name = "feishu"

    def __init__(self, config: dict[str, Any]) -> None:
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
        # B-196: Lark's WS uses at-least-once event delivery — on
        # reconnect / network blip the same message_id can land twice
        # (or more). Without dedup the agent runs the turn N times and
        # the user sees duplicate replies. LRU keyed by message_id; cap
        # at 512 keeps memory bounded while covering ~hours of busy chat.
        self._seen_msg_ids: OrderedDict[str, float] = OrderedDict()
        self._seen_cap = 512

    # ── public API ──────────────────────────────────────────────

    def subscribe(
        self, handler: Callable[[InboundMessage], Awaitable[None]],
    ) -> None:
        self._handlers.append(handler)

    async def start(self) -> None:
        if self._ws_task is not None:
            return  # idempotent
        # Local import keeps lark-oapi as an optional dep.
        import lark_oapi as lark
        from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

        # lark.Client.builder() is the canonical entry point.
        self._client = (
            lark.Client.builder()
            .app_id(self._app_id)
            .app_secret(self._app_secret)
            .build()
        )

        # Event dispatcher binds handler functions per event type.
        # Keep a reference to the loop so the lark thread-pool callback
        # can schedule async work back onto our event loop.
        loop = asyncio.get_running_loop()

        def _on_im_message(event: P2ImMessageReceiveV1) -> None:
            """Lark's dispatcher calls this from a background thread.
            Translate to InboundMessage + put back on our event loop."""
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

        ws_client = (
            lark.ws.Client(
                self._app_id, self._app_secret,
                event_handler=dispatcher,
                log_level=lark.LogLevel.WARNING,
            )
        )

        # ws_client.start() is BLOCKING (lark-oapi's design — it
        # internally runs an asyncio event loop). Run it in a worker
        # thread so we don't block the daemon's main loop.
        #
        # B-194: lark-oapi 1.4.x captures `loop = asyncio.get_event_loop()`
        # at module import time (lark_oapi/ws/client.py L25-29). When
        # daemon imports lark from inside its async context, that
        # module-level `loop` becomes the daemon's main loop. Then
        # `Client.start()` does `loop.run_until_complete(...)` on it —
        # the main loop is already running, so we get
        # "This event loop is already running" + the WS never connects
        # (silent failure: adapter shows running=True but no events).
        # Fix: in the worker thread, give lark its own dedicated event
        # loop by overriding the module global before calling start().
        def _start_in_thread() -> None:
            import asyncio as _asyncio
            new_loop = _asyncio.new_event_loop()
            _asyncio.set_event_loop(new_loop)
            try:
                import lark_oapi.ws.client as _lark_ws_client_mod
                _lark_ws_client_mod.loop = new_loop
            except ImportError:
                pass
            ws_client.start()

        async def _runner() -> None:
            try:
                await asyncio.to_thread(_start_in_thread)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                _log.warning("feishu.ws_loop_failed err=%s", exc)

        self._ws_task = loop.create_task(_runner(), name="feishu-ws")
        _log.info("feishu.started app_id=%s", self._app_id[:8] + "***")

    async def stop(self) -> None:
        if self._ws_task is None:
            return
        self._ws_task.cancel()
        try:
            await self._ws_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        self._ws_task = None
        _log.info("feishu.stopped")

    async def _upload_image(self, image_path: str) -> str:
        """B-199: upload a local image to Lark, return image_key.

        Used by ``send`` when ``OutboundMessage.attachments`` carries
        local image paths. The image_key returned is what the IM API
        wants in ``content.image_key`` for a ``msg_type=image``
        message. Lark's image upload is a separate call from the
        message send.

        Raises ``FileNotFoundError`` / ``RuntimeError`` so callers
        can surface a meaningful error instead of swallowing into a
        polite "我没办法" — the original failure mode that triggered
        this fix (chat-2026-05-03 17:51 sequence).
        """
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

    async def send(
        self, target: ChannelTarget, payload: OutboundMessage,
    ) -> str:
        if self._client is None:
            raise RuntimeError("feishu adapter not started")
        from lark_oapi.api.im.v1 import (
            CreateMessageRequest, CreateMessageRequestBody,
            ReplyMessageRequest, ReplyMessageRequestBody,
        )

        # B-199: image attachments. Each path in ``attachments`` is
        # uploaded then sent as its own ``msg_type=image`` message.
        # Order is attachments first, then the main text message —
        # mirrors Slack/Discord conventions where images appear in-
        # line above the text. Failures upload-side surface as
        # exceptions; the caller (ChannelDispatcher) decides whether
        # to fall through to text-only or surface the error.
        last_msg_id = ""
        for att in (payload.attachments or ()):
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
                    .receive_id(target.ref)
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

        # Feishu requires JSON-serialised content. Plain text uses
        # {"text": "..."} shape. Skip the text send entirely when
        # content is empty AND we already sent images — caller asked
        # for image-only delivery (e.g. "screenshot please").
        if not payload.content.strip() and last_msg_id:
            return last_msg_id

        # B-209: route markdown replies through msg_type=interactive
        # (card with markdown element) so feishu renders **bold** /
        # `code` / ## headers / lists properly instead of showing
        # raw characters. Plain text stays msg_type=text — cards
        # add chrome that's overkill for "OK 收到" one-liners.
        # Oversized payloads (> _CARD_MAX_CHARS) fall back to text
        # so we don't fail the card POST on a huge tool dump.
        use_card = (
            _looks_like_markdown(payload.content)
            and len(payload.content) <= _CARD_MAX_CHARS
        )
        if use_card:
            card = _build_lark_markdown_card(payload.content)
            content_str = json.dumps(card, ensure_ascii=False)
            msg_type = "interactive"
        else:
            content_str = json.dumps(
                {"text": payload.content}, ensure_ascii=False,
            )
            msg_type = "text"

        if payload.reply_to:
            req = (
                ReplyMessageRequest.builder()
                .message_id(payload.reply_to)
                .request_body(
                    ReplyMessageRequestBody.builder()
                    .content(content_str)
                    .msg_type(msg_type)
                    .build()
                )
                .build()
            )
            resp = await asyncio.to_thread(
                self._client.im.v1.message.reply, req,
            )
        else:
            # ChannelTarget.ref carries the chat_id (oc_xxx) for
            # direct sends. receive_id_type=chat_id sends to a group.
            req = (
                CreateMessageRequest.builder()
                .receive_id_type("chat_id")
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(target.ref)
                    .content(content_str)
                    .msg_type(msg_type)
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
        # Lark response.data.message_id (or .message in some shapes)
        msg_id = ""
        if getattr(resp, "data", None) is not None:
            msg_id = (
                getattr(resp.data, "message_id", None)
                or getattr(getattr(resp.data, "message", None), "message_id", "")
                or ""
            )
        return msg_id or f"feishu:{int(time.time())}"

    # ── internal ────────────────────────────────────────────────

    async def _handle_event(self, event: Any) -> None:
        """Translate lark P2ImMessageReceiveV1 → InboundMessage and
        fan out to subscribers."""
        try:
            msg = event.event.message
            sender = event.event.sender
        except AttributeError:
            return
        # Only handle text messages for v1. Rich-text / images come
        # back as JSON-encoded content; the agent can ask the user
        # to use text.
        msg_type = getattr(msg, "message_type", "") or ""
        if msg_type != "text":
            _log.debug("feishu.skip_non_text type=%s", msg_type)
            return
        # content is JSON-encoded: '{"text":"hi @bot"}'
        text = ""
        try:
            content_obj = json.loads(getattr(msg, "content", "") or "{}")
            text = (content_obj.get("text") or "").strip()
            # Strip leading @bot mention text (lark renders it as
            # `@_user_1` placeholder when the bot is mentioned).
            text = _strip_at_mentions(text)
        except (json.JSONDecodeError, TypeError, ValueError):
            return
        if not text:
            return

        chat_id = getattr(msg, "chat_id", "") or ""
        msg_id = getattr(msg, "message_id", "") or ""
        # B-196: drop duplicate deliveries by message_id. Lark's WS may
        # redeliver the same event on reconnect; we'd otherwise process
        # it twice and the user sees N copies of the same reply.
        if msg_id and msg_id in self._seen_msg_ids:
            _log.info("feishu.duplicate_skipped msg_id=%s", msg_id)
            return
        if msg_id:
            self._seen_msg_ids[msg_id] = time.time()
            # Trim from the front (oldest) when over cap.
            while len(self._seen_msg_ids) > self._seen_cap:
                self._seen_msg_ids.popitem(last=False)
        user_id = (
            getattr(getattr(sender, "sender_id", None), "open_id", "")
            or getattr(getattr(sender, "sender_id", None), "user_id", "")
            or "unknown"
        )

        # B-273: scan inbound text for prompt injection BEFORE handing
        # off to run_turn. Lark group-chat members are not necessarily
        # the daemon owner — anyone with chat access can send a
        # message that gets fed to the agent as if the owner typed it.
        # Without this scan a hostile group member can stage an
        # "ignore previous instructions" attack via Feishu. Policy
        # default is DETECT_ONLY so legit user messages aren't
        # blocked; operators who run open chat can flip to BLOCK in
        # config. Scanner is best-effort — failures don't drop the
        # message (would be worse UX than the residual risk).
        try:
            from xmclaw.security import (
                PolicyMode,
                SOURCE_CHANNEL,
                apply_policy,
            )
            policy_str = str(self._config.get("injection_policy", "detect_only")).lower()
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
                return  # drop message — don't fan out to agent
            text = decision.content
        except Exception as exc:  # noqa: BLE001
            _log.debug("feishu.scan_skipped err=%s", exc)

        inbound = InboundMessage(
            target=ChannelTarget(channel="feishu", ref=chat_id),
            user_ref=user_id,
            content=text,
            raw={"message_id": msg_id, "msg_type": msg_type},
        )
        for h in list(self._handlers):
            try:
                await h(inbound)
            except Exception as exc:  # noqa: BLE001
                _log.warning("feishu.handler_failed err=%s", exc)


def _strip_at_mentions(text: str) -> str:
    """Lark renders @-mentions as ``@_user_<n>`` placeholders. Strip
    them so the bot doesn't see junk in the prompt."""
    import re
    cleaned = re.sub(r"@_user_\d+\s*", "", text)
    return cleaned.strip()
