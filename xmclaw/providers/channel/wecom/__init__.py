"""WeCom (企业微信 / Wechat Work) channel adapter — manifest.

B-384 (Sprint 2): real adapter at ``adapter:WeComAdapter`` (sibling to
B-380 / B-381 / B-382 Telegram / Discord / Slack). WeCom has two API
surfaces:

1. **Internal-bot webhook** — `https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=<key>`
   POST JSON. **One-way**: the daemon sends, no inbound delivery.
   Useful for notification-style use (release alerts, daily digests).
2. **Self-built app callback** — receive encrypted XML over HTTP
   callback. Requires a public URL (or cloudflared tunnel) + AES-CBC
   decrypt + WeCom token signature verification.

Since XMclaw is local-first, **v1 ships outbound-only**. The webhook
surface covers the realistic deployment shape for the Chinese-market
notification use case. Inbound (self-built app callback) is documented
out-of-scope until the daemon's tunnel/cloudflared bootstrap is wired
generically (qwenpaw `tunnel/cloudflare.py` parity work). The adapter
class accepts inbound `subscribe()` calls to keep the ABC contract; it
just never fans anything out.

`implementation_status="ready"` — outbound IS fully functional. The
docstring + send-only manifest config make the inbound limitation
unambiguous.
"""
from xmclaw.providers.channel.base import PluginManifest

MANIFEST = PluginManifest(
    id="wecom",
    label="企业微信 / WeCom",
    adapter_factory_path="xmclaw.providers.channel.wecom.adapter:WeComAdapter",
    # No extra SDK — pure REST POST via httpx (already a runtime dep).
    requires=(),
    # Internal-bot webhook is outbound-only; the daemon initiates the
    # POST. No callback URL needed. (Inbound for self-built apps would
    # set this to True once the cloudflared bootstrap lands.)
    needs_tunnel=False,
    config_schema={
        "webhook_url": "secret (required) — `https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...` "
                       "from 群设置 → 群机器人 → 添加 → 复制 webhook URL",
        "msgtype": "string (optional) — text | markdown | image | news | file "
                   "(default markdown). Webhook bot supports those five only.",
        "mentioned_list": "list[str] (optional) — userid list to @mention; "
                          "use ['@all'] to mention everyone (text msgtype only).",
        "mentioned_mobile_list": "list[str] (optional) — mobile-number list to "
                                 "@mention (text msgtype only).",
    },
    implementation_status="ready",  # B-384: outbound webhook works real
)
