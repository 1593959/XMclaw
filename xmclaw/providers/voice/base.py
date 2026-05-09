"""Voice provider ABCs — STTProvider + TTSProvider.

B-388. The contracts are intentionally tiny — voice is a
batch-in / batch-out concern at this layer:

* :class:`STTProvider` ``transcribe(audio_bytes) -> text``
* :class:`TTSProvider` ``synthesize(text, voice="default") -> bytes``

Streaming variants (partial transcripts, sentence-by-sentence TTS)
will arrive as separate methods when a real-time channel needs them;
keeping the v1 surface narrow lets every backend (faster-whisper,
edge-tts, future Azure / OpenAI / ElevenLabs adapters) satisfy the
ABC without empty-method noise.
"""
from __future__ import annotations

import abc


class STTProvider(abc.ABC):
    """Speech-to-text provider.

    Implementations accept raw audio bytes (any common container the
    backend can decode — wav / mp3 / m4a / webm / ogg are all in scope
    for faster-whisper) and return the recognized text.
    """

    @abc.abstractmethod
    async def transcribe(self, audio_bytes: bytes) -> str:
        """Transcribe ``audio_bytes`` to text.

        Returns the recognized text. Empty string for silent / empty
        audio (NOT a raised exception — the caller decides whether
        empty is a failure).
        """
        ...


class TTSProvider(abc.ABC):
    """Text-to-speech provider.

    Implementations accept a text string + optional voice id and
    return audio bytes. Container format is backend-specific —
    :class:`EdgeTTS` returns mp3, future providers may return wav /
    ogg / opus. Callers that need a specific format should write to
    a file with the matching extension.
    """

    @abc.abstractmethod
    async def synthesize(self, text: str, voice: str = "default") -> bytes:
        """Render ``text`` as audio bytes using the given ``voice``.

        ``voice="default"`` lets the backend pick its own default
        (e.g. EdgeTTS picks ``zh-CN-XiaoxiaoNeural`` if constructed
        without a voice). Backends that don't recognize the voice
        string raise :class:`ValueError` with the available list.
        """
        ...
