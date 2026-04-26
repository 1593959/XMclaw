"""Feishu (Lark) channel adapter — manifest + scaffolding.

Direct port target: ``qwenpaw/src/qwenpaw/app/channels/feishu/`` which
uses lark-oapi's WebSocket long-poll (no public IP needed). The
WebSocket-inbound model is why Feishu is the easiest of the four
Chinese channels to bring up — no cloudflared, no webhook signing, just
``app_id`` + ``app_secret`` and the lark-oapi SDK does the heavy
lifting.

The full adapter implementation lands once the user wires
``app_id`` / ``app_secret`` into ``~/.xmclaw/config.json``. This module
is the registry-discovery surface; the concrete ``FeishuAdapter`` class
imports lark-oapi lazily so daemons that don't enable Feishu don't
need the dep installed.
"""
from xmclaw.providers.channel.base import PluginManifest

MANIFEST = PluginManifest(
    id="feishu",
    label="飞书 / Lark",
    adapter_factory_path="xmclaw.providers.channel.feishu.adapter:FeishuAdapter",
    requires=("lark-oapi>=1.4.0",),
    needs_tunnel=False,  # lark-oapi WS long-poll, no public IP needed
    config_schema={
        "app_id": "string (required)",
        "app_secret": "secret (required)",
        "verification_token": "secret (optional, only for webhook fallback)",
    },
)
