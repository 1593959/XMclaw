"""OpenAICompatTTS — text-to-speech via an OpenAI-compatible
``/audio/speech`` endpoint.

2026-06-16. Covers OpenAI (``tts-1`` / ``tts-1-hd`` / ``gpt-4o-mini-tts``)
and any aggregator that mirrors the shape. The response is the raw audio
body (no JSON envelope), so we return ``resp.content`` directly.

Implements :class:`TTSProvider` so it shares the ``voice_synthesize`` /
``speak`` plumbing. ``voice`` maps to OpenAI's ``voice`` field (``alloy`` /
``nova`` / ``shimmer`` / …).
"""
from __future__ import annotations

import httpx

from xmclaw.providers.voice.base import TTSProvider
from xmclaw.utils.log import get_logger

logger = get_logger(__name__)

_DEFAULT_MODEL = "tts-1"
_DEFAULT_VOICE = "alloy"


class OpenAICompatTTS(TTSProvider):
    """TTS backed by an OpenAI-compatible ``/audio/speech`` endpoint.

    Parameters
    ----------
    api_key : str
        Bearer token.
    model : str
        e.g. ``tts-1`` / ``tts-1-hd`` / ``gpt-4o-mini-tts``.
    base_url : str
        OpenAI-compatible base ending in ``/v1``.
    voice : str
        Default voice when ``synthesize(voice="default")``.
    response_format : str
        Audio container (``mp3`` / ``wav`` / ``opus`` / …). Default mp3.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str = _DEFAULT_MODEL,
        base_url: str,
        voice: str = _DEFAULT_VOICE,
        response_format: str = "mp3",
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("api_key is required")
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("base_url is required")
        self._key = api_key.strip()
        self._model = (model or _DEFAULT_MODEL).strip()
        self._base = base_url.strip().rstrip("/")
        self.voice = voice or _DEFAULT_VOICE
        self._fmt = response_format or "mp3"

    async def synthesize(self, text: str, voice: str = "default") -> bytes:
        if not text:
            return b""
        chosen = self.voice if voice == "default" else voice
        body = {
            "model": self._model,
            "input": text,
            "voice": chosen,
            "response_format": self._fmt,
        }
        headers = {
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self._base}/audio/speech",
                headers=headers, json=body, timeout=120.0,
            )
            resp.raise_for_status()
            return resp.content


__all__ = ["OpenAICompatTTS"]
