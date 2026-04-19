"""企业微信 (WeChat Work) integration.

Supports two modes:
- group_bot: Simple webhook-based group bot (outbound only, no setup)
- application: Full application bot with bidirectional messaging

Group Bot Setup (simplest):
1. Open a WeChat Work group → Add Bot → Custom Bot
2. Copy the webhook URL: https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx
3. Fill in config.json — you're done!

Application Bot Setup (full):
1. Create an app at https://work.weixin.qq.com/ → App Management
2. Get CorpID, AgentID, and AppSecret from the app settings
3. Configure the receiving URL: https://your-domain.com/api/wechat/webhook
4. Set "API Receive" permission in the app
5. For local dev: use a tunnel (ngrok, cloudflare tunnel) to expose callback URL

Config:
{
  "wechat": {
    "enabled": true,
    "mode": "group_bot",   // "group_bot" or "application"
    "webhook_url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx",
    "corp_id": "",
    "agent_id": "",
    "app_secret": "",
    "callback_token": "",  // verification token for application mode
    "callback_aes_key": "", // AES key for application mode
    "default_to_user": ""  // default user/chat to reply to
  }
}
"""
from __future__ import annotations
import asyncio
import hashlib
import hmac
import base64
import json
import time
import xml.etree.ElementTree as ET
from typing import Any

from xmclaw.utils.log import logger
from .base import Integration


class WeChatIntegration(Integration):
    """WeChat Work (企业微信) integration.

    Group Bot mode: send-only via webhook POST (no auth needed).
    Application mode: bidirectional via official API + HTTP callback.
    """

    name = "wechat"

    def __init__(self, config: dict):
        super().__init__(config)
        self.mode: str = config.get("mode", "group_bot")
        self.webhook_url: str = config.get("webhook_url", "")
        self.corp_id: str = config.get("corp_id", "")
        self.agent_id: str = config.get("agent_id", "")
        self.app_secret: str = config.get("app_secret", "")
        self.callback_token: str = config.get("callback_token", "")
        self.callback_aes_key: str = config.get("callback_aes_key", "")
        self.default_to_user: str = config.get("default_to_user", "")
        self._access_token: str | None = None
        self._token_expires_at: float = 0
        self._running = False

    # ── Group Bot Mode ─────────────────────────────────────────────────────────

    async def connect(self) -> None:
        if self.mode == "group_bot":
            await self._connect_group_bot()
        elif self.mode == "application":
            await self._connect_application()
        else:
            logger.error("wechat_unknown_mode", mode=self.mode)

    async def _connect_group_bot(self) -> None:
        """Group bot: validate webhook URL and mark ready."""
        if not self.webhook_url:
            logger.error("wechat_webhook_url_missing")
            return

        # Validate by checking URL format
        if not self.webhook_url.startswith("https://qyapi.weixin.qq.com/cgi-bin/webhook/send"):
            logger.error("wechat_invalid_webhook_url", url=self.webhook_url[:30])
            return

        self._running = True
        logger.info("wechat_group_bot_ready", mode=self.mode)

    async def _connect_application(self) -> None:
        """Application bot: get access token and mark ready."""
        if not all([self.corp_id, self.agent_id, self.app_secret]):
            logger.error("wechat_app_credentials_missing",
                        corp_id=bool(self.corp_id),
                        agent_id=bool(self.agent_id),
                        app_secret=bool(self.app_secret))
            return

        await self._refresh_access_token()
        self._running = True
        logger.info("wechat_application_ready",
                   corp_id=self.corp_id, agent_id=self.agent_id)

    async def _refresh_access_token(self) -> None:
        """Fetch a new access token from WeChat Work."""
        import aiohttp

        url = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
        params = {"corpid": self.corp_id, "corpsecret": self.app_secret}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params,
                                      timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    data = await resp.json()

            if data.get("errcode") == 0:
                self._access_token = data["access_token"]
                self._token_expires_at = time.time() + data.get("expires_in", 7200) - 60
                logger.info("wechat_access_token_refreshed")
            else:
                logger.error("wechat_token_failed",
                            errcode=data.get("errcode"), errmsg=data.get("errmsg"))

        except Exception as e:
            logger.error("wechat_token_request_failed", error=str(e))

    async def _ensure_token(self) -> bool:
        """Ensure we have a valid access token."""
        if not self._access_token or time.time() >= self._token_expires_at:
            await self._refresh_access_token()
        return bool(self._access_token)

    # ── Webhook Callback (Application mode) ───────────────────────────────────

    async def handle_webhook(self, payload: bytes, headers: dict,
                             query_params: dict) -> dict:
        """Handle incoming webhook from WeChat Work.

        Called by daemon HTTP server at GET/POST /api/wechat/webhook.

        GET:  WeChat Work verification challenge (URL validation)
        POST: Encrypted event notification

        Returns a dict response for the HTTP reply.
        """
        # GET: URL validation challenge
        if b"GET" in payload or not payload.strip():
            msg_signature = query_params.get("msg_signature", "")
            timestamp = query_params.get("timestamp", "")
            nonce = query_params.get("nonce", "")
            echostr = query_params.get("echostr", "")

            if echostr and self.callback_token and self.callback_aes_key:
                decrypted = self._decrypt_echo(echostr, msg_signature,
                                                timestamp, nonce)
                if decrypted:
                    return {"body": decrypted, "status": 200}
                return {"body": "验证失败", "status": 400}

            # Simple verification without AES
            return {"body": "", "status": 200}

        # POST: Process event notification
        if not self._running:
            return {"body": "not running", "status": 503}

        try:
            await self._handle_wechat_event(payload, query_params)
        except Exception as e:
            logger.error("wechat_webhook_handle_error", error=str(e))

        return {"body": "success", "status": 200}

    async def _handle_wechat_event(self, payload: bytes,
                                    query_params: dict) -> None:
        """Parse and dispatch a WeChat Work event."""
        try:
            # WeChat Work encrypts messages — decrypt if AES key is set
            if self.callback_aes_key and self.callback_token:
                msg_signature = query_params.get("msg_signature", "")
                timestamp = query_params.get("timestamp", "")
                nonce = query_params.get("nonce", "")
                plain_xml = self._decrypt_xml(payload.decode("utf-8"),
                                              msg_signature, timestamp, nonce)
            else:
                plain_xml = payload.decode("utf-8")

            root = ET.fromstring(plain_xml)
            msg_type = root.findtext("MsgType", "")
            from_user = root.findtext("FromUserName", "")
            content = root.findtext("Content", "").strip()
            event = root.findtext("Event", "")

            if event == "subscribe" or event == "enter_agent":
                text = "你好！我是 XMclaw，有什么可以帮你的吗？"
                await self._send_to_user(from_user, text)
                return

            if msg_type == "text" and content:
                source_id = f"wechat:{from_user}"
                metadata = {
                    "from_user": from_user,
                    "platform": "wechat",
                    "msg_type": msg_type,
                }
                await self._dispatch(source_id, content, metadata)

        except ET.ParseError:
            logger.warning("wechat_xml_parse_error")
        except Exception as e:
            logger.error("wechat_event_error", error=str(e))

    def _decrypt_xml(self, encrypted_xml: str, signature: str,
                      timestamp: str, nonce: str) -> str:
        """Decrypt an encrypted WeChat Work XML payload using AES-256-CBC."""
        try:
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
            from cryptography.hazmat.backends import default_backend
            from cryptography.hazmat.primitives import padding
            import base64 as _b64

            # Parse the encrypted XML
            root = ET.fromstring(encrypted_xml)
            encrypt_node = root.find("Encrypt")
            if encrypt_node is None:
                return encrypted_xml

            encrypt = encrypt_node.text or ""

            # Verify signature
            sort_str = sorted([self.callback_token, timestamp, nonce, encrypt])
            sign_str = "".join(sort_str)
            expected = hashlib.sha1(sign_str.encode()).hexdigest()
            if expected != signature:
                logger.warning("wechat_signature_mismatch")
                return encrypted_xml

            # AES decrypt
            aes_key_b64 = self.callback_aes_key + "=" * (32 - len(self.callback_aes_key))
            aes_key = _b64.b64decode(aes_key_b64)
            encrypt_bytes = _b64.b64decode(encrypt)

            # Extract IV (first 16 bytes of ciphertext for CBC)
            iv = encrypt_bytes[:16]
            ciphertext = encrypt_bytes[16:]

            cipher = Cipher(algorithms.AES(aes_key), modes.CBC(iv), backend=default_backend())
            decryptor = cipher.decryptor()
            padded = decryptor.update(ciphertext) + decryptor.finalize()

            # Remove PKCS7 padding
            pad_len = padded[-1]
            plain = padded[:-pad_len]

            # Format: random(4) + msg_len(4) + msg + from_appid
            msg_len = int.from_bytes(plain[4:8], "big")
            msg_xml = plain[8:8 + msg_len].decode("utf-8")
            return msg_xml

        except Exception as e:
            logger.error("wechat_decrypt_error", error=str(e))
            return encrypted_xml

    def _decrypt_echo(self, echostr: str, signature: str,
                       timestamp: str, nonce: str) -> str | None:
        """Decrypt WeChat Work URL verification challenge."""
        try:
            return self._decrypt_xml(
                f"<xml><Encrypt>{echostr}</Encrypt></xml>",
                signature, timestamp, nonce
            )
        except Exception:
            return None

    # ── Send ──────────────────────────────────────────────────────────────────

    async def send(self, text: str, target: str | None = None) -> None:
        """Send a message via WeChat Work.

        Group bot: POST to webhook URL.
        Application: send via official API to specific user or chat.
        """
        if not self._running:
            return

        if self.mode == "group_bot":
            await self._send_webhook(text)
        elif self.mode == "application":
            to_user = target or self.default_to_user
            if to_user:
                await self._send_api(to_user, text)
        else:
            logger.warning("wechat_unknown_mode", mode=self.mode)

    async def _send_webhook(self, text: str) -> None:
        """Send a message via group bot webhook."""
        import aiohttp

        if not self.webhook_url:
            return

        payload = {
            "msgtype": "text",
            "text": {
                "content": text[:2048],  # WeChat limit
            }
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.webhook_url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json()
                    if data.get("errcode") != 0:
                        logger.error("wechat_webhook_failed",
                                   errcode=data.get("errcode"),
                                   errmsg=data.get("errmsg"))
        except Exception as e:
            logger.error("wechat_webhook_error", error=str(e))

    async def _send_api(self, to_user: str, text: str) -> None:
        """Send a message via WeChat Work application API."""
        import aiohttp

        if not await self._ensure_token():
            logger.error("wechat_no_access_token")
            return

        url = "https://qyapi.weixin.qq.com/cgi-bin/message/send"
        params = {"access_token": self._access_token}
        payload = {
            "touser": to_user,
            "msgtype": "text",
            "agentid": self.agent_id,
            "text": {"content": text[:2048]},
            "safe": 0,
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, json=payload, params=params,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json()
                    if data.get("errcode") != 0:
                        logger.error("wechat_api_send_failed",
                                   errcode=data.get("errcode"),
                                   errmsg=data.get("errmsg"))

        except Exception as e:
            logger.error("wechat_api_send_error", error=str(e))

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def disconnect(self) -> None:
        self._running = False
        self._access_token = None
        logger.info("wechat_disconnected")
