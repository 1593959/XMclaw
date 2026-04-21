"""ChannelAdapter interface.

Anti-req #7: every concrete channel MUST pass
``tests/conformance/channel_test_suite.py`` before it can ship.
"""
from xmclaw.providers.channel.base import (
    ChannelAdapter,
    ChannelTarget,
    InboundMessage,
    OutboundMessage,
)

__all__ = [
    "ChannelAdapter",
    "ChannelTarget",
    "InboundMessage",
    "OutboundMessage",
]
