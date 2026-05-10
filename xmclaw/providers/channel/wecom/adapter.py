"""WeComAdapter — outbound-only WeCom (企业微信) channel.

B-384 (Sprint 2). Direct sibling of the B-380 / B-381 / B-382 Telegram
/ Discord / Slack adapters. WeCom has two API surfaces:

1. **Internal-bot webhook** (this adapter)
   POST to ``https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=<key>``
   with a JSON body. One-way: the daemon sends, no inbound delivery.
   Useful for notification-style use (release alerts, daily digests,
   agent-finished pings into the team chat).
2. **Self-built app callback** (NOT in v1)
   Receive encrypted XML via HTTP callback. Requires a public URL +
   AES-CBC decrypt + signature verification. XMclaw is local-first;
   inbound waits for the cloudflared bootstrap to land generically.

Why outbound-only is honest "ready"
-----------------------------------

The adapter exposes the full :class:`ChannelAdapter` ABC: ``start``,
``stop``, ``send``, ``subscribe``. ``subscribe`` is a no-op — the
adapter accepts the registration so the dispatcher can wire it
uniformly, but no inbound message is ever fanned out. Setting
``implementation_status="ready"`` is correct because the *user-visible*
operation (notify a WeCom group from the agent) DOES work. The
docstring + manifest config + this module make the limitation explicit
so anyone enabling WeCom expecting bidirectional chat hits a clear
docstring instead of a silent mystery.

Outbound flow
-------------

  ``adapter.send(target, payload)`` → POST JSON to
  ``webhook_url`` (or override per-call via ``target.ref`` if the user
  passes a different webhook key) → WeCom returns
  ``{"errcode": 0, "errmsg": "ok"}`` on success. WeCom imposes a
  4096-character cap per message; we chunk longer replies into
  successive posts so big tool dumps don't get truncated.

Config (``config.channels.wecom.{...}``)
----------------------------------------

  webhook_url            : str (required) — full webhook URL with the
                           ``?key=<bot-key>`` query string. Get one
                           from group chat → 群设置 → 群机器人 → 添加.
  msgtype                : 'text' | 'markdown' | 'image' | 'news' |
                           'file' (default 'markdown'). The webhook
                           bot ONLY supports those five — chat-app
                           features like 'mpnews' / 'miniprogram_notice'
                           come from the self-built-app surface (which
                           this adapter doesn't speak).
  mentioned_list         : list[str] (optional) — userid list to
                           @mention. Special value '@all' tags everyone
                           in the group. Only honored when
                           ``msgtype='text'`` (markdown @-mentions
                           require ``<@userid>`` syntax inside the
                           content; we leave that to the caller).
  mentioned_mobile_list  : list[str] (optional) — mobile-number list
                           to @mention. Same text-only constraint.

Reference: https://developer.work.weixin.qq.com/document/path/91770
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from xmclaw.providers.channel.base import (
    ChannelAdapter,
    ChannelTarget,
    InboundMessage,
    OutboundMessage,
)
from xmclaw.providers.channel._shared import split_text
from xmclaw.utils.log import get_logger


_log = get_logger(__name__)


# WeCom hard cap per message body. Longer messages return errcode 93000
# ("message body too long") — same posture Telegram / Discord / Slack
# adapters use: chunk on word / line boundaries first, fall back to a
# hard cut if a single token exceeds the cap.
_WECOM_MAX_CHARS = 4096

# WeCom webhook host — used to validate ``webhook_url`` at __init__.
# Catching a typo'd host at boot beats a 404 surfacing on the first
# notification (and lets the setup-endpoint health check actually
# verify shape rather than blindly POST against a foreign domain).
_WECOM_HOST = "qyapi.weixin.qq.com"

# Default request timeout. WeCom usually responds in <500ms; a 10s
# ceiling means a slow proxy / DNS hiccup doesn't block the agent's
# turn for the FastAPI default 60s.
_WECOM_TIMEOUT_S = 10.0

# Allowed msgtypes for webhook bots. Reject anything else at __init__
# so a typo like 'markdwon' surfaces immediately.
_ALLOWED_MSGTYPES: frozenset[str] = frozenset(
    {"text", "markdown", "image", "news", "file"}
)



def _coerce_str_list(raw: Any, *, key: str) -> list[str]:
    """Validate + coerce a config-supplied list of strings.

    WeCom userid / mobile entries are opaque strings. Empty / missing →
    empty list (no @mention). Non-list raw raises so a typo like
    ``mentioned_list: "@all"`` (which is a string, not a list) doesn't
    silently degrade to a 4-char list of single characters.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(
            f"channels.wecom.{key} must be a list of str, got "
            f"{type(raw).__name__}"
        )
    out: list[str] = []
    for entry in raw:
        if not isinstance(entry, str):
            raise ValueError(
                f"channels.wecom.{key} entries must be str, got "
                f"{type(entry).__name__}"
            )
        s = entry.strip()
        if s:
            out.append(s)
    return out


def _validate_webhook_url(url: str) -> None:
    """Reject obviously-broken webhook URLs at __init__ time.

    WeCom webhook URLs always sit at qyapi.weixin.qq.com/cgi-bin/webhook/send
    with a ``key=...`` query param. We do shape-only validation here —
    the real authentication happens on the first POST. The point is
    "catch a typo before the daemon boots", not "verify the bot exists".
    """
    if not url:
        raise ValueError(
            "WeCom adapter needs config.channels.wecom.webhook_url "
            "(get one from 群设置 → 群机器人 → 添加 → 复制 webhook URL; "
            "shape: https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...)"
        )
    if not url.startswith(("http://", "https://")):
        raise ValueError(
            f"channels.wecom.webhook_url must start with https:// or http://, "
            f"got {url[:30]!r}"
        )
    if _WECOM_HOST not in url:
        # Common typo: pasting the LARK/Feishu webhook into the WeCom
        # field. Catch it before the operator wonders why nothing arrives.
        raise ValueError(
            f"channels.wecom.webhook_url should point at {_WECOM_HOST!r}, "
            f"got {url!r}. (Did you mean to use channels.feishu instead?)"
        )
    if "key=" not in url:
        raise ValueError(
            "channels.wecom.webhook_url must include a 'key=...' query "
            "parameter — that's the bot key. Verify the URL you copied "
            "from 群设置 → 群机器人 → 添加."
        )


class WeComAdapter(ChannelAdapter):
    """WeCom (企业微信) internal-bot webhook channel adapter.

    Outbound-only by design (see module docstring). ``subscribe`` is a
    no-op accepted for ABC compliance — the dispatcher can register a
    handler uniformly across channels even though no inbound is ever
    fanned out.

    Args:
        config: dict with at minimum ``webhook_url``. Optional
                ``msgtype`` (default 'markdown'),
                ``mentioned_list`` / ``mentioned_mobile_list`` (text-
                msgtype @-mentions).
    """

    name = "wecom"

    def __init__(self, config: dict[str, Any]) -> None:
        self._cfg = config or {}
        self._webhook_url = (self._cfg.get("webhook_url") or "").strip()
        _validate_webhook_url(self._webhook_url)

        raw_msgtype = self._cfg.get("msgtype") or "markdown"
        if not isinstance(raw_msgtype, str):
            raise ValueError(
                f"channels.wecom.msgtype must be a string, got "
                f"{type(raw_msgtype).__name__}"
            )
        msgtype = raw_msgtype.strip().lower()
        if msgtype not in _ALLOWED_MSGTYPES:
            raise ValueError(
                f"channels.wecom.msgtype must be one of "
                f"{sorted(_ALLOWED_MSGTYPES)}, got {msgtype!r}"
            )
        self._msgtype: str = msgtype

        # Pre-coerce mention lists at __init__ so a misconfigured entry
        # surfaces at boot rather than on the first send. Empty lists
        # are fine (= no @mention).
        self._mentioned_list: list[str] = _coerce_str_list(
            self._cfg.get("mentioned_list"), key="mentioned_list",
        )
        self._mentioned_mobile_list: list[str] = _coerce_str_list(
            self._cfg.get("mentioned_mobile_list"),
            key="mentioned_mobile_list",
        )

        # Lazy: build the httpx client inside start() so a daemon that
        # never enables WeCom doesn't carry an open connection pool.
        self._client: httpx.AsyncClient | None = None
        # ABC contract: subscribe() accepts handlers but never invokes
        # them — outbound-only. Stored so the dispatcher's wiring code
        # doesn't error on the registration call.
        self._handlers: list[Callable[[InboundMessage], Awaitable[None]]] = []
        # Surface field for setup-endpoint health (B-368 pattern). When
        # start() fails (e.g. httpx import borked, transient network),
        # this holds a human-readable string the UI can show.
        self.last_start_error: str | None = None

    # ── public API ──────────────────────────────────────────────

    def subscribe(
        self, handler: Callable[[InboundMessage], Awaitable[None]],
    ) -> None:
        """Accept handler registration without ever invoking it.

        WeCom internal-bot webhooks are one-way: there is no inbound
        delivery channel for this adapter to translate. The method is
        present so ChannelDispatcher's per-adapter wiring loop doesn't
        special-case WeCom. If a future adapter version implements the
        self-built-app callback surface, it'll start fanning these out.
        """
        self._handlers.append(handler)
        _log.debug(
            "wecom.subscribe_noop",
            note=(
                "WeCom webhook is outbound-only; handler registered but "
                "will never be invoked. Inbound requires the self-built "
                "app callback surface (out of scope for v1)."
            ),
        )

    async def start(self) -> None:
        """Spin up the httpx async client. Idempotent."""
        if self._client is not None:
            return
        try:
            self._client = httpx.AsyncClient(timeout=_WECOM_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001
            self.last_start_error = (
                f"WeCom AsyncClient init failed: "
                f"{type(exc).__name__}: {exc}"
            )
            raise RuntimeError(self.last_start_error) from exc
        self.last_start_error = None
        # Mask the bot key when logging — anything after 'key=' is
        # secret. Show only the host + path prefix.
        safe_url = self._webhook_url.split("?", 1)[0] + "?key=***"
        _log.info(
            "wecom.started",
            webhook_prefix=safe_url,
            msgtype=self._msgtype,
            mention_count=len(self._mentioned_list)
            + len(self._mentioned_mobile_list),
        )

    async def stop(self) -> None:
        """Close the httpx client. Idempotent + error-tolerant."""
        if self._client is None:
            return
        client = self._client
        self._client = None
        try:
            await client.aclose()
        except Exception as exc:  # noqa: BLE001
            _log.warning("wecom.client_close_failed", err=str(exc))
        _log.info("wecom.stopped")

    async def send(
        self, target: ChannelTarget, payload: OutboundMessage,
    ) -> str:
        """POST the agent's reply to the WeCom webhook.

        ``target.ref`` is informational — WeCom internal-bot webhooks
        are URL-keyed (the bot key lives in the URL itself), not
        target-keyed. We log it so multi-room deployments can correlate,
        but every message goes to ``self._webhook_url``.
        """
        if target.channel != self.name:
            raise ValueError(
                f"WeComAdapter cannot send to channel={target.channel!r}; "
                f"expected {self.name!r}"
            )
        if self._client is None:
            raise RuntimeError("wecom adapter not started")

        chunks = split_text(payload.content, _WECOM_MAX_CHARS)
        if not chunks:
            # Empty content — nothing to send. WeCom's webhook rejects
            # empty body with errcode 44004; bail quietly to mirror the
            # Slack adapter posture.
            return ""

        last_msgid = ""
        for i, chunk in enumerate(chunks):
            body = self._build_body(chunk, is_first_chunk=(i == 0))
            try:
                resp = await self._post_with_retry(body)
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "wecom.send_failed",
                    target_ref=target.ref,
                    msgtype=self._msgtype,
                    err=str(exc),
                )
                # Re-raise the LAST chunk failure so the dispatcher's
                # outer try/except records channel.send_failed and the
                # operator at least sees the delivery dropped. Earlier-
                # chunk failures inside a multi-chunk send still log +
                # raise (so a partially-delivered message surfaces — we
                # don't silently swallow). Telegram chooses to keep
                # going on early failures; WeCom's webhook is more
                # rate-limited (20 messages/min per bot), so partial
                # delivery is more likely to be a 45009 throttle. Bail
                # immediately + tell the operator.
                raise RuntimeError(
                    f"wecom send failed on chunk {i + 1}/{len(chunks)}: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
            # WeCom doesn't return a per-message id; we synthesize one
            # from response timing so the dispatcher's logging line has
            # a non-empty handle.
            last_msgid = f"wecom:{int(resp.get('_t', time.time()))}:{i}"
        return last_msgid or f"wecom:{int(time.time())}"

    # ── internal ────────────────────────────────────────────────

    def _build_body(self, content: str, *, is_first_chunk: bool) -> dict[str, Any]:
        """Compose the WeCom webhook JSON body for a single chunk.

        Only the first chunk carries @mention metadata (text msgtype) —
        otherwise every chunk in a multi-chunk send would re-tag
        everyone, which spams notifications. The text msgtype is the
        only one that supports the structured ``mentioned_list`` /
        ``mentioned_mobile_list`` fields; for markdown we leave the
        caller to embed ``<@userid>`` inline if they want @-mentions.
        """
        if self._msgtype == "text":
            text_body: dict[str, Any] = {"content": content}
            if is_first_chunk and self._mentioned_list:
                text_body["mentioned_list"] = list(self._mentioned_list)
            if is_first_chunk and self._mentioned_mobile_list:
                text_body["mentioned_mobile_list"] = list(
                    self._mentioned_mobile_list,
                )
            return {"msgtype": "text", "text": text_body}
        if self._msgtype == "markdown":
            return {
                "msgtype": "markdown",
                "markdown": {"content": content},
            }
        # image / news / file would need binary upload or rich payload
        # construction the agent's text reply doesn't directly produce.
        # Default to text-shaped body so the operator's send still
        # delivers SOMETHING; log the mismatch so they can flip msgtype
        # if they're not seeing the right rendering.
        _log.warning(
            "wecom.msgtype_fallback",
            requested=self._msgtype,
            note=(
                "image/news/file msgtypes need richer payload than the "
                "agent's plain text emits; falling back to markdown body"
            ),
        )
        return {"msgtype": "markdown", "markdown": {"content": content}}

    async def _post_with_retry(self, body: dict[str, Any]) -> dict[str, Any]:
        """POST to the webhook; retry once on transient (5xx / network) failure.

        WeCom's webhook returns ``{"errcode": int, "errmsg": str}`` —
        errcode 0 is success, anything else is a logical error. We
        retry on:

          * httpx network errors (connect timeout, read timeout, DNS)
          * HTTP 5xx (server-side hiccup, common around CN business
            hours when WeCom is mid-deploy)

        We do NOT retry on:

          * 4xx (bad request — same body next time, same failure)
          * errcode != 0 (validation / rate limit / disabled bot —
            retrying loses time for the agent loop)

        The retry is single-shot; persistent outages get a clean
        RuntimeError so the dispatcher records send_failed.
        """
        assert self._client is not None  # guarded by send()
        last_exc: Exception | None = None
        for attempt in (1, 2):
            try:
                response = await self._client.post(
                    self._webhook_url, json=body,
                )
            except httpx.RequestError as exc:
                last_exc = exc
                _log.info(
                    "wecom.network_retry",
                    attempt=attempt,
                    err=str(exc),
                )
                if attempt == 1:
                    await asyncio.sleep(0.5)
                    continue
                raise RuntimeError(
                    f"WeCom network error after retry: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
            if response.status_code >= 500:
                last_exc = RuntimeError(
                    f"WeCom HTTP {response.status_code}"
                )
                _log.info(
                    "wecom.5xx_retry",
                    attempt=attempt,
                    status=response.status_code,
                )
                if attempt == 1:
                    await asyncio.sleep(0.5)
                    continue
                raise RuntimeError(
                    f"WeCom returned HTTP {response.status_code} "
                    f"after retry"
                )
            if response.status_code >= 400:
                # 4xx: client mistake; don't retry. Body usually carries
                # the WeCom errcode/errmsg even though status was 400.
                raise RuntimeError(
                    f"WeCom HTTP {response.status_code}: {response.text[:200]}"
                )
            # 2xx — parse the WeCom envelope.
            try:
                payload = response.json()
            except Exception as exc:  # noqa: BLE001 — non-JSON 2xx means foreign host
                raise RuntimeError(
                    f"WeCom returned non-JSON 2xx: {response.text[:200]}"
                ) from exc
            errcode = int(payload.get("errcode", -1))
            if errcode != 0:
                # Common errcodes (https://developer.work.weixin.qq.com/document/path/90313):
                #   45009 — frequency limit exceeded (20 msgs/min per bot)
                #   93000 — message body too long
                #   44004 — empty message body
                # Surface them verbatim — the operator can look up the
                # code and act (slow down, switch to markdown, etc.).
                raise RuntimeError(
                    f"WeCom errcode={errcode} errmsg="
                    f"{payload.get('errmsg', '')!r}"
                )
            # Stamp wall-clock so send() can build a stable msg id even
            # without a real WeCom-side id.
            payload["_t"] = time.time()
            return payload
        # Defensive fallthrough — shouldn't reach here because both
        # branches above either return or raise.
        raise RuntimeError(
            f"wecom send exhausted retries; last_exc="
            f"{type(last_exc).__name__ if last_exc else 'None'}"
        )
