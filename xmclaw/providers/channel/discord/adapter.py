"""DiscordAdapter — bidirectional Discord channel.

B-381 (Sprint 2). Direct sibling of the B-380 Telegram adapter. Uses
``discord.py>=2``'s native gateway WebSocket (``Client.start``) so the
daemon doesn't need a public IP / cloudflared tunnel — Discord's
gateway keeps the WS open from our side.

Inbound flow
------------

  Discord channel (DM or guild text) → gateway push →
  discord.Client dispatches ``on_message`` →
  _on_message_async → wrap as InboundMessage → fan out to subscribers
  (typically ChannelDispatcher) → AgentLoop.run_turn(
  session_id="discord:<channel_id>", content) → AgentLoop emits events
  → ChannelDispatcher pulls last assistant text → adapter.send() back
  to the Discord channel via ``channel.send``.

Outbound flow
-------------

  ``adapter.send(target, payload)`` → ``client.get_channel(channel_id)
  .send(content=payload.content, reference=msg_ref)``. Discord caps
  each message at 2000 chars; we chunk longer replies into successive
  sends so big tool dumps don't get truncated silently.

Config (read from config.channels.discord.{...})
------------------------------------------------

  bot_token             : 'MTIzNDU2...' — Bot token from
                          https://discord.com/developers/applications
  allowed_user_ids      : list[int] — when non-empty, drop messages
                          from sender ids NOT in the list (B-337 parity)
  allowed_channel_ids   : list[int] — when non-empty, drop messages
                          from channels NOT in the list (DMs vs guild
                          channels split)
  injection_policy      : 'detect_only' | 'redact' | 'block' (default
                          detect_only) — same Epic #14 policy Feishu /
                          Telegram use

The adapter starts the gateway loop in the background; ``stop`` shuts
it down cleanly. discord.py's Client handles network blips +
reconnects internally — we don't need an explicit backoff loop.
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
from xmclaw.providers.channel._shared import split_text
from xmclaw.utils.log import get_logger


_log = get_logger(__name__)


# Discord's per-message hard cap. Anything longer must be split or the
# API returns 400 Bad Request (50035: "Must be 2000 or fewer in length").
# We split on word boundaries first, fall back to hard cut at the cap.
_DISCORD_MAX_CHARS = 2000

# Hint shown when ``discord.py`` is not installed. Importing at top-level
# would crash the daemon for users who never enable Discord, so we fail
# at start() time with this message instead.
_INSTALL_HINT = (
    "discord.py is not installed. Install it via "
    "`pip install xmclaw[channels-discord]` (or `pip install "
    "'discord.py>=2'` directly) and restart the daemon."
)



class DiscordAdapter(ChannelAdapter):
    """Discord bot channel adapter (gateway mode).

    Args:
        config: dict with at minimum ``bot_token``. Optional
                ``allowed_user_ids`` (list of int Discord user
                snowflake ids), ``allowed_channel_ids`` (list of int
                channel snowflake ids), ``injection_policy`` (Epic #14).
    """

    name = "discord"

    def __init__(self, config: dict[str, Any]) -> None:
        self._cfg = config or {}
        self._bot_token = (self._cfg.get("bot_token") or "").strip()
        if not self._bot_token:
            raise ValueError(
                "Discord adapter needs config.channels.discord.bot_token "
                "(get one from https://discord.com/developers/applications)"
            )
        # Pre-coerce allowlists to int sets at __init__ so a misconfigured
        # entry (string instead of int) surfaces at boot rather than on
        # the first inbound message.
        self._allowed_user_ids: set[int] = _coerce_id_set(
            self._cfg.get("allowed_user_ids"), key="allowed_user_ids",
        )
        self._allowed_channel_ids: set[int] = _coerce_id_set(
            self._cfg.get("allowed_channel_ids"), key="allowed_channel_ids",
        )
        # Lazy: build inside start() so the heavy discord.py import
        # doesn't fire until the user actually enables this channel
        # (and so missing-dep doesn't crash daemon at import).
        self._client: Any = None
        self._client_task: asyncio.Task | None = None
        self._handlers: list[Callable[[InboundMessage], Awaitable[None]]] = []
        # Mirrors telegram's dedup ring buffer — gateway has at-least-
        # once semantics on reconnect (rare but possible). LRU keyed by
        # (channel_id, message_id); cap at 512.
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
        if self._client is not None:
            return  # idempotent
        try:
            import discord
        except ImportError as exc:
            self.last_start_error = _INSTALL_HINT
            raise RuntimeError(_INSTALL_HINT) from exc

        # Discord gateway intents — we need messages + message_content
        # (privileged intent: must be enabled in the Developer Portal
        # under Bot → Privileged Gateway Intents). guilds carries the
        # channel/guild metadata we need for outbound resolution.
        try:
            intents = discord.Intents.default()
            intents.message_content = True
            intents.messages = True
            intents.guilds = True
        except Exception as exc:  # noqa: BLE001
            self.last_start_error = (
                f"Discord intents construction failed: "
                f"{type(exc).__name__}: {exc}"
            )
            raise RuntimeError(self.last_start_error) from exc

        client = discord.Client(intents=intents)

        # Register on_message via decorator-style assignment. discord.py
        # collects these as event listeners.
        @client.event
        async def on_message(message: Any) -> None:  # noqa: ANN401
            await self._on_message_async(message)

        # Bot connection-failure surfaces (LoginFailure, HTTPException)
        # are raised from client.start; we resolve the discord.errors
        # module here so we can map them to actionable RuntimeErrors.
        try:
            from discord.errors import HTTPException, LoginFailure
        except ImportError as exc:  # pragma: no cover — should travel with the lib
            self.last_start_error = _INSTALL_HINT
            raise RuntimeError(_INSTALL_HINT) from exc

        # client.start blocks for the lifetime of the gateway connection;
        # we run it as a background task and let the lifespan stop loop
        # cancel it via stop(). Token validation happens on the first
        # gateway IDENTIFY — LoginFailure surfaces inside the task.
        ready_event = asyncio.Event()

        @client.event
        async def on_ready() -> None:  # noqa: ANN001
            ready_event.set()

        async def _run_client() -> None:
            try:
                await client.start(self._bot_token)
            except LoginFailure as exc:
                self.last_start_error = (
                    "Discord rejected the bot_token (LoginFailure). "
                    "Double-check the token at "
                    "https://discord.com/developers/applications "
                    "→ Bot → Reset Token; do not include the 'Bot ' prefix."
                )
                _log.warning("discord.login_failed", err=str(exc))
                ready_event.set()  # unblock start() so it can raise
                raise
            except HTTPException as exc:
                self.last_start_error = (
                    f"Discord HTTP error during connect: "
                    f"{type(exc).__name__}: {exc}. "
                    "Check network reachability to discord.com."
                )
                _log.warning("discord.http_failed", err=str(exc))
                ready_event.set()
                raise
            except Exception as exc:  # noqa: BLE001
                self.last_start_error = (
                    f"Discord client.start failed: "
                    f"{type(exc).__name__}: {exc}"
                )
                _log.warning("discord.start_failed", err=str(exc))
                ready_event.set()
                raise

        task = asyncio.create_task(_run_client(), name="discord-gateway")
        self._client_task = task
        # Wait briefly for either on_ready (success) or the task to
        # explode (auth fail). We don't block forever — production gateways
        # connect in <2s; if we hit timeout we hand control back so the
        # supervisor can keep moving (the task stays running and may
        # connect later).
        try:
            await asyncio.wait_for(ready_event.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            _log.warning("discord.ready_timeout — proceeding without sync ack")

        # If the task already failed during connect, raise its exception
        # synchronously so the dispatcher records start_failed.
        if task.done() and task.exception() is not None:
            err = task.exception()
            try:
                if not client.is_closed():
                    await client.close()
            except Exception:  # noqa: BLE001
                pass
            self._client_task = None
            # last_start_error was set by the inner except already.
            if self.last_start_error is None:
                self.last_start_error = (
                    f"Discord start() failed: {type(err).__name__}: {err}"
                )
            raise RuntimeError(self.last_start_error) from err

        self._client = client
        self.last_start_error = None
        # Mask all but the leading id segment when logging the token —
        # never emit the secret half. Discord tokens look like
        # "MTIzNDU2.AAAA.BBBB"; the first dot-segment is the bot id
        # base64, safe to log; keep it short to avoid surface area.
        token_prefix = self._bot_token.split(".", 1)[0][:8]
        _log.info(
            "discord.started",
            bot_token_prefix=f"{token_prefix}***",
            allowlist_users=len(self._allowed_user_ids),
            allowlist_channels=len(self._allowed_channel_ids),
        )

    async def stop(self) -> None:
        if self._client is None:
            return
        client = self._client
        task = self._client_task
        self._client = None
        self._client_task = None
        # discord.py's recommended shutdown: client.close() drains the
        # gateway + cancels internal tasks; the start() task then
        # completes. We swallow each step's errors so a half-shutdown
        # doesn't leave the next step un-attempted.
        try:
            if not client.is_closed():
                await client.close()
        except Exception as exc:  # noqa: BLE001
            _log.warning("discord.close_failed", err=str(exc))
        if task is not None:
            try:
                # Give the task a brief window to exit cleanly after
                # close(); cancel if it lingers.
                await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
            except asyncio.TimeoutError:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001
                _log.warning("discord.task_join_failed", err=str(exc))
        _log.info("discord.stopped")

    async def send(
        self, target: ChannelTarget, payload: OutboundMessage,
    ) -> str:
        if target.channel != self.name:
            raise ValueError(
                f"DiscordAdapter cannot send to channel={target.channel!r}; "
                f"expected {self.name!r}"
            )
        if self._client is None:
            raise RuntimeError("discord adapter not started")
        try:
            channel_id = int(target.ref)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"discord target.ref must be int channel_id, got {target.ref!r}"
            ) from exc

        client = self._client
        # get_channel hits the in-memory channel cache — populated as
        # the gateway delivers GUILD_CREATE / channel events. For DMs
        # that the bot has never seen, fall back to fetch_channel
        # (an HTTP roundtrip).
        channel = client.get_channel(channel_id)
        if channel is None:
            try:
                channel = await client.fetch_channel(channel_id)
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(
                    f"discord channel {channel_id} not found: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc

        # B-199 parity: image attachments first, then the main text
        # message. Discord's send() takes a ``file=discord.File(...)``
        # for attachments; we lazy-import at call time so the adapter
        # module itself stays import-clean for users without discord.py.
        last_msg_id = ""
        if payload.attachments:
            try:
                import discord  # type: ignore[import-not-found]
            except ImportError as exc:
                raise RuntimeError(_INSTALL_HINT) from exc
            for att in payload.attachments:
                try:
                    file_obj = discord.File(att)
                    photo_msg = await channel.send(file=file_obj)
                    last_msg_id = str(getattr(photo_msg, "id", "") or last_msg_id)
                except FileNotFoundError as exc:
                    _log.warning("discord.image_missing", path=att, err=str(exc))
                except Exception as exc:  # noqa: BLE001
                    _log.warning("discord.image_send_failed", path=att, err=str(exc))

        # Skip the text send when content is empty AND we already sent
        # images — same posture as feishu's image-only path.
        if not payload.content.strip() and last_msg_id:
            return last_msg_id

        # Discord caps each message at 2000 chars. Chunk longer replies;
        # the reference (reply_to) only attaches to the first chunk so
        # the conversation thread doesn't get muddied.
        chunks = split_text(payload.content, _DISCORD_MAX_CHARS)
        first_reply_to = _to_int_or_none(payload.reply_to)
        last_id = last_msg_id
        for i, chunk in enumerate(chunks):
            try:
                kwargs: dict[str, Any] = {"content": chunk}
                if i == 0 and first_reply_to is not None:
                    # discord.py's reply API: pass a MessageReference
                    # built from the channel + message id. We fetch the
                    # source message lazily to build the reference; if
                    # it's gone (deleted), fall back to a plain send.
                    try:
                        ref_msg = await channel.fetch_message(first_reply_to)
                        kwargs["reference"] = ref_msg
                    except Exception as exc:  # noqa: BLE001
                        _log.debug(
                            "discord.reply_ref_fetch_failed",
                            msg_id=first_reply_to, err=str(exc),
                        )
                msg = await channel.send(**kwargs)
                last_id = str(getattr(msg, "id", "") or last_id)
            except Exception as exc:  # noqa: BLE001 — bus event surfaces it
                _log.warning(
                    "discord.send_failed",
                    channel_id=channel_id, err=str(exc),
                )
                # Re-raise the LAST chunk failure so dispatcher's outer
                # try/except records the channel.send_failed event and
                # the user is at least told the delivery dropped.
                # Earlier-chunk failures are logged but we attempt the
                # rest — better than dropping a 5-chunk reply on a
                # transient hiccup mid-thread.
                if i == len(chunks) - 1:
                    raise RuntimeError(
                        f"discord send failed: {type(exc).__name__}: {exc}"
                    ) from exc
        return last_id or f"discord:{int(time.time())}"

    # ── internal ────────────────────────────────────────────────

    async def _on_message_async(self, message: Any) -> None:
        """discord.py calls this for every incoming message.

        Translate to InboundMessage + fan out to subscribers (typically
        the ChannelDispatcher). Drop unauthorized senders silently — the
        log line tells the operator someone tried.
        """
        if message is None:
            return

        # Skip our own messages — without this we'd echo every reply
        # back into ourselves and infinite-loop.
        client = self._client
        if client is not None:
            self_user = getattr(client, "user", None)
            author = getattr(message, "author", None)
            if self_user is not None and author is not None:
                self_id = getattr(self_user, "id", None)
                author_id = getattr(author, "id", None)
                if self_id is not None and author_id == self_id:
                    return

        # Ignore bot-flagged authors (other bots) — agents-talking-to-
        # agents loops are a known Discord footgun. Operators who want
        # cross-bot relays can flip this in a follow-up.
        author = getattr(message, "author", None)
        if author is not None and getattr(author, "bot", False):
            return

        text = (getattr(message, "content", None) or "").strip()
        if not text:
            return

        channel = getattr(message, "channel", None)
        channel_id = int(getattr(channel, "id", 0) or 0)
        if channel_id == 0:
            _log.debug("discord.no_channel_id", msg_id=getattr(message, "id", None))
            return

        user_id = int(getattr(author, "id", 0) or 0) if author is not None else 0
        username = ""
        if author is not None:
            username = getattr(author, "name", "") or ""
        msg_id_raw = getattr(message, "id", None)
        msg_id = str(msg_id_raw) if msg_id_raw is not None else ""

        # Dedup against the LRU. Same message can theoretically land
        # twice on a gateway resume / partial-replay; guard upfront.
        # Key on (channel_id, msg_id) so two unrelated channels with the
        # same numeric msg_id aren't conflated.
        dedup_key = f"{channel_id}:{msg_id}"
        if msg_id and dedup_key in self._seen_msg_ids:
            _log.info("discord.duplicate_skipped", msg_id=msg_id, channel_id=channel_id)
            return
        if msg_id:
            self._seen_msg_ids[dedup_key] = time.time()
            while len(self._seen_msg_ids) > self._seen_cap:
                self._seen_msg_ids.popitem(last=False)

        # Allowlist gate. Empty / missing config = no restriction
        # (preserves "any DM works" default for solo operators); set
        # the lists to lock down to a known set of users / channels.
        if self._allowed_user_ids and user_id not in self._allowed_user_ids:
            _log.warning(
                "discord.inbound_dropped_unauthorized_user",
                channel_id=channel_id, user_id=user_id, username=username,
                allowlist_size=len(self._allowed_user_ids),
            )
            return
        if self._allowed_channel_ids and channel_id not in self._allowed_channel_ids:
            _log.warning(
                "discord.inbound_dropped_unauthorized_channel",
                channel_id=channel_id, user_id=user_id,
                allowlist_size=len(self._allowed_channel_ids),
            )
            return

        # Epic #14: scan inbound text for prompt injection BEFORE
        # handing off to run_turn. Discord guild members aren't
        # necessarily the daemon owner — without this scan a hostile
        # member can stage an "ignore previous instructions" attack.
        # Default DETECT_ONLY so legit messages aren't blocked;
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
                    "channel": "discord",
                    "channel_id": channel_id,
                    "user_ref": user_id,
                    "message_id": msg_id,
                },
            )
            if decision.blocked:
                _log.warning(
                    "discord.inbound_blocked",
                    channel_id=channel_id, msg_id=msg_id,
                    findings=[f.pattern_id for f in decision.scan.findings][:5],
                )
                return  # drop message — don't fan out to agent
            text = decision.content
        except Exception as exc:  # noqa: BLE001
            _log.debug("discord.scan_skipped", err=str(exc))

        inbound = InboundMessage(
            target=ChannelTarget(channel=self.name, ref=str(channel_id)),
            user_ref=str(user_id) if user_id else (username or "unknown"),
            content=text,
            raw={
                "message_id": msg_id,
                "channel_id": channel_id,
                "user_id": user_id,
                "username": username,
            },
        )
        for h in list(self._handlers):
            try:
                await h(inbound)
            except Exception as exc:  # noqa: BLE001
                _log.warning("discord.handler_failed", err=str(exc))


# ── helpers ────────────────────────────────────────────────────────


def _coerce_id_set(raw: Any, *, key: str) -> set[int]:
    """Validate + coerce a config-supplied id list to a set of ints.

    Discord user / channel ids are 64-bit snowflakes (always positive);
    accepting strings would silently miss matches against the int the
    API returns. Empty / missing → empty set (no restriction). Non-list
    raw raises so a typo like ``allowed_user_ids: "12345"`` doesn't
    read as a 5-char allowlist that always fails.
    """
    if raw is None:
        return set()
    if not isinstance(raw, list):
        raise ValueError(
            f"channels.discord.{key} must be a list of int ids, got "
            f"{type(raw).__name__}"
        )
    out: set[int] = set()
    for entry in raw:
        if isinstance(entry, bool):
            # bool is an int subclass in Python; reject explicitly.
            raise ValueError(
                f"channels.discord.{key} entries must be int, got bool"
            )
        if isinstance(entry, int):
            out.add(entry)
            continue
        if isinstance(entry, str) and entry.strip().isdigit():
            out.add(int(entry.strip()))
            continue
        raise ValueError(
            f"channels.discord.{key} entries must be int or "
            f"int-shaped string, got {entry!r}"
        )
    return out


def _to_int_or_none(value: Any) -> int | None:
    """Best-effort int coercion for reply_to_message_id.

    InboundMessage.raw["message_id"] is set to ``str(message_id)`` by
    the inbound handler, but the dispatcher then forwards it through
    OutboundMessage.reply_to as a string. discord.py's reply API wants
    an int for fetch_message; non-numeric → None (skip the reply
    attachment but still send the message).
    """
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None
