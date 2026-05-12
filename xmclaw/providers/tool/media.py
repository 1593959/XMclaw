"""MediaTools — live microphone + camera + audio playback.

2026-05-12. Closes the three I/O gaps that ``voice_transcribe`` /
``voice_synthesize`` left open:

  * **mic_record** — record N seconds from default microphone → WAV
    file. Pair with ``voice_transcribe`` for offline STT round-trips.
  * **voice_listen** — convenience combo: record from mic, hand bytes
    to the configured STT provider, return transcript. One tool call
    instead of two.
  * **speak** — TTS + immediate playback through default speakers in
    one step. ``voice_synthesize`` saves a file; ``speak`` actually
    makes a noise come out.
  * **camera_capture** — grab one still frame from the default
    webcam → JPG file. Returns size + path + optional base64 (capped).
  * **camera_list** — enumerate available webcams (indexes that
    actually open).

Why a separate provider (not bolted onto BuiltinTools)?
=======================================================

The deps here (``sounddevice`` / ``opencv-python`` / ``simpleaudio``)
are heavier than the BuiltinTools surface and have OS-specific
runtime requirements (PortAudio for sounddevice, V4L2 / DirectShow /
AVFoundation for opencv). Splitting into a dedicated optional
provider keeps the default ``xmclaw`` install slim — most agents
that don't actually drive media don't pay the import cost.

Safety
======

* **Provider-level off by default**: ``tools.media.enabled`` in
  ``daemon/config.json`` must be ``true``.
* **Recording time-bound**: ``mic_record`` caps duration at 60 s per
  call. The LLM can chain calls for longer captures but can't
  silently leave the mic open.
* **Camera caches off**: ``camera_capture`` opens the device, grabs
  one frame, closes immediately. No persistent camera handle.
* **No live stream**: this module deliberately does NOT expose a
  continuous "watch the mic" / "watch the camera" surface. Those
  go through the cognition layer's perception watchers when added,
  not as agent-callable tools (different threat model).
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
import wave
from pathlib import Path
from typing import Any

from xmclaw.core.ir import ToolCall, ToolResult, ToolSpec
from xmclaw.providers.tool.base import ToolProvider


# ── Defaults / caps ───────────────────────────────────────────────────


_DEFAULT_MEDIA_DIR = "media"   # under data_dir / v2
_MAX_RECORD_S = 60.0           # hard cap per call
_DEFAULT_SAMPLE_RATE = 16000   # 16 kHz mono — what Whisper wants
_DEFAULT_CHANNELS = 1
_BASE64_DEFAULT_CAP = 512 * 1024


# ── Specs ─────────────────────────────────────────────────────────────


_MIC_RECORD_SPEC = ToolSpec(
    name="mic_record",
    description=(
        "Record audio from the default microphone for N seconds. "
        "Returns {path, duration_s, sample_rate, channels, "
        "audio_bytes}. WAV format (16-bit PCM, 16 kHz mono by "
        "default — matches what Whisper / faster-whisper expects "
        "natively).\n\n"
        "``duration`` must be 0.1-60 seconds (per-call cap). For "
        "longer captures, chain multiple calls (the agent should "
        "know to do this so the user can interrupt between segments).\n\n"
        "Needs ``sounddevice`` (cross-platform PortAudio wrapper). "
        "On Linux also: ``sudo apt install libportaudio2``."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "duration": {
                "type": "number",
                "description": "Seconds to record (0.1-60).",
            },
            "sample_rate": {
                "type": "integer",
                "description": "Hz, default 16000 (Whisper-native).",
            },
            "channels": {
                "type": "integer",
                "description": "1 = mono (default), 2 = stereo.",
            },
        },
        "required": ["duration"],
    },
)

_VOICE_LISTEN_SPEC = ToolSpec(
    name="voice_listen",
    description=(
        "Record from the mic AND transcribe in one call. Combines "
        "``mic_record`` + the configured STT provider so the agent "
        "can do 'listen for 5 seconds and tell me what they said' "
        "in a single hop instead of two.\n\n"
        "Returns {text, audio_path, duration_s, audio_bytes}. "
        "Requires both ``sounddevice`` AND a wired STT (config "
        "``voice.stt`` + ``pip install 'xmclaw[voice-stt]'``)."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "duration": {
                "type": "number",
                "description": "Seconds to listen (0.1-60). Default 5.",
            },
        },
    },
)

_SPEAK_SPEC = ToolSpec(
    name="speak",
    description=(
        "Synthesize text with the configured TTS provider AND play "
        "through default speakers immediately. Use this for live "
        "voice responses; use ``voice_synthesize`` when you only "
        "need the audio file (e.g. saving a podcast clip).\n\n"
        "Returns {chars, voice, played, path}. Blocks until "
        "playback completes by default — pass ``await_playback: "
        "false`` to fire-and-forget."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "voice": {
                "type": "string",
                "description": "TTS provider voice id (e.g. "
                               "``en-US-AriaNeural``). Default is "
                               "what the provider was constructed with.",
            },
            "await_playback": {
                "type": "boolean",
                "description": "Wait for playback to finish before "
                               "returning. Default true.",
            },
        },
        "required": ["text"],
    },
)

_CAMERA_CAPTURE_SPEC = ToolSpec(
    name="camera_capture",
    description=(
        "Grab one still frame from the default webcam (or a "
        "specific index). Returns {path, size: [w, h], "
        "camera_index, base64_jpg (capped)}. Opens the camera, "
        "captures one frame, releases immediately — no persistent "
        "handle.\n\n"
        "Needs ``opencv-python`` (~50 MB). First call on macOS "
        "triggers the camera-permission OS prompt the first time. "
        "On Linux requires ``/dev/video*`` device + user in "
        "``video`` group."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "camera_index": {
                "type": "integer",
                "description": "0 = default. Use camera_list to "
                               "find others.",
            },
            "include_base64": {
                "type": "boolean",
                "description": "Return base64 inline. Default true.",
            },
        },
    },
)

_CAMERA_LIST_SPEC = ToolSpec(
    name="camera_list",
    description=(
        "Probe camera indexes 0-5 and return the ones that actually "
        "open. Use to disambiguate when the user has multiple "
        "webcams (built-in + USB). Each open-probe is brief but "
        "costs ~0.5s of latency × number of indexes; that's why "
        "we cap the scan at 6."
    ),
    parameters_schema={"type": "object", "properties": {}},
)


# ── Provider ──────────────────────────────────────────────────────────


class MediaTools(ToolProvider):
    """Live mic + camera + audio playback.

    Constructor params:

    * ``media_dir`` — where recordings + frames are written.
      Default = ``<data_dir>/v2/media``.
    * ``stt_provider`` / ``tts_provider`` — duck-typed objects with
      ``transcribe(bytes) -> str`` / ``synthesize(text, voice) ->
      bytes`` resp. Hand the same providers BuiltinTools got so
      ``voice_listen`` / ``speak`` share state with
      ``voice_transcribe`` / ``voice_synthesize``.
    * ``base64_size_cap`` — bytes returned inline as base64.
      Default 512 KB. Larger files keep ``path`` and drop the
      inline base64.
    """

    def __init__(
        self,
        *,
        media_dir: str | Path | None = None,
        stt_provider: Any = None,
        tts_provider: Any = None,
        base64_size_cap: int = _BASE64_DEFAULT_CAP,
    ) -> None:
        if media_dir is None:
            from xmclaw.utils.paths import data_dir
            media_dir = data_dir() / "v2" / _DEFAULT_MEDIA_DIR
        self._media_dir = Path(media_dir)
        self._stt = stt_provider
        self._tts = tts_provider
        self._base64_cap = int(base64_size_cap)

    def list_tools(self) -> list[ToolSpec]:
        out = [_MIC_RECORD_SPEC, _SPEAK_SPEC, _CAMERA_CAPTURE_SPEC, _CAMERA_LIST_SPEC]
        # voice_listen needs both deps; we still list it unconditionally
        # so the LLM sees the capability — the install hint surfaces
        # at invoke time, not at list_tools.
        out.append(_VOICE_LISTEN_SPEC)
        return out

    async def invoke(self, call: ToolCall) -> ToolResult:
        t0 = time.perf_counter()
        name = call.name
        args = call.args or {}
        try:
            if name == "mic_record":      return await self._mic_record(call, t0, args)
            if name == "voice_listen":    return await self._voice_listen(call, t0, args)
            if name == "speak":           return await self._speak(call, t0, args)
            if name == "camera_capture":  return await self._camera_capture(call, t0, args)
            if name == "camera_list":     return await self._camera_list(call, t0)
        except Exception as exc:  # noqa: BLE001 — surface, never propagate
            return _fail(call, t0, f"{type(exc).__name__}: {exc}")
        return _fail(call, t0, f"unknown tool: {name!r}")

    # ── Mic ────────────────────────────────────────────────────────

    async def _mic_record(
        self, call: ToolCall, t0: float, args: dict,
    ) -> ToolResult:
        try:
            duration = float(args["duration"])
        except (KeyError, TypeError, ValueError):
            return _fail(call, t0, "duration (number, 0.1-60) required")
        if not 0.1 <= duration <= _MAX_RECORD_S:
            return _fail(
                call, t0,
                f"duration must be 0.1-{_MAX_RECORD_S}s "
                f"(got {duration}); chain calls for longer captures",
            )
        sample_rate = int(args.get("sample_rate", _DEFAULT_SAMPLE_RATE))
        channels = int(args.get("channels", _DEFAULT_CHANNELS))
        if channels not in (1, 2):
            return _fail(call, t0, "channels must be 1 (mono) or 2 (stereo)")

        try:
            import sounddevice as sd  # type: ignore
            import numpy as np  # type: ignore
        except ImportError as exc:
            return _fail(call, t0, _sounddevice_install_hint(exc))

        path = self._mkpath("rec", "wav")

        def _record_and_save() -> dict[str, Any]:
            # sounddevice.rec returns once recording is finished
            # (with .wait()). 16-bit PCM keeps WAV compatible with
            # Whisper / faster-whisper out of the box.
            audio = sd.rec(
                frames=int(duration * sample_rate),
                samplerate=sample_rate,
                channels=channels,
                dtype="int16",
            )
            sd.wait()
            audio_np = np.asarray(audio, dtype=np.int16)
            with wave.open(str(path), "wb") as wf:
                wf.setnchannels(channels)
                wf.setsampwidth(2)  # int16 = 2 bytes
                wf.setframerate(sample_rate)
                wf.writeframes(audio_np.tobytes())
            return {
                "path": str(path),
                "duration_s": duration,
                "sample_rate": sample_rate,
                "channels": channels,
                "audio_bytes": path.stat().st_size,
            }

        try:
            result = await asyncio.to_thread(_record_and_save)
        except Exception as exc:  # noqa: BLE001
            return _fail(
                call, t0,
                f"sounddevice record failed: {type(exc).__name__}: {exc} "
                "(check default input device + permissions)",
            )
        return _ok(call, t0, json.dumps(result, ensure_ascii=False))

    async def _voice_listen(
        self, call: ToolCall, t0: float, args: dict,
    ) -> ToolResult:
        if self._stt is None:
            return _fail(
                call, t0,
                "voice_listen needs an STT provider — wire ``voice.stt`` "
                "in daemon/config.json + ``pip install 'xmclaw[voice-stt]'``",
            )
        duration = float(args.get("duration", 5.0))
        if not 0.1 <= duration <= _MAX_RECORD_S:
            return _fail(
                call, t0,
                f"duration must be 0.1-{_MAX_RECORD_S}s",
            )
        # Reuse mic_record under the hood — same wav file shape.
        sub_args = {"duration": duration}
        sub_call = ToolCall(
            id=call.id + "-rec",
            name="mic_record",
            args=sub_args,
            provenance=call.provenance,
            session_id=call.session_id,
        )
        rec_result = await self._mic_record(sub_call, t0, sub_args)
        if not rec_result.ok:
            return _fail(call, t0, f"mic capture failed: {rec_result.error}")
        rec_payload = json.loads(rec_result.content)
        wav_path = Path(rec_payload["path"])

        try:
            audio_bytes = wav_path.read_bytes()
        except OSError as exc:
            return _fail(call, t0, f"read recording failed: {exc}")

        try:
            text = await self._stt.transcribe(audio_bytes)
        except ImportError as exc:
            return _fail(call, t0, str(exc))
        except Exception as exc:  # noqa: BLE001
            return _fail(
                call, t0,
                f"STT failed: {type(exc).__name__}: {exc}",
            )

        return _ok(call, t0, json.dumps({
            "text": text,
            "audio_path": str(wav_path),
            "duration_s": duration,
            "audio_bytes": len(audio_bytes),
        }, ensure_ascii=False))

    # ── Speak ──────────────────────────────────────────────────────

    async def _speak(
        self, call: ToolCall, t0: float, args: dict,
    ) -> ToolResult:
        if self._tts is None:
            return _fail(
                call, t0,
                "speak needs a TTS provider — wire ``voice.tts`` in "
                "daemon/config.json + ``pip install 'xmclaw[voice-tts]'``",
            )
        text = args.get("text")
        if not isinstance(text, str) or not text.strip():
            return _fail(call, t0, "text (non-empty string) required")
        voice = str(args.get("voice", "default"))
        await_playback = bool(args.get("await_playback", True))

        # 1. Synthesize.
        try:
            audio_bytes = await self._tts.synthesize(text, voice=voice)
        except ImportError as exc:
            return _fail(call, t0, str(exc))
        except Exception as exc:  # noqa: BLE001
            return _fail(
                call, t0,
                f"TTS synth failed: {type(exc).__name__}: {exc}",
            )

        # 2. Persist (so caller can replay / debug).
        path = self._mkpath("tts", "mp3")
        try:
            path.write_bytes(audio_bytes)
        except OSError as exc:
            return _fail(call, t0, f"write tts file failed: {exc}")

        # 3. Play. Two backends in order of preference:
        #    a) ``simpleaudio`` — pure-Python, wave-only (we'd need
        #       to decode mp3 → wav first; skip).
        #    b) ``playsound`` — cross-platform, mp3-aware, simple
        #       blocking call. Fall through to mpv / system "open"
        #       if neither installed.
        played = False
        play_err: str | None = None

        def _do_play() -> None:
            nonlocal played, play_err
            try:
                from playsound import playsound  # type: ignore
                playsound(str(path), block=True)
                played = True
                return
            except ImportError:
                pass
            except Exception as exc:  # noqa: BLE001
                play_err = f"playsound: {exc}"
            # Fallback: OS-level open. Doesn't BLOCK reliably but at
            # least makes a sound on most systems.
            import platform
            try:
                if platform.system() == "Windows":
                    import os as _os
                    _os.startfile(str(path))  # type: ignore[attr-defined]
                    played = True
                elif platform.system() == "Darwin":
                    import subprocess
                    subprocess.run(["afplay", str(path)], check=False)
                    played = True
                else:  # Linux
                    import subprocess
                    subprocess.run(["aplay", str(path)], check=False)
                    played = True
            except Exception as exc:  # noqa: BLE001
                play_err = (play_err or "") + f" | os-open: {exc}"

        if await_playback:
            await asyncio.to_thread(_do_play)
        else:
            # Fire-and-forget: schedule but don't await.
            asyncio.create_task(asyncio.to_thread(_do_play))
            played = True  # optimistic — caller said don't wait

        return _ok(call, t0, json.dumps({
            "chars": len(text),
            "voice": voice,
            "played": played,
            "path": str(path),
            "play_error": play_err,
            "audio_bytes": len(audio_bytes),
        }, ensure_ascii=False))

    # ── Camera ─────────────────────────────────────────────────────

    async def _camera_capture(
        self, call: ToolCall, t0: float, args: dict,
    ) -> ToolResult:
        try:
            import cv2  # type: ignore
        except ImportError as exc:
            return _fail(call, t0, _opencv_install_hint(exc))
        idx = int(args.get("camera_index", 0))
        # B-Vision: default OFF. hop_loop attaches the JPEG as a real
        # vision content block via ``metadata.attach_image`` — same
        # migration as screen_capture / image_read.
        include_b64 = bool(args.get("include_base64", False))
        path = self._mkpath(f"cam{idx}", "jpg")

        def _capture() -> tuple[int, int]:
            cap = cv2.VideoCapture(idx)
            try:
                if not cap.isOpened():
                    raise RuntimeError(
                        f"camera index {idx} did not open "
                        "(check permissions + that no other app is using it)",
                    )
                # Some webcams need a warm-up frame to expose
                # correctly. Discard the first frame, use the second.
                cap.read()
                ok, frame = cap.read()
                if not ok or frame is None:
                    raise RuntimeError(
                        f"camera index {idx} opened but read() returned empty",
                    )
                # Encode to JPG with quality 90 (good for vision LLM
                # input, ~50-150 KB for a typical 720p frame).
                ok2, buf = cv2.imencode(
                    ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 90],
                )
                if not ok2:
                    raise RuntimeError("cv2.imencode failed")
                path.write_bytes(buf.tobytes())
                h, w = frame.shape[:2]
                return (int(w), int(h))
            finally:
                cap.release()

        try:
            (w, h) = await asyncio.to_thread(_capture)
        except RuntimeError as exc:
            return _fail(call, t0, str(exc))
        except Exception as exc:  # noqa: BLE001
            return _fail(
                call, t0,
                f"camera capture failed: {type(exc).__name__}: {exc}",
            )

        result: dict[str, Any] = {
            "path": str(path),
            "size": [w, h],
            "camera_index": idx,
            "vision_attached": True,
        }
        if include_b64:
            try:
                raw = path.read_bytes()
                if len(raw) <= self._base64_cap:
                    result["base64_jpg"] = base64.b64encode(raw).decode("ascii")
                else:
                    result["base64_omitted"] = (
                        f"{len(raw)} bytes > cap {self._base64_cap}"
                    )
            except OSError as exc:
                result["base64_omitted"] = f"read failed: {exc}"
        return ToolResult(
            call_id=call.id, ok=True,
            content=json.dumps(result, ensure_ascii=False),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
            metadata={"attach_image": str(path)},
        )

    async def _camera_list(self, call: ToolCall, t0: float) -> ToolResult:
        try:
            import cv2  # type: ignore
        except ImportError as exc:
            return _fail(call, t0, _opencv_install_hint(exc))

        def _probe() -> list[int]:
            opened: list[int] = []
            for i in range(6):
                cap = cv2.VideoCapture(i)
                try:
                    if cap.isOpened():
                        opened.append(i)
                finally:
                    cap.release()
            return opened

        try:
            indexes = await asyncio.to_thread(_probe)
        except Exception as exc:  # noqa: BLE001
            return _fail(
                call, t0,
                f"camera enumeration failed: {type(exc).__name__}: {exc}",
            )
        return _ok(call, t0, json.dumps({
            "count": len(indexes),
            "camera_indexes": indexes,
        }))

    # ── Helpers ────────────────────────────────────────────────────

    def _mkpath(self, prefix: str, ext: str) -> Path:
        self._media_dir.mkdir(parents=True, exist_ok=True)
        name = f"{int(time.time())}_{prefix}_{int(time.time() * 1000) % 10000}.{ext}"
        return self._media_dir / name


# ── Helpers (module-level) ────────────────────────────────────────────


def _ok(call: ToolCall, t0: float, content: Any) -> ToolResult:
    return ToolResult(
        call_id=call.id, ok=True, content=content,
        latency_ms=(time.perf_counter() - t0) * 1000.0,
    )


def _fail(call: ToolCall, t0: float, err: str) -> ToolResult:
    return ToolResult(
        call_id=call.id, ok=False, content=None, error=err,
        latency_ms=(time.perf_counter() - t0) * 1000.0,
    )


def _sounddevice_install_hint(exc: ImportError) -> str:
    import platform
    extras = ""
    if platform.system() == "Linux":
        extras = " — on Linux also: ``sudo apt install libportaudio2``"
    elif platform.system() == "Darwin":
        extras = (
            " — on macOS first call triggers the Microphone permission "
            "OS prompt; allow it in System Settings → Privacy → Microphone."
        )
    return (
        f"mic_record / voice_listen needs ``sounddevice`` + ``numpy``: "
        f"pip install 'xmclaw[media]'{extras}\n  (underlying: {exc})"
    )


def _opencv_install_hint(exc: ImportError) -> str:
    import platform
    extras = ""
    if platform.system() == "Darwin":
        extras = (
            " — on macOS first call triggers the Camera permission "
            "OS prompt; allow it in System Settings → Privacy → Camera."
        )
    elif platform.system() == "Linux":
        extras = (
            " — on Linux: ensure /dev/video* exists and your user is "
            "in the ``video`` group (``sudo usermod -aG video $USER`` then re-login)."
        )
    return (
        f"camera_capture / camera_list needs ``opencv-python``: "
        f"pip install 'xmclaw[media]'{extras}\n  (underlying: {exc})"
    )


__all__ = ["MediaTools"]
