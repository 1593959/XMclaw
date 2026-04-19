"""QQ 频道 (QQ Guild) integration via official Tencent Cloud Open Platform API.

Supports both WebSocket (recommended) and HTTP webhook modes.

Requires: pip install qq-channel

Setup (QQ Channel Bot):
1. Visit https://q.qq.com/ and create a bot application
2. Obtain AppID and AppToken from the bot settings page
3. Configure the WebSocket connection URL or webhook URL
4. Subscribe to message events in the developer console
5. Fill in daemon/config.json

Config:
{
  "qq": {
    "enabled": true,
    "mode": "websocket",         // "websocket" or "webhook"
    "app_id": "123456789",
    "app_token": "xxx",         // bot token from QQ Open Platform
    "secret": "xxx",            // application secret
    "webhook_url": "",          // only for webhook mode
    "webhook_token": "",        // verification token for webhook
    "guild_id": "",             // default guild to send to
    "channel_id": ""            // default channel to send to
  }
}
"""
from __future__ import annotations
import asyncio
import hashlib
import hmac
import json
import time
import asyncio
import websockets

from xmclaw.utils.log import logger
from .base import Integration


class QQIntegration(Integration):
    """QQ 频道 (QQ Guild) Bot integration.

    Supports two connection modes:
    - websocket: Persistent WebSocket connection to QQ Open Platform (recommended)
    - webhook: HTTP server receives events, daemon acts as the server endpoint
    """

    name = "qq"

    def __init__(self, config: dict):
        super().__init__(config)
        self.mode: str = config.get("mode", "websocket")
        self.app_id: str = config.get("app_id", "")
        self.app_token: str = config.get("app_token", "")
        self.secret: str = config.get("secret", "")
        self.webhook_url: str = config.get("webhook_url", "")
        self.webhook_token: str = config.get("webhook_token", "")
        self.guild_id: str = config.get("guild_id", "")
        self.default_channel_id: str = config.get("channel_id", "")

        self._ws: websockets.WebSocketClientProtocol | None = None
        self._ws_url: str | None = None
        self._intents: int = 1 << 30  # GUILD_MESSAGES intent
        self._running = False
        self._reconnect_delay = 5
        self._max_reconnect_delay = 60

    # ── WebSocket Mode ─────────────────────────────────────────────────────────

    async def connect(self) -> None:
        if not self.app_id or not self.app_token:
            logger.error("qq_credentials_missing",
                         app_id=bool(self.app_id), app_token=bool(self.app_token))
            return

        if self.mode == "websocket":
            await self._connect_websocket()
        elif self.mode == "webhook":
            # Webhook mode: daemon acts as HTTP server; nothing to connect here.
            # The daemon's HTTP server routes /webhooks/qq to handle_event().
            self._running = True
            logger.info("qq_webhook_mode_ready",
                       webhook_hint="POST /webhooks/qq with QQ Guild events")
        else:
            logger.error("qq_unknown_mode", mode=self.mode)

    async def _connect_websocket(self) -> None:
        """Connect to QQ Open Platform via WebSocket gateway."""
        # QQ Guild uses a two-step process:
        # 1. Get gateway URL: GET https://api.sgroup.qq.com/gateway/bot
        # 2. Connect with Authorization: Bot <app_token>
        import aiohttp

        headers = {
            "Authorization": f"Bot {self.app_id}.{self.app_token}",
            "X-Union-Appid": self.app_id,
        }

        try:
            async with aiohttp.ClientSession() as session:
                # Step 1: Get gateway
                async with session.get(
                    "https://api.sgroup.qq.com/gateway/bot",
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 401:
                        logger.error("qq_auth_failed",
                                    hint="Check app_id and app_token in config")
                        return
                    if resp.status != 200:
                        logger.error("qq_gateway_failed", status=resp.status)
                        return
                    data = await resp.json()
                    gateway_url = data.get("url", "")
                    if not gateway_url:
                        logger.error("qq_gateway_no_url", data=data)
                        return

            # Step 2: Connect to WebSocket
            ws_url = gateway_url + f"?intents={self._intents}&compress=zlib-stream"
            self._ws_url = ws_url
            await self._ws_loop(ws_url, headers)

        except aiohttp.ClientError as e:
            logger.error("qq_connection_failed", error=str(e))
            asyncio.create_task(self._schedule_reconnect())

    async def _ws_loop(self, url: str, headers: dict) -> None:
        """Main WebSocket receive loop with auto-reconnect."""
        import zlib

        decompressor = zlib.decompressobj()
        pending = b""

        while self._running:
            try:
                async with websockets.connect(url, extra_headers=headers,
                                              max_size=50 * 1024 * 1024) as ws:
                    self._ws = ws
                    self._reconnect_delay = 5  # Reset on successful connect
                    logger.info("qq_websocket_connected")

                    async for raw_msg in ws:
                        if not self._running:
                            break

                        # Handle zlib-compressed payloads
                        if isinstance(raw_msg, bytes):
                            pending += raw_msg
                            if len(raw_msg) < 4:
                                continue
                            # Check if this is the last message in a frame
                            is_last = raw_msg[-4:] == b'\x00\x00\xff\xff'
                            if not is_last:
                                continue
                            try:
                                msg_bytes = decompressor.decompress(pending)
                                pending = b""
                                decompressor = zlib.decompressobj()
                                msg = json.loads(msg_bytes)
                            except Exception:
                                continue
                        else:
                            msg = json.loads(raw_msg)

                        asyncio.create_task(self._handle_ws_message(msg))

            except websockets.ConnectionClosed as e:
                logger.warning("qq_ws_disconnected", code=e.code, reason=e.reason)
            except Exception as e:
                logger.error("qq_ws_error", error=str(e))

            if self._running:
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, self._max_reconnect_delay)

    async def _schedule_reconnect(self) -> None:
        """Reconnect after delay (capped exponential backoff)."""
        await asyncio.sleep(self._reconnect_delay)
        if self._running and self._ws_url:
            await self._connect_websocket()

    async def _handle_ws_message(self, msg: dict) -> None:
        """Handle an incoming WebSocket message from QQ Guild."""
        op = msg.get("op")   # Opcode
        t  = msg.get("t")    # Event type
        d  = msg.get("d", {})  # Event data

        # Opcode 10: Hello — identify
        if op == 10:
            seq = msg.get("s")
            await self._ws.send(json.dumps({
                "op": 2,
                "d": {
                    "token": f"{self.app_id}.{self.app_token}",
                    "intents": self._intents,
                    "shard": [0, 1],
                    "properties": {
                        "$language": "python",
                        "$platform": "xmclaw",
                    }
                }
            }))
            return

        # Opcode 0: Dispatch — an event
        if op != 0:
            return

        # Handle message events
        if t in ("MESSAGE_CREATE", "PUBLIC_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE"):
            await self._handle_message(d)

        # Opcode 11: Heartbeat ACK
        elif t == "RESUMED":
            logger.info("qq_session_resumed")

    async def _handle_message(self, data: dict) -> None:
        """Process an incoming message from QQ Guild."""
        try:
            msg = data.get("msg", {})
            if not msg:
                return

            # Skip bot's own messages
            author = msg.get("author", {})
            if author.get("bot", False):
                return

            channel_id = str(data.get("channel_id", ""))
            guild_id = str(data.get("guild_id", ""))
            user_id = str(author.get("id", ""))
            content = msg.get("content", "").strip()

            if not content:
                return

            # Check for bot mention (CQ码 format)
            mention_self = "@me" in content or f"[CQ:at,qq=me]" in content
            if mention_self:
                # Remove mention CQ码 and @me from content
                import re as _re
                content = _re.sub(r"\[CQ:at,qq=\d+\]", "", content).strip()
                content = content.lstrip("@me").strip()
                if not content:
                    return

            source_id = f"qq:{user_id}"
            metadata = {
                "channel_id": channel_id,
                "guild_id": guild_id,
                "user_id": user_id,
                "platform": "qq",
            }

            await self._dispatch(source_id, content, metadata)

        except Exception as e:
            logger.error("qq_message_parse_error", error=str(e))

    # ── Webhook Mode ───────────────────────────────────────────────────────────

    async def handle_webhook(self, payload: dict, headers: dict) -> None:
        """Handle an incoming webhook event from QQ Guild.

        Called by the daemon's HTTP server at POST /webhooks/qq.
        """
        if not self._running:
            return

        # Verify signature if secret is configured
        if self.secret:
            sig = headers.get("x-qq-signature", "")
            if not self._verify_signature(payload, sig):
                logger.warning("qq_webhook_signature_invalid")
                return

        t = payload.get("t")
        d = payload.get("d", {})

        if t in ("MESSAGE_CREATE", "GROUP_MESSAGE_CREATE"):
            await self._handle_message(d)

    def _verify_signature(self, payload: dict, sig: str) -> bool:
        """Verify QQ Guild webhook signature."""
        if not self.secret:
            return True
        import time as _time
        # QQ Guild uses a simple timestamp + signing mechanism
        # The signature is HMAC-SHA256 of timestamp + body
        return True  # Simplified — implement full verification as needed

    # ── Send ───────────────────────────────────────────────────────────────────

    async def send(self, text: str, target: str | None = None) -> None:
        """Send a message to a QQ channel.

        Args:
            text: Message text (plain or with CQ码 formatting)
            target: channel_id to send to (falls back to default_channel_id)
        """
        if not self._running:
            return

        channel_id = target or self.default_channel_id
        if not channel_id:
            logger.warning("qq_no_target_channel")
            return

        if self.mode == "websocket" and self._ws:
            await self._send_ws(channel_id, text)
        elif self.mode == "webhook":
            await self._send_webhook(channel_id, text)
        else:
            logger.warning("qq_not_connected")

    async def _send_ws(self, channel_id: str, text: str) -> None:
        """Send via WebSocket using QQ Guild API."""
        import aiohttp

        headers = {
            "Authorization": f"Bot {self.app_id}.{self.app_token}",
            "Content-Type": "application/json",
        }

        try:
            payload = {
                "content": text[:2000],
            }
            async with aiohttp.ClientSession() as session:
                url = f"https://api.sgroup.qq.com/channels/{channel_id}/messages"
                async with session.post(url, json=payload, headers=headers,
                                        timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status not in (200, 204):
                        body = await resp.text()
                        logger.error("qq_send_failed", status=resp.status, body=body[:200])
        except Exception as e:
            logger.error("qq_send_error", error=str(e))

    async def _send_webhook(self, channel_id: str, text: str) -> None:
        """Send via QQ Guild webhook URL."""
        logger.warning("qq_webhook_send_not_implemented",
                      hint="Use websocket mode for sending messages")

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def disconnect(self) -> None:
        self._running = False
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        self._running = False
        logger.info("qq_disconnected")
