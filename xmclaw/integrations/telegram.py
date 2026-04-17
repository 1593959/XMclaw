"""Telegram Bot integration."""
from __future__ import annotations
from xmclaw.utils.log import logger
from .base import Integration

try:
    from telegram import Update
    from telegram.ext import ApplicationBuilder, MessageHandler, ContextTypes, filters
    _TELEGRAM_AVAILABLE = True
except ImportError:
    _TELEGRAM_AVAILABLE = False


class TelegramIntegration(Integration):
    """Telegram Bot integration using python-telegram-bot."""

    name = "telegram"

    def __init__(self, config: dict):
        super().__init__(config)
        self.bot_token: str = config.get("bot_token", "")
        self.chat_id: str = str(config.get("chat_id", ""))
        self._app = None

    async def connect(self) -> None:
        if not _TELEGRAM_AVAILABLE:
            logger.error("telegram_lib_missing", hint="pip install python-telegram-bot")
            return
        if not self.bot_token:
            logger.error("telegram_token_missing")
            return

        self._app = ApplicationBuilder().token(self.bot_token).build()

        async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
            if not update.message or not update.message.text:
                return
            user_id = str(update.effective_user.id) if update.effective_user else "unknown"
            await self._dispatch(
                f"telegram:{user_id}",
                update.message.text,
                {"chat_id": str(update.effective_chat.id), "platform": "telegram"},
            )

        self._app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling()
        self._running = True
        logger.info("telegram_connected")

    async def disconnect(self) -> None:
        if self._app:
            try:
                await self._app.updater.stop()
                await self._app.stop()
                await self._app.shutdown()
            except Exception as e:
                logger.warning("telegram_disconnect_error", error=str(e))
        self._running = False
        logger.info("telegram_disconnected")

    async def send(self, text: str, target: str | None = None) -> None:
        if not self._app:
            return
        chat_id = target or self.chat_id
        if not chat_id:
            logger.warning("telegram_no_chat_id")
            return
        try:
            await self._app.bot.send_message(chat_id=chat_id, text=text)
        except Exception as e:
            logger.error("telegram_send_failed", error=str(e))
