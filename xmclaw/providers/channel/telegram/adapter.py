"""Telegram channel adapter — scaffold (B-329).

Manifest in ``__init__.py`` declares this module + class, so importing
the package + reading manifest fields (``requires`` / ``needs_tunnel``
/ ``config_schema``) works. Instantiating the class raises a clear
NotImplementedError pointing at the port target.
"""
from __future__ import annotations

from xmclaw.providers.channel._scaffold import ScaffoldChannelAdapter


class TelegramAdapter(ScaffoldChannelAdapter):
    CHANNEL_NAME = "Telegram"
    PORT_TARGET = "qwenpaw/src/qwenpaw/app/channels/telegram/"
    EXTRA_NOTE = (
        "python-telegram-bot supports both webhook + long-poll. "
        "Default to long-poll (no public URL needed)."
    )
