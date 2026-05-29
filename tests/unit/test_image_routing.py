"""Unit tests for daemon/image_routing.py.

The flow we care about:
  - vision-capable profile  → mode "native", paths passthrough, text gets path hint
  - vision-less profile     → mode "text", paths dropped, OCR text folded in
  - OCR backend missing     → degraded `[image: name — OCR unavailable]` marker
  - config defaults         → unset supports_vision → True (don't surprise-OCR)
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from xmclaw.daemon import image_routing as ir


# ─── supports_vision / decide_image_mode ───────────────────────────

def test_supports_vision_default_true_when_unset():
    cfg = {"llm": {"profiles": [{"id": "p1"}]}}
    assert ir.supports_vision(cfg, "p1") is True
    assert ir.decide_image_mode(cfg, "p1") == "native"


def test_supports_vision_explicit_false():
    cfg = {"llm": {"profiles": [{"id": "deepseek_", "supports_vision": False}]}}
    assert ir.supports_vision(cfg, "deepseek_") is False
    assert ir.decide_image_mode(cfg, "deepseek_") == "text"


def test_supports_vision_explicit_true():
    cfg = {"llm": {"profiles": [{"id": "k", "supports_vision": True}]}}
    assert ir.supports_vision(cfg, "k") is True


def test_supports_vision_unknown_profile_falls_back_to_top_level():
    cfg = {"llm": {"anthropic": {"supports_vision": False}}}
    # Unknown profile id → top-level anthropic block consulted.
    assert ir.supports_vision(cfg, "unknown") is False


def test_supports_vision_no_config_defaults_true():
    assert ir.supports_vision(None, None) is True
    assert ir.supports_vision({}, None) is True


# ─── enrich_user_message: native mode ──────────────────────────────

def test_enrich_native_keeps_paths_and_adds_hints(tmp_path: Path):
    p = tmp_path / "shot.png"
    p.write_bytes(b"fake")
    enriched, out_paths = ir.enrich_user_message(
        "what's this?", [str(p)], "native",
    )
    assert out_paths == [str(p)]
    assert "what's this?" in enriched
    assert f"[Image attached at: {p}]" in enriched


def test_enrich_native_empty_text_uses_default_question(tmp_path: Path):
    p = tmp_path / "x.png"
    p.write_bytes(b"fake")
    enriched, _ = ir.enrich_user_message(None, [str(p)], "native")
    assert "What do you see in this image?" in enriched


def test_enrich_no_images_passthrough():
    enriched, paths = ir.enrich_user_message("hi", [], "text")
    assert enriched == "hi"
    assert paths == []


# ─── enrich_user_message: text mode (OCR backend missing) ──────────

def test_enrich_text_mode_no_ocr_backend(monkeypatch, tmp_path: Path):
    """When every OCR backend returns None (none installed), the
    image must still be acknowledged in the enriched text — never
    silently dropped."""
    p = tmp_path / "screenshot.png"
    p.write_bytes(b"fake")

    monkeypatch.setattr(ir, "ocr_local", lambda _path: None)

    enriched, out_paths = ir.enrich_user_message(
        "看看这个", [str(p)], "text",
    )
    assert out_paths == []  # paths dropped — translator won't see pixels
    assert "看看这个" in enriched
    assert "screenshot.png" in enriched
    assert "OCR unavailable" in enriched


def test_enrich_text_mode_with_ocr_text(monkeypatch, tmp_path: Path):
    p = tmp_path / "shot.png"
    p.write_bytes(b"fake")
    monkeypatch.setattr(ir, "ocr_local", lambda _path: "Hello\n你好")

    enriched, out_paths = ir.enrich_user_message(
        "describe", [str(p)], "text",
    )
    assert out_paths == []
    assert "[image OCR | shot.png]" in enriched
    assert "Hello" in enriched
    assert "你好" in enriched
    assert "describe" in enriched
    # OCR block should come BEFORE the user text — model sees
    # the attachment context first.
    assert enriched.index("Hello") < enriched.index("describe")


def test_enrich_text_mode_empty_ocr_result(monkeypatch, tmp_path: Path):
    """OCR ran but found no text (purely visual image)."""
    p = tmp_path / "logo.png"
    p.write_bytes(b"fake")
    monkeypatch.setattr(ir, "ocr_local", lambda _path: "")

    enriched, _ = ir.enrich_user_message("?", [str(p)], "text")
    assert "no text detected" in enriched
    assert "logo.png" in enriched


def test_enrich_text_mode_multiple_images(monkeypatch, tmp_path: Path):
    paths = []
    for i in range(3):
        p = tmp_path / f"img{i}.png"
        p.write_bytes(b"fake")
        paths.append(str(p))

    monkeypatch.setattr(ir, "ocr_local", lambda path: f"text-of-{os.path.basename(path)}")

    enriched, out = ir.enrich_user_message("all three", paths, "text")
    assert out == []
    for i in range(3):
        assert f"img{i}.png" in enriched
        assert f"text-of-img{i}.png" in enriched


# ─── ocr_local error handling ──────────────────────────────────────

def test_ocr_local_missing_file_returns_none():
    assert ir.ocr_local("/nonexistent/path/to/img.png") is None


def test_ocr_local_empty_path_returns_none():
    assert ir.ocr_local("") is None
