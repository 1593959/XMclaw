"""OpenAICompatSTT — speech-to-text via an OpenAI-compatible
``/audio/transcriptions`` endpoint.

2026-06-17. Covers OpenAI (``whisper-1`` / ``gpt-4o-transcribe``) and any
aggregator that mirrors the multipart shape. Symmetric to
:class:`OpenAICompatTTS` — the remote counterpart to the local
:class:`WhisperSTT`, wired from an ``audio_in`` model profile.

Implements the :class:`STTProvider` ABC so it shares the
``voice_transcribe`` / ``voice_listen`` plumbing.
"""
from __future__ import annotations

import httpx

from xmclaw.providers.voice.base import STTProvider
from xmclaw.utils.log import get_logger

logger = get_logger(__name__)

_DEFAULT_MODEL = "whisper-1"


class OpenAICompatSTT(STTProvider):
    """STT backed by an OpenAI-compatible ``/audio/transcriptions`` endpoint.

    Parameters
    ----------
    api_key : str
        Bearer token.
    model : str
        e.g. ``whisper-1`` / ``gpt-4o-transcribe``.
    base_url : str
        OpenAI-compatible base ending in ``/v1``.
    language : str | None
        Optional ISO-639-1 hint (e.g. ``zh`` / ``en``). Omitted when None.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str = _DEFAULT_MODEL,
        base_url: str,
        language: str | None = None,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("api_key is required")
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("base_url is required")
        self._key = api_key.strip()
        self._model = (model or _DEFAULT_MODEL).strip()
        self._base = base_url.strip().rstrip("/")
        self._language = (language or "").strip() or None

    async def transcribe(self, audio_bytes: bytes) -> str:
        if not audio_bytes:
            return ""
        data: dict[str, str] = {"model": self._model}
        if self._language:
            data["language"] = self._language
        # Container is inferred by the server from the bytes; a generic
        # filename + octet-stream is accepted by OpenAI-shape endpoints.
        files = {"file": ("audio.wav", audio_bytes, "application/octet-stream")}
        headers = {"Authorization": f"Bearer {self._key}"}
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self._base}/audio/transcriptions",
                headers=headers, data=data, files=files, timeout=120.0,
            )
            from xmclaw.utils.http_errors import raise_for_vendor_error
            raise_for_vendor_error(resp, f"STT /audio/transcriptions (model={self._model})")
            payload = resp.json()
        text = payload.get("text") if isinstance(payload, dict) else None
        return text if isinstance(text, str) else ""


__all__ = ["OpenAICompatSTT"]
