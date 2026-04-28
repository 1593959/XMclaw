"""WeCom (企业微信) channel adapter — manifest + scaffolding.

Direct port target: ``qwenpaw/src/qwenpaw/app/channels/wecom/``. WeCom
requires public webhook URL — the daemon auto-starts cloudflared (see
:mod:`xmclaw.utils.tunnel`) when this channel is enabled and no
explicit ``public_url`` is set.
"""
from xmclaw.providers.channel.base import PluginManifest

MANIFEST = PluginManifest(
    id="wecom",
    label="企业微信 / WeCom",
    adapter_factory_path="xmclaw.providers.channel.wecom.adapter:WeComAdapter",
    requires=("requests>=2.31",),  # WeCom uses plain HTTPS
    needs_tunnel=True,
    config_schema={
        "corp_id": "string (required)",
        "corp_secret": "secret (required)",
        "agent_id": "string (required)",
        "token": "secret (webhook signing)",
        "encoding_aes_key": "secret (webhook crypto)",
        "public_url": "string (optional, auto via cloudflared if absent)",
    },
    implementation_status="scaffold",  # B-38: adapter module not yet implemented
)
