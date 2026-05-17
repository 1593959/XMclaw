"""Unit tests for MediaTools.

Mocks ``sounddevice`` / ``cv2`` / TTS / STT providers so the tests
run on CI / headless boxes without a real mic, camera, or speaker.
Live verification ("agent actually records you saying X") is left
to manual ``xmclaw chat`` runs on a desktop with a mic.
"""
from __future__ import annotations

import asyncio
import json
import sys
import types
import wave
from pathlib import Path
from typing import Any

import pytest

from xmclaw.core.ir import ToolCall


def _call(name: str, args: dict[str, Any] | None = None) -> ToolCall:
    return ToolCall(
        id=f"t-{name}",
        name=name,
        args=args or {},
        provenance="synthetic",
    )


def _json(result):
    return json.loads(result.content) if result.ok else None


# ── Fixtures ──────────────────────────────────────────────────────


@pytest.fixture
def fake_sounddevice(monkeypatch: pytest.MonkeyPatch):
    """Mock sounddevice + numpy so we can run mic tests without a mic."""
    sd_mod = types.ModuleType("sounddevice")
    sd_mod.calls: list[tuple[str, tuple, dict]] = []

    class _FakeArray:
        """Minimal numpy.ndarray stand-in for what we need:
        ``np.asarray(...)`` + ``.tobytes()``. Stores frame count so
        the WAV we write has the right size."""

        def __init__(self, frames: int, channels: int = 1):
            self._size = frames * channels * 2  # int16 = 2 bytes

        def tobytes(self) -> bytes:
            return b"\x00" * self._size

    last_rec = {}

    def _rec(frames, samplerate, channels, dtype):
        last_rec["frames"] = frames
        last_rec["sample_rate"] = samplerate
        last_rec["channels"] = channels
        sd_mod.calls.append(("rec", (frames,), {
            "samplerate": samplerate, "channels": channels, "dtype": dtype,
        }))
        return _FakeArray(frames, channels)

    def _wait():
        sd_mod.calls.append(("wait", (), {}))

    sd_mod.rec = _rec
    sd_mod.wait = _wait
    sd_mod._last_rec = last_rec

    np_mod = types.ModuleType("numpy")
    np_mod.int16 = "int16"  # type tag

    def _asarray(arr, dtype=None):
        return arr  # passthrough — our _FakeArray already has .tobytes()

    np_mod.asarray = _asarray

    monkeypatch.setitem(sys.modules, "sounddevice", sd_mod)
    monkeypatch.setitem(sys.modules, "numpy", np_mod)
    return sd_mod


@pytest.fixture
def fake_cv2(monkeypatch: pytest.MonkeyPatch):
    """Mock cv2 so camera tests run without a real webcam."""
    cv2_mod = types.ModuleType("cv2")
    cv2_mod.IMWRITE_JPEG_QUALITY = 1
    cv2_mod.opens: dict[int, bool] = {0: True, 1: True, 2: False, 3: False, 4: False, 5: False}

    class _FakeCap:
        def __init__(self, idx):
            self.idx = idx

        def isOpened(self) -> bool:
            return cv2_mod.opens.get(self.idx, False)

        def read(self):
            if not self.isOpened():
                return (False, None)

            class _Frame:
                shape = (480, 640, 3)  # h, w, channels
            return (True, _Frame())

        def release(self):
            pass

    cv2_mod.VideoCapture = _FakeCap

    def _imencode(ext, frame, params):
        return (True, _FakeBuf())

    class _FakeBuf:
        def tobytes(self) -> bytes:
            # Minimal JPEG header — enough that path.write_bytes succeeds.
            return b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"x" * 100

    cv2_mod.imencode = _imencode
    monkeypatch.setitem(sys.modules, "cv2", cv2_mod)
    return cv2_mod


class _FakeSTT:
    def __init__(self):
        self.last_bytes = None

    async def transcribe(self, audio_bytes: bytes) -> str:
        self.last_bytes = audio_bytes
        return "hello world — recognized"


class _FakeTTS:
    def __init__(self):
        self.last_text = None
        self.last_voice = None

    async def synthesize(self, text: str, voice: str = "default") -> bytes:
        self.last_text = text
        self.last_voice = voice
        return b"FAKE_MP3_BYTES" + text.encode("utf-8")[:64]


@pytest.fixture
def tools(tmp_path: Path):
    from xmclaw.providers.tool.media import MediaTools
    return MediaTools(
        media_dir=tmp_path / "media",
        stt_provider=_FakeSTT(),
        tts_provider=_FakeTTS(),
        base64_size_cap=2 * 1024 * 1024,
    )


# ── list_tools ────────────────────────────────────────────────────


def test_lists_all_five_tools(tools):
    names = {s.name for s in tools.list_tools()}
    assert names == {
        "mic_record", "voice_listen", "speak",
        "camera_capture", "camera_list",
    }


# ── Mic ───────────────────────────────────────────────────────────


async def test_mic_record_writes_wav(tools, fake_sounddevice):
    r = await tools.invoke(_call("mic_record", {"duration": 2.0}))
    assert r.ok, r.error
    p = _json(r)
    assert p["duration_s"] == 2.0
    assert p["sample_rate"] == 16000
    assert p["channels"] == 1
    wav_path = Path(p["path"])
    assert wav_path.is_file()
    # Validate WAV header round-trips through wave module
    with wave.open(str(wav_path), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getframerate() == 16000
        assert wf.getsampwidth() == 2  # int16
        # frames = duration * rate
        assert wf.getnframes() == int(2.0 * 16000)


async def test_mic_record_duration_cap(tools, fake_sounddevice):
    r = await tools.invoke(_call("mic_record", {"duration": 999.0}))
    assert not r.ok
    assert "60" in r.error


async def test_mic_record_missing_duration(tools, fake_sounddevice):
    r = await tools.invoke(_call("mic_record", {}))
    assert not r.ok
    assert "duration" in r.error.lower()


async def test_mic_record_invalid_channels(tools, fake_sounddevice):
    r = await tools.invoke(_call("mic_record", {"duration": 1.0, "channels": 5}))
    assert not r.ok
    assert "channels" in r.error


async def test_mic_record_without_sounddevice(tools, monkeypatch):
    monkeypatch.setitem(sys.modules, "sounddevice", None)
    r = await tools.invoke(_call("mic_record", {"duration": 1.0}))
    assert not r.ok
    assert "sounddevice" in r.error.lower()


# ── voice_listen ──────────────────────────────────────────────────


async def test_voice_listen_chain(tools, fake_sounddevice):
    """End-to-end: mic_record → STT.transcribe → return text."""
    r = await tools.invoke(_call("voice_listen", {"duration": 3.0}))
    assert r.ok, r.error
    p = _json(r)
    assert p["text"] == "hello world — recognized"
    assert p["duration_s"] == 3.0
    # STT got the audio bytes
    assert tools._stt.last_bytes is not None
    # WAV file is non-empty
    assert Path(p["audio_path"]).stat().st_size > 0


async def test_voice_listen_no_stt_configured(tmp_path: Path, fake_sounddevice):
    """No stt_provider → graceful fail with install hint."""
    from xmclaw.providers.tool.media import MediaTools
    tools = MediaTools(media_dir=tmp_path / "m", stt_provider=None)
    r = await tools.invoke(_call("voice_listen", {"duration": 1.0}))
    assert not r.ok
    assert "voice.stt" in r.error or "STT" in r.error


# ── Speak ─────────────────────────────────────────────────────────


async def test_speak_synth_and_writes_mp3(tools, monkeypatch):
    """speak should TTS-synth + persist. We mock the playback layer
    so the test doesn't actually try to open audio out (no test
    environment has speakers wired up)."""

    # Stub playsound so the "playback" doesn't try to find real speakers.
    psound_mod = types.ModuleType("playsound")
    played_paths = []

    def _playsound(path, block=True):
        played_paths.append((path, block))

    psound_mod.playsound = _playsound
    monkeypatch.setitem(sys.modules, "playsound", psound_mod)

    r = await tools.invoke(_call("speak", {
        "text": "你好世界",
        "voice": "zh-CN-XiaoxiaoNeural",
    }))
    assert r.ok, r.error
    p = _json(r)
    assert p["chars"] == 4
    assert p["voice"] == "zh-CN-XiaoxiaoNeural"
    assert p["played"] is True
    assert Path(p["path"]).stat().st_size > 0
    # TTS provider saw the right input
    assert tools._tts.last_text == "你好世界"
    assert tools._tts.last_voice == "zh-CN-XiaoxiaoNeural"
    # Playsound got called
    assert len(played_paths) == 1


async def test_speak_no_tts_configured(tmp_path: Path):
    from xmclaw.providers.tool.media import MediaTools
    tools = MediaTools(media_dir=tmp_path / "m", tts_provider=None)
    r = await tools.invoke(_call("speak", {"text": "hi"}))
    assert not r.ok
    assert "voice.tts" in r.error or "TTS" in r.error


async def test_speak_empty_text(tools):
    r = await tools.invoke(_call("speak", {"text": "  "}))
    assert not r.ok
    assert "text" in r.error.lower()


async def test_speak_fire_and_forget(tools, monkeypatch):
    """await_playback=false → tool returns immediately, doesn't wait
    for actual playback to finish."""
    psound_mod = types.ModuleType("playsound")
    psound_mod.playsound = lambda *a, **kw: None
    monkeypatch.setitem(sys.modules, "playsound", psound_mod)

    r = await tools.invoke(_call("speak", {
        "text": "background", "await_playback": False,
    }))
    assert r.ok
    p = _json(r)
    assert p["played"] is True


# ── Camera ────────────────────────────────────────────────────────


async def test_camera_capture(tools, fake_cv2):
    """B-Vision: camera_capture default is NO base64 in the tool
    content — the frame rides on metadata.attach_image and becomes a
    real vision content block on the next turn."""
    r = await tools.invoke(_call("camera_capture", {"camera_index": 0}))
    assert r.ok, r.error
    p = _json(r)
    assert p["camera_index"] == 0
    assert p["size"] == [640, 480]
    assert Path(p["path"]).is_file()
    assert p.get("vision_attached") is True
    assert "base64_jpg" not in p
    assert r.metadata.get("attach_image") == p["path"]


async def test_camera_capture_opt_in_base64(tools, fake_cv2):
    """Explicit include_base64=True still works for non-LLM callers."""
    r = await tools.invoke(_call(
        "camera_capture", {"camera_index": 0, "include_base64": True},
    ))
    assert r.ok, r.error
    p = _json(r)
    assert "base64_jpg" in p


async def test_camera_capture_unopened_index(tools, fake_cv2):
    r = await tools.invoke(_call("camera_capture", {"camera_index": 3}))
    assert not r.ok
    assert "did not open" in r.error or "index" in r.error.lower()


async def test_camera_list_probes(tools, fake_cv2):
    r = await tools.invoke(_call("camera_list"))
    assert r.ok
    p = _json(r)
    # Fixture: index 0 and 1 open, 2-5 don't
    assert p["count"] == 2
    assert p["camera_indexes"] == [0, 1]


async def test_camera_capture_without_cv2(tools, monkeypatch):
    monkeypatch.setitem(sys.modules, "cv2", None)
    r = await tools.invoke(_call("camera_capture"))
    assert not r.ok
    assert "opencv" in r.error.lower() or "cv2" in r.error


# ── Unknown tool ──────────────────────────────────────────────────


async def test_unknown_tool_name(tools):
    r = await tools.invoke(_call("teleport"))
    assert not r.ok
    assert "unknown tool" in r.error


# ── Factory integration: off-by-default ───────────────────────────


def test_factory_does_not_wire_when_media_disabled():
    """tools.media.enabled=false (or absent) → MediaTools NOT in
    agent's tool stack."""
    from xmclaw.daemon.factory import build_tools_from_config
    cfg = {
        "tools": {"allowed_dirs": [], "media": {"enabled": False}},
        "llm": {"provider": "anthropic", "api_key": "test"},
    }
    provider = build_tools_from_config(cfg)
    names = {s.name for s in provider.list_tools()}
    assert "mic_record" not in names
    assert "camera_capture" not in names


def test_factory_wires_when_media_enabled():
    from xmclaw.daemon.factory import build_tools_from_config
    cfg = {
        "tools": {"allowed_dirs": [], "media": {"enabled": True}},
        "llm": {"provider": "anthropic", "api_key": "test"},
    }
    provider = build_tools_from_config(cfg)
    names = {s.name for s in provider.list_tools()}
    assert "mic_record" in names
    assert "voice_listen" in names
    assert "speak" in names
    assert "camera_capture" in names
    assert "camera_list" in names


def test_factory_wires_voice_providers_when_configured():
    """voice.stt block → WhisperSTT instance; voice.tts → EdgeTTS.
    BuiltinTools should advertise voice_transcribe / voice_synthesize."""
    from xmclaw.daemon.factory import build_tools_from_config
    cfg = {
        "tools": {"allowed_dirs": []},
        "voice": {
            "stt": {"model": "tiny", "device": "cpu"},
            "tts": {"voice": "en-US-AriaNeural"},
        },
        "llm": {"provider": "anthropic", "api_key": "test"},
    }
    provider = build_tools_from_config(cfg)
    names = {s.name for s in provider.list_tools()}
    assert "voice_transcribe" in names
    assert "voice_synthesize" in names
