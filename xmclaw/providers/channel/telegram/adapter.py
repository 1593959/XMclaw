"""TelegramAdapter — bidirectional Telegram channel.

B-380 (Sprint 2). Implements the scaffolded ``__init__.py`` MANIFEST as
a working :class:`ChannelAdapter`. Uses ``python-telegram-bot``'s
long-poll mode (``Application.updater.start_polling``) so the daemon
doesn't need a public IP / cloudflared tunnel — Telegram's bot API
keeps the WS open from our side.

Inbound flow
------------

  Telegram chat (DM or group) → Bot API push to long-poll →
  python-telegram-bot Updater dispatches MessageHandler →
  _on_message_async → wrap as InboundMessage → fan out to subscribers
  (typically ChannelDispatcher) → AgentLoop.run_turn(
  session_id="telegram:<chat_id>", content) → AgentLoop emits events
  → ChannelDispatcher pulls last assistant text → adapter.send() back
  to the chat via Bot.send_message.

Outbound flow
-------------

  ``adapter.send(target, payload)`` → Bot.send_message(chat_id=target.ref,
  text=payload.content, reply_to_message_id=payload.reply_to or None).
  Telegram has a 4096-char hard limit per message; we chunk longer
  replies into successive sends so big tool dumps don't get truncated
  silently.

Config (read from config.channels.telegram.{...})
-------------------------------------------------

  bot_token         : 'NNNNN:AAAA...' — talk to @BotFather to mint one
  allowed_user_ids  : list[int] — when non-empty, drop messages from
                      sender ids NOT in the list (B-337 parity)
  allowed_chat_ids  : list[int] — when non-empty, drop messages from
                      chats NOT in the list (groups vs DMs split)
  injection_policy  : 'detect_only' | 'redact' | 'block' (default
                      detect_only) — same Epic #14 policy Feishu uses
  parse_mode        : str | None (default None) — pass-through to
                      Telegram. 'MarkdownV2' / 'HTML' / 'Markdown' if
                      the agent emits markdown the user wants rendered;
                      None ships plain text (safest).

The adapter starts the long-poll loop in the background; ``stop``
shuts it down cleanly. python-telegram-bot's Application handles
network blips + reconnects internally — we don't need an explicit
backoff loop.
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


# Telegram's message size cap. Anything longer must be split or the API
# returns 400 Bad Request. We split on word boundaries first, fall back
# to hard cut at the cap.
_TELEGRAM_MAX_CHARS = 4096

# Hint shown when ``python-telegram-bot`` is not installed. Importing
# at top-level would crash the daemon for users who never enable
# Telegram, so we fail at start() / __init__ time with this message
# instead.
_INSTALL_HINT = (
    "python-telegram-bot is not installed. Install it via "
    "`pip install xmclaw[channels]` (or `pip install "
    "'python-telegram-bot>=21.0'` directly) and restart the daemon."
)


def _split_for_telegram(text: str, cap: int = _TELEGRAM_MAX_CHARS) -> list[str]:
    """Chunk ``text`` into pieces <= ``cap`` chars each.

    Prefers paragraph / line breaks; falls back to a hard cut when a
    single line is itself longer than the cap. Telegram won't render
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


class TelegramAdapter(ChannelAdapter):
    """Telegram bot channel adapter (long-poll mode).

    Args:
        config: dict with at minimum ``bot_token``. Optional
                ``allowed_user_ids`` (list of int Telegram user ids),
                ``allowed_chat_ids`` (list of int chat ids),
                ``injection_policy`` (Epic #14), ``parse_mode``.
    """

    name = "telegram"

    def __init__(self, config: dict[str, Any]) -> None:
        self._cfg = config or {}
        self._bot_token = (self._cfg.get("bot_token") or "").strip()
        if not self._bot_token:
            raise ValueError(
                "Telegram adapter needs config.channels.telegram.bot_token "
                "(get one from @BotFather)"
            )
        # Pre-coerce allowlists to int sets at __init__ so a misconfigured
        # entry (string instead of int) surfaces at boot rather than on
        # the first inbound message.
        self._allowed_user_ids: set[int] = _coerce_id_set(
            self._cfg.get("allowed_user_ids"), key="allowed_user_ids",
        )
        self._allowed_chat_ids: set[int] = _coerce_id_set(
            self._cfg.get("allowed_chat_ids"), key="allowed_chat_ids",
        )
        raw_parse_mode = self._cfg.get("parse_mode")
        self._parse_mode: str | None = (
            str(raw_parse_mode).strip() if isinstance(raw_parse_mode, str) and raw_parse_mode.strip() else None
        )
        # Lazy: build inside start() so the heavy python-telegram-bot
        # import doesn't fire until the user actually enables this
        # channel (and so missing-dep doesn't crash daemon at import).
        self._application: Any = None
        self._handlers: list[Callable[[InboundMessage], Awaitable[None]]] = []
        # Mirrors feishu's dedup ring buffer — Telegram's long-poll has
        # at-least-once semantics on reconnect (same update_id can land
        # twice). LRU keyed by message_id; cap at 512.
        self._seen_msg_ids: OrderedDict[str, float] = OrderedDict()
        self._seen_cap = 512
        # Surface field for setup-endpoint health (B-368 pattern). When
        # start() fails (bad token, network blocked at boot), this
        # holds a human-readable string the UI can show.
        self.last_start_error: str | None = None

    # ── public API ──────────────────────────────────────────────

    def subscribe(
        self, handler: Callable[[InboundMessage], Awaitable[None]],
    ) -> None:
        self._handlers.append(handler)

    async def start(self) -> None:
        if self._application is not None:
            return  # idempotent
        try:
            from telegram.ext import Application, MessageHandler, filters
        except ImportError as exc:
            self.last_start_error = _INSTALL_HINT
            raise RuntimeError(_INSTALL_HINT) from exc

        try:
            from telegram.error import InvalidToken, TelegramError
        except ImportError as exc:  # pragma: no cover — should travel with the lib
            self.last_start_error = _INSTALL_HINT
            raise RuntimeError(_INSTALL_HINT) from exc

        application = Application.builder().token(self._bot_token).build()
        # MessageHandler with TEXT filter — we ignore non-text updates
        # (stickers / photos / commands routed elsewhere). Commands
        # starting with '/' are excluded so "/start" doesn't get fed to
        # the agent as a literal user message.
        application.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND, self._on_message_async,
            )
        )

        # Validate the bot token by hitting getMe before we kick off
        # the long-poll loop. Bad token → InvalidToken (the python-
        # telegram-bot 401-equivalent). Surface as a clear RuntimeError
        # so the dispatcher's start_failed log + setup endpoint can show
        # the user "your bot_token is wrong" instead of cryptic stacks.
        try:
            await application.initialize()
        except InvalidToken as exc:
            self.last_start_error = (
                "Telegram rejected the bot_token (HTTP 401). Double-check "
                "the value at @BotFather; do not include the 'bot' prefix."
            )
            try:
                await application.shutdown()
            except Exception:  # noqa: BLE001
                pass
            raise RuntimeError(self.last_start_error) from exc
        except TelegramError as exc:
            # Network errors at boot time also stop the start; we don't
            # have a retry-with-backoff here because the daemon's
            # supervisor restarts the whole process on persistent
            # outages (and the long-poll updater handles transient
            # blips internally once it's running).
            self.last_start_error = (
                f"Telegram start() failed: {type(exc).__name__}: {exc}. "
                "Check network reachability to api.telegram.org."
            )
            try:
                await application.shutdown()
            except Exception:  # noqa: BLE001
                pass
            raise RuntimeError(self.last_start_error) from exc

        try:
            await application.start()
            await application.updater.start_polling(
                # drop_pending_updates avoids replaying messages that
                # arrived while the daemon was offline — same
                # at-least-once posture feishu uses, but Telegram does
                # have an explicit knob.
                drop_pending_updates=True,
            )
        except Exception as exc:  # noqa: BLE001
            self.last_start_error = (
                f"Telegram updater.start_polling failed: "
                f"{type(exc).__name__}: {exc}"
            )
            try:
                await application.stop()
                await application.shutdown()
            except Exception:  # noqa: BLE001
                pass
            raise RuntimeError(self.last_start_error) from exc

        self._application = application
        self.last_start_error = None
        # Mask all but the leading id segment when logging the token —
        # never emit the secret half ("12345:AAAA..." → "12345:***").
        token_prefix = self._bot_token.split(":", 1)[0]
        _log.info(
            "telegram.started",
            bot_token_prefix=f"{token_prefix}:***",
            allowlist_users=len(self._allowed_user_ids),
            allowlist_chats=len(self._allowed_chat_ids),
        )

    async def stop(self) -> None:
        if self._application is None:
            return
        application = self._application
        self._application = None
        # Reverse-order shutdown matches python-telegram-bot's docs.
        # Each step swallows its own errors so a half-shutdown doesn't
        # leave the next step un-attempted (and the daemon shutdown
        # path keeps moving — cf. lifespan stop loop).
        try:
            if application.updater is not None and application.updater.running:
                await application.updater.stop()
        except Exception as exc:  # noqa: BLE001
            _log.warning("telegram.updater_stop_failed", err=str(exc))
        try:
            if application.running:
                await application.stop()
        except Exception as exc:  # noqa: BLE001
            _log.warning("telegram.stop_failed", err=str(exc))
        try:
            await application.shutdown()
        except Exception as exc:  # noqa: BLE001
            _log.warning("telegram.shutdown_failed", err=str(exc))
        _log.info("telegram.stopped")

    async def send(
        self, target: ChannelTarget, payload: OutboundMessage,
    ) -> str:
        if target.channel != self.name:
            raise ValueError(
                f"TelegramAdapter cannot send to channel={target.channel!r}; "
                f"expected {self.name!r}"
            )
        if self._application is None:
            raise RuntimeError("telegram adapter not started")
        try:
            chat_id = int(target.ref)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"telegram target.ref must be int chat_id, got {target.ref!r}"
            ) from exc

        bot = self._application.bot

        # B-199 parity: image attachments first, then the main text
        # message. Telegram's send_photo accepts a file path / URL /
        # bytes; we use the path string since that's what Feishu's
        # contract surfaces from the dispatcher's _extract_local_image_paths.
        last_msg_id = ""
        for att in (payload.attachments or ()):
            try:
                with open(att, "rb") as f:
                    photo_msg = await bot.send_photo(
                        chat_id=chat_id,
                        photo=f,
                        reply_to_message_id=_to_int_or_none(payload.reply_to),
                    )
                last_msg_id = str(getattr(photo_msg, "message_id", "") or last_msg_id)
            except FileNotFoundError as exc:
                _log.warning("telegram.image_missing", path=att, err=str(exc))
            except Exception as exc:  # noqa: BLE001
                _log.warning("telegram.image_send_failed", path=att, err=str(exc))

        # Skip the text send when content is empty AND we already sent
        # images — same posture as feishu's image-only path.
        if not payload.content.strip() and last_msg_id:
            return last_msg_id

        # Telegram caps each message at 4096 chars. Chunk longer
        # replies; the reply_to_message_id only attaches to the first
        # chunk so the conversation thread doesn't get muddied.
        chunks = _split_for_telegram(payload.content)
        first_reply_to = _to_int_or_none(payload.reply_to)
        last_id = last_msg_id
        for i, chunk in enumerate(chunks):
            try:
                msg = await bot.send_message(
                    chat_id=chat_id,
                    text=chunk,
                    reply_to_message_id=first_reply_to if i == 0 else None,
                    parse_mode=self._parse_mode,
                )
                last_id = str(getattr(msg, "message_id", "") or last_id)
            except Exception as exc:  # noqa: BLE001 — bus event surfaces it
                _log.warning(
                    "telegram.send_failed",
                    chat_id=chat_id, err=str(exc),
                    parse_mode=self._parse_mode,
                )
                # Re-raise the LAST chunk failure so dispatcher's outer
                # try/except records the channel.send_failed event and
                # the user is at least told the delivery dropped.
                # Earlier-chunk failures are logged but we attempt the
                # rest — better than dropping a 5-chunk reply on a
                # transient hiccup mid-thread.
                if i == len(chunks) - 1:
                    raise RuntimeError(
                        f"telegram send failed: {type(exc).__name__}: {exc}"
                    ) from exc
        return last_id or f"telegram:{int(time.time())}"

    # ── internal ────────────────────────────────────────────────

    async def _on_message_async(self, update: Any, context: Any) -> None:
        """python-telegram-bot calls this for every text update.

        Translate to InboundMessage + fan out to subscribers (typically
        the ChannelDispatcher). Drop unauthorized senders silently — the
        log line tells the operator someone tried.
        """
        message = getattr(update, "message", None)
        if message is None:
            return
        text = (getattr(message, "text", None) or "").strip()
        if not text:
            return

        chat = getattr(message, "chat", None)
        chat_id = int(getattr(chat, "id", 0) or 0)
        if chat_id == 0:
            _log.debug("telegram.no_chat_id", update_id=getattr(update, "update_id", None))
            return

        from_user = getattr(message, "from_user", None)
        user_id = int(getattr(from_user, "id", 0) or 0)
        username = getattr(from_user, "username", "") or ""
        msg_id_raw = getattr(message, "message_id", None)
        msg_id = str(msg_id_raw) if msg_id_raw is not None else ""

        # Dedup against the LRU. Same update can land twice on a
        # network blip / drop_pending_updates not catching everything.
        # Key on (chat_id, msg_id) so two unrelated chats with the
        # same numeric msg_id aren't conflated.
        dedup_key = f"{chat_id}:{msg_id}"
        if msg_id and dedup_key in self._seen_msg_ids:
            _log.info("telegram.duplicate_skipped", msg_id=msg_id, chat_id=chat_id)
            return
        if msg_id:
            self._seen_msg_ids[dedup_key] = time.time()
            while len(self._seen_msg_ids) > self._seen_cap:
                self._seen_msg_ids.popitem(last=False)

        # Allowlist gate. Empty / missing config = no restriction
        # (preserves "any DM works" default for solo operators); set
        # the lists to lock down to a known set of users / chats.
        if self._allowed_user_ids and user_id not in self._allowed_user_ids:
            _log.warning(
                "telegram.inbound_dropped_unauthorized_user",
                chat_id=chat_id, user_id=user_id, username=username,
                allowlist_size=len(self._allowed_user_ids),
            )
            return
        if self._allowed_chat_ids and chat_id not in self._allowed_chat_ids:
            _log.warning(
                "telegram.inbound_dropped_unauthorized_chat",
                chat_id=chat_id, user_id=user_id,
                allowlist_size=len(self._allowed_chat_ids),
            )
            return

        # Epic #14: scan inbound text for prompt injection BEFORE
        # handing off to run_turn. Telegram group chat members aren't
        # necessarily the daemon owner — without this scan a hostile
        # group member can stage an "ignore previous instructions"
        # attack. Default DETECT_ONLY so legit messages aren't blocked;
        # operators flip to BLOCK in config when they run open chat.
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
                    "channel": "telegram",
                    "chat_id": chat_id,
                    "user_ref": user_id,
                    "message_id": msg_id,
                },
            )
            if decision.blocked:
                _log.warning(
                    "telegram.inbound_blocked",
                    chat_id=chat_id, msg_id=msg_id,
                    findings=[f.pattern_id for f in decision.scan.findings][:5],
                )
                return  # drop message — don't fan out to agent
            text = decision.content
        except Exception as exc:  # noqa: BLE001
            _log.debug("telegram.scan_skipped", err=str(exc))

        inbound = InboundMessage(
            target=ChannelTarget(channel=self.name, ref=str(chat_id)),
            user_ref=str(user_id) if user_id else (username or "unknown"),
            content=text,
            raw={
                "message_id": msg_id,
                "chat_id": chat_id,
                "user_id": user_id,
                "username": username,
            },
        )
        for h in list(self._handlers):
            try:
                await h(inbound)
            except Exception as exc:  # noqa: BLE001
                _log.warning("telegram.handler_failed", err=str(exc))


# ── helpers ────────────────────────────────────────────────────────


def _coerce_id_set(raw: Any, *, key: str) -> set[int]:
    """Validate + coerce a config-supplied id list to a set of ints.

    Telegram user / chat ids are 64-bit ints (sometimes negative for
    supergroups); accepting strings would silently miss matches against
    the int the API returns. Empty / missing → empty set (no
    restriction). Non-list raw raises so a typo like
    ``allowed_user_ids: "12345"`` doesn't read as a 5-char allowlist
    that always fails.
    """
    if raw is None:
        return set()
    if not isinstance(raw, list):
        raise ValueError(
            f"channels.telegram.{key} must be a list of int ids, got "
            f"{type(raw).__name__}"
        )
    out: set[int] = set()
    for entry in raw:
        if isinstance(entry, bool):
            # bool is an int subclass in Python; reject explicitly.
            raise ValueError(
                f"channels.telegram.{key} entries must be int, got bool"
            )
        if isinstance(entry, int):
            out.add(entry)
            continue
        if isinstance(entry, str) and entry.strip().lstrip("-").isdigit():
            out.add(int(entry.strip()))
            continue
        raise ValueError(
            f"channels.telegram.{key} entries must be int or "
            f"int-shaped string, got {entry!r}"
        )
    return out


def _to_int_or_none(value: Any) -> int | None:
    """Best-effort int coercion for reply_to_message_id.

    InboundMessage.raw["message_id"] is set to ``str(message_id)`` by
    the inbound handler, but the dispatcher then forwards it through
    OutboundMessage.reply_to as a string. Telegram's API wants an int,
    so coerce here; non-numeric → None (skip the reply attachment but
    still send the message).
    """
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None
