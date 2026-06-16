"""MiniMaxTTS — text-to-speech via MiniMax T2A (``/t2a_v2``).

2026-06-16. MiniMax T2A is a synchronous HTTP API: the response carries
the audio as a *hex-encoded* string in ``data.audio``. Implements the
:class:`TTSProvider` ABC so it shares the ``voice_synthesize`` / ``speak``
plumbing with EdgeTTS.

The ``voice`` arg maps to MiniMax's ``voice_setting.voice_id`` (e.g.
``female-shaonv`` / ``male-qn-qingse`` / ``English_Graceful_Lady``).
"""
from __future__ import annotations

import httpx

from xmclaw.providers.voice.base import TTSProvider
from xmclaw.utils.log import get_logger

logger = get_logger(__name__)

_DEFAULT_BASE_URL = "https://api.minimax.io/v1"
_DEFAULT_MODEL = "speech-02-hd"
_DEFAULT_VOICE = "female-shaonv"


class MiniMaxTTS(TTSProvider):
    """TTS backed by MiniMax's ``/t2a_v2`` synchronous endpoint.

    Parameters
    ----------
    api_key : str
        MiniMax API key (``Authorization: Bearer``).
    model : str
        e.g. ``speech-02-hd`` / ``speech-02-turbo`` / ``speech-2.8-hd``.
    base_url : str | None
        API base ending in ``/v1``. Defaults to the global host.
    voice : str
        Default ``voice_id`` when ``synthesize(voice="default")``.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str = _DEFAULT_MODEL,
        base_url: str | None = None,
        voice: str = _DEFAULT_VOICE,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("api_key is required")
        self._key = api_key.strip()
        self._model = (model or _DEFAULT_MODEL).strip()
        self._base = (base_url or _DEFAULT_BASE_URL).strip().rstrip("/")
        self.voice = voice or _DEFAULT_VOICE

    async def synthesize(self, text: str, voice: str = "default") -> bytes:
        if not text:
            return b""
        chosen = self.voice if voice == "default" else voice
        body = {
            "model": self._model,
            "text": text,
            "stream": False,
            "voice_setting": {
                "voice_id": chosen,
                "speed": 1.0,
                "vol": 1.0,
                "pitch": 0,
            },
            "audio_setting": {"sample_rate": 32000, "format": "mp3"},
        }
        headers = {
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self._base}/t2a_v2", headers=headers, json=body, timeout=120.0,
            )
            resp.raise_for_status()
            data = resp.json()

        br = data.get("base_resp")
        if isinstance(br, dict) and isinstance(br.get("status_code"), int) and br["status_code"] != 0:
            raise RuntimeError(
                f"MiniMax T2A error {br['status_code']}: {br.get('status_msg') or ''}"
            )
        audio_hex = (data.get("data") or {}).get("audio") if isinstance(data.get("data"), dict) else None
        if not isinstance(audio_hex, str) or not audio_hex:
            raise RuntimeError(f"MiniMax T2A returned no audio: {data}")
        try:
            return bytes.fromhex(audio_hex)
        except ValueError as exc:
            raise RuntimeError(f"MiniMax T2A audio not hex-decodable: {exc}") from exc


__all__ = ["MiniMaxTTS"]
