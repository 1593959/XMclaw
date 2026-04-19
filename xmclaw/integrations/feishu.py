"""飞书 (Feishu/Lark) integration via official Open Platform WebSocket API.

Requires: pip install lark-oapi

Setup:
1. Go to https://open.feishu.cn/app → create an app
2. Enable "Bot" capability + "Message" events
3. Subscribe to events: im.message.receive_v1
4. Set permissions: im:message, im:message.group_at_msg, bot:sub
5. Get app_id, app_secret from Basic Info page
6. Fill in daemon/config.json

Config:
{
  "feishu": {
    "enabled": true,
    "app_id": "cli_xxx",
    "app_secret": "xxx",
    "bot_name": "XMclaw",        // optional, bot mention name
    "default_chat_id": ""        // default chat to send to
  }
}
"""
from __future__ import annotations
import asyncio
import json

from xmclaw.utils.log import logger
from .base import Integration

try:
    from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBuilder
    from lark_oapi.adapter.websocket import WebSocketClient
    from lark_oapi.event import EventDispatcher
    from lark_oapi.event.callback import Callback, EventHandler
    from lark_oapi.api.im.v1.model import P2ImMessageReceiveV1
    _FEISHU_AVAILABLE = True
except ImportError:
    _FEISHU_AVAILABLE = False


class FeishuIntegration(Integration):
    """Feishu (Lark) Bot integration via WebSocket long connection.

    Connects to Feishu's Open Platform using the official WebSocket client.
    Handles both direct messages and group @-mentions.
    """

    name = "feishu"

    def __init__(self, config: dict):
        super().__init__(config)
        self.app_id: str = config.get("app_id", "")
        self.app_secret: str = config.get("app_secret", "")
        self.bot_name: str = config.get("bot_name", "XMclaw")
        self.default_chat_id: str = config.get("default_chat_id", "")
        self._client: WebSocketClient | None = None
        self._dispatcher: EventDispatcher | None = None
        self._running = False

    async def connect(self) -> None:
        if not _FEISHU_AVAILABLE:
            logger.error(
                "feishu_lark_missing",
                hint="pip install lark-oapi"
            )
            return

        if not self.app_id or not self.app_secret:
            logger.error("feishu_credentials_missing",
                         app_id=bool(self.app_id), app_secret=bool(self.app_secret))
            return

        try:
            # Build event handler
            handler = FeishuEventHandler(self)

            # Build dispatcher — catches all subscribed events
            self._dispatcher = (
                EventDispatcher.builder(9003, handler)
                .build()
            )

            # Build and start WebSocket client
            self._client = (
                WebSocketClient.builder()
                .app_id(self.app_id)
                .app_secret(self.app_secret)
                .event_dispatcher(self._dispatcher)
                .build()
            )
            self._client.start()
            self._running = True
            logger.info("feishu_connected", app_id=self.app_id[:10])

        except Exception as e:
            logger.error("feishu_connect_failed", error=str(e))

    async def disconnect(self) -> None:
        if self._client:
            try:
                self._client.stop()
            except Exception:
                pass
        self._running = False
        logger.info("feishu_disconnected")

    async def send(self, text: str, target: str | None = None) -> None:
        """Send a message to a Feishu chat.

        Args:
            text: Message text (supports Feishu Markdown)
            target: chat_id to send to (falls back to default_chat_id)
        """
        if not self._running:
            logger.warning("feishu_send_before_connect")
            return

        chat_id = target or self.default_chat_id
        if not chat_id:
            logger.warning("feishu_no_target_chat")
            return

        if not _FEISHU_AVAILABLE:
            return

        try:
            from lark_oapi.api.im.v1 import CreateMessageRequest
            from lark_oapi.api.im.v1.model import CreateMessageRequest as CMR
            from lark_oapi.api.im.v1.model import CreateMessageRequestBuilder

            # Build request
            request = (
                CreateMessageRequestBuilder()
                .receive_id_type("chat_id")
                .create_message_request(
                    CMR.builder()
                    .receive_id(chat_id)
                    .msg_type("text")
                    .content(json.dumps({"text": text[:4000]}))  # Feishu limit
                    .build()
                )
                .build()
            )

            # Use the SDK's api handler
            from lark_oapi.api.im.v1 import ImApi
            from lark_oapi import Config
            conf = Config.init_anonymous(self.app_id, self.app_secret)
            client = ImApi(client=conf)
            resp = await client.im.v1.message.create.async_(
                CreateMessageRequestBuilder()
                .receive_id_type("chat_id")
                .create_message_request(
                    CMR.builder()
                    .receive_id(chat_id)
                    .msg_type("text")
                    .content(json.dumps({"text": text[:4000]}))
                    .build()
                )
                .build()
            )
            if not resp.success():
                logger.error("feishu_send_failed",
                             code=resp.code, msg=resp.msg)
        except Exception as e:
            logger.error("feishu_send_error", error=str(e))


class FeishuEventHandler:
    """Handles incoming Feishu events dispatched from the WebSocket client."""

    def __init__(self, integration: FeishuIntegration):
        self.integration = integration

    def handle(self, event_data: dict) -> None:
        """Called synchronously by the Feishu SDK event dispatcher."""
        event_type = event_data.get("header", {}).get("event_type", "")

        if event_type == "im.message.receive_v1":
            try:
                # Schedule async handling
                asyncio.create_task(self._handle_message(event_data))
            except RuntimeError:
                # No running event loop — log and skip
                logger.warning("feishu_event_no_loop", event_type=event_type)
        else:
            logger.debug("feishu_unhandled_event", event_type=event_type)

    async def _handle_message(self, event_data: dict) -> None:
        """Process an incoming message event."""
        try:
            event = event_data.get("event", {})
            sender = event.get("sender", {})
            chat = event.get("chat_id", "")

            # Skip bot's own messages
            if sender.get("sender_type") == "bot":
                return

            # Get message content
            message = event.get("message", {})
            msg_type = message.get("msg_type", "")
            content_raw = message.get("content", "{}")
            try:
                content = json.loads(content_raw)
            except Exception:
                content = {}

            # Extract text
            if msg_type == "text":
                text = content.get("text", "").strip()
            elif msg_type == "post":
                # Rich text post — extract plain text
                text = self._extract_post_text(content)
            else:
                text = "[非文本消息]"

            if not text:
                return

            source_id = f"feishu:{sender.get('id', 'unknown')}"
            metadata = {
                "chat_id": chat,
                "user_id": sender.get("id", ""),
                "sender_type": sender.get("sender_type", ""),
                "msg_type": msg_type,
                "message_id": message.get("message_id", ""),
                "platform": "feishu",
            }

            await self.integration._dispatch(source_id, text, metadata)

        except Exception as e:
            logger.error("feishu_message_handle_error", error=str(e))

    def _extract_post_text(self, content: dict) -> str:
        """Extract plain text from a Feishu post (rich text) message."""
        parts = []
        try:
            for section in content.get("post", {}).get("zh_cn", {}).get("content", []):
                for item in section:
                    if isinstance(item, dict):
                        if item.get("tag") == "text":
                            parts.append(item.get("text", ""))
                        elif item.get("tag") == "at":
                            parts.append(f"@{item.get('user_name', 'user')} ")
        except Exception:
            pass
        return " ".join(parts).strip()
