from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from pathlib import Path
from typing import Any

from xmclaw.core.ir import ToolCall, ToolResult
from xmclaw.providers.tool._helpers import _fail as _fail



class BuiltinToolsVoiceMixin:
    """Voice tools: voice_transcribe, voice_synthesize."""

    async def _voice_transcribe(
        self, call: ToolCall, t0: float,
    ) -> ToolResult:
        """B-388: hand audio bytes to the wired STT provider.

        Accepts EXACTLY ONE of ``audio_path`` (filesystem path) or
        ``audio_b64`` (base64-encoded). Both → reject (the caller's
        intent is ambiguous). Neither → reject (need an input).
        """
        import base64
        import json
        args = call.args or {}
        audio_path = args.get("audio_path")
        audio_b64 = args.get("audio_b64")
        has_path = isinstance(audio_path, str) and audio_path
        has_b64 = isinstance(audio_b64, str) and audio_b64
        if has_path and has_b64:
            return _fail(
                call, t0,
                "voice_transcribe accepts exactly one of audio_path / audio_b64",
            )
        if not (has_path or has_b64):
            return _fail(call, t0, "voice_transcribe needs an audio source")

        if has_path:
            try:
                p = Path(audio_path).expanduser().resolve()
            except (OSError, RuntimeError) as exc:
                return _fail(call, t0, f"audio_path resolve failed: {exc}")
            try:
                audio_bytes = p.read_bytes()
            except FileNotFoundError:
                return _fail(call, t0, f"audio file not found: {audio_path}")
            except PermissionError as exc:
                return _fail(call, t0, f"permission denied: {exc}")
            source = "audio_path"
        else:
            try:
                audio_bytes = base64.b64decode(audio_b64, validate=False)
            except Exception as exc:  # noqa: BLE001
                return _fail(call, t0, f"audio_b64 decode failed: {exc}")
            source = "audio_b64"

        try:
            text = await self._stt_provider.transcribe(audio_bytes)  # type: ignore[union-attr]
        except ImportError as exc:
            return _fail(call, t0, str(exc))
        except Exception as exc:  # noqa: BLE001
            return _fail(call, t0, f"{type(exc).__name__}: {exc}")

        payload = json.dumps(
            {"text": text, "audio_bytes": len(audio_bytes), "source": source},
            ensure_ascii=False,
        )
        return ToolResult(
            call_id=call.id,
            ok=True,
            content=payload,
            error=None,
            latency_ms=(time.monotonic() - t0) * 1000.0,
            side_effects=(),
        )

    async def _voice_synthesize(
        self, call: ToolCall, t0: float,
    ) -> ToolResult:
        """B-388: text → mp3 via the wired TTS provider.

        Writes the result to ``$XMC_DATA_DIR/v2/audio/<uuid>.mp3`` and
        records the resolved path on ``side_effects`` so the grader can
        verify the write actually landed.
        """
        import json
        import os
        import uuid
        args = call.args or {}
        text = args.get("text")
        voice = args.get("voice", "default")
        if not isinstance(text, str):
            return _fail(call, t0, "voice_synthesize: 'text' must be a string")
        if not isinstance(voice, str):
            voice = "default"

        try:
            audio_bytes = await self._tts_provider.synthesize(text, voice=voice)  # type: ignore[union-attr]
        except ImportError as exc:
            return _fail(call, t0, str(exc))
        except Exception as exc:  # noqa: BLE001
            return _fail(call, t0, f"{type(exc).__name__}: {exc}")

        # Patch A (2026-05-10): use paths.data_dir() (avoids the
        # same hand-rolled XMC_DATA_DIR fallback as builtin.py).
        from xmclaw.utils.paths import data_dir as _xmc_data_dir
        audio_dir = _xmc_data_dir() / "v2" / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)
        audio_path = audio_dir / f"{uuid.uuid4().hex}.mp3"
        audio_path.write_bytes(audio_bytes)

        payload = json.dumps(
            {"audio_path": str(audio_path), "bytes": len(audio_bytes)},
            ensure_ascii=False,
        )
        return ToolResult(
            call_id=call.id,
            ok=True,
            content=payload,
            error=None,
            latency_ms=(time.monotonic() - t0) * 1000.0,
            side_effects=(f"wrote audio to {audio_path.resolve()}",),
        )
