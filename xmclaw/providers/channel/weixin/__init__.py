"""Personal WeChat (个人微信) channel adapter — manifest + scaffolding.

Direct port target: ``the upstream agent/src/the upstream agent/app/channels/weixin/``.
Personal WeChat is the trickiest of the four Chinese channels — there
is no official API for personal accounts, so the reference uses a 3rd-party
relay (typically wechatferry or wxauto). Concrete adapter lands once
the user picks a relay and provides credentials.
"""
from xmclaw.providers.channel.base import PluginManifest

MANIFEST = PluginManifest(
    id="weixin",
    label="个人微信 / WeChat",
    adapter_factory_path="xmclaw.providers.channel.weixin.adapter:WeChatAdapter",
    requires=("wxauto>=39.0.0",),  # placeholder — pick relay at integration time
    needs_tunnel=False,
    config_schema={
        "relay_kind": "string (wxauto | wechatferry)",
        "config": "dict (relay-specific config)",
    },
    implementation_status="scaffold",  # B-38: adapter module not yet implemented
)
