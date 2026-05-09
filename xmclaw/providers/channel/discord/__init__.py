"""Discord channel adapter — manifest.

B-381 (Sprint 2): real adapter at ``adapter:DiscordAdapter`` (sibling
to the B-380 Telegram graduation). Uses ``discord.py>=2``'s native
gateway WebSocket — like Telegram's long-poll mode, the SDK keeps the
inbound connection open from our side, so the daemon doesn't need a
public IP / cloudflared tunnel.
"""
from xmclaw.providers.channel.base import PluginManifest

MANIFEST = PluginManifest(
    id="discord",
    label="Discord",
    adapter_factory_path="xmclaw.providers.channel.discord.adapter:DiscordAdapter",
    requires=("discord.py>=2",),
    needs_tunnel=False,  # gateway WS — no inbound webhook required
    config_schema={
        "bot_token": "secret (required) — get from "
                     "https://discord.com/developers/applications "
                     "(Bot → Reset Token)",
        "allowed_user_ids": "list[int] (optional) — non-empty locks "
                            "inbound to listed Discord user (snowflake) ids",
        "allowed_channel_ids": "list[int] (optional) — non-empty locks "
                               "inbound to listed channel (snowflake) ids "
                               "(DMs vs guild channels split)",
        "injection_policy": "string (optional) — detect_only | redact "
                            "| block (default detect_only)",
    },
    implementation_status="ready",  # B-381: real adapter wired
)
