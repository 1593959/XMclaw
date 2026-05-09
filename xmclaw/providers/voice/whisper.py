"""WhisperSTT — local STT via faster-whisper.

B-388. faster-whisper is the CTranslate2 reimplementation of OpenAI's
Whisper that runs ~4× faster on CPU at fp16 / int8 quantization. The
``tiny`` model is ~75 MB and runs comfortably on any laptop CPU; the
``large-v3`` model is ~3 GB and warrants a GPU. Default is ``tiny``
so the first-run download stays light.

The faster-whisper SDK is imported lazily inside the constructor so
``import xmclaw.providers.voice`` works on installs that didn't pull
the ``[voice-stt]`` / ``[voice]`` extra. The missing-dep error names
the right pip extra so the operator can fix it without grep.

Why not OpenAI's official ``whisper`` package? It pulls in torch,
which is a 2 GB+ dependency. ``faster-whisper`` ships its own runtime
(CTranslate2) and is roughly 200 MB total — small enough to bundle
into a portable XMclaw install when the user opts into voice.
"""
from __future__ import annotations

import asyncio
import io
from typing import Any

from xmclaw.providers.voice.base import STTProvider


_INSTALL_HINT = (
    "WhisperSTT needs the ``faster-whisper`` package. "
    "Install with: pip install 'xmclaw[voice-stt]' "
    "(or: pip install faster-whisper)"
)


class WhisperSTT(STTProvider):
    """Local STT backed by faster-whisper.

    Parameters
    ----------
    model_name : str
        Whisper model id. One of: ``tiny``, ``base``, ``small``,
        ``medium``, ``large-v3``. Larger = more accurate + slower +
        bigger first-run download. Default ``tiny``.
    device : str
        ``"cpu"`` or ``"cuda"``. Default ``"cpu"`` — every laptop
        works without driver hassle. Set ``"cuda"`` only if you have
        the CUDA runtime + a recent NVIDIA GPU.
    compute_type : str
        Quantization mode. ``"int8"`` (default) is the lightest;
        ``"int8_float16"`` / ``"float16"`` / ``"float32"`` trade
        precision for memory. Per faster-whisper docs, ``int8`` on
        CPU is the sweet spot for the ``tiny``/``base`` models.
    language : str | None
        ISO-639-1 hint (``"zh"``, ``"en"``, …). ``None`` (default)
        lets Whisper auto-detect — slower but works for mixed-
        language audio.
    """

    def __init__(
        self,
        model_name: str = "tiny",
        device: str = "cpu",
        compute_type: str = "int8",
        language: str | None = None,
    ) -> None:
        self.model_name = model_name
        self.device = device
        self.compute_type = compute_type
        self.language = language
        # Lazy: WhisperModel is heavy (200 MB+ of CTranslate2 +
        # downloads model weights on first use). We construct the
        # provider eagerly during config validation, but defer the
        # actual model load until the first ``transcribe`` call so
        # tests + import-time checks stay fast.
        self._model: Any = None

    def _load_model(self) -> Any:
        """Lazy-load the underlying ``WhisperModel`` on first use.

        Raises :class:`ImportError` with an actionable install hint
        when ``faster-whisper`` is missing — the caller (tool
        dispatcher) catches this and surfaces it as a structured
        ``ToolResult(ok=False, error=...)``.
        """
        if self._model is not None:
            return self._model
        try:
            from faster_whisper import WhisperModel  # type: ignore
        except ImportError as exc:
            raise ImportError(_INSTALL_HINT) from exc
        # ``download_root=None`` lets faster-whisper use its default
        # huggingface cache (``~/.cache/huggingface/hub``). Users who
        # want a specific dir can set ``HF_HOME`` env var.
        self._model = WhisperModel(
            self.model_name,
            device=self.device,
            compute_type=self.compute_type,
        )
        return self._model

    async def transcribe(self, audio_bytes: bytes) -> str:
        """Transcribe raw audio bytes (any decodable container).

        Runs the actual decode + transcribe in a worker thread because
        faster-whisper's API is sync and CPU-bound; doing it inline
        would stall the daemon's event loop.
        """
        if not audio_bytes:
            return ""

        def _do_transcribe() -> str:
            model = self._load_model()
            # faster-whisper accepts a file-like with ``.read``; the
            # underlying ctranslate2 backend pulls it through ffmpeg
            # for decoding. No temp file needed.
            buf = io.BytesIO(audio_bytes)
            segments, _info = model.transcribe(
                buf,
                language=self.language,
                vad_filter=True,  # drop silence segments — saves time
            )
            # ``segments`` is a generator; materialising it actually
            # runs the transcription.
            text_parts = [seg.text for seg in segments]
            return "".join(text_parts).strip()

        return await asyncio.to_thread(_do_transcribe)
