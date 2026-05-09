"""Telegram channel adapter — manifest.

B-380 (Sprint 2): real adapter at ``adapter:TelegramAdapter`` (was
B-329 scaffold). Direct port reference: ``qwenpaw/src/qwenpaw/app/
channels/telegram/`` + Hermes Agent's telegram channel. Uses
``python-telegram-bot``'s long-poll mode (no public URL needed).
"""
from xmclaw.providers.channel.base import PluginManifest

MANIFEST = PluginManifest(
    id="telegram",
    label="Telegram",
    adapter_factory_path="xmclaw.providers.channel.telegram.adapter:TelegramAdapter",
    requires=("python-telegram-bot>=21.0",),
    needs_tunnel=False,  # long-poll mode — no inbound webhook required
    config_schema={
        "bot_token": "secret (required) — get from @BotFather",
        "allowed_user_ids": "list[int] (optional) — non-empty locks "
                            "inbound to listed Telegram user ids",
        "allowed_chat_ids": "list[int] (optional) — non-empty locks "
                            "inbound to listed chat ids (groups vs DMs)",
        "injection_policy": "string (optional) — detect_only | redact "
                            "| block (default detect_only)",
        "parse_mode": "string (optional) — None | Markdown | "
                      "MarkdownV2 | HTML (default None = plain text)",
    },
    implementation_status="ready",  # B-380: real adapter wired
)
