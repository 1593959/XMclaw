"""Discord integration."""
from __future__ import annotations
from xmclaw.utils.log import logger
from .base import Integration

try:
    import discord
    _DISCORD_AVAILABLE = True
except ImportError:
    _DISCORD_AVAILABLE = False


class DiscordIntegration(Integration):
    """Discord Bot integration."""

    name = "discord"

    def __init__(self, config: dict):
        super().__init__(config)
        self.bot_token: str = config.get("bot_token", "")
        self.channel_id: int = int(config.get("channel_id", 0) or 0)
        self._client = None

    async def connect(self) -> None:
        if not _DISCORD_AVAILABLE:
            logger.error("discord_py_missing", hint="pip install discord.py")
            return
        if not self.bot_token:
            logger.error("discord_token_missing")
            return

        intents = discord.Intents.default()
        intents.message_content = True
        self._client = discord.Client(intents=intents)

        @self._client.event
        async def on_ready():
            self._running = True
            logger.info("discord_connected", user=str(self._client.user))

        @self._client.event
        async def on_message(message):
            if message.author == self._client.user:
                return
            await self._dispatch(
                f"discord:{message.author.id}",
                message.content,
                {"channel_id": str(message.channel.id), "platform": "discord"},
            )

        import asyncio
        asyncio.create_task(self._client.start(self.bot_token))

    async def disconnect(self) -> None:
        if self._client:
            await self._client.close()
        self._running = False
        logger.info("discord_disconnected")

    async def send(self, text: str, target: str | None = None) -> None:
        if not self._client:
            return
        channel_id = int(target) if target else self.channel_id
        if not channel_id:
            logger.warning("discord_no_channel")
            return
        channel = self._client.get_channel(channel_id)
        if channel:
            try:
                await channel.send(text)
            except Exception as e:
                logger.error("discord_send_failed", error=str(e))
