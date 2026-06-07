"""Voice providers — STT (speech → text) + TTS (text → speech).

B-388 (Sprint 2). XMclaw scored 0/100 on voice vs a naive approach' 8 TTS + 3
STT providers. This package adds the most-recommended local-first /
zero-key combination:

* :class:`WhisperSTT` — local STT via faster-whisper (CPU-friendly,
  no API key, multilingual). Optional dep ``faster-whisper>=1.0``.
* :class:`EdgeTTS` — TTS via Microsoft Edge's free voice service.
  No API key required. Optional dep ``edge-tts>=6``.

Both providers lazy-import their SDK inside the constructor so a
fresh ``pip install xmclaw`` (without the ``[voice]`` extra) doesn't
crash at import time. The error message points at the right extra
when the SDK is missing.

Top-level imports re-export the concrete classes so external callers
can write ``from xmclaw.providers.voice import WhisperSTT``. The
:mod:`base` ABCs are stable contracts; concrete classes may add extra
constructor kwargs for backend-specific tuning (model size, voice id).
"""
from __future__ import annotations

from xmclaw.providers.voice.base import STTProvider, TTSProvider
from xmclaw.providers.voice.edge_tts import EdgeTTS
from xmclaw.providers.voice.whisper import WhisperSTT

__all__ = ["STTProvider", "TTSProvider", "WhisperSTT", "EdgeTTS"]
