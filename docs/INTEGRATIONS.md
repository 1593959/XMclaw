---
title: "Integrations"
summary: "External platform integrations: Slack, Discord, Telegram, GitHub, Notion, 飞书, QQ频道, 企业微信"
---

# Integrations

XMclaw can connect to external platforms to send and receive messages. Each integration registers as a bot/agent that responds to messages on its platform.

## Architecture

All integrations follow the same pattern:

1. **IntegrationManager** (in `xmclaw/integrations/manager.py`) starts all enabled integrations at daemon startup
2. Each integration receives messages via its platform's API (WebSocket or webhook)
3. Messages are forwarded to the AgentOrchestrator with context metadata
4. Agent responses are sent back via the integration's send API

```
Platform API (WebSocket / Webhook)
         ↓
Integration (feishu.py, qq.py, ...)
         ↓
IntegrationManager._make_handler()
         ↓
AgentOrchestrator.get_or_create_agent()
         ↓
Agent.run() → response
         ↓
Integration.send()
         ↓
Platform API
```

---

## Slack

**Setup**: See [Slack Bolt Socket Mode docs](https://tools.slack.com/slack-apps-toolkit/slack-bolt-python).

**Required**: `pip install slack-bolt`

**Config** (`daemon/config.json`):
```json
{
  "integrations": {
    "slack": {
      "enabled": true,
      "bot_token": "xoxb-...",
      "app_token": "xapp-...",
      "channel": "#general"
    }
  }
}
```

- **Socket Mode**: No public URL needed; Slack connects to your app.
- Subscribe to `message.im`, `message.channels`, `message.groups` events in Slack app settings.

---

## Discord

**Required**: `pip install discord.py`

**Config**:
```json
{
  "integrations": {
    "discord": {
      "enabled": true,
      "bot_token": "Bot xxx",
      "channel_id": "123456789"
    }
  }
}
```

- Enable **Message Content Intent** in Discord Developer Portal → Bot → Privileged Gateway Intents.

---

## Telegram

**Required**: `pip install aiohttp`

**Config**:
```json
{
  "integrations": {
    "telegram": {
      "enabled": true,
      "bot_token": "123456:ABC-DEF...",
      "chat_id": "123456789"
    }
  }
}
```

- Talk to [@BotFather](https://t.me/botfather) to create a bot and get the token.
- Set webhook: `https://your-domain.com/api/telegram/webhook` or use long polling.

---

## GitHub

**Config**:
```json
{
  "integrations": {
    "github": {
      "enabled": true,
      "token": "ghp_xxx",
      "repo": "owner/repo",
      "poll_interval": 60
    }
  }
}
```

- Requires a GitHub Personal Access Token with `repo` scope.
- Supports Issue/PR comment monitoring and issue creation.

---

## Notion

**Required**: `pip install notion-client`

**Config**:
```json
{
  "integrations": {
    "notion": {
      "enabled": true,
      "api_key": "secret_xxx",
      "database_id": "xxx"
    }
  }
}
```

- Create a Notion integration at [notion.so/my-integrations](https://www.notion.so/my-integrations).
- Share the database with the integration.

---

## 飞书 (Feishu / Lark)

**Required**: `pip install lark-oapi`

**WebSocket Mode** (recommended — no public URL needed):
```json
{
  "integrations": {
    "feishu": {
      "enabled": true,
      "app_id": "cli_xxx",
      "app_secret": "xxx",
      "bot_name": "XMclaw",
      "default_chat_id": ""
    }
  }
}
```

**Setup steps**:
1. Go to [open.feishu.cn/app](https://open.feishu.cn/app) → Create App
2. Enable **Bot** capability + **Message** events
3. Subscribe to events: `im.message.receive_v1`
4. Add permissions: `im:message`, `im:message.group_at_msg`, `bot:sub`
5. Get **App ID** and **App Secret** from Basic Info page

**Supported message types**: `text`, `post` (rich text)

---

## QQ频道 (QQ Guild)

**Two connection modes**:

### WebSocket Mode (recommended)
```json
{
  "integrations": {
    "qq": {
      "enabled": true,
      "mode": "websocket",
      "app_id": "123456789",
      "app_token": "xxx",
      "secret": "xxx",
      "channel_id": ""
    }
  }
}
```

**Setup**:
1. Visit [q.qq.com](https://q.qq.com) → create a bot application
2. Get **AppID** and **AppToken** from bot settings
3. Subscribe to message events in the developer console

### Webhook Mode
```json
{
  "integrations": {
    "qq": {
      "enabled": true,
      "mode": "webhook",
      "webhook_url": "https://your-domain.com/api/integrations/qq/webhook",
      "app_id": "xxx"
    }
  }
}
```

- Configure the webhook URL in QQ Open Platform → Event Configuration
- Daemon must be publicly accessible (use cloudflare tunnel, ngrok, etc.)

**Features**: Group messages, @-mention detection (CQ码 format), auto-reconnect

---

## 企业微信 (WeChat Work)

**Two modes**:

### Group Bot Mode (simplest — outbound only)
```json
{
  "integrations": {
    "wechat": {
      "enabled": true,
      "mode": "group_bot",
      "webhook_url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx"
    }
  }
}
```

**Setup**:
1. Open a WeChat Work group → Add Bot → Custom Bot
2. Copy the webhook URL
3. Done! Messages are sent directly to the group via webhook.

> Note: Group bot is send-only. For bidirectional messaging, use Application mode.

### Application Mode (full bidirectional)
```json
{
  "integrations": {
    "wechat": {
      "enabled": true,
      "mode": "application",
      "corp_id": "wwxxx",
      "agent_id": "1000001",
      "app_secret": "xxx",
      "callback_token": "xxx",
      "callback_aes_key": "xxx"
    }
  }
}
```

**Setup**:
1. Create an app at [work.weixin.qq.com](https://work.weixin.qq.com) → App Management
2. Get **CorpID**, **AgentID**, **AppSecret** from app settings
3. Set the callback URL: `https://your-domain.com/api/integrations/wechat/webhook`
4. For local dev: use a tunnel (cloudflare tunnel, ngrok) to expose the callback URL
5. Enable "API Receive" permission in the app

**Features**: Automatic access token management, AES-256 encrypted callback, event-based message handling

---

## Webhook API Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/api/integrations/feishu/webhook` | POST | Feishu event callback |
| `/api/integrations/qq/webhook` | POST | QQ Guild webhook |
| `/api/integrations/wechat/webhook` | GET | WeChat Work URL verification |
| `/api/integrations/wechat/webhook` | POST | WeChat Work event callback |
| `/api/integrations/status` | GET | All integration status |

---

## Adding a New Integration

1. Create `xmclaw/integrations/your_platform.py` extending `Integration`
2. Implement: `connect()`, `disconnect()`, `send()`
3. Register in `manager.py`: add to `_REGISTRY` dict
4. Add config template in `daemon/config.py`
5. Add webhook routes in `daemon/server.py` if needed
6. Add tests
