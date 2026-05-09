"""Email channel adapter — manifest.

B-393 (Sprint 2): IMAP poll for inbound + SMTP for outbound. Uses
stdlib ``imaplib`` / ``smtplib`` / ``email`` so no third-party SDK is
pulled in for the base install — every dep is already present in
CPython. Polling (not IDLE) because most consumer mail providers
either rate-limit or outright disallow IMAP IDLE.

Direct port reference:
  * QwenPaw ``qwenpaw/app/channels/email/`` (sync imaplib poll loop)
  * Hermes Agent ``hermes/integrations/email/`` (smtplib outbound)
"""
from xmclaw.providers.channel.base import PluginManifest

MANIFEST = PluginManifest(
    id="email",
    label="Email (IMAP poll + SMTP)",
    adapter_factory_path="xmclaw.providers.channel.email.adapter:EmailChannelAdapter",
    requires=(),  # stdlib only — no extra dep needed for base install
    needs_tunnel=False,  # IMAP poll is outbound from the daemon
    config_schema={
        "imap_host": "string (required) — e.g. 'imap.gmail.com'",
        "imap_port": "int (optional, default 993) — IMAP4 SSL port",
        "imap_user": "string (required) — full mailbox address",
        "imap_password": "secret (required) — App Password for "
                         "Gmail/Outlook (NOT the account password); "
                         "see https://support.google.com/accounts/"
                         "answer/185833. Falls through to "
                         "channels.email.imap_password in the secrets "
                         "store when blank.",
        "imap_folder": "string (optional, default INBOX) — folder to "
                       "poll for new messages",
        "imap_processed_folder": "string (optional) — when set, "
                                 "successfully-dispatched messages "
                                 "are moved to this folder instead "
                                 "of just being marked as read",
        "poll_interval_s": "int (optional, default 30) — seconds "
                           "between IMAP UNSEEN polls",
        "smtp_host": "string (required) — e.g. 'smtp.gmail.com'",
        "smtp_port": "int (optional, default 465 for SSL / 587 for "
                     "STARTTLS)",
        "smtp_user": "string (required) — SMTP auth username (often "
                     "same as imap_user)",
        "smtp_password": "secret (required) — App Password; falls "
                         "through to channels.email.smtp_password in "
                         "the secrets store when blank.",
        "smtp_use_ssl": "bool (optional, default true) — true uses "
                        "SMTP_SSL on port 465, false uses STARTTLS "
                        "on port 587",
        "from_address": "string (optional, default = imap_user) — "
                        "the From: header on outbound mail",
        "from_name": "string (optional, default 'XMclaw') — display "
                     "name on the From: header",
        "allowed_senders": "list[str] (optional) — non-empty locks "
                           "inbound to listed sender addresses; "
                           "comparison is lowercase. Empty = "
                           "no restriction.",
        "injection_policy": "string (optional) — detect_only | redact "
                            "| block (default detect_only)",
    },
    implementation_status="ready",  # B-393: real adapter wired
)
