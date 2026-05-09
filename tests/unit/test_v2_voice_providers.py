"""B-388 — voice provider + voice-tool unit tests.

Pins:
  * ``base.py`` ABCs import without any optional dep installed
  * ``WhisperSTT`` / ``EdgeTTS`` import without their optional dep
    (lazy SDK import inside the constructor / first call)
  * Missing optional dep surfaces as ``ImportError`` with a
    ``pip install ...`` hint, NOT a generic ModuleNotFoundError
  * ``BuiltinTools`` hides voice tools when no provider is wired
  * With mock providers, ``voice_transcribe`` round-trips audio
    bytes (path + b64 modes) into the recognized text
  * With mock providers, ``voice_synthesize`` writes the audio file
    to ``~/.xmclaw/v2/audio/<uuid>.mp3`` (under the env-overridden
    XMC_DATA_DIR) and returns the resolved path in side_effects

These tests don't exercise the real faster-whisper / edge-tts
backends — that would need real audio + network. The unit lane stays
hermetic; the live smoke test in `python -c "..."` (per the AGENTS
spec) is the operator's reality check.
"""
from __future__ import annotations

import base64
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

from xmclaw.core.ir import ToolCall
from xmclaw.providers.tool.builtin import BuiltinTools


def _call(name: str, args: dict[str, Any] | None = None) -> ToolCall:
    return ToolCall(name=name, args=args or {}, provenance="synthetic")


# ── ABCs always import (no optional dep needed) ───────────────────────


def test_base_abcs_import_without_optional_deps() -> None:
    """``xmclaw.providers.voice.base`` is pure Python — should import
    cleanly regardless of whether faster-whisper / edge-tts is on the
    machine. The contract surface (STTProvider / TTSProvider) is
    what callers depend on, not the concrete impls."""
    from xmclaw.providers.voice.base import STTProvider, TTSProvider
    assert STTProvider is not None
    assert TTSProvider is not None
    # ABCs must reject direct instantiation.
    with pytest.raises(TypeError):
        STTProvider()  # type: ignore[abstract]
    with pytest.raises(TypeError):
        TTSProvider()  # type: ignore[abstract]


def test_top_level_package_reexports_classes() -> None:
    """``from xmclaw.providers.voice import WhisperSTT, EdgeTTS`` must
    work at import time even without the SDKs installed — the SDK
    import is deferred to the constructor / first call."""
    from xmclaw.providers.voice import EdgeTTS, STTProvider, TTSProvider, WhisperSTT
    assert WhisperSTT is not None
    assert EdgeTTS is not None
    # Subclass relationship — concrete impls satisfy the ABC.
    assert issubclass(WhisperSTT, STTProvider)
    assert issubclass(EdgeTTS, TTSProvider)


# ── lazy import: missing dep surfaces a clear hint ────────────────────


def test_whisper_constructor_succeeds_without_sdk() -> None:
    """Constructing :class:`WhisperSTT` must NOT load the SDK — that
    would defeat the lazy-import design (a fresh ``pip install
    xmclaw`` should boot without ``faster-whisper`` installed)."""
    from xmclaw.providers.voice import WhisperSTT
    # Any reasonable constructor args go.
    stt = WhisperSTT(model_name="tiny", device="cpu", compute_type="int8")
    assert stt.model_name == "tiny"
    # Internal model handle is None until first transcribe.
    assert stt._model is None  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_whisper_transcribe_without_sdk_raises_install_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the user calls ``transcribe`` without ``faster-whisper``
    installed, the ImportError must mention the install command so
    the operator can fix it without grep-ing source."""
    if importlib.util.find_spec("faster_whisper"):
        pytest.skip("faster-whisper is installed; this pin needs the missing-dep state")
    from xmclaw.providers.voice import WhisperSTT
    stt = WhisperSTT()
    with pytest.raises(ImportError) as exc:
        await stt.transcribe(b"\x00\x00\x00\x00")  # any non-empty bytes triggers load
    msg = str(exc.value)
    assert "faster-whisper" in msg
    # The hint must include an actionable pip install command.
    assert "pip install" in msg


@pytest.mark.asyncio
async def test_edge_tts_synthesize_without_sdk_raises_install_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same lazy-import contract for TTS: missing SDK becomes an
    ImportError with an install hint at first use."""
    if importlib.util.find_spec("edge_tts"):
        pytest.skip("edge-tts is installed; this pin needs the missing-dep state")
    from xmclaw.providers.voice import EdgeTTS
    tts = EdgeTTS()
    with pytest.raises(ImportError) as exc:
        await tts.synthesize("hello")
    msg = str(exc.value)
    assert "edge-tts" in msg
    assert "pip install" in msg


@pytest.mark.asyncio
async def test_whisper_transcribe_empty_bytes_short_circuits() -> None:
    """Empty audio bytes return the empty string — no SDK load, no
    error. Lets callers pre-check without paying the model-load cost
    on the silent / no-audio path."""
    from xmclaw.providers.voice import WhisperSTT
    stt = WhisperSTT()
    result = await stt.transcribe(b"")
    assert result == ""
    # Confirms NO SDK load happened.
    assert stt._model is None  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_edge_tts_synthesize_empty_text_short_circuits() -> None:
    """Empty text → empty bytes, no SDK load."""
    from xmclaw.providers.voice import EdgeTTS
    tts = EdgeTTS()
    result = await tts.synthesize("")
    assert result == b""


# ── BuiltinTools wiring ───────────────────────────────────────────────


def test_voice_tools_hidden_when_no_provider_wired() -> None:
    """Default BuiltinTools (no STT/TTS providers) must not advertise
    the voice tools. Otherwise the LLM gets a tool it can't actually
    invoke."""
    names = {t.name for t in BuiltinTools().list_tools()}
    assert "voice_transcribe" not in names
    assert "voice_synthesize" not in names


def test_voice_transcribe_advertised_when_stt_wired() -> None:
    class _MockSTT:
        async def transcribe(self, audio_bytes: bytes) -> str:  # noqa: ARG002
            return "hello"

    tools = BuiltinTools(stt_provider=_MockSTT())
    names = {t.name for t in tools.list_tools()}
    assert "voice_transcribe" in names
    # TTS still hidden — gating is per-direction.
    assert "voice_synthesize" not in names


def test_voice_synthesize_advertised_when_tts_wired() -> None:
    class _MockTTS:
        async def synthesize(self, text: str, voice: str = "default") -> bytes:  # noqa: ARG002
            return b"\xff\xfb\x90\x00"  # 4 mp3-ish bytes

    tools = BuiltinTools(tts_provider=_MockTTS())
    names = {t.name for t in tools.list_tools()}
    assert "voice_synthesize" in names
    assert "voice_transcribe" not in names


@pytest.mark.asyncio
async def test_voice_transcribe_with_audio_path(tmp_path: Path) -> None:
    """End-to-end: tool reads the file, hands bytes to the STT
    provider, returns the recognized text in a JSON-encoded payload
    that the LLM can parse."""
    captured: dict[str, Any] = {}

    class _MockSTT:
        async def transcribe(self, audio_bytes: bytes) -> str:
            captured["audio_bytes"] = audio_bytes
            return "你好，世界"

    audio_file = tmp_path / "in.wav"
    payload = b"FAKE_WAV_BYTES_FOR_TEST_ONLY"
    audio_file.write_bytes(payload)

    tools = BuiltinTools(allowed_dirs=[tmp_path], stt_provider=_MockSTT())
    result = await tools.invoke(_call("voice_transcribe", {
        "audio_path": str(audio_file),
    }))
    assert result.ok is True, result.error
    decoded = json.loads(result.content)
    assert decoded["text"] == "你好，世界"
    assert decoded["audio_bytes"] == len(payload)
    assert decoded["source"] == "audio_path"
    # Mock got the raw bytes from the file.
    assert captured["audio_bytes"] == payload


@pytest.mark.asyncio
async def test_voice_transcribe_with_audio_b64() -> None:
    captured: dict[str, Any] = {}

    class _MockSTT:
        async def transcribe(self, audio_bytes: bytes) -> str:
            captured["audio_bytes"] = audio_bytes
            return "decoded text"

    raw = b"\x00\x01\x02\x03 hello"
    b64 = base64.b64encode(raw).decode("ascii")

    tools = BuiltinTools(stt_provider=_MockSTT())
    result = await tools.invoke(_call("voice_transcribe", {
        "audio_b64": b64,
    }))
    assert result.ok is True, result.error
    decoded = json.loads(result.content)
    assert decoded["text"] == "decoded text"
    assert decoded["source"] == "audio_b64"
    assert captured["audio_bytes"] == raw


@pytest.mark.asyncio
async def test_voice_transcribe_rejects_both_sources() -> None:
    """Passing both audio_path and audio_b64 must be rejected — the
    tool can't pick which one the caller meant."""
    class _MockSTT:
        async def transcribe(self, audio_bytes: bytes) -> str:  # noqa: ARG002
            return "should not run"

    tools = BuiltinTools(stt_provider=_MockSTT())
    result = await tools.invoke(_call("voice_transcribe", {
        "audio_path": "/some/path",
        "audio_b64": "aGVsbG8=",
    }))
    assert result.ok is False
    assert "exactly one" in (result.error or "")


@pytest.mark.asyncio
async def test_voice_transcribe_rejects_missing_source() -> None:
    class _MockSTT:
        async def transcribe(self, audio_bytes: bytes) -> str:  # noqa: ARG002
            return ""

    tools = BuiltinTools(stt_provider=_MockSTT())
    result = await tools.invoke(_call("voice_transcribe", {}))
    assert result.ok is False
    assert "audio source" in (result.error or "")


@pytest.mark.asyncio
async def test_voice_transcribe_without_provider() -> None:
    tools = BuiltinTools()  # no stt_provider
    result = await tools.invoke(_call("voice_transcribe", {
        "audio_b64": "aGVsbG8=",
    }))
    assert result.ok is False
    assert "not configured" in (result.error or "")


@pytest.mark.asyncio
async def test_voice_transcribe_surfaces_import_error() -> None:
    """When the underlying provider raises ImportError (SDK missing),
    the tool must surface the message so the operator sees the
    install hint."""
    class _BrokenSTT:
        async def transcribe(self, audio_bytes: bytes) -> str:  # noqa: ARG002
            raise ImportError("WhisperSTT needs the faster-whisper package. pip install xmclaw[voice-stt]")

    tools = BuiltinTools(stt_provider=_BrokenSTT())
    result = await tools.invoke(_call("voice_transcribe", {
        "audio_b64": "aGVsbG8=",
    }))
    assert result.ok is False
    assert "faster-whisper" in (result.error or "")
    assert "pip install" in (result.error or "")


@pytest.mark.asyncio
async def test_voice_synthesize_writes_audio_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: tool calls the TTS provider, writes mp3 bytes to
    ``XMC_DATA_DIR/v2/audio/<uuid>.mp3``, returns the path in
    side_effects."""
    captured: dict[str, Any] = {}

    class _MockTTS:
        async def synthesize(self, text: str, voice: str = "default") -> bytes:
            captured["text"] = text
            captured["voice"] = voice
            return b"\xff\xfb\x90\x00FAKE_MP3_BYTES"

    monkeypatch.setenv("XMC_DATA_DIR", str(tmp_path))

    tools = BuiltinTools(tts_provider=_MockTTS())
    result = await tools.invoke(_call("voice_synthesize", {
        "text": "Hello world",
    }))
    assert result.ok is True, result.error
    payload = json.loads(result.content)
    audio_path = Path(payload["audio_path"])
    # Must land under the env-overridden data dir.
    assert audio_path.is_file()
    assert audio_path.suffix == ".mp3"
    assert audio_path.parent == tmp_path / "v2" / "audio"
    assert audio_path.read_bytes() == b"\xff\xfb\x90\x00FAKE_MP3_BYTES"
    assert payload["bytes"] == len(b"\xff\xfb\x90\x00FAKE_MP3_BYTES")
    # default voice was forwarded as the literal string "default" — the
    # provider decides what that means.
    assert captured["voice"] == "default"
    assert captured["text"] == "Hello world"
    # side_effects records the resolved path so the grader can verify
    # the write actually landed.
    assert len(result.side_effects) == 1
    assert str(audio_path.resolve()) in result.side_effects[0]


@pytest.mark.asyncio
async def test_voice_synthesize_forwards_voice_arg(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class _MockTTS:
        async def synthesize(self, text: str, voice: str = "default") -> bytes:
            captured["voice"] = voice
            return b"audio"

    monkeypatch.setenv("XMC_DATA_DIR", str(tmp_path))
    tools = BuiltinTools(tts_provider=_MockTTS())
    result = await tools.invoke(_call("voice_synthesize", {
        "text": "hi",
        "voice": "en-US-AriaNeural",
    }))
    assert result.ok is True, result.error
    assert captured["voice"] == "en-US-AriaNeural"


@pytest.mark.asyncio
async def test_voice_synthesize_rejects_non_string_text() -> None:
    class _MockTTS:
        async def synthesize(self, text: str, voice: str = "default") -> bytes:  # noqa: ARG002
            return b""

    tools = BuiltinTools(tts_provider=_MockTTS())
    result = await tools.invoke(_call("voice_synthesize", {"text": 12345}))
    assert result.ok is False
    assert "text" in (result.error or "")


@pytest.mark.asyncio
async def test_voice_synthesize_without_provider() -> None:
    tools = BuiltinTools()  # no tts_provider
    result = await tools.invoke(_call("voice_synthesize", {"text": "hi"}))
    assert result.ok is False
    assert "not configured" in (result.error or "")


@pytest.mark.asyncio
async def test_voice_synthesize_surfaces_provider_import_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _BrokenTTS:
        async def synthesize(self, text: str, voice: str = "default") -> bytes:  # noqa: ARG002
            raise ImportError("EdgeTTS needs the edge-tts package. pip install xmclaw[voice-tts]")

    monkeypatch.setenv("XMC_DATA_DIR", str(tmp_path))
    tools = BuiltinTools(tts_provider=_BrokenTTS())
    result = await tools.invoke(_call("voice_synthesize", {"text": "hi"}))
    assert result.ok is False
    assert "edge-tts" in (result.error or "")


# ── set_voice_providers post-construction wiring ──────────────────────


def test_set_voice_providers_wires_after_construction() -> None:
    """Symmetric with set_memory_manager / set_embedder — the daemon
    factory may need to wire voice providers AFTER BuiltinTools was
    built (e.g. when config_watcher hot-reloads ``voice`` block)."""
    class _MockSTT:
        async def transcribe(self, audio_bytes: bytes) -> str:  # noqa: ARG002
            return ""

    class _MockTTS:
        async def synthesize(self, text: str, voice: str = "default") -> bytes:  # noqa: ARG002
            return b""

    tools = BuiltinTools()
    assert {"voice_transcribe", "voice_synthesize"}.isdisjoint(
        {t.name for t in tools.list_tools()},
    )

    tools.set_voice_providers(stt=_MockSTT(), tts=_MockTTS())
    names = {t.name for t in tools.list_tools()}
    assert {"voice_transcribe", "voice_synthesize"} <= names
