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

__all__ = ["build_tts_backend", "build_stt_backend"]


def build_stt_backend(
    *,
    api_key: str,
    model: str,
    base_url: str | None,
    language: str | None = None,
) -> Any | None:
    """Return a remote STT backend for an ``audio_in`` profile, or None.

    Symmetric to :func:`build_tts_backend`. OpenAI / generic compat speak
    ``/audio/transcriptions``; MiniMax/Volcengine ASR use native
    file-upload+poll flows not covered here, so those return None and the
    caller falls back to local WhisperSTT.

    Conservative on purpose: ``audio_in`` is a broad capability (a
    multimodal *chat* model like gpt-4o-audio also carries it but is NOT a
    transcription endpoint). Only models whose name clearly marks them as
    transcription models wire to ``/audio/transcriptions``; everything else
    returns None → local Whisper.
    """
    m = (model or "").lower()
    is_transcription = (
        "whisper" in m or "transcribe" in m or "asr" in m or "-stt" in m
    )
    if not is_transcription:
        return None
    vendor = detect_media_vendor(model, base_url)
    if vendor in ("openai", "openai_compat") and base_url:
        from xmclaw.providers.voice.openai_stt import OpenAICompatSTT
        kwargs: dict[str, Any] = {
            "api_key": api_key, "model": model, "base_url": base_url,
        }
        if language:
            kwargs["language"] = language
        return OpenAICompatSTT(**kwargs)
    return None


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
