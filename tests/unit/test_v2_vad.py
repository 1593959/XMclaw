"""Static-scan tests for Sprint 2 Wave 14 — energy-based VAD.

Same pattern as test_v2_voice_loop.py: no jsdom, so we verify the
module exports + state machine constants + algorithm invariants at
the file-content level. Catches accidental rename / API drift; a
true behavioural test would need Playwright + a real audio context.
"""
from __future__ import annotations

from pathlib import Path

STATIC_DIR = (
    Path(__file__).resolve().parents[2] / "xmclaw" / "daemon" / "static"
)


def read(rel: str) -> str:
    return (STATIC_DIR / rel).read_text(encoding="utf-8")


def test_vad_module_exists_and_exports_factory() -> None:
    src = read("lib/vad.js")
    assert "export function createEnergyVad" in src
    assert "export const vadSupported" in src
    assert "export const VAD_STATES" in src


def test_vad_documents_state_machine() -> None:
    src = read("lib/vad.js")
    for state in ("IDLE", "CALIBRATING", "LISTENING", "SPEAKING"):
        assert f"{state}:" in src, f"VAD_STATES.{state} missing"


def test_vad_calibration_period_constant() -> None:
    """1s calibration window. If we drop below 500ms the threshold
    estimate gets noisy; above 2s feels laggy to start."""
    src = read("lib/vad.js")
    assert "CALIBRATE_MS = 1000" in src


def test_vad_minimum_threshold_floor() -> None:
    """Without a floor, dead-quiet rooms calibrate to ~0 and any blip
    triggers — the floor keeps us robust on padded mics."""
    src = read("lib/vad.js")
    assert "MIN_THRESHOLD = 0.015" in src


def test_vad_handles_unsupported_browser() -> None:
    """Server-side / no-mic / Firefox-too-old should return a no-op
    controller, not crash."""
    src = read("lib/vad.js")
    assert "supported: false" in src
    assert "AudioContext / getUserMedia unavailable" in src


def test_vad_controller_shape() -> None:
    """Public API surface the caller relies on."""
    src = read("lib/vad.js")
    for method in (
        "start", "stop",
        "getState", "getThreshold", "getNoiseFloor",
    ):
        assert method in src, f"controller method {method!r} missing"


def test_vad_emits_speech_start_and_end_callbacks() -> None:
    src = read("lib/vad.js")
    assert "onSpeechStart" in src
    assert "onSpeechEnd" in src
    assert "onTick" in src
    assert "onError" in src


def test_vad_uses_byte_time_domain_data() -> None:
    """We pick byte-time-domain (centered at 128) over float-frequency
    because RMS on time-domain is the right energy measure + uses
    less memory. Regression: if someone swaps to getFloatFrequencyData
    the normalisation math breaks."""
    src = read("lib/vad.js")
    assert "getByteTimeDomainData" in src


def test_vad_hangover_and_min_speech_defaults() -> None:
    src = read("lib/vad.js")
    # Hangover: 400ms below threshold before declaring end.
    # Defaults documented in JSDoc so editors can rely on them.
    assert "hangoverMs" in src
    assert "minSpeechMs" in src
    assert "400" in src
    assert "200" in src
