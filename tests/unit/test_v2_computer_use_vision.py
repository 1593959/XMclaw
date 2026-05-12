"""Unit tests for the vision-grounding tools added to ComputerUseTools.

Tests cover the OCR pipeline (rapidocr → paddleocr → pytesseract
fallback chain), the find/click composites, and the wait_for_text
polling loop. We mock the OCR engines via sys.modules injection so
tests run without a real screen / GPU / Chinese-OCR weights.
"""
from __future__ import annotations

import asyncio
import json
import sys
import types
from typing import Any
from unittest.mock import patch

import pytest

from xmclaw.core.ir import ToolCall


def _call(name: str, args: dict[str, Any] | None = None) -> ToolCall:
    return ToolCall(
        id=f"t-{name}", name=name, args=args or {},
        provenance="synthetic",
    )


def _json(result):
    return json.loads(result.content) if result.ok else None


# ── Helpers under test ─────────────────────────────────────────────


def test_match_text_in_blocks_substring():
    from xmclaw.providers.tool.computer_use import _match_text_in_blocks
    blocks = [
        {"text": "魔丸群 (12)", "bbox": [10, 20, 100, 30],
         "center": [60, 35], "confidence": 0.95, "engine": "rapidocr"},
        {"text": "Settings", "bbox": [200, 20, 80, 30],
         "center": [240, 35], "confidence": 0.92, "engine": "rapidocr"},
        {"text": "魔丸", "bbox": [10, 100, 80, 30],
         "center": [50, 115], "confidence": 0.88, "engine": "rapidocr"},
    ]
    matches = _match_text_in_blocks(blocks, "魔丸")
    # Both "魔丸群 (12)" and "魔丸" contain "魔丸" — both returned,
    # higher confidence first.
    assert len(matches) == 2
    assert matches[0]["text"] == "魔丸群 (12)"
    assert matches[0]["confidence"] == 0.95


def test_match_text_in_blocks_exact():
    from xmclaw.providers.tool.computer_use import _match_text_in_blocks
    blocks = [
        {"text": "魔丸群 (12)", "bbox": [0, 0, 1, 1],
         "center": [0, 0], "confidence": 0.95, "engine": "x"},
        {"text": "魔丸", "bbox": [0, 0, 1, 1],
         "center": [0, 0], "confidence": 0.88, "engine": "x"},
    ]
    matches = _match_text_in_blocks(blocks, "魔丸", exact=True)
    assert len(matches) == 1
    assert matches[0]["text"] == "魔丸"


def test_match_text_in_blocks_case_insensitive():
    from xmclaw.providers.tool.computer_use import _match_text_in_blocks
    blocks = [{
        "text": "WeChat", "bbox": [0, 0, 1, 1],
        "center": [0, 0], "confidence": 0.9, "engine": "x",
    }]
    assert len(_match_text_in_blocks(blocks, "wechat")) == 1


def test_offset_blocks():
    from xmclaw.providers.tool.computer_use import _offset_blocks
    blocks = [{
        "text": "X", "bbox": [10, 20, 30, 40], "center": [25, 40],
        "confidence": 0.9, "engine": "x",
    }]
    _offset_blocks(blocks, 100, 200)
    assert blocks[0]["bbox"] == [110, 220, 30, 40]
    assert blocks[0]["center"] == [125, 240]


# ── Mock OCR engines ────────────────────────────────────────────────


def _fake_blocks_from_engine(engine_name: str) -> list[dict]:
    """The screen the mock OCR engines "see"."""
    return [
        {"text": "魔丸群 (12)", "bbox": [50, 100, 120, 32],
         "center": [110, 116], "confidence": 0.95, "engine": engine_name},
        {"text": "工作群",      "bbox": [50, 140, 80, 32],
         "center": [90, 156], "confidence": 0.92, "engine": engine_name},
        {"text": "Search",      "bbox": [200, 30, 100, 28],
         "center": [250, 44], "confidence": 0.88, "engine": engine_name},
    ]


@pytest.fixture
def fake_ocr_pipeline(monkeypatch: pytest.MonkeyPatch):
    """Replace _run_ocr_full_pipeline so we don't need a real screen.

    Returns a small recorder; tests can mutate ``recorder['result']``
    to change what the pipeline emits, or ``recorder['raises']`` to
    have it raise _NoOCREngineError.
    """
    import xmclaw.providers.tool.computer_use as cu

    recorder = {
        "result": _fake_blocks_from_engine("mock"),
        "calls": [],
        "raises": None,
    }

    def _fake(region, min_conf):
        recorder["calls"].append((region, min_conf))
        if recorder["raises"]:
            raise recorder["raises"]
        return recorder["result"]

    monkeypatch.setattr(cu, "_run_ocr_full_pipeline", _fake)
    return recorder


@pytest.fixture
def tools(tmp_path):
    from xmclaw.providers.tool.computer_use import ComputerUseTools
    return ComputerUseTools(
        screenshot_dir=tmp_path / "shots",
        base64_size_cap=2 * 1024 * 1024,
    )


# ── screen_ocr ─────────────────────────────────────────────────────


async def test_screen_ocr_returns_blocks(tools, fake_ocr_pipeline):
    r = await tools.invoke(_call("screen_ocr"))
    assert r.ok, r.error
    p = _json(r)
    assert p["count"] == 3
    assert p["blocks"][0]["text"] == "魔丸群 (12)"


async def test_screen_ocr_passes_region_through(tools, fake_ocr_pipeline):
    r = await tools.invoke(_call("screen_ocr", {
        "region": [10, 20, 800, 600],
        "min_confidence": 0.75,
    }))
    assert r.ok
    region, min_conf = fake_ocr_pipeline["calls"][-1]
    assert region == [10, 20, 800, 600]
    assert min_conf == 0.75


async def test_screen_ocr_no_engine_installed(tools, fake_ocr_pipeline):
    from xmclaw.providers.tool.computer_use import _NoOCREngineError
    fake_ocr_pipeline["raises"] = _NoOCREngineError(
        "No OCR engine installed. Pick one: rapidocr-onnxruntime",
    )
    r = await tools.invoke(_call("screen_ocr"))
    assert not r.ok
    assert "OCR engine" in r.error


# ── find_on_screen ─────────────────────────────────────────────────


async def test_find_on_screen_finds_chinese(tools, fake_ocr_pipeline):
    r = await tools.invoke(_call("find_on_screen", {"text": "魔丸"}))
    assert r.ok, r.error
    p = _json(r)
    assert p["found"] is True
    assert p["match_text"] == "魔丸群 (12)"
    # bbox center
    assert p["x"] == 110
    assert p["y"] == 116


async def test_find_on_screen_returns_alternatives(tools, fake_ocr_pipeline):
    """When multiple blocks match, the top one is the answer + the
    rest land in all_matches so the LLM can disambiguate."""
    fake_ocr_pipeline["result"] = [
        {"text": "魔丸群", "bbox": [50, 100, 60, 30],
         "center": [80, 115], "confidence": 0.95, "engine": "m"},
        {"text": "魔丸资料群", "bbox": [50, 140, 100, 30],
         "center": [100, 155], "confidence": 0.88, "engine": "m"},
        {"text": "魔丸 stickers", "bbox": [50, 180, 120, 30],
         "center": [110, 195], "confidence": 0.80, "engine": "m"},
    ]
    r = await tools.invoke(_call("find_on_screen", {"text": "魔丸"}))
    p = _json(r)
    assert p["match_text"] == "魔丸群"
    assert len(p["all_matches"]) == 2
    assert p["all_matches"][0]["text"] == "魔丸资料群"


async def test_find_on_screen_not_found_returns_sample(
    tools, fake_ocr_pipeline,
):
    """When nothing matches, we surface a sample of what WAS read
    so the LLM can see the OCR's view of the screen and adjust."""
    r = await tools.invoke(_call("find_on_screen", {"text": "nonexistent"}))
    assert not r.ok
    err_payload = json.loads(r.error)
    assert err_payload["found"] is False
    assert err_payload["wanted"] == "nonexistent"
    assert len(err_payload["sample_blocks"]) > 0


async def test_find_on_screen_empty_text(tools, fake_ocr_pipeline):
    r = await tools.invoke(_call("find_on_screen", {"text": "   "}))
    assert not r.ok
    assert "text" in r.error


# ── click_on_text ──────────────────────────────────────────────────


async def test_click_on_text_calls_pyautogui_click(
    tools, fake_ocr_pipeline, monkeypatch,
):
    """End-to-end: find_on_screen → mouse_click at center."""
    pg = types.ModuleType("pyautogui")
    pg.FAILSAFE = False
    pg.PAUSE = 0.0
    pg.calls = []
    pg.size = lambda: (1920, 1080)
    pg.position = lambda: (0, 0)

    def _click(*args, **kwargs):
        pg.calls.append(("click", args, kwargs))

    pg.click = _click
    monkeypatch.setitem(sys.modules, "pyautogui", pg)

    r = await tools.invoke(_call("click_on_text", {
        "text": "魔丸",
        "button": "left",
        "count": 2,
    }))
    assert r.ok, r.error
    p = _json(r)
    assert p["clicked"] is True
    assert p["x"] == 110 and p["y"] == 116
    assert p["match_text"] == "魔丸群 (12)"
    # The actual click landed
    assert pg.calls[0][0] == "click"
    assert pg.calls[0][2] == {
        "x": 110, "y": 116, "button": "left", "clicks": 2,
    }


async def test_click_on_text_propagates_no_match(tools, fake_ocr_pipeline):
    """If find_on_screen can't locate the text, click_on_text also
    surfaces the diagnostic (with sample_blocks)."""
    r = await tools.invoke(_call("click_on_text", {"text": "不存在的群"}))
    assert not r.ok
    err = json.loads(r.error)
    assert err["found"] is False


# ── wait_for_text ──────────────────────────────────────────────────


async def test_wait_for_text_finds_on_first_poll(tools, fake_ocr_pipeline):
    r = await tools.invoke(_call("wait_for_text", {
        "text": "魔丸",
        "timeout_s": 2,
        "poll_interval_s": 0.5,
    }))
    assert r.ok, r.error
    p = _json(r)
    assert p["found"] is True
    assert p["attempts"] == 1


async def test_wait_for_text_polls_until_appears(
    tools, fake_ocr_pipeline,
):
    """Simulate text appearing on the 3rd poll. Pipeline returns
    empty list first 2 calls, real result on 3rd."""
    call_count = {"n": 0}
    real_result = fake_ocr_pipeline["result"]

    def _dynamic(region, min_conf):
        call_count["n"] += 1
        if call_count["n"] < 3:
            return []
        return real_result

    import xmclaw.providers.tool.computer_use as cu
    with patch.object(cu, "_run_ocr_full_pipeline", _dynamic):
        r = await tools.invoke(_call("wait_for_text", {
            "text": "魔丸",
            "timeout_s": 5,
            "poll_interval_s": 0.3,
        }))
    assert r.ok, r.error
    p = _json(r)
    assert p["attempts"] == 3
    assert p["elapsed_s"] >= 0.6  # 2 sleeps × 0.3s minimum


async def test_wait_for_text_times_out(tools, fake_ocr_pipeline):
    fake_ocr_pipeline["result"] = []  # nothing ever shows up
    r = await tools.invoke(_call("wait_for_text", {
        "text": "never",
        "timeout_s": 1.0,
        "poll_interval_s": 0.3,
    }))
    assert not r.ok
    err = json.loads(r.error)
    assert err["found"] is False
    assert err["timed_out_after_s"] == 1.0
    assert err["attempts"] >= 2


# ── screen_region_capture ──────────────────────────────────────────


async def test_screen_region_capture_invalid_region(tools):
    r = await tools.invoke(_call("screen_region_capture", {
        "region": [0, 0, 0, 100],  # zero width
    }))
    assert not r.ok
    assert "width/height" in r.error or "region" in r.error


async def test_screen_region_capture_missing_region(tools):
    r = await tools.invoke(_call("screen_region_capture", {}))
    assert not r.ok
    assert "region" in r.error
