"""SlackAdapter — bidirectional Slack channel.

B-382 (Sprint 2). Implements the :file:`__init__.py` MANIFEST as a
working :class:`ChannelAdapter`. Uses ``slack-bolt``'s Socket Mode
(``AsyncSocketModeHandler``) so the daemon doesn't need a public
webhook / cloudflared tunnel — Slack pushes events over a WebSocket
that we open with the app-level (``xapp-...``) token.

Inbound flow
------------

  Slack DM / channel message → Slack Events API → Socket Mode WS
  push to ``AsyncSocketModeHandler`` → ``AsyncApp`` dispatches the
  ``message`` event to our handler → ``_on_message_async`` → wrap
  as :class:`InboundMessage` → fan out to subscribers (typically
  :class:`ChannelDispatcher`) → ``AgentLoop.run_turn(session_id=
  "slack:<channel_id>", content)`` → AgentLoop emits events →
  ``ChannelDispatcher`` pulls last assistant text → ``adapter.send``
  back to the Slack channel via ``client.chat_postMessage``.

Outbound flow
-------------

  ``adapter.send(target, payload)`` → ``WebClient.chat_postMessage(
  channel=target.ref, text=payload.content, thread_ts=payload.reply_to
  or None)``. Slack's ``text`` field is technically capped at 40k but
  rendering degrades hard above ~4000; we split on word / line
  boundaries below that and emit successive posts so big tool dumps
  don't truncate.

Config (read from config.channels.slack.{...})
----------------------------------------------

  bot_token                    : 'xoxb-...' — bot user OAuth token
                                 (api.slack.com/apps → OAuth & Permissions)
  app_token                    : 'xapp-...' — app-level token with
                                 ``connections:write`` (api.slack.com/apps
                                 → Basic Information → App-Level Tokens).
                                 This is the Socket Mode key; without
                                 it no WS opens.
  allowed_user_ids             : list[str] — when non-empty, drop
                                 messages from sender ids NOT in the
                                 list. Slack ids are 'Uxxxxxxxx' for
                                 users / 'Wxxxxxxxx' for enterprise.
  allowed_channel_ids          : list[str] — when non-empty, drop
                                 messages from channels NOT in the
                                 list. Cxxx = public channel, Gxxx =
                                 private, Dxxx = DM.
  dispatch_session_id_prefix   : str — informational. The dispatcher
                                 composes session_id as
                                 ``f"{adapter.name}:{target.ref}"``;
                                 since ``adapter.name == "slack"`` and
                                 ``target.ref`` is the channel id, the
                                 actual session id is
                                 ``slack:<channel_id>``. The prefix
                                 config exists for parity with the task
                                 spec / for tooling that wants the
                                 namespace string.
  injection_policy             : 'detect_only' | 'redact' | 'block'
                                 (default detect_only) — same Epic #14
                                 policy Feishu / Telegram use. Slack
                                 channels can have many members; without
                                 a scan a hostile user could stage an
                                 'ignore previous instructions' attack
                                 via a public channel.

The adapter starts the Socket Mode handler in the background; ``stop``
shuts it down cleanly. slack-bolt handles network blips + WS
reconnects internally — we don't add an outer backoff loop.
"""
from __future__ import annotations

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


# Slack's chat.postMessage `text` field accepts up to 40k chars but
# rendering degrades hard above ~4000 (line breaks ignored, "Show more"
# truncation, etc.). Stay below 4000 to match the way humans actually
# read Slack threads.
_SLACK_MAX_CHARS = 3900

# Hint shown when ``slack-bolt`` is not installed. Importing at top
# level would crash the daemon for users who never enable Slack, so
# we fail at start() time with this message instead.
_INSTALL_HINT = (
    "slack-bolt is not installed. Install it via "
    "`pip install xmclaw[channels-slack]` (or "
    "`pip install 'slack-bolt>=1.18' 'slack-sdk>=3'` directly) "
    "and restart the daemon."
)


def _split_for_slack(text: str, cap: int = _SLACK_MAX_CHARS) -> list[str]:
    """Chunk ``text`` into pieces <= ``cap`` chars each.

    Prefers paragraph / line breaks; falls back to a hard cut when a
    single line is itself longer than the cap. Slack renders "..."
    cleanly between successive posts, so we don't add markers.
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

    Slack ids are opaque strings (Uxxx, Cxxx, Dxxx, etc). Empty /
    missing → empty set (no restriction). Non-list raw raises so a
    typo like ``allowed_user_ids: "U123"`` doesn't read as a 4-char
    allowlist that always fails.
    """
    if raw is None:
        return set()
    if not isinstance(raw, list):
        raise ValueError(
            f"channels.slack.{key} must be a list of str ids, got "
            f"{type(raw).__name__}"
        )
    out: set[str] = set()
    for entry in raw:
        if not isinstance(entry, str):
            raise ValueError(
                f"channels.slack.{key} entries must be str, got "
                f"{type(entry).__name__}"
            )
        s = entry.strip()
        if s:
            out.add(s)
    return out


class SlackAdapter(ChannelAdapter):
    """Slack bot channel adapter (Socket Mode).

    Args:
        config: dict with at minimum ``bot_token`` (xoxb-) and
                ``app_token`` (xapp-). Optional ``allowed_user_ids``
                (list of Slack user-id strings), ``allowed_channel_ids``
                (list of channel-id strings), ``dispatch_session_id_prefix``
                (informational), ``injection_policy`` (Epic #14).
    """

    name = "slack"

    def __init__(self, config: dict[str, Any]) -> None:
        self._cfg = config or {}
        self._bot_token = (self._cfg.get("bot_token") or "").strip()
        self._app_token = (self._cfg.get("app_token") or "").strip()
        if not self._bot_token:
            raise ValueError(
                "Slack adapter needs config.channels.slack.bot_token "
                "(xoxb-... from api.slack.com/apps → OAuth & Permissions)"
            )
        if not self._app_token:
            raise ValueError(
                "Slack adapter needs config.channels.slack.app_token "
                "(xapp-... with connections:write scope — Socket Mode key, "
                "see api.slack.com/apps → Basic Information → App-Level Tokens)"
            )
        # Pre-coerce allowlists to str sets at __init__ so a misconfigured
        # entry surfaces at boot rather than on the first inbound message.
        self._allowed_user_ids: set[str] = _coerce_str_set(
            self._cfg.get("allowed_user_ids"), key="allowed_user_ids",
        )
        self._allowed_channel_ids: set[str] = _coerce_str_set(
            self._cfg.get("allowed_channel_ids"), key="allowed_channel_ids",
        )
        # Informational only — the dispatcher uses adapter.name + target.ref
        # to compose session_id. Stored so the setup endpoint can echo it.
        self._session_prefix: str = str(
            self._cfg.get("dispatch_session_id_prefix") or "slack-"
        )
        # Lazy: build inside start() so the heavy slack-bolt import
        # doesn't fire until the user actually enables this channel.
        self._app: Any = None
        self._handler: Any = None
        self._handlers: list[Callable[[InboundMessage], Awaitable[None]]] = []
        # Slack Events API has at-least-once delivery. Same client_msg_id
        # / event_id can land twice on Socket Mode reconnect. LRU keyed
        # by (channel, ts) so two unrelated channels with overlapping ts
        # aren't conflated.
        self._seen_msg_ids: OrderedDict[str, float] = OrderedDict()
        self._seen_cap = 512
        # Surface field for setup-endpoint health (B-368 pattern).
        # When start() fails (bad token, network blocked at boot), this
        # holds a human-readable string the UI can show.
        self.last_start_error: str | None = None

    # ── public API ──────────────────────────────────────────────

    def subscribe(
        self, handler: Callable[[InboundMessage], Awaitable[None]],
    ) -> None:
        self._handlers.append(handler)

    async def start(self) -> None:
        if self._app is not None:
            return  # idempotent
        try:
            from slack_bolt.async_app import AsyncApp
            from slack_bolt.adapter.socket_mode.async_handler import (
                AsyncSocketModeHandler,
            )
        except ImportError as exc:
            self.last_start_error = _INSTALL_HINT
            raise RuntimeError(_INSTALL_HINT) from exc

        try:
            app = AsyncApp(token=self._bot_token)
        except Exception as exc:  # noqa: BLE001
            self.last_start_error = (
                f"Slack AsyncApp init failed: {type(exc).__name__}: {exc}. "
                "Check the bot_token (must start with 'xoxb-')."
            )
            raise RuntimeError(self.last_start_error) from exc

        # Subscribe to ``message`` events — covers DMs (Dxxx), public
        # channels (Cxxx), private channels (Gxxx). app_mention would
        # only fire when the bot is @-mentioned; using ``message``
        # matches the contract claim "subscribe to message events".
        # We filter bot-authored echoes inside _on_message_async so
        # the agent doesn't talk to itself.
        @app.event("message")
        async def _on_message(event: dict[str, Any], **_kwargs: Any) -> None:
            try:
                await self._on_message_async(event)
            except Exception as exc:  # noqa: BLE001
                _log.warning("slack.dispatch_failed", err=str(exc))

        try:
            handler = AsyncSocketModeHandler(app, self._app_token)
            await handler.connect_async()
        except Exception as exc:  # noqa: BLE001
            self.last_start_error = (
                f"Slack Socket Mode connect failed: "
                f"{type(exc).__name__}: {exc}. "
                "Common causes: app_token missing 'connections:write' "
                "scope, Socket Mode disabled in app settings, or network "
                "blocked from the daemon to slack.com."
            )
            raise RuntimeError(self.last_start_error) from exc

        self._app = app
        self._handler = handler
        self.last_start_error = None
        # Mask both tokens when logging — never emit the secret part.
        _log.info(
            "slack.started",
            bot_token_prefix=self._bot_token[:5] + "***",
            app_token_prefix=self._app_token[:5] + "***",
            allowlist_users=len(self._allowed_user_ids),
            allowlist_channels=len(self._allowed_channel_ids),
        )

    async def stop(self) -> None:
        if self._handler is None and self._app is None:
            return
        handler = self._handler
        self._handler = None
        self._app = None
        # close_async winds down the WS connection cleanly. Each step
        # swallows its own errors so a half-shutdown doesn't leave the
        # next step un-attempted (matches feishu / telegram posture).
        if handler is not None:
            try:
                close = getattr(handler, "close_async", None)
                if close is not None:
                    await close()
                else:
                    # Older bolt builds expose .disconnect_async; fall
                    # through gracefully.
                    disc = getattr(handler, "disconnect_async", None)
                    if disc is not None:
                        await disc()
            except Exception as exc:  # noqa: BLE001
                _log.warning("slack.handler_close_failed", err=str(exc))
        _log.info("slack.stopped")

    async def send(
        self, target: ChannelTarget, payload: OutboundMessage,
    ) -> str:
        if target.channel != self.name:
            raise ValueError(
                f"SlackAdapter cannot send to channel={target.channel!r}; "
                f"expected {self.name!r}"
            )
        if self._app is None:
            raise RuntimeError("slack adapter not started")
        if not target.ref:
            raise ValueError("slack target.ref must be a non-empty channel id")

        client = self._app.client

        # Slack's chat.postMessage does not have a separate image-upload
        # surface like Feishu / Telegram. Files go through files.upload_v2
        # / files.completeUploadExternal. Skip attachments for now —
        # callers that need image delivery via Slack should follow up
        # with a files.upload_v2 hop. For text replies we just post.
        last_ts = ""
        chunks = _split_for_slack(payload.content)
        if not chunks and not target.ref:
            return ""
        if not chunks:
            # Empty content + no attachments — nothing to send. Slack
            # rejects empty posts (no_text error), so bail quietly.
            return ""

        # Slack threads via thread_ts: pass the parent ts on every
        # chunk so all chunks land in the same thread. (Telegram only
        # attaches reply_to to the first chunk because their UI
        # strongly visualises just that one; Slack's threading is
        # different — every reply needs the parent ts or it lands in
        # the channel root.)
        thread_ts = payload.reply_to or None
        for chunk in chunks:
            try:
                resp = await client.chat_postMessage(
                    channel=target.ref,
                    text=chunk,
                    thread_ts=thread_ts,
                )
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "slack.send_failed",
                    channel=target.ref, err=str(exc),
                )
                raise RuntimeError(
                    f"slack send failed: {type(exc).__name__}: {exc}"
                ) from exc
            # Slack response ts is the message identifier; expose it as
            # the message_id we return for the dispatcher to log.
            ts = ""
            if isinstance(resp, dict):
                ts = str(resp.get("ts") or "")
            else:
                ts = str(getattr(resp, "ts", "") or "")
            if ts:
                last_ts = ts
        return last_ts or f"slack:{int(time.time())}"

    # ── internal ────────────────────────────────────────────────

    async def _on_message_async(self, event: dict[str, Any]) -> None:
        """slack-bolt calls this for every ``message`` event.

        Translate to InboundMessage + fan out to subscribers (typically
        the ChannelDispatcher). Drop unauthorized senders silently — the
        log line tells the operator someone tried.
        """
        if not isinstance(event, dict):
            return
        # Filter bot-authored events to avoid self-echo loops. Slack
        # marks bot messages with ``bot_id`` AND/OR ``subtype="bot_message"``.
        # Also drop edits / deletions ("message_changed", "message_deleted")
        # — those have no fresh user content; we'd reprocess stale text.
        subtype = event.get("subtype") or ""
        if subtype in {
            "bot_message", "message_changed", "message_deleted",
            "channel_join", "channel_leave",
        }:
            return
        if event.get("bot_id"):
            return

        text = (event.get("text") or "").strip()
        if not text:
            return

        channel_id = (event.get("channel") or "").strip()
        if not channel_id:
            _log.debug("slack.no_channel_id", event_ts=event.get("ts"))
            return

        user_id = (event.get("user") or "").strip()
        msg_ts = (event.get("ts") or "").strip()
        client_msg_id = (event.get("client_msg_id") or "").strip()

        # Dedup against the LRU. Same event can land twice on a
        # network blip / Socket Mode reconnect. Key on (channel, ts);
        # Slack guarantees ts is unique within a channel.
        dedup_key = f"{channel_id}:{msg_ts}" if msg_ts else (
            f"cmsg:{client_msg_id}" if client_msg_id else ""
        )
        if dedup_key and dedup_key in self._seen_msg_ids:
            _log.info(
                "slack.duplicate_skipped", channel_id=channel_id, ts=msg_ts,
            )
            return
        if dedup_key:
            self._seen_msg_ids[dedup_key] = time.time()
            while len(self._seen_msg_ids) > self._seen_cap:
                self._seen_msg_ids.popitem(last=False)

        # Allowlist gate. Empty / missing config = no restriction
        # (preserves "any DM works" default for solo operators); set
        # the lists to lock down to a known set of users / channels.
        if self._allowed_user_ids and user_id not in self._allowed_user_ids:
            _log.warning(
                "slack.inbound_dropped_unauthorized_user",
                channel_id=channel_id, user_id=user_id,
                allowlist_size=len(self._allowed_user_ids),
            )
            return
        if (
            self._allowed_channel_ids
            and channel_id not in self._allowed_channel_ids
        ):
            _log.warning(
                "slack.inbound_dropped_unauthorized_channel",
                channel_id=channel_id, user_id=user_id,
                allowlist_size=len(self._allowed_channel_ids),
            )
            return

        # Epic #14: scan inbound text for prompt injection BEFORE
        # handing off to run_turn. A public Slack channel's members
        # aren't necessarily the daemon owner; without this scan a
        # hostile member can stage an "ignore previous instructions"
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
                    "channel": "slack",
                    "channel_id": channel_id,
                    "user_ref": user_id,
                    "message_ts": msg_ts,
                },
            )
            if decision.blocked:
                _log.warning(
                    "slack.inbound_blocked",
                    channel_id=channel_id, ts=msg_ts,
                    findings=[
                        f.pattern_id for f in decision.scan.findings
                    ][:5],
                )
                return  # drop message — don't fan out to agent
            text = decision.content
        except Exception as exc:  # noqa: BLE001
            _log.debug("slack.scan_skipped", err=str(exc))

        inbound = InboundMessage(
            target=ChannelTarget(channel=self.name, ref=channel_id),
            user_ref=user_id or "unknown",
            content=text,
            raw={
                "ts": msg_ts,
                "channel": channel_id,
                "user": user_id,
                "client_msg_id": client_msg_id,
                # Slack threading: when the user posted in a thread,
                # ``thread_ts`` carries the parent ts. ChannelDispatcher
                # forwards inbound.raw["message_id"] as OutboundMessage.
                # reply_to — we point that at thread_ts when present so
                # the agent's reply lands in the same thread, otherwise
                # the message ts so a reply in the channel root threads
                # under the user's message.
                "message_id": event.get("thread_ts") or msg_ts,
            },
        )
        for h in list(self._handlers):
            try:
                await h(inbound)
            except Exception as exc:  # noqa: BLE001
                _log.warning("slack.handler_failed", err=str(exc))
