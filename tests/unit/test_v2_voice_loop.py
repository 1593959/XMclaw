"""Static-scan tests for Sprint 2 Wave 7 — continuous voice loop.

Mirrors the test_v2_ui_chat.py pattern: no Node / no jsdom, but the
wiring between voice_loop.js + audio.js + Composer + Chat is
load-bearing enough that we want regression coverage. These assertions
catch silent module-rename / API-shape drift.

Real state-machine tests need jsdom — out of scope for this sprint.
"""
from __future__ import annotations

from pathlib import Path

STATIC_DIR = (
    Path(__file__).resolve().parents[2] / "xmclaw" / "daemon" / "static"
)


def read(rel: str) -> str:
    return (STATIC_DIR / rel).read_text(encoding="utf-8")


# ── voice_loop.js exports the documented API ──────────────────────


def test_voice_loop_module_exists() -> None:
    src = read("lib/voice_loop.js")
    # The four phases the state machine cycles through.
    for phase in ("IDLE", "LISTENING", "SUBMITTING", "SPEAKING"):
        assert f'{phase}:' in src, f"PHASES.{phase} missing"


def test_voice_loop_exports_factory_and_support_flag() -> None:
    src = read("lib/voice_loop.js")
    assert "export function createVoiceLoop" in src
    assert "export const voiceLoopSupported" in src
    assert "export const PHASES" in src


def test_voice_loop_uses_audio_module_primitives() -> None:
    """The loop must compose audio.js's createRecognizer + speak —
    no duplicate browser-API plumbing of its own."""
    src = read("lib/voice_loop.js")
    assert "createRecognizer" in src
    assert "speak" in src
    assert "plainTextForTts" in src
    assert "from \"./audio.js\"" in src


def test_voice_loop_documents_state_transitions() -> None:
    """The header comment must include the state machine so future
    editors don't break it without realizing."""
    src = read("lib/voice_loop.js")
    # Header arrow-diagram tokens.
    assert "listening" in src.lower()
    assert "submitting" in src.lower()
    assert "speaking" in src.lower()
    assert "deliverReply" in src


def test_voice_loop_handles_unsupported_browser() -> None:
    """Firefox / SR-less browsers must get a no-op loop, not a
    runtime error on the first call."""
    src = read("lib/voice_loop.js")
    assert "supported: false" in src
    assert "SpeechRecognition unavailable" in src


def test_voice_loop_returns_controller_shape() -> None:
    """Controller API surface for Composer to call."""
    src = read("lib/voice_loop.js")
    for method in ("start", "stop", "cancel", "deliverReply", "isActive", "getPhase"):
        assert method in src, f"controller method {method!r} missing"


# ── Composer wires the loop on the "对话" toggle ──────────────────


def test_composer_imports_voice_loop() -> None:
    src = read("components/molecules/Composer.js")
    assert "from \"../../lib/voice_loop.js\"" in src
    assert "createVoiceLoop" in src
    assert "voiceLoopSupported" in src


def test_composer_renders_continuous_voice_chip() -> None:
    src = read("components/molecules/Composer.js")
    # Button + visual label markers we added.
    assert "voiceActive" in src
    assert "stopVoiceLoop" in src
    assert "startVoiceLoop" in src
    # Toolbar label that lets the user discover the feature.
    assert "对话" in src


def test_composer_uses_lastAssistantText_prop() -> None:
    """The reply-text plumbing from Chat.js must reach the loop's
    deliverReply call."""
    src = read("components/molecules/Composer.js")
    assert "lastAssistantText" in src
    assert "deliverReply" in src


# ── Chat passes lastAssistantText down ────────────────────────────


def test_chat_passes_last_assistant_text_to_composer() -> None:
    src = read("pages/Chat.js")
    assert "lastAssistantText" in src
    # Filter must skip the in-flight message so we don't TTS-narrate
    # while the recognizer is paused.
    assert "pendingAssistantId" in src
    assert "lastAssistantText=${lastAssistantText}" in src


# ── audio.js still exports what voice_loop depends on ─────────────


def test_audio_module_still_exports_primitives_voice_loop_uses() -> None:
    src = read("lib/audio.js")
    for sym in (
        "export function createRecognizer",
        "export const sttSupported",
        "export function speak",
        "export function stopSpeaking",
        "export function isSpeaking",
        "export function plainTextForTts",
        "export function getAudioPrefs",
    ):
        assert sym in src, f"audio.js missing: {sym}"
