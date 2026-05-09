"""DingTalkAdapter — bidirectional 钉钉 channel.

B-383 (Sprint 2). Sibling of the B-380/B-381/B-382 Telegram / Discord
/ Slack adapters. Uses ``dingtalk-stream``'s Stream Mode (long-running
WebSocket from our side to DingTalk) so the daemon doesn't need a
public webhook / cloudflared tunnel — the SDK opens an outbound WS
that DingTalk pushes events through.

Inbound flow
------------

  钉钉群里 @机器人 → DingTalk push → dingtalk_stream client routes
  ChatbotMessage to our registered AsyncChatbotHandler →
  process(callback_message) → _handle_callback (on the event loop) →
  wrap as InboundMessage → fan out to subscribers (typically
  ChannelDispatcher) → AgentLoop.run_turn(
  session_id="dingtalk:<conversation_id>", content) → AgentLoop emits
  events → ChannelDispatcher pulls last assistant text →
  adapter.send() back to the 钉钉 group via the message's
  session_webhook URL.

Outbound flow
-------------

  ``adapter.send(target, payload)`` → look up the original
  ChatbotMessage by conversation_id (we cache it on inbound), then
  POST the reply text to that message's ``session_webhook`` (the
  SDK's recommended path for bot replies — works for both single chat
  and group chat without OpenAPI access tokens). 钉钉's text
  ``content`` field has a ~5000-char practical cap (server returns
  errors above that); we split on word/line boundaries below that
  and emit successive posts so big tool dumps don't truncate.

Config (read from config.channels.dingtalk.{...})
-------------------------------------------------

  client_id                  : 'dingxxx' — DingTalk app key (Stream
                               Mode uses client_id + client_secret as
                               OAuth-style credentials)
  client_secret              : paired secret
  robot_code                 : (optional) robot code; defaults to
                               client_id for single-app builds (the
                               common case)
  allowed_user_ids           : list[str] — when non-empty, drop messages
                               from sender_staff_ids NOT in the list
                               (B-337 parity)
  allowed_conversation_ids   : list[str] — when non-empty, drop
                               messages from conversation ids NOT in
                               the list (DMs vs groups split)
  injection_policy           : 'detect_only' | 'redact' | 'block'
                               (default detect_only) — same Epic #14
                               policy Feishu / Telegram / Discord /
                               Slack use. 钉钉 group members aren't
                               necessarily the daemon owner; without
                               this scan a hostile group member can
                               stage an "ignore previous instructions"
                               attack.

The adapter starts the Stream client in the background as an asyncio
task; ``stop`` cancels it. dingtalk-stream handles network blips +
WS reconnects internally — we don't add an outer backoff loop.
"""
from __future__ import annotations

import asyncio
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
from xmclaw.utils.log import get_logger


_log = get_logger(__name__)


# 钉钉's reply text content has no documented hard cap, but the SDK's
# session_webhook payload starts to choke / reject around ~5000 chars
# in practice. Stay below that with margin so big tool dumps go
# through as successive posts rather than fail the call.
_DINGTALK_MAX_CHARS = 4500

# Hint shown when ``dingtalk-stream`` is not installed. Importing at
# top level would crash the daemon for users who never enable
# DingTalk, so we fail at start() time with this message instead.
_INSTALL_HINT = (
    "dingtalk-stream is not installed. Install it via "
    "`pip install xmclaw[channels-dingtalk]` (or "
    "`pip install 'dingtalk-stream>=0.20'` directly) and restart the "
    "daemon."
)


def _split_for_dingtalk(text: str, cap: int = _DINGTALK_MAX_CHARS) -> list[str]:
    """Chunk ``text`` into pieces <= ``cap`` chars each.

    Prefers paragraph / line breaks; falls back to a hard cut when a
    single line is itself longer than the cap. 钉钉 won't render
    "..." continuation indicators, so we just emit successive
    messages and let the user scroll.
    """
    if not text:
        return []
    if len(text) <= cap:
        return [text]
    out: list[str] = []
    remaining = text
    while len(remaining) > cap:
        # Try newline boundary within the cap window.
        cut = remaining.rfind("\n", 0, cap)
        if cut <= 0:
            cut = remaining.rfind(" ", 0, cap)
        if cut <= 0:
            cut = cap  # hard cut — no whitespace in the window
        out.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        out.append(remaining)
    return out


def _coerce_str_set(raw: Any, *, key: str) -> set[str]:
    """Validate + coerce a config-supplied id list to a set of strs.

    钉钉 staff_ids and conversation_ids are opaque strings (e.g.
    'manager4321' for staff, 'cidLfaa==' for conversations). Empty /
    missing → empty set (no restriction). Non-list raw raises so a
    typo like ``allowed_user_ids: "manager4321"`` doesn't read as a
    11-char allowlist that always fails.
    """
    if raw is None:
        return set()
    if not isinstance(raw, list):
        raise ValueError(
            f"channels.dingtalk.{key} must be a list of str ids, got "
            f"{type(raw).__name__}"
        )
    out: set[str] = set()
    for entry in raw:
        if not isinstance(entry, str):
            raise ValueError(
                f"channels.dingtalk.{key} entries must be str, got "
                f"{type(entry).__name__}"
            )
        s = entry.strip()
        if s:
            out.add(s)
    return out


class DingTalkAdapter(ChannelAdapter):
    """钉钉 / DingTalk channel adapter (Stream Mode).

    Args:
        config: dict with at minimum ``client_id`` + ``client_secret``.
                Optional ``robot_code`` (defaults to client_id),
                ``allowed_user_ids`` (list of staff_id strings),
                ``allowed_conversation_ids`` (list of conversation_id
                strings), ``injection_policy`` (Epic #14).
    """

    name = "dingtalk"

    def __init__(self, config: dict[str, Any]) -> None:
        self._cfg = config or {}
        self._client_id = (self._cfg.get("client_id") or "").strip()
        self._client_secret = (self._cfg.get("client_secret") or "").strip()
        if not self._client_id:
            raise ValueError(
                "DingTalk adapter needs config.channels.dingtalk.client_id "
                "(get it from open.dingtalk.com → 应用开发 → 凭证与基础信息)"
            )
        if not self._client_secret:
            raise ValueError(
                "DingTalk adapter needs config.channels.dingtalk.client_secret "
                "(paired with client_id)"
            )
        # robot_code defaults to client_id — common case for self-built
        # apps where the same id identifies the bot. Operators with
        # multi-robot setups override it explicitly.
        self._robot_code = (
            self._cfg.get("robot_code") or self._client_id
        ).strip()
        # Pre-coerce allowlists to str sets at __init__ so a misconfigured
        # entry surfaces at boot rather than on the first inbound message.
        self._allowed_user_ids: set[str] = _coerce_str_set(
            self._cfg.get("allowed_user_ids"), key="allowed_user_ids",
        )
        self._allowed_conversation_ids: set[str] = _coerce_str_set(
            self._cfg.get("allowed_conversation_ids"),
            key="allowed_conversation_ids",
        )
        # Lazy: build inside start() so the heavy dingtalk-stream
        # import doesn't fire until the user actually enables this
        # channel.
        self._client: Any = None
        self._client_task: asyncio.Task | None = None
        self._handler: Any = None
        self._handlers: list[Callable[[InboundMessage], Awaitable[None]]] = []
        # 钉钉's ChatbotMessage carries a session_webhook URL that's
        # the canonical reply path (works for single chat + group chat
        # without OpenAPI access tokens). The webhook expires (default
        # ~3h via session_webhook_expired_time) but for the lifetime
        # of a conversation it's the right address. Cache the latest
        # message per conversation_id so send() can find the webhook.
        # LRU-bounded; oldest evicted at cap.
        self._conversation_msgs: OrderedDict[str, Any] = OrderedDict()
        self._conversation_cap = 256
        # Mirrors slack/discord/telegram dedup ring buffer. 钉钉's
        # Stream Mode has at-least-once semantics on reconnect — the
        # same message_id can land twice. LRU keyed by
        # (conversation_id, message_id); cap at 512.
        self._seen_msg_ids: OrderedDict[str, float] = OrderedDict()
        self._seen_cap = 512
        # Surface field for setup-endpoint health (B-368 pattern).
        # When start() fails (bad credentials, network blocked at
        # boot), this holds a human-readable string the UI can show.
        self.last_start_error: str | None = None

    # ── public API ──────────────────────────────────────────────

    def subscribe(
        self, handler: Callable[[InboundMessage], Awaitable[None]],
    ) -> None:
        self._handlers.append(handler)

    async def start(self) -> None:
        if self._client is not None:
            return  # idempotent
        try:
            import dingtalk_stream
        except ImportError as exc:
            self.last_start_error = _INSTALL_HINT
            raise RuntimeError(_INSTALL_HINT) from exc

        # The SDK uses Credential(client_id, client_secret) →
        # DingTalkStreamClient(credential) → register an
        # AsyncChatbotHandler against ChatbotMessage.TOPIC.
        try:
            credential = dingtalk_stream.Credential(
                self._client_id, self._client_secret,
            )
            client = dingtalk_stream.DingTalkStreamClient(credential)
        except Exception as exc:  # noqa: BLE001
            self.last_start_error = (
                f"DingTalk client init failed: "
                f"{type(exc).__name__}: {exc}. "
                "Check the client_id / client_secret values."
            )
            raise RuntimeError(self.last_start_error) from exc

        # Capture a reference to our event loop so the SDK's
        # background-thread callback can schedule async work back
        # onto our loop — same posture feishu uses.
        loop = asyncio.get_running_loop()
        adapter = self

        class _Handler(dingtalk_stream.AsyncChatbotHandler):
            """Bridge the SDK's process() callback to the adapter's
            async event-loop dispatch. AsyncChatbotHandler runs
            ``process`` in a thread-pool worker; we schedule the
            real handling on the adapter's loop so subscribers see
            their handlers awaited normally."""

            def process(self, callback_message: Any) -> tuple[int, str]:
                try:
                    asyncio.run_coroutine_threadsafe(
                        adapter._handle_callback(callback_message), loop,
                    )
                except Exception as exc:  # noqa: BLE001
                    _log.warning("dingtalk.dispatch_failed", err=str(exc))
                # The base class's raw_process returns STATUS_OK for us;
                # we just need to not raise here.
                return (
                    dingtalk_stream.AckMessage.STATUS_OK,
                    "ok",
                )

        try:
            handler = _Handler()
            client.register_callback_handler(
                dingtalk_stream.ChatbotMessage.TOPIC, handler,
            )
        except Exception as exc:  # noqa: BLE001
            self.last_start_error = (
                f"DingTalk handler registration failed: "
                f"{type(exc).__name__}: {exc}"
            )
            raise RuntimeError(self.last_start_error) from exc

        # client.start() is the long-running coroutine that keeps the
        # WS open; run it as a background task so the daemon's
        # lifespan loop can cancel it via stop(). The SDK's start()
        # never returns under normal operation; it only exits on
        # KeyboardInterrupt or unhandled exception (which it handles
        # internally with sleep + retry).
        async def _runner() -> None:
            try:
                await client.start()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                _log.warning("dingtalk.ws_loop_failed", err=str(exc))

        self._client = client
        self._handler = handler
        self._client_task = loop.create_task(_runner(), name="dingtalk-stream")
        self.last_start_error = None
        _log.info(
            "dingtalk.started",
            client_id_prefix=self._client_id[:6] + "***",
            robot_code_prefix=self._robot_code[:6] + "***",
            allowlist_users=len(self._allowed_user_ids),
            allowlist_conversations=len(self._allowed_conversation_ids),
        )

    async def stop(self) -> None:
        if self._client_task is None and self._client is None:
            return
        task = self._client_task
        self._client_task = None
        self._client = None
        self._handler = None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        _log.info("dingtalk.stopped")

    async def send(
        self, target: ChannelTarget, payload: OutboundMessage,
    ) -> str:
        if target.channel != self.name:
            raise ValueError(
                f"DingTalkAdapter cannot send to channel={target.channel!r}; "
                f"expected {self.name!r}"
            )
        if self._client is None:
            raise RuntimeError("dingtalk adapter not started")
        if not target.ref:
            raise ValueError(
                "dingtalk target.ref must be a non-empty conversation id"
            )

        # Look up the cached ChatbotMessage so we have its
        # session_webhook (the SDK's recommended reply path —
        # works for both single chat and group chat without
        # OpenAPI access tokens).
        original = self._conversation_msgs.get(target.ref)
        if original is None:
            raise RuntimeError(
                f"dingtalk conversation {target.ref!r} has no cached "
                "session_webhook (no inbound message yet). "
                "钉钉 replies require an inbound message to bind to."
            )

        chunks = _split_for_dingtalk(payload.content)
        if not chunks:
            # Empty content — nothing to send. 钉钉 rejects empty text
            # so bail quietly.
            return ""

        # Each chunk fires a separate reply via the SDK's reply_text
        # helper. reply_text is synchronous (uses requests.post under
        # the hood); push to a worker thread so we don't block the
        # event loop.
        last_msg_id = ""
        for i, chunk in enumerate(chunks):
            try:
                handler = self._handler
                if handler is None:
                    raise RuntimeError("dingtalk handler not started")
                # reply_text returns the response JSON dict (or None on
                # SDK-side failure — the SDK swallows requests errors
                # and logs them). We treat None as a failure on the
                # last chunk so the dispatcher can record send_failed.
                resp = await asyncio.to_thread(
                    handler.reply_text, chunk, original,
                )
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "dingtalk.send_failed",
                    conversation_id=target.ref, err=str(exc),
                )
                # Re-raise the LAST chunk failure so dispatcher's outer
                # try/except records the channel.send_failed event and
                # the user is at least told the delivery dropped.
                if i == len(chunks) - 1:
                    raise RuntimeError(
                        f"dingtalk send failed: {type(exc).__name__}: {exc}"
                    ) from exc
                continue
            if resp is None and i == len(chunks) - 1:
                # SDK swallowed an HTTP error. Surface it so the
                # dispatcher can log channel.send_failed.
                raise RuntimeError(
                    "dingtalk send failed: reply_text returned None "
                    "(check daemon logs for SDK error)"
                )
            # The webhook response shape is {"errcode": 0, "errmsg":
            # "ok"} — there's no message_id surfaced. We still emit a
            # synthetic id so the dispatcher's logging line carries
            # something meaningful.
            if resp is not None:
                last_msg_id = str(resp.get("messageId") or last_msg_id)
        return last_msg_id or f"dingtalk:{int(time.time())}"

    # ── internal ────────────────────────────────────────────────

    async def _handle_callback(self, callback_message: Any) -> None:
        """Translate dingtalk_stream's CallbackMessage → InboundMessage
        and fan out to subscribers."""
        # Lazy SDK reach — already imported in start(), but defensive
        # so this method can also be unit-tested with duck-typed events
        # without the SDK present.
        try:
            import dingtalk_stream
        except ImportError:
            _log.debug("dingtalk.callback_skipped — SDK missing")
            return

        try:
            msg = dingtalk_stream.ChatbotMessage.from_dict(
                callback_message.data,
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning("dingtalk.parse_failed", err=str(exc))
            return

        await self._handle_message(msg)

    async def _handle_message(self, msg: Any) -> None:
        """Route a parsed ChatbotMessage to subscribers.

        Split out from _handle_callback so unit tests can duck-type
        message objects directly without going through
        dingtalk_stream.ChatbotMessage.from_dict.
        """
        # Only handle text messages for v1. richText / picture come
        # back as alternative content fields; the agent can ask the
        # user to use text.
        msg_type = getattr(msg, "message_type", "") or ""
        if msg_type != "text":
            _log.debug("dingtalk.skip_non_text", msg_type=msg_type)
            return

        text_obj = getattr(msg, "text", None)
        text = ""
        if text_obj is not None:
            text = (getattr(text_obj, "content", "") or "").strip()
        if not text:
            return

        conversation_id = (
            getattr(msg, "conversation_id", "") or ""
        ).strip()
        if not conversation_id:
            _log.debug(
                "dingtalk.no_conversation_id",
                msg_id=getattr(msg, "message_id", None),
            )
            return

        msg_id = (getattr(msg, "message_id", "") or "").strip()
        # Dedup against the LRU. 钉钉's Stream Mode has at-least-once
        # delivery — same callback can arrive twice on reconnect.
        # Key on (conversation_id, msg_id) so two unrelated
        # conversations with overlapping ids aren't conflated.
        dedup_key = f"{conversation_id}:{msg_id}" if msg_id else ""
        if dedup_key and dedup_key in self._seen_msg_ids:
            _log.info(
                "dingtalk.duplicate_skipped",
                conversation_id=conversation_id, msg_id=msg_id,
            )
            return
        if dedup_key:
            self._seen_msg_ids[dedup_key] = time.time()
            while len(self._seen_msg_ids) > self._seen_cap:
                self._seen_msg_ids.popitem(last=False)

        # Cache the message so send() can find session_webhook later.
        # LRU-bounded so a long-running daemon doesn't grow this map
        # unboundedly. Note: each new inbound REPLACES the cached
        # message for that conversation — DingTalk's session_webhook
        # rotates per-message and the latest one is what the SDK
        # expects for reply_text.
        self._conversation_msgs[conversation_id] = msg
        # Move-to-end so LRU eviction works correctly.
        self._conversation_msgs.move_to_end(conversation_id)
        while len(self._conversation_msgs) > self._conversation_cap:
            self._conversation_msgs.popitem(last=False)

        # Sender ids: 钉钉 has both sender_id (DingTalk-internal) and
        # sender_staff_id (corp-side staff id, more useful for
        # allowlist matching). Prefer staff_id, fall back to sender_id.
        sender_staff_id = (
            getattr(msg, "sender_staff_id", "") or ""
        ).strip()
        sender_id = (getattr(msg, "sender_id", "") or "").strip()
        user_ref = sender_staff_id or sender_id or "unknown"

        # Allowlist gate. Empty / missing config = no restriction
        # (preserves "any group member can use the agent" default for
        # solo operators); set the lists to lock down to a known set
        # of users / conversations.
        if (
            self._allowed_user_ids
            and user_ref not in self._allowed_user_ids
        ):
            _log.warning(
                "dingtalk.inbound_dropped_unauthorized_user",
                conversation_id=conversation_id, user_ref=user_ref,
                allowlist_size=len(self._allowed_user_ids),
            )
            return
        if (
            self._allowed_conversation_ids
            and conversation_id not in self._allowed_conversation_ids
        ):
            _log.warning(
                "dingtalk.inbound_dropped_unauthorized_conversation",
                conversation_id=conversation_id, user_ref=user_ref,
                allowlist_size=len(self._allowed_conversation_ids),
            )
            return

        # Epic #14: scan inbound text for prompt injection BEFORE
        # handing off to run_turn. 钉钉 group chat members aren't
        # necessarily the daemon owner — without this scan a hostile
        # group member can stage an "ignore previous instructions"
        # attack. Default DETECT_ONLY so legit messages aren't
        # blocked; operators flip to BLOCK in config when they run
        # open chat.
        try:
            from xmclaw.security import (
                PolicyMode,
                SOURCE_CHANNEL,
                apply_policy,
            )
            policy_str = str(
                self._cfg.get("injection_policy", "detect_only")
            ).lower()
            try:
                policy = PolicyMode(policy_str)
            except ValueError:
                policy = PolicyMode.DETECT_ONLY
            decision = apply_policy(
                text,
                policy=policy,
                source=SOURCE_CHANNEL,
                extra={
                    "channel": "dingtalk",
                    "conversation_id": conversation_id,
                    "user_ref": user_ref,
                    "message_id": msg_id,
                },
            )
            if decision.blocked:
                _log.warning(
                    "dingtalk.inbound_blocked",
                    conversation_id=conversation_id, msg_id=msg_id,
                    findings=[
                        f.pattern_id for f in decision.scan.findings
                    ][:5],
                )
                return  # drop message — don't fan out to agent
            text = decision.content
        except Exception as exc:  # noqa: BLE001
            _log.debug("dingtalk.scan_skipped", err=str(exc))

        inbound = InboundMessage(
            target=ChannelTarget(channel=self.name, ref=conversation_id),
            user_ref=user_ref,
            content=text,
            raw={
                "message_id": msg_id,
                "conversation_id": conversation_id,
                "sender_id": sender_id,
                "sender_staff_id": sender_staff_id,
                "conversation_type": getattr(msg, "conversation_type", ""),
            },
        )
        for h in list(self._handlers):
            try:
                await h(inbound)
            except Exception as exc:  # noqa: BLE001
                _log.warning("dingtalk.handler_failed", err=str(exc))
