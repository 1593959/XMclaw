"""TTS-backend dispatch for remote (API-key) text-to-speech.

2026-06-17. EdgeTTS (free, no key) stays the zero-config default. When the
user configures an ``audio_out`` model profile, this picks the matching
remote backend: MiniMax native ``/t2a_v2`` or an OpenAI-compatible
``/audio/speech``. Volcengine seed-tts uses a native binary/websocket API
not covered here — those profiles return None and fall back to EdgeTTS.

Returns a :class:`~xmclaw.providers.voice.base.TTSProvider` or None.
"""
from __future__ import annotations

from typing import Any

from xmclaw.utils.vendor_detect import detect_media_vendor

__all__ = ["build_tts_backend"]


def build_tts_backend(
    *,
    api_key: str,
    model: str,
    base_url: str | None,
    voice: str | None = None,
) -> Any | None:
    """Return a remote TTS backend for the given profile, or None."""
    vendor = detect_media_vendor(model, base_url)

    if vendor == "minimax":
        from xmclaw.providers.voice.minimax_tts import MiniMaxTTS
        kwargs: dict[str, Any] = {
            "api_key": api_key, "model": model, "base_url": base_url,
        }
        if voice:
            kwargs["voice"] = voice
        return MiniMaxTTS(**kwargs)

    if vendor in ("openai", "openai_compat"):
        from xmclaw.providers.voice.openai_tts import OpenAICompatTTS
        if not base_url:
            return None
        kwargs = {"api_key": api_key, "model": model, "base_url": base_url}
        if voice:
            kwargs["voice"] = voice
        return OpenAICompatTTS(**kwargs)

    # ark (Volcengine seed-tts native) / replicate / unknown → no portable
    # backend; caller falls back to EdgeTTS.
    return None
