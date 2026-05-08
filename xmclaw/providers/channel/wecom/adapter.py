"""WeCom (企业微信) channel adapter — scaffold (B-329).

Manifest lives in ``__init__.py``. This file exists so
``adapter_factory_path`` resolves; instantiation raises with the
port target.
"""
from __future__ import annotations

from xmclaw.providers.channel._scaffold import ScaffoldChannelAdapter


class WeComAdapter(ScaffoldChannelAdapter):
    CHANNEL_NAME = "WeCom (企业微信)"
    PORT_TARGET = "qwenpaw/src/qwenpaw/app/channels/wecom/"
    EXTRA_NOTE = (
        "Requires a public webhook URL. The daemon auto-starts "
        "cloudflared (xmclaw.utils.tunnel) when this channel is "
        "enabled and no explicit public_url is set."
    )
