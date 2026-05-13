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
        # Local import keeps lark-oapi as an optional dep.
        #
        # 2026-05-11 perf fix: the synchronous ``import lark_oapi``
        # cascade triggers ``pkg_resources.declare_namespace`` whose
        # importlib walk costs ~3.75s on cold module cache (Windows +
        # antivirus scanning is the worst case). Doing it inline in
        # this coroutine blocks the entire daemon event loop —
        # uvicorn can't get back to the "Application startup complete"
        # log line until it finishes, which pushes /health past the
        # CLI's wait timeout. Pushing it to a worker thread via
        # ``asyncio.to_thread`` lets the loop keep running other
        # coroutines (including uvicorn's own ones) while pkg_resources
        # cooks. The import is a one-shot and after this point
        # everything is synchronous Python that's already in cache.
        lark, P2ImMessageReceiveV1 = await asyncio.to_thread(
            self._import_lark_modules,
        )

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

        # B-369 (Sprint 1): reconnect loop. Pre-B-369 the lark-oapi WS
        # client's ``start()`` returned cleanly when the underlying
        # transport dropped (NAT timeout / ISP idle prune; daemon.log
        # showed ``[Lark] receive message loop exit, err: no close
        # frame received or sent`` 1-3 times/day) and ``_runner`` then
        # exited, leaving the adapter dead but the daemon convinced
        # the bot was up. The user discovered hours later when their
        # 飞书 群 wasn't responding. Now: rebuild the ws_client AND
        # restart in a retry loop with capped exponential backoff.
        # Each retry is logged so daemon.log + the SetupBanner can
        # surface "feishu reconnecting" without ambiguity.

        def _build_ws_client() -> Any:
            return lark.ws.Client(
                self._app_id, self._app_secret,
                event_handler=dispatcher,
                log_level=lark.LogLevel.WARNING,
            )

        # Hold the LATEST ws_client on a closure cell so stop() can
        # call its ._stop / ._exit shape if available (lark-oapi 1.4
        # private API; we tolerate AttributeError).
        ws_client_holder: dict[str, Any] = {"client": None}

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
            # B-369: capped exponential backoff. 1s, 2s, 4s, …, 60s,
            # 60s, 60s. Reset to 1s after a successful long run (≥60s
            # uptime suggests the connection was healthy and the drop
            # is transient — don't punish reconnect speed).
            backoff_s = 1.0
            backoff_max_s = 60.0
            while True:
                client = _build_ws_client()
                ws_client_holder["client"] = client
                started_at = time.monotonic()
                try:
                    await asyncio.to_thread(_start_in_thread, client)
                except asyncio.CancelledError:
                    raise  # daemon shutdown — propagate
                except Exception as exc:  # noqa: BLE001
                    _log.warning(
                        "feishu.ws_loop_failed err=%s — will reconnect", exc,
                    )
                else:
                    # ``start()`` returned without exception — that's
                    # the "loop exit, no close frame" path. lark-oapi
                    # logs the underlying error at ERROR level, we
                    # log the reconnect intent at WARNING.
                    _log.warning(
                        "feishu.ws_loop_returned — connection dropped, "
                        "will reconnect",
                    )
                # Reset backoff if the previous run lasted long enough
                # to count as "stable session that just got pruned"
                # rather than "instantly failing with bad credentials".
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

    async def _download_message_resource(
        self, message_id: str, file_key: str, *, kind: str = "image",
    ) -> bytes | None:
        """Wave 12: download an inbound image/file by (message_id, key).

        Returns raw bytes on success, ``None`` on failure (caller logs +
        skips). Lark's WS push gives us the image_key — fetching the
        bytes is a separate REST call.

        Uses ``im.v1.message_resource.get`` which supports kind ∈
        {"image", "file"}.
        """
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
        # The response's file-like body lives on resp.file; some
        # lark-oapi versions also stick the bytes on resp.raw.content.
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
        # Wave 12: handle text + image + post (rich text with image).
        # Other types (file/audio/video/etc.) still skip — those need
        # heavier processing pipelines than the agent has plumbing for.
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
            # {"image_key": "img_v3_xxx"}
            key = content_obj.get("image_key")
            if isinstance(key, str) and key:
                image_keys.append(key)
        elif msg_type == "post":
            # Rich text: nested {"title": "...", "content": [[{"tag":...}]]}
            # We pull out text spans + image refs.
            text, image_keys = _flatten_post(content_obj)
            text = _strip_at_mentions(text)
        if not text and not image_keys:
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
            # B-326: was ``self._config`` — typo against ``self._cfg``
            # set in __init__. AttributeError was being swallowed by
            # the broad ``except Exception`` below, so every Feishu
            # inbound bypassed the injection scanner regardless of the
            # operator's ``injection_policy`` setting. ``injection_policy:
            # block`` was a 100% no-op until this fix.
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
                return  # drop message — don't fan out to agent
            text = decision.content
        except Exception as exc:  # noqa: BLE001
            _log.debug("feishu.scan_skipped err=%s", exc)

        # B-337 (audit #8): allowlist gate. The base.py docstring
        # promised "Allowlist: per-sender authorization gate ...
        # Phase 4+." but Phase 4 didn't land — every group-chat
        # member could drive the agent regardless of the operator's
        # multi-tenant intent. Now: when ``allowed_user_refs`` is set
        # in config (a list of open_id / user_id strings), inbound
        # messages from sender ids NOT in the list are dropped with
        # a clear log line. Empty / missing config = no restriction
        # (preserves the current default "any group member can use
        # the agent" behaviour for solo operators).
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

        # Wave 12: download any inbound image bytes and persist to the
        # workspace uploads dir. Run AFTER injection / allowlist gates
        # so unauthorized senders don't get free image downloads.
        image_paths: list[str] = []
        if image_keys and msg_id:
            image_paths = await self._fetch_and_save_images(
                msg_id, image_keys,
            )
        if not text and not image_paths:
            # All image fetches failed AND no text — nothing for the
            # agent to act on. Log + drop.
            _log.info(
                "feishu.inbound_empty_after_fetch msg_id=%s",
                msg_id,
            )
            return

        # If user only sent image(s) without caption, give the agent a
        # tiny default prompt so the LLM has SOMETHING textual to ground
        # its reply on. Otherwise some LLM clients drop messages with
        # empty text + only image content.
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
        """Download every image_key in this message → ~/.xmclaw/v2/
        uploads/feishu_<msgid>_<i>.<ext>. Returns absolute paths of
        successfully downloaded images (failures log + skip)."""
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
        for i, key in enumerate(image_keys[:4]):  # cap at 4 per msg
            data = await self._download_message_resource(
                message_id, key, kind="image",
            )
            if not data:
                continue
            # Sniff extension from magic bytes — Lark doesn't return
            # mime in the resource fetch.
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


def _strip_at_mentions(text: str) -> str:
    """Lark renders @-mentions as ``@_user_<n>`` placeholders. Strip
    them so the bot doesn't see junk in the prompt."""
    import re
    cleaned = re.sub(r"@_user_\d+\s*", "", text)
    return cleaned.strip()


def _flatten_post(content_obj: dict) -> tuple[str, list[str]]:
    """Walk a Lark ``post`` rich-text payload, return (joined_text,
    image_keys). Lark's post schema is nested list-of-lists where each
    leaf is a tagged dict (``text`` / ``a`` / ``at`` / ``img``)."""
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
    """Return a file extension based on magic bytes. Used after Lark
    download since the resource fetch doesn't return mime."""
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
