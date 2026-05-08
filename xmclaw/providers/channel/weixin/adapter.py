"""Personal WeChat (个人微信) channel adapter — scaffold (B-329).

Manifest lives in ``__init__.py``. This file exists so
``adapter_factory_path`` resolves; instantiation raises with the
port target.
"""
from __future__ import annotations

from xmclaw.providers.channel._scaffold import ScaffoldChannelAdapter


class WeChatAdapter(ScaffoldChannelAdapter):
    CHANNEL_NAME = "Personal WeChat (个人微信)"
    PORT_TARGET = "qwenpaw/src/qwenpaw/app/channels/weixin/"
    EXTRA_NOTE = (
        "No official API for personal accounts. QwenPaw uses a "
        "3rd-party relay (wxauto / wechatferry); concrete adapter "
        "lands once the user picks a relay + provides credentials."
    )
