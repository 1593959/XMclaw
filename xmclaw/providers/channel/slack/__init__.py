"""Slack channel adapter — manifest.

B-382 (Sprint 2): real adapter at ``adapter:SlackAdapter``. Uses
slack-bolt's Socket Mode (``AsyncSocketModeHandler``) so the daemon
doesn't need a public webhook — Slack's app-level token opens the
WS from our side.

Direct port reference:
  * OpenClaw ``extensions/slack/`` (Slack Bolt JS, same Socket Mode
    posture)
  * Hermes Agent ``hermes/integrations/slack/`` (Bolt Python, the
    library this adapter uses)
"""
from xmclaw.providers.channel.base import PluginManifest

MANIFEST = PluginManifest(
    id="slack",
    label="Slack",
    adapter_factory_path="xmclaw.providers.channel.slack.adapter:SlackAdapter",
    requires=("slack-bolt>=1.18", "slack-sdk>=3"),
    needs_tunnel=False,  # Socket Mode — Slack pushes events over WS
    config_schema={
        "bot_token": "secret (required) — Slack 'xoxb-...' bot token "
                     "from https://api.slack.com/apps → OAuth & Permissions",
        "app_token": "secret (required) — 'xapp-...' app-level token "
                     "with connections:write scope (Socket Mode key)",
        "allowed_user_ids": "list[str] (optional) — non-empty locks "
                            "inbound to listed Slack user ids "
                            "(Uxxx / Wxxx). Empty = no restriction.",
        "allowed_channel_ids": "list[str] (optional) — non-empty locks "
                               "inbound to listed channel ids "
                               "(Cxxx public, Gxxx private, Dxxx DM). "
                               "Empty = no restriction.",
        "dispatch_session_id_prefix": "string (optional) — informational; "
                                      "the dispatcher composes session_id "
                                      "as 'slack:<channel_id>' from the "
                                      "adapter name + ChannelTarget.ref.",
        "injection_policy": "string (optional) — detect_only | redact "
                            "| block (default detect_only)",
    },
    implementation_status="ready",  # B-382: real adapter wired
)
