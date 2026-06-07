"""DingTalk channel adapter — manifest.

B-383 (Sprint 2): real adapter at ``adapter:DingTalkAdapter`` (was
B-329 scaffold). Direct port reference: ``the upstream agent/src/the upstream agent/app/
channels/dingtalk/`` + the open-dingtalk Python SDK
(`dingtalk-stream` on PyPI). Uses the SDK's WebSocket Stream Mode —
DingTalk pushes events to us over an outbound-from-our-side WS, so
the daemon doesn't need a public IP / cloudflared tunnel.
"""
from xmclaw.providers.channel.base import PluginManifest

MANIFEST = PluginManifest(
    id="dingtalk",
    label="钉钉 / DingTalk",
    adapter_factory_path="xmclaw.providers.channel.dingtalk.adapter:DingTalkAdapter",
    requires=("dingtalk-stream>=0.20",),
    needs_tunnel=False,  # Stream Mode — DingTalk pushes events over WS
    config_schema={
        "client_id": "string (required) — DingTalk app key from "
                     "open.dingtalk.com → 应用开发 → 凭证与基础信息",
        "client_secret": "secret (required) — paired with client_id",
        "robot_code": "string (optional) — robot code; defaults to "
                      "client_id when omitted (single-app builds)",
        "allowed_user_ids": "list[str] (optional) — non-empty locks "
                            "inbound to listed sender staff_ids. "
                            "Empty = no restriction.",
        "allowed_conversation_ids": "list[str] (optional) — non-empty "
                                    "locks inbound to listed conversation "
                                    "ids (group chats vs DMs split). "
                                    "Empty = no restriction.",
        "injection_policy": "string (optional) — detect_only | redact "
                            "| block (default detect_only)",
    },
    implementation_status="ready",  # B-383: real adapter wired
)
