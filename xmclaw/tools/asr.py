"""ASR (Automatic Speech Recognition) tool — OpenAI Whisper API + local fallback."""
from __future__ import annotations
import base64
import tempfile
from pathlib import Path
from xmclaw.tools.base import Tool
from xmclaw.utils.log import logger


class ASRTool(Tool):
    """Transcribe audio to text using Whisper API or local model."""

    name = "asr"
    description = (
        "Transcribe audio file to text. Accepts a local file path or base64-encoded audio data. "
        "Supports mp3, mp4, mpeg, mpga, m4a, wav, webm formats."
    )
    parameters = {
        "audio": {
            "type": "string",
            "description": "Local file path OR base64-encoded audio data (prefix with 'data:audio/..;base64,')",
        },
        "language": {
            "type": "string",
            "description": "Optional ISO-639-1 language code, e.g. 'zh' or 'en'. Auto-detects if omitted.",
        },
        "prompt": {
            "type": "string",
            "description": "Optional hint text to improve transcription accuracy.",
        },
    }

    async def execute(self, audio: str, language: str = "", prompt: str = "") -> str:
        # Resolve audio to a file path
        audio_path = await self._resolve_audio(audio)
        if not audio_path:
            return "[ASR Error: Could not resolve audio input]"

        # Try OpenAI Whisper API first
        result = await self._transcribe_openai(audio_path, language, prompt)
        if result is not None:
            return result

        # Fallback: local whisper
        result = await self._transcribe_local(audio_path, language)
        if result is not None:
            return result

        # Fallback: SpeechRecognition library
        result = await self._transcribe_speech_recognition(audio_path, language)
        if result is not None:
            return result

        return "[ASR Error: No transcription backend available. Install openai, whisper, or SpeechRecognition.]"

    async def _resolve_audio(self, audio: str) -> Path | None:
        if audio.startswith("data:audio") or audio.startswith("data:video"):
            # base64 data URI
            try:
                header, b64 = audio.split(",", 1)
                ext = "mp3"
                for fmt in ["mp3", "wav", "m4a", "webm", "mp4", "ogg"]:
                    if fmt in header:
                        ext = fmt
                        break
                data = base64.b64decode(b64)
                tmp = tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False)
                tmp.write(data)
                tmp.close()
                return Path(tmp.name)
            except Exception as e:
                logger.error("asr_base64_decode_failed", error=str(e))
                return None
        path = Path(audio)
        if path.exists():
            return path
        return None

    async def _transcribe_openai(self, path: Path, language: str, prompt: str) -> str | None:
        try:
            from openai import AsyncOpenAI
            from xmclaw.daemon.config import DaemonConfig
            cfg = DaemonConfig.load()
            llm_cfg = cfg.llm or {}
            # Try openai provider first, then anthropic provider's key won't work for Whisper
            oai_cfg = llm_cfg.get("openai", {})
            api_key = oai_cfg.get("api_key", "")
            base_url = oai_cfg.get("base_url", "https://api.openai.com/v1")
            if not api_key:
                return None
            client = AsyncOpenAI(api_key=api_key, base_url=base_url)
            kwargs = {"model": "whisper-1"}
            if language:
                kwargs["language"] = language
            if prompt:
                kwargs["prompt"] = prompt
            with open(path, "rb") as f:
                resp = await client.audio.transcriptions.create(file=f, **kwargs)
            return resp.text
        except ImportError:
            return None
        except Exception as e:
            logger.warning("asr_openai_failed", error=str(e))
            return None

    async def _transcribe_local(self, path: Path, language: str) -> str | None:
        try:
            import whisper  # type: ignore
            import asyncio
            loop = asyncio.get_event_loop()
            model = await loop.run_in_executor(None, lambda: whisper.load_model("base"))
            opts = {}
            if language:
                opts["language"] = language
            result = await loop.run_in_executor(None, lambda: model.transcribe(str(path), **opts))
            return result.get("text", "")
        except ImportError:
            return None
        except Exception as e:
            logger.warning("asr_local_failed", error=str(e))
            return None

    async def _transcribe_speech_recognition(self, path: Path, language: str) -> str | None:
        """Transcribe using SpeechRecognition library (Google API or offline Sphinx)."""
        try:
            import speech_recognition as sr
            import asyncio
            
            r = sr.Recognizer()
            with sr.AudioFile(str(path)) as source:
                audio = r.record(source)
            
            # Try Google API (online)
            try:
                lang = language if language else "zh-CN"
                text = r.recognize_google(audio, language=lang)
                return text
            except Exception:
                pass
            
            # Try Sphinx offline (if pocketsphinx is installed)
            try:
                lang = language if language else "zh-CN"
                text = r.recognize_sphinx(audio, language=lang)
                return text
            except Exception:
                pass
            
            return None
        except ImportError:
            return None
        except Exception as e:
            logger.warning("asr_speech_recognition_failed", error=str(e))
            return None
