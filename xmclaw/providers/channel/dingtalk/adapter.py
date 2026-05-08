"""DingTalk channel adapter — scaffold (B-329).

Manifest lives in ``__init__.py``. This file exists so
``adapter_factory_path`` resolves; instantiation raises with the
port target.
"""
from __future__ import annotations

from xmclaw.providers.channel._scaffold import ScaffoldChannelAdapter


class DingTalkAdapter(ScaffoldChannelAdapter):
    CHANNEL_NAME = "DingTalk (钉钉)"
    PORT_TARGET = "qwenpaw/src/qwenpaw/app/channels/dingtalk/"
    EXTRA_NOTE = (
        "Uses dingtalk_stream (long-poll WebSocket, no public IP). "
        "AI-card payload format per dingtalk/channel.py:5-12."
    )
