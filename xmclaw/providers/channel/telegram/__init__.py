"""Telegram channel adapter — manifest + scaffolding.

Direct port target: ``qwenpaw/src/qwenpaw/app/channels/telegram/``
which uses ``python-telegram-bot``. Telegram supports both webhook +
long-poll modes; long-poll works without a public URL, so we default to
``needs_tunnel=False`` (user can flip to webhook mode in config).
"""
from xmclaw.providers.channel.base import PluginManifest

MANIFEST = PluginManifest(
    id="telegram",
    label="Telegram",
    adapter_factory_path="xmclaw.providers.channel.telegram.adapter:TelegramAdapter",
    requires=("python-telegram-bot>=21.0",),
    needs_tunnel=False,  # default to long-poll mode
    config_schema={
        "bot_token": "secret (required)",
        "mode": "string (poll | webhook, default poll)",
        "public_url": "string (only when mode=webhook)",
    },
)
