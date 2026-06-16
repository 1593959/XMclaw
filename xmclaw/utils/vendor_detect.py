"""Vendor detection for media-generation backends.

2026-06-17. Media generation (image / video / TTS) has only a handful of
*wire protocols*, not one-per-vendor: OpenAI-compatible sync, Replicate-
style async tasks, and a few native envelopes (MiniMax). Rather than a
bespoke class per vendor, the dispatch layer maps a profile to one of
these protocols. This function does the mapping from ``model`` + ``base_url``.

Lives in ``utils`` so both ``providers/media`` (image/video) and
``providers/voice`` (TTS) can share it without crossing the
sibling-package import ban.

Returns one of: ``"replicate"``, ``"minimax"``, ``"ark"``, ``"openai"``,
``"openai_compat"``. The last is the catch-all that speaks the plain
OpenAI shape (``/images/generations`` / ``/audio/speech``).
"""
from __future__ import annotations

__all__ = ["detect_media_vendor"]


def detect_media_vendor(model: str | None, base_url: str | None) -> str:
    m = (model or "").lower()
    b = (base_url or "").lower()

    if "replicate" in b:
        return "replicate"

    # MiniMax: its own host, or the model families it serves (Hailuo /
    # abab / T2V / I2V video, image-01, speech-0x TTS).
    if (
        "minimax" in b
        or "minimax" in m
        or "hailuo" in m
        or m.startswith("abab")
        or m.startswith("t2v")
        or m.startswith("i2v")
        or m == "image-01"
        or m.startswith("speech-0")
    ):
        return "minimax"

    # Volcengine Ark / Doubao (Seedance video, Seedream image, seed-tts).
    if (
        "volces" in b
        or "ark" in b
        or "doubao" in m
        or "seedance" in m
        or "seedream" in m
        or "seed-tts" in m
        or "seed-asr" in m
    ):
        return "ark"

    # OpenAI proper (DALL-E / gpt-image / tts-1 / whisper).
    if (
        "openai.com" in b
        or m.startswith("dall-e")
        or "dalle" in m
        or "gpt-image" in m
        or m.startswith("tts-1")
        or m.startswith("gpt-4o-mini-tts")
        or m == "whisper-1"
    ):
        return "openai"

    return "openai_compat"
