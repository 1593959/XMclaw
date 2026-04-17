"""TTS (Text-to-Speech) tool — OpenAI TTS API + edge-tts fallback."""
from __future__ import annotations
import base64
import tempfile
from pathlib import Path
from xmclaw.tools.base import Tool
from xmclaw.utils.log import logger


OPENAI_VOICES = ["alloy", "echo", "fable", "onyx", "nova", "shimmer"]


class TTSTool(Tool):
    """Convert text to speech audio. Returns path to audio file or base64 data URI."""

    name = "tts"
    description = (
        "Convert text to speech. Returns the path to a generated MP3 file and a base64 data URI "
        "for immediate playback. Powered by OpenAI TTS or edge-tts."
    )
    parameters = {
        "text": {
            "type": "string",
            "description": "Text to convert to speech.",
        },
        "voice": {
            "type": "string",
            "description": f"Voice name. OpenAI options: {', '.join(OPENAI_VOICES)}. Edge-TTS: use BCP-47 voice name like 'zh-CN-XiaoxiaoNeural'.",
        },
        "speed": {
            "type": "number",
            "description": "Speech speed multiplier, 0.25–4.0. Default 1.0.",
        },
    }

    async def execute(self, text: str, voice: str = "alloy", speed: float = 1.0) -> str:
        if not text.strip():
            return "[TTS Error: Empty text]"

        audio_path, b64_uri = await self._synthesize_openai(text, voice, speed)
        if audio_path:
            return f"[TTS OK] File: {audio_path}\n{b64_uri}"

        audio_path, b64_uri = await self._synthesize_edge(text, voice)
        if audio_path:
            return f"[TTS OK] File: {audio_path}\n{b64_uri}"

        return "[TTS Error: No TTS backend available. Install openai or edge-tts.]"

    async def _synthesize_openai(self, text: str, voice: str, speed: float) -> tuple[str, str]:
        try:
            from openai import AsyncOpenAI
            from xmclaw.daemon.config import DaemonConfig
            cfg = DaemonConfig.load()
            oai_cfg = (cfg.llm or {}).get("openai", {})
            api_key = oai_cfg.get("api_key", "")
            base_url = oai_cfg.get("base_url", "https://api.openai.com/v1")
            if not api_key:
                return "", ""
            client = AsyncOpenAI(api_key=api_key, base_url=base_url)
            v = voice if voice in OPENAI_VOICES else "alloy"
            sp = max(0.25, min(4.0, float(speed)))
            response = await client.audio.speech.create(
                model="tts-1",
                voice=v,
                input=text,
                speed=sp,
            )
            return self._save_audio(response.content, "mp3")
        except ImportError:
            return "", ""
        except Exception as e:
            logger.warning("tts_openai_failed", error=str(e))
            return "", ""

    async def _synthesize_edge(self, text: str, voice: str) -> tuple[str, str]:
        try:
            import edge_tts  # type: ignore
            import asyncio
            v = voice if voice else "zh-CN-XiaoxiaoNeural"
            communicate = edge_tts.Communicate(text, v)
            chunks = []
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    chunks.append(chunk["data"])
            audio_bytes = b"".join(chunks)
            return self._save_audio(audio_bytes, "mp3")
        except ImportError:
            return "", ""
        except Exception as e:
            logger.warning("tts_edge_failed", error=str(e))
            return "", ""

    def _save_audio(self, audio_bytes: bytes, ext: str) -> tuple[str, str]:
        try:
            tmp = tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False)
            tmp.write(audio_bytes)
            tmp.close()
            b64 = base64.b64encode(audio_bytes).decode("utf-8")
            data_uri = f"data:audio/{ext};base64,{b64}"
            return tmp.name, data_uri
        except Exception as e:
            logger.error("tts_save_failed", error=str(e))
            return "", ""
