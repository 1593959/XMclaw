"""EmailChannelAdapter — bidirectional email channel.

B-393 (Sprint 2). IMAP poll for inbound + SMTP for outbound. Uses
stdlib ``imaplib`` / ``smtplib`` / ``email`` so the base install
doesn't grow a new dep — every consumer mail provider speaks IMAP4
SSL + SMTP_SSL or STARTTLS out of the box.

Inbound flow
------------

  IMAP server INBOX → ``_poll_loop`` (background task, period
  ``poll_interval_s``) → ``imaplib.IMAP4_SSL.search('UNSEEN')`` →
  ``fetch(num, '(RFC822)')`` → ``email.message_from_bytes`` →
  extract ``from`` / ``subject`` / ``Message-ID`` / plain-text body
  → wrap as :class:`InboundMessage` → fan out to subscribers
  (typically :class:`ChannelDispatcher`) →
  ``AgentLoop.run_turn(session_id="email:<sender_addr>", content)``
  → AgentLoop emits events → ``ChannelDispatcher`` pulls last
  assistant text → ``adapter.send`` back to the sender via
  ``smtplib`` with ``In-Reply-To`` / ``References`` set so the reply
  threads under the original.

Outbound flow
-------------

  ``adapter.send(target, payload)`` → build
  :class:`email.message.EmailMessage` → ``smtplib.SMTP_SSL.send_message``
  (or STARTTLS via ``SMTP.starttls`` when ``smtp_use_ssl=False``).
  Both legs run inside ``asyncio.to_thread`` because ``smtplib`` /
  ``imaplib`` are sync-only.

Critical gotchas
----------------

* **Gmail / Outlook need App Passwords**, not the account password.
  When IMAP login returns the Gmail-specific 535 5.7.8 error code
  we surface a clear hint pointing at
  https://support.google.com/accounts/answer/185833.
* IMAP IDLE is **not** used — many consumer providers rate-limit or
  outright disallow it; polling at 30s default is the safe path.
* RFC 2047 MIME-encoded subjects + From names are decoded via
  ``email.header.decode_header`` so non-ASCII headers don't arrive
  as ``=?UTF-8?B?...?=`` literal.
* HTML-only emails are downgraded to a stripped-tag plain-text
  rendering with stdlib ``html.parser`` (no BeautifulSoup dep
  added). When both ``text/plain`` and ``text/html`` are present
  the plain part wins.
"""
from __future__ import annotations

import asyncio
import email
import email.message
import email.policy
import email.utils
import hashlib
import imaplib
import os
import re
import smtplib
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from email.header import decode_header, make_header
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from xmclaw.providers.channel.base import (
    ChannelAdapter,
    ChannelTarget,
    InboundMessage,
    OutboundMessage,
)
from xmclaw.utils.log import get_logger


_log = get_logger(__name__)


# Gmail returns SMTP "535 5.7.8 Username and Password not accepted" /
# IMAP "AUTHENTICATIONFAILED" with the same root cause: the user is
# trying to log in with their account password instead of an App
# Password. Detect both shapes and surface the same actionable message.
_GMAIL_AUTH_ERROR_MARKERS = (
    "535 5.7.8",
    "5.7.8",
    "AUTHENTICATIONFAILED",
    "[ALERT]",
    "Application-specific password required",
)
_GMAIL_APP_PASSWORD_HINT = (
    "Email auth failed. Most consumer providers (Gmail, Outlook, "
    "163, Yahoo) require an App Password instead of the account "
    "password — see https://support.google.com/accounts/answer/185833 "
    "for Gmail. Generate one and update channels.email.imap_password "
    "/ smtp_password (or store it in xmclaw secrets under "
    "channels.email.imap_password / channels.email.smtp_password)."
)


class _HTMLStripper(HTMLParser):
    """Tiny stdlib-only HTML → text converter.

    Used as a fallback when an inbound email only ships ``text/html``
    (no ``text/plain`` part). We don't need a real renderer — the
    agent just needs the prose. Heuristics:

    * Strip script / style content entirely.
    * Treat ``<br>`` and block-level tags as paragraph breaks.
    * Collapse runs of whitespace.

    We intentionally do NOT pull in BeautifulSoup just for this fallback;
    consumer-grade emails mostly come through with a ``text/plain``
    part, and when they don't this stripped version is good enough for
    the agent to work with.
    """

    _BLOCK_TAGS = {
        "p", "div", "br", "hr", "li", "tr", "h1", "h2", "h3", "h4",
        "h5", "h6", "blockquote", "section", "article", "header",
        "footer",
    }
    _SKIP_TAGS = {"script", "style", "head", "title", "meta", "link"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        elif tag in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._chunks.append(data)

    def get_text(self) -> str:
        joined = "".join(self._chunks)
        # Collapse 3+ newlines to 2 (paragraph break) and squash runs of
        # spaces/tabs but preserve paragraph structure.
        joined = re.sub(r"[ \t]+", " ", joined)
        joined = re.sub(r"\n{3,}", "\n\n", joined)
        return joined.strip()


def _decode_header_value(raw: str | None) -> str:
    """RFC 2047-decode a header value (Subject / From) safely.

    Many subjects arrive as ``=?UTF-8?B?5L2g5aW9?=``; without this
    decode the agent would see the literal encoded form. ``make_header``
    handles concatenation of partial-encoded chunks (e.g. mixed-charset
    subjects) better than calling ``decode_header`` and joining by hand.
    """
    if raw is None:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except Exception:  # noqa: BLE001 — malformed header should not crash poll
        return raw


def _extract_email_address(raw_from: str) -> str:
    """Pull the bare ``user@host`` part out of a ``From`` header.

    ``email.utils.parseaddr`` handles every flavor we care about:
    ``"Alice <alice@example.com>"`` → ``("Alice", "alice@example.com")``,
    ``"alice@example.com"`` → ``("", "alice@example.com")``, malformed
    → ``("", "")``. We normalize to lowercase so allowlist checks are
    case-insensitive (RFC 5321: local-part is technically case-sensitive
    but in practice nobody routes that way).
    """
    if not raw_from:
        return ""
    decoded = _decode_header_value(raw_from)
    _, addr = email.utils.parseaddr(decoded)
    return addr.strip().lower()


def _extract_plain_body(msg: email.message.Message) -> str:
    """Best-effort plain-text extraction from a parsed email.

    Walk the MIME tree:
    1. First pass: find any ``text/plain`` non-attachment part. If
       multiple exist, prefer the first (multipart/alternative usually
       lists plain before html).
    2. Fallback: find a ``text/html`` part and run :class:`_HTMLStripper`
       over it.
    3. Last resort: empty string.

    Decoding charset uses the ``Content-Type``'s charset hint and falls
    back to utf-8 with errors='replace' so a non-UTF-8 body doesn't
    crash the poll. We never trust a vendor-supplied charset blindly —
    a missing / lying charset is too common to gate on.
    """
    plain_text: str | None = None
    html_text: str | None = None

    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disposition = (part.get("Content-Disposition") or "").lower()
            if "attachment" in disposition:
                continue
            if ctype == "text/plain" and plain_text is None:
                plain_text = _decode_payload(part)
            elif ctype == "text/html" and html_text is None:
                html_text = _decode_payload(part)
    else:
        # Non-multipart: the whole message is the body.
        ctype = msg.get_content_type()
        if ctype == "text/plain":
            plain_text = _decode_payload(msg)
        elif ctype == "text/html":
            html_text = _decode_payload(msg)

    if plain_text and plain_text.strip():
        return plain_text.strip()
    if html_text and html_text.strip():
        stripper = _HTMLStripper()
        try:
            stripper.feed(html_text)
            stripper.close()
        except Exception:  # noqa: BLE001
            return html_text.strip()
        return stripper.get_text()
    return ""


def _extract_attachments(
    msg: email.message.Message,
    *,
    max_count: int = 10,
    max_size_bytes: int = 25 * 1024 * 1024,
) -> list[dict[str, Any]]:
    """Extract attachment metadata + bytes from a parsed email.

    Returns a list of dicts with keys:
      * ``filename`` — sanitized name (empty if none provided)
      * ``content_type`` — MIME type
      * ``size`` — decoded payload size in bytes
      * ``payload`` — raw ``bytes``

    Guards:
      * ``max_count`` — stops after N attachments (defensive against
        mail-bomb attacks).
      * ``max_size_bytes`` — skips individual attachments that exceed
        the limit (prevents disk exhaustion).
    """
    attachments: list[dict[str, Any]] = []
    if not msg.is_multipart():
        return attachments

    for part in msg.walk():
        if len(attachments) >= max_count:
            break
        disposition = (part.get("Content-Disposition") or "").lower()
        if "attachment" not in disposition:
            continue

        # Extract filename safely.
        filename = part.get_filename() or ""
        # Sanitize: basename only, no path traversal.
        if filename:
            filename = Path(filename).name
            # Drop any non-printable / control chars.
            filename = "".join(c for c in filename if c.isprintable())
        if not filename:
            # Synthesize a name from content-type.
            ctype = part.get_content_type() or "application/octet-stream"
            ext = ctype.split("/")[-1].replace("+", "_")[:20]
            filename = f"untitled.{ext}"

        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            continue
        if len(payload) > max_size_bytes:
            _log.info(
                "email.attachment_oversized",
                filename=filename,
                size=len(payload),
                limit=max_size_bytes,
            )
            continue

        attachments.append({
            "filename": filename,
            "content_type": part.get_content_type() or "application/octet-stream",
            "size": len(payload),
            "payload": payload,
        })

    return attachments


def _save_email_attachments(
    attachments: list[dict[str, Any]],
    *,
    upload_dir: Path,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Persist extracted attachments to disk.

    Returns ``(image_paths, attachment_metas)`` where:
      * ``image_paths`` — absolute paths of image attachments that
        can be fed to ``AgentLoop.run_turn(user_images=...)``.
      * ``attachment_metas`` — lightweight dicts (no payload bytes)
        describing non-image attachments for the agent's reference.

    Files are written to ``upload_dir / email /`` with a collision-
    resistant name: ``{base}_{short_hash}{ext}``.
    """
    upload_dir = upload_dir / "email"
    upload_dir.mkdir(parents=True, exist_ok=True)

    image_paths: list[str] = []
    attachment_metas: list[dict[str, Any]] = []

    for att in attachments:
        filename = att["filename"]
        payload: bytes = att["payload"]
        content_type: str = att["content_type"]

        # Collision-resistant name.
        short_hash = hashlib.sha256(payload[:4096]).hexdigest()[:8]
        stem = Path(filename).stem
        ext = Path(filename).suffix
        safe_name = f"{stem}_{short_hash}{ext}"
        dest = upload_dir / safe_name

        # Deduplicate: if same content already exists, reuse path.
        if not dest.exists():
            try:
                dest.write_bytes(payload)
            except OSError as exc:
                _log.warning("email.attachment_save_failed", filename=filename, err=str(exc))
                continue

        abs_path = str(dest.resolve())
        if content_type.startswith("image/"):
            image_paths.append(abs_path)
        else:
            attachment_metas.append({
                "filename": filename,
                "content_type": content_type,
                "size": att["size"],
                "path": abs_path,
            })

    return image_paths, attachment_metas


def _decode_payload(part: email.message.Message) -> str:
    """Decode a single MIME part's payload to a str.

    ``get_payload(decode=True)`` returns bytes (with quoted-printable /
    base64 already undone). We then decode using the part's charset
    hint, falling back to utf-8 with replacement on failures.
    """
    raw = part.get_payload(decode=True)
    if raw is None:
        return ""
    if not isinstance(raw, bytes):
        return str(raw)
    charset = part.get_content_charset() or "utf-8"
    try:
        return raw.decode(charset, errors="replace")
    except (LookupError, UnicodeDecodeError):
        return raw.decode("utf-8", errors="replace")


def _coerce_str_set_lower(raw: Any, *, key: str) -> set[str]:
    """Validate + coerce a config-supplied address list to a lowercase set.

    Empty / missing → empty set (no restriction). Non-list raw raises
    so a typo like ``allowed_senders: "alice@example.com"`` doesn't
    read as a 19-char allowlist that always fails.
    """
    if raw is None:
        return set()
    if not isinstance(raw, list):
        raise ValueError(
            f"channels.email.{key} must be a list of email addresses, "
            f"got {type(raw).__name__}"
        )
    out: set[str] = set()
    for entry in raw:
        if not isinstance(entry, str):
            raise ValueError(
                f"channels.email.{key} entries must be str, got "
                f"{type(entry).__name__}"
            )
        s = entry.strip().lower()
        if s:
            out.add(s)
    return out


class EmailChannelAdapter(ChannelAdapter):
    """Email channel adapter (IMAP poll + SMTP send).

    Args:
        config: dict with at minimum ``imap_host``, ``imap_user``,
                ``smtp_host``, ``smtp_user``. ``imap_password`` /
                ``smtp_password`` may be empty in config and resolved
                from the secrets store under
                ``channels.email.imap_password`` /
                ``channels.email.smtp_password``. Optional:
                ``imap_port`` (default 993), ``smtp_port`` (default
                465 for SSL / 587 for STARTTLS), ``imap_folder``
                (default INBOX), ``imap_processed_folder`` (move
                processed messages here), ``poll_interval_s``
                (default 30), ``smtp_use_ssl`` (default true),
                ``from_address`` / ``from_name``,
                ``allowed_senders`` (B-337 parity),
                ``injection_policy`` (Epic #14).
    """

    name = "email"

    def __init__(self, config: dict[str, Any]) -> None:
        self._cfg = config or {}
        self._imap_host = (self._cfg.get("imap_host") or "").strip()
        self._imap_port = int(self._cfg.get("imap_port") or 993)
        self._imap_user = (self._cfg.get("imap_user") or "").strip()
        self._imap_folder = (self._cfg.get("imap_folder") or "INBOX").strip()
        self._imap_processed_folder = (
            self._cfg.get("imap_processed_folder") or ""
        ).strip()
        self._poll_interval_s = max(5, int(self._cfg.get("poll_interval_s") or 30))

        self._smtp_host = (self._cfg.get("smtp_host") or "").strip()
        # 465 = SMTPS (SSL on connect); 587 = STARTTLS (upgrade after EHLO).
        # Auto-pick based on use_ssl unless user overrides.
        self._smtp_use_ssl = bool(self._cfg.get("smtp_use_ssl", True))
        default_smtp_port = 465 if self._smtp_use_ssl else 587
        self._smtp_port = int(self._cfg.get("smtp_port") or default_smtp_port)
        self._smtp_user = (self._cfg.get("smtp_user") or "").strip()

        self._from_address = (
            self._cfg.get("from_address") or self._imap_user or self._smtp_user
        ).strip()
        self._from_name = (self._cfg.get("from_name") or "XMclaw").strip()

        if not self._imap_host:
            raise ValueError(
                "Email adapter needs config.channels.email.imap_host "
                "(e.g. 'imap.gmail.com')"
            )
        if not self._imap_user:
            raise ValueError(
                "Email adapter needs config.channels.email.imap_user"
            )
        if not self._smtp_host:
            raise ValueError(
                "Email adapter needs config.channels.email.smtp_host "
                "(e.g. 'smtp.gmail.com')"
            )
        if not self._smtp_user:
            raise ValueError(
                "Email adapter needs config.channels.email.smtp_user"
            )

        # Lazy: resolve passwords on start() so a daemon importing the
        # manifest doesn't touch the secrets store. Exposed as private
        # attrs so tests can pre-fill them.
        self._imap_password: str | None = self._cfg.get("imap_password") or None
        self._smtp_password: str | None = self._cfg.get("smtp_password") or None

        self._allowed_senders: set[str] = _coerce_str_set_lower(
            self._cfg.get("allowed_senders"), key="allowed_senders",
        )

        # Polling task + stop event built inside start() so a no-op
        # construct doesn't allocate asyncio resources.
        self._poll_task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None
        self._handlers: list[Callable[[InboundMessage], Awaitable[None]]] = []

        # Dedup LRU keyed by Message-ID. IMAP UNSEEN can re-deliver on
        # a reconnect when the server hadn't yet flushed the seen-flag
        # update; the LRU is cheaper than reconciling server state.
        self._seen_msg_ids: OrderedDict[str, float] = OrderedDict()
        self._seen_cap = 512

        # Surface field for setup-endpoint health (B-368 pattern).
        self.last_start_error: str | None = None

    # ── public API ──────────────────────────────────────────────

    def subscribe(
        self, handler: Callable[[InboundMessage], Awaitable[None]],
    ) -> None:
        self._handlers.append(handler)

    async def start(self) -> None:
        if self._poll_task is not None:
            return  # idempotent

        # Resolve passwords from the secrets store if not provided in
        # config — same fall-through pattern the LLM adapters use.
        # We import locally so adapter import doesn't pay the cost when
        # the channel is never enabled.
        if not self._imap_password:
            try:
                from xmclaw.utils.secrets import get_secret
                self._imap_password = get_secret("channels.email.imap_password")
            except Exception as exc:  # noqa: BLE001
                _log.debug("email.secrets_lookup_failed", err=str(exc))
        if not self._smtp_password:
            try:
                from xmclaw.utils.secrets import get_secret
                self._smtp_password = get_secret("channels.email.smtp_password")
            except Exception as exc:  # noqa: BLE001
                _log.debug("email.secrets_lookup_failed", err=str(exc))

        if not self._imap_password:
            self.last_start_error = (
                "Email adapter has no IMAP password — set "
                "channels.email.imap_password in config or store it as "
                "channels.email.imap_password in the secrets store."
            )
            raise RuntimeError(self.last_start_error)
        if not self._smtp_password:
            self.last_start_error = (
                "Email adapter has no SMTP password — set "
                "channels.email.smtp_password in config or store it as "
                "channels.email.smtp_password in the secrets store."
            )
            raise RuntimeError(self.last_start_error)

        # Validate the IMAP login by opening + closing a probe connection
        # synchronously inside a thread. This surfaces auth failure at
        # boot time the same way Telegram's getMe does, so the operator
        # gets the App Password hint immediately rather than after the
        # first poll cycle.
        try:
            await asyncio.to_thread(self._probe_imap_login)
        except Exception as exc:  # noqa: BLE001
            # last_start_error already set inside _probe.
            raise RuntimeError(self.last_start_error or str(exc)) from exc

        loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        self._poll_task = loop.create_task(
            self._poll_loop(), name="email-imap-poll",
        )
        self.last_start_error = None
        _log.info(
            "email.started",
            imap_host=self._imap_host,
            imap_user_prefix=self._imap_user.split("@", 1)[0][:4] + "***",
            smtp_host=self._smtp_host,
            poll_interval_s=self._poll_interval_s,
            allowlist_senders=len(self._allowed_senders),
        )

    async def stop(self) -> None:
        if self._poll_task is None:
            return
        if self._stop_event is not None:
            self._stop_event.set()
        task = self._poll_task
        self._poll_task = None
        try:
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
            _log.warning("email.poll_join_failed", err=str(exc))
        self._stop_event = None
        _log.info("email.stopped")

    async def send(
        self, target: ChannelTarget, payload: OutboundMessage,
    ) -> str:
        if target.channel != self.name:
            raise ValueError(
                f"EmailChannelAdapter cannot send to channel={target.channel!r}; "
                f"expected {self.name!r}"
            )
        if not target.ref:
            raise ValueError("email target.ref must be a non-empty recipient address")

        # Build EmailMessage off the event loop — purely CPU/IO-free.
        msg = email.message.EmailMessage()
        # Subject: the dispatcher passes the original subject (or a
        # fallback) via the InboundMessage.raw payload, but we don't
        # see it on the OutboundMessage shape. Use a sensible default
        # and prefix Re: when a reply_to msgid is set.
        subject_raw = ""
        # OutboundMessage doesn't carry a subject field; treat content's
        # first line up to 80 chars as a subject hint when not replying,
        # else "Re: <stored subject from raw>". We keep it dumb because
        # the dispatcher's contract is single-text-out, and threading
        # is what the user actually cares about.
        first_line = (payload.content.splitlines() or [""])[0].strip()
        subject_raw = first_line[:80] if first_line else "XMclaw reply"
        if payload.reply_to:
            # Strip any leading "Re: " the user may have already typed,
            # then add exactly one. RFC 5322 doesn't bound the count of
            # Re: prefixes but mail UIs collapse them at one anyway.
            cleaned = re.sub(r"^(re:\s*)+", "", subject_raw, flags=re.IGNORECASE)
            subject_raw = f"Re: {cleaned}".strip()

        msg["Subject"] = subject_raw
        if self._from_name:
            msg["From"] = email.utils.formataddr(
                (self._from_name, self._from_address),
            )
        else:
            msg["From"] = self._from_address
        msg["To"] = target.ref
        msg["Date"] = email.utils.formatdate(localtime=True)
        msg["Message-ID"] = email.utils.make_msgid(domain=_msgid_domain(self._from_address))
        if payload.reply_to:
            # ``reply_to`` carries the original Message-ID so the SMTP
            # send threads under the source message in Gmail / Outlook
            # / iOS Mail. Wrap it with angle brackets if the dispatcher
            # passed a bare id.
            msg_id = payload.reply_to.strip()
            if msg_id and not msg_id.startswith("<"):
                msg_id = f"<{msg_id}>"
            msg["In-Reply-To"] = msg_id
            msg["References"] = msg_id

        msg.set_content(payload.content or "")

        try:
            await asyncio.to_thread(self._smtp_send_message, msg)
        except smtplib.SMTPAuthenticationError as exc:
            err = str(exc)
            if any(marker in err for marker in _GMAIL_AUTH_ERROR_MARKERS):
                _log.warning(
                    "email.smtp_auth_app_password_required", err=err,
                )
                raise RuntimeError(_GMAIL_APP_PASSWORD_HINT) from exc
            _log.warning("email.smtp_auth_failed", err=err)
            raise RuntimeError(f"email send failed (auth): {err}") from exc
        except Exception as exc:  # noqa: BLE001
            _log.warning("email.send_failed", to=target.ref, err=str(exc))
            raise RuntimeError(
                f"email send failed: {type(exc).__name__}: {exc}"
            ) from exc

        # Return our own Message-ID as the canonical id — the SMTP
        # protocol does not echo back a server-assigned id.
        out_msg_id = msg.get("Message-ID") or f"email:{int(time.time())}"
        return str(out_msg_id)

    # ── internal: IMAP polling ──────────────────────────────────

    async def _poll_loop(self) -> None:
        """Background task: poll IMAP UNSEEN every ``poll_interval_s``.

        Exits cleanly when ``self._stop_event`` is set. Each poll
        iteration runs in a worker thread (imaplib is sync) and
        swallows its own errors so a transient network blip doesn't
        kill the loop.
        """
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            try:
                await asyncio.to_thread(self._poll_once_sync)
            except Exception as exc:  # noqa: BLE001
                _log.warning("email.poll_iteration_failed", err=str(exc))
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._poll_interval_s,
                )
                # If wait returned without TimeoutError the event is set
                # — exit promptly.
                if self._stop_event.is_set():
                    return
            except asyncio.TimeoutError:
                pass  # normal: tick due

    def _probe_imap_login(self) -> None:
        """Open + close an IMAP4_SSL probe to validate creds at boot.

        Mirrors Telegram's getMe gate. Bad password → catch the
        ``imaplib.IMAP4.error`` raised by ``login()``, sniff for
        Gmail / Outlook auth-error markers, and surface the App
        Password hint when that's the cause.
        """
        try:
            imap = imaplib.IMAP4_SSL(self._imap_host, self._imap_port)
        except Exception as exc:  # noqa: BLE001
            self.last_start_error = (
                f"IMAP connect failed: {type(exc).__name__}: {exc}. "
                f"Check the host/port (got {self._imap_host}:"
                f"{self._imap_port}) and network reachability."
            )
            raise RuntimeError(self.last_start_error) from exc

        try:
            imap.login(self._imap_user, self._imap_password or "")
        except imaplib.IMAP4.error as exc:
            err = str(exc)
            if any(marker in err for marker in _GMAIL_AUTH_ERROR_MARKERS):
                self.last_start_error = _GMAIL_APP_PASSWORD_HINT
                raise RuntimeError(_GMAIL_APP_PASSWORD_HINT) from exc
            self.last_start_error = (
                f"IMAP login failed: {err}. Verify imap_user "
                f"({self._imap_user}) + imap_password are correct."
            )
            raise RuntimeError(self.last_start_error) from exc
        except Exception as exc:  # noqa: BLE001
            self.last_start_error = (
                f"IMAP login error: {type(exc).__name__}: {exc}"
            )
            raise RuntimeError(self.last_start_error) from exc
        finally:
            try:
                imap.logout()
            except Exception:  # noqa: BLE001
                pass

    def _poll_once_sync(self) -> None:
        """One IMAP poll cycle — search UNSEEN, fetch, dispatch."""
        try:
            imap = imaplib.IMAP4_SSL(self._imap_host, self._imap_port)
        except Exception as exc:  # noqa: BLE001
            _log.warning("email.imap_connect_failed", err=str(exc))
            return

        try:
            try:
                imap.login(self._imap_user, self._imap_password or "")
            except imaplib.IMAP4.error as exc:
                _log.warning("email.imap_login_failed", err=str(exc))
                return

            try:
                imap.select(self._imap_folder)
            except imaplib.IMAP4.error as exc:
                _log.warning(
                    "email.imap_select_failed",
                    folder=self._imap_folder, err=str(exc),
                )
                return

            try:
                typ, data = imap.search(None, "UNSEEN")
            except imaplib.IMAP4.error as exc:
                _log.warning("email.imap_search_failed", err=str(exc))
                return

            if typ != "OK" or not data or not data[0]:
                return
            ids = data[0].split()
            for num in ids:
                self._handle_one_message_sync(imap, num)
        finally:
            try:
                imap.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                imap.logout()
            except Exception:  # noqa: BLE001
                pass

    def _handle_one_message_sync(
        self, imap: imaplib.IMAP4_SSL, num: bytes,
    ) -> None:
        """Fetch one IMAP message, parse, dispatch, mark/move."""
        # imaplib's typeshed stubs annotate the message-id arg as ``str``,
        # though the runtime accepts bytes. Decode at the boundary so
        # mypy is happy and the value is human-readable for log lines.
        num_str = num.decode("ascii", errors="replace") if isinstance(num, bytes) else str(num)
        try:
            typ, fetched = imap.fetch(num_str, "(RFC822)")
        except imaplib.IMAP4.error as exc:
            _log.warning("email.imap_fetch_failed", num=num_str, err=str(exc))
            return
        if typ != "OK" or not fetched:
            return

        # imaplib's fetch returns a list of tuples (header_info, body)
        # or strings; pick the first bytes payload.
        raw_bytes: bytes | None = None
        for chunk in fetched:
            if isinstance(chunk, tuple) and len(chunk) >= 2:
                payload = chunk[1]
                if isinstance(payload, (bytes, bytearray)):
                    raw_bytes = bytes(payload)
                    break
        if raw_bytes is None:
            return

        try:
            parsed = email.message_from_bytes(
                raw_bytes, policy=email.policy.default,
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning("email.parse_failed", err=str(exc))
            return

        # Dispatch synchronously into the running event loop. We are on
        # a worker thread (started via asyncio.to_thread) — schedule the
        # async dispatch back to the loop.
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = None  # pragma: no cover — to_thread is always loop-attached
        if loop is None:
            return

        future = asyncio.run_coroutine_threadsafe(
            self._dispatch_parsed_message(parsed), loop,
        )
        try:
            # Cap wait so a slow handler doesn't stall the IMAP cursor —
            # we still want to advance to the next UNSEEN id even if
            # the agent's run_turn takes its time.
            future.result(timeout=60)
        except Exception as exc:  # noqa: BLE001
            _log.warning("email.dispatch_failed", err=str(exc))

        # Mark as read (or move to processed folder) after dispatch.
        try:
            if self._imap_processed_folder:
                # Copy + delete = "move" in IMAP4 (no MOVE on every
                # server pre-RFC 6851; copy/delete is universal).
                imap.copy(num_str, self._imap_processed_folder)
                imap.store(num_str, "+FLAGS", r"\Deleted")
                imap.expunge()
            else:
                imap.store(num_str, "+FLAGS", r"\Seen")
        except imaplib.IMAP4.error as exc:
            _log.warning(
                "email.imap_mark_failed",
                num=num_str,
                target_folder=self._imap_processed_folder or "[INBOX/Seen]",
                err=str(exc),
            )

    async def _dispatch_parsed_message(
        self, parsed: email.message.Message,
    ) -> None:
        """Translate a parsed email.Message → InboundMessage, fan out."""
        from_raw = parsed.get("From", "") or ""
        sender_addr = _extract_email_address(from_raw)
        sender_display = _decode_header_value(from_raw)
        subject = _decode_header_value(parsed.get("Subject", ""))
        msg_id = (parsed.get("Message-ID") or "").strip()

        if not sender_addr:
            _log.debug("email.no_sender_address")
            return

        # Dedup. Empty Message-ID skips the cache (rare, but malformed
        # senders strip it; we'd rather process those than silently drop).
        if msg_id and msg_id in self._seen_msg_ids:
            _log.info("email.duplicate_skipped", msg_id=msg_id)
            return
        if msg_id:
            self._seen_msg_ids[msg_id] = time.time()
            while len(self._seen_msg_ids) > self._seen_cap:
                self._seen_msg_ids.popitem(last=False)

        # Allowlist gate.
        if (
            self._allowed_senders
            and sender_addr not in self._allowed_senders
        ):
            _log.warning(
                "email.inbound_dropped_unauthorized_sender",
                sender=sender_addr,
                allowlist_size=len(self._allowed_senders),
            )
            return

        body = _extract_plain_body(parsed)

        # B-393 Phase 2: extract + persist attachments. Images are
        # surfaced via ``user_images`` (AgentLoop vision support);
        # non-images are listed in the text so the agent knows they
        # exist and can reference them by path.
        attachments = _extract_attachments(parsed)
        image_paths: list[str] = []
        attachment_metas: list[dict[str, Any]] = []
        if attachments:
            try:
                from xmclaw.utils.paths import uploads_dir
                image_paths, attachment_metas = _save_email_attachments(
                    attachments,
                    upload_dir=uploads_dir(),
                )
            except Exception as exc:  # noqa: BLE001
                _log.warning("email.attachment_process_failed", err=str(exc))

        # Build an attachment summary block for non-image files.
        attachment_block = ""
        if attachment_metas:
            lines = ["\n[Attachments:]"]
            for m in attachment_metas:
                lines.append(
                    f"  - {m['filename']} ({m['content_type']}, "
                    f"{m['size']} bytes) — saved at {m['path']}"
                )
            attachment_block = "\n".join(lines)

        # Empty body + no attachments → nothing to process.
        if not body.strip() and not image_paths and not attachment_metas:
            _log.debug("email.empty_body_skipped", msg_id=msg_id)
            return

        # Compose the inbound payload prefixed with the subject so the
        # agent sees the user's framing (mail conversations often live
        # in the subject line). Falls back to body-only when subject is
        # generic / empty.
        if subject and subject.strip():
            content = f"Subject: {subject.strip()}\n\n{body}"
        else:
            content = body

        if attachment_block:
            content += attachment_block

        # Epic #14: scan inbound text for prompt injection BEFORE
        # handing off to run_turn. Email is the most spoofable channel
        # XMclaw has — anyone can send the daemon a message; without
        # the scan a hostile sender can stage an "ignore previous
        # instructions" attack. Default DETECT_ONLY so legit messages
        # aren't blocked; operators flip to BLOCK in config when they
        # run open chat (e.g. a public support inbox).
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
                content,
                policy=policy,
                source=SOURCE_CHANNEL,
                extra={
                    "channel": "email",
                    "sender": sender_addr,
                    "message_id": msg_id,
                    "subject": subject,
                },
            )
            if decision.blocked:
                _log.warning(
                    "email.inbound_blocked",
                    sender=sender_addr, msg_id=msg_id,
                    findings=[
                        f.pattern_id for f in decision.scan.findings
                    ][:5],
                )
                return  # drop message — don't fan out to agent
            content = decision.content
        except Exception as exc:  # noqa: BLE001
            _log.debug("email.scan_skipped", err=str(exc))

        inbound = InboundMessage(
            target=ChannelTarget(channel=self.name, ref=sender_addr),
            user_ref=sender_addr,
            content=content,
            raw={
                "message_id": msg_id,
                "from": from_raw,
                "from_address": sender_addr,
                "from_display": sender_display,
                "subject": subject,
                "images": image_paths,
                "attachments": attachment_metas,
            },
        )
        for h in list(self._handlers):
            try:
                await h(inbound)
            except Exception as exc:  # noqa: BLE001
                _log.warning("email.handler_failed", err=str(exc))

    # ── internal: SMTP send ─────────────────────────────────────

    def _smtp_send_message(self, msg: email.message.EmailMessage) -> None:
        """Open SMTP connection, login, send. Raises on failure.

        Runs inside ``asyncio.to_thread`` because ``smtplib`` is
        sync-only. Each call opens + closes a fresh connection — SMTP
        sends in our usage are rare enough (one per agent reply) that
        connection pooling isn't worth the complexity (and idle
        timeouts on Gmail / 163 / Outlook would invalidate a long-lived
        socket anyway).
        """
        if self._smtp_use_ssl:
            smtp_cls: Any = smtplib.SMTP_SSL
            client = smtp_cls(self._smtp_host, self._smtp_port, timeout=30)
        else:
            smtp_cls = smtplib.SMTP
            client = smtp_cls(self._smtp_host, self._smtp_port, timeout=30)
            client.ehlo()
            try:
                client.starttls()
                client.ehlo()
            except smtplib.SMTPException:
                # Some local relays don't speak STARTTLS — surface the
                # failure clearly.
                client.quit()
                raise
        try:
            client.login(self._smtp_user, self._smtp_password or "")
            client.send_message(msg)
        finally:
            try:
                client.quit()
            except Exception:  # noqa: BLE001
                pass


# ── helpers ────────────────────────────────────────────────────────


def _msgid_domain(from_address: str) -> str:
    """Domain segment for our outbound Message-ID.

    ``email.utils.make_msgid`` builds ``<random.timestamp@domain>``.
    Using the From: address's domain when possible keeps the id
    plausibly in the same namespace as the sender (helps with SPF /
    DMARC scoring on receiving servers); fall back to ``xmclaw.local``
    when the from is malformed.
    """
    if "@" in from_address:
        tail = from_address.rsplit("@", 1)[-1].strip()
        if tail:
            return tail
    return "xmclaw.local"
