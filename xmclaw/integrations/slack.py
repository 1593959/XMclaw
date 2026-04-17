"""Slack integration via Slack Bolt (Socket Mode)."""
from __future__ import annotations
from xmclaw.utils.log import logger
from .base import Integration

try:
    from slack_bolt.async_app import AsyncApp
    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
    _SLACK_AVAILABLE = True
except ImportError:
    _SLACK_AVAILABLE = False


class SlackIntegration(Integration):
    """Slack Bot integration using Bolt + Socket Mode (no public URL needed)."""

    name = "slack"

    def __init__(self, config: dict):
        super().__init__(config)
        self.bot_token: str = config.get("bot_token", "")
        self.app_token: str = config.get("app_token", "")   # xapp-... token
        self.channel: str = config.get("channel", "")       # default channel to send to
        self._app = None
        self._handler = None

    async def connect(self) -> None:
        if not _SLACK_AVAILABLE:
            logger.error("slack_sdk_missing", hint="pip install slack-bolt")
            return
        if not self.bot_token or not self.app_token:
            logger.error("slack_tokens_missing")
            return
        self._app = AsyncApp(token=self.bot_token)

        @self._app.message("")
        async def on_message(message, say):
            text = message.get("text", "")
            user = message.get("user", "unknown")
            channel = message.get("channel", "")
            await self._dispatch(f"slack:{user}", text, {"channel": channel, "platform": "slack"})

        self._handler = AsyncSocketModeHandler(self._app, self.app_token)
        await self._handler.start_async()
        self._running = True
        logger.info("slack_connected")

    async def disconnect(self) -> None:
        if self._handler:
            await self._handler.close_async()
        self._running = False
        logger.info("slack_disconnected")

    async def send(self, text: str, target: str | None = None) -> None:
        if not self._app:
            return
        channel = target or self.channel
        if not channel:
            logger.warning("slack_no_channel")
            return
        try:
            await self._app.client.chat_postMessage(channel=channel, text=text)
        except Exception as e:
            logger.error("slack_send_failed", error=str(e))
