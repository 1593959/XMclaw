"""EdgeTTS — TTS via Microsoft Edge's free voice service.

B-388. ``edge-tts`` is a tiny pure-Python wrapper around the same
voice service the Edge browser's "Read aloud" feature uses. It's
free, requires no API key, and ships hundreds of multilingual voices.
The catch: it's an unofficial endpoint Microsoft can change without
notice — for production-critical TTS on a paid plan, swap to Azure
Cognitive Services using the same :class:`TTSProvider` ABC.

Default voice is ``zh-CN-XiaoxiaoNeural`` because the user base is
primarily Chinese-speaking. English alternatives include
``en-US-AriaNeural``, ``en-US-JennyNeural``, ``en-GB-SoniaNeural``.

Lazy import: ``edge-tts`` is light (~30 KB pure Python + websocket-
client) but still optional. ``import xmclaw.providers.voice`` works
without it; calling ``synthesize`` without it raises ``ImportError``
with an install hint, which the tool dispatcher catches and turns
into a structured ``ToolResult(ok=False, error=...)``.
"""
from __future__ import annotations

from typing import Any

from xmclaw.providers.voice.base import TTSProvider


_INSTALL_HINT = (
    "EdgeTTS needs the ``edge-tts`` package. "
    "Install with: pip install 'xmclaw[voice-tts]' "
    "(or: pip install edge-tts)"
)

DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"


class EdgeTTS(TTSProvider):
    """TTS backed by Microsoft Edge's free voice service.

    Parameters
    ----------
    voice : str
        Default voice id when ``synthesize`` is called with
        ``voice="default"``. Defaults to ``zh-CN-XiaoxiaoNeural``.
        Override per-call by passing an explicit voice string.
    rate : str
        Speech rate. Format ``"+N%"`` / ``"-N%"`` (e.g. ``"+0%"``,
        ``"+15%"``, ``"-25%"``). Default ``"+0%"`` (natural pace).
    volume : str
        Output volume in the same percentage form. Default ``"+0%"``.
    """

    def __init__(
        self,
        voice: str = DEFAULT_VOICE,
        rate: str = "+0%",
        volume: str = "+0%",
    ) -> None:
        self.voice = voice
        self.rate = rate
        self.volume = volume

    def _import_sdk(self) -> Any:
        """Lazy-import ``edge_tts``. Raises :class:`ImportError` with
        an actionable install hint when the package is missing."""
        try:
            import edge_tts  # type: ignore
        except ImportError as exc:
            raise ImportError(_INSTALL_HINT) from exc
        return edge_tts

    async def synthesize(self, text: str, voice: str = "default") -> bytes:
        """Synthesize ``text`` as mp3 bytes.

        ``voice="default"`` resolves to the constructor-supplied
        default. Pass an explicit voice string (e.g.
        ``"en-US-AriaNeural"``) to override per-call.
        """
        if not text:
            return b""
        edge_tts = self._import_sdk()
        chosen = self.voice if voice == "default" else voice
        # ``Communicate`` streams back chunked binary frames; we
        # accumulate the audio frames into a single bytes buffer.
        # Non-audio frames (word-boundary, sentence metadata) are
        # ignored — callers that need timing data can subclass and
        # collect WordBoundary frames separately.
        comm = edge_tts.Communicate(
            text, chosen, rate=self.rate, volume=self.volume,
        )
        chunks: list[bytes] = []
        async for chunk in comm.stream():
            if chunk.get("type") == "audio":
                data = chunk.get("data")
                if isinstance(data, (bytes, bytearray)):
                    chunks.append(bytes(data))
        return b"".join(chunks)
