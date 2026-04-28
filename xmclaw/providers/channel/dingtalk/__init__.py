"""DingTalk channel adapter — manifest + scaffolding.

Direct port target: ``qwenpaw/src/qwenpaw/app/channels/dingtalk/``
which uses ``dingtalk_stream`` (long-poll WebSocket, no public IP) and
the AI-card payload format ("single reply unless ``sessionWebhook``
present", ``app/channels/dingtalk/channel.py:5-12``).

Concrete adapter lands when the user provides ``client_id`` /
``client_secret``.
"""
from xmclaw.providers.channel.base import PluginManifest

MANIFEST = PluginManifest(
    id="dingtalk",
    label="钉钉 / DingTalk",
    adapter_factory_path="xmclaw.providers.channel.dingtalk.adapter:DingTalkAdapter",
    requires=("dingtalk-stream>=0.20.0",),
    needs_tunnel=False,
    config_schema={
        "client_id": "string (required)",
        "client_secret": "secret (required)",
        "robot_code": "string (required for AI-card replies)",
    },
    implementation_status="scaffold",  # B-38: adapter module not yet implemented
)
