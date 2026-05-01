"""B-135 — ContentTools unit tests.

Pins:
  * list_tools advertises every tool when its enable flag is on
  * each tool degrades to ok=False with a clear "install X" hint
    when the optional dep is missing — never raises out of invoke()
  * happy paths for the deps we KNOW are bundled in the dev env
    (mss, python-docx, openpyxl, pyperclip, PIL)
  * pdf_read returns a structured "install pypdf" error since pypdf
    is not in the dev env (acts as the "missing-dep" pin)
"""
from __future__ import annotations

import base64
import importlib.util
import json
from pathlib import Path

import pytest

from xmclaw.core.ir import ToolCall
from xmclaw.providers.tool.content import ContentTools


def _call(name: str, args: dict | None = None) -> ToolCall:
    return ToolCall(
        name=name, args=args or {}, provenance="synthetic",
    )


# ── tool list ─────────────────────────────────────────────────────


def test_list_tools_default_includes_all() -> None:
    names = {s.name for s in ContentTools().list_tools()}
    assert names == {
        "screenshot", "image_read", "pdf_read", "docx_read",
        "xlsx_read", "clipboard_read", "clipboard_write",
    }


def test_list_tools_disable_screenshot() -> None:
    names = {s.name for s in ContentTools(enable_screenshot=False).list_tools()}
    assert "screenshot" not in names
    assert "image_read" in names  # always on


def test_list_tools_disable_clipboard() -> None:
    names = {s.name for s in ContentTools(enable_clipboard=False).list_tools()}
    assert "clipboard_read" not in names
    assert "clipboard_write" not in names


def test_list_tools_disable_documents() -> None:
    names = {s.name for s in ContentTools(enable_documents=False).list_tools()}
    assert "pdf_read" not in names
    assert "docx_read" not in names
    assert "xlsx_read" not in names


# ── unknown tool name ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unknown_tool_returns_structured_error() -> None:
    r = await ContentTools().invoke(_call("does_not_exist"))
    assert r.ok is False
    assert "unknown tool" in (r.error or "")


# ── pdf_read missing-dep pin ─────────────────────────────────────


@pytest.mark.asyncio
async def test_pdf_read_missing_pypdf_dep(tmp_path: Path) -> None:
    """pypdf isn't in the dev env (only pdfplumber/etc. would help).
    The tool must surface an actionable 'install pypdf' message
    rather than an ImportError."""
    if importlib.util.find_spec("pypdf"):
        pytest.skip("pypdf is installed; this test only fires without it")
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    r = await ContentTools().invoke(_call("pdf_read", {"path": str(pdf)}))
    assert r.ok is False
    assert "pypdf" in (r.error or "")


# ── image_read happy + size cap ──────────────────────────────────


@pytest.mark.asyncio
async def test_image_read_returns_base64(tmp_path: Path) -> None:
    img = tmp_path / "tiny.png"
    # 1x1 PNG (transparent) — minimal valid PNG bytes.
    img.write_bytes(bytes.fromhex(
        "89504E470D0A1A0A0000000D49484452000000010000000108060000001F15C4"
        "890000000A49444154789C636000000000020001E221BC330000000049454E44"
        "AE426082"
    ))
    r = await ContentTools().invoke(_call("image_read", {"path": str(img)}))
    assert r.ok is True
    payload = json.loads(r.content)
    assert payload["mime"] == "image/png"
    assert payload["bytes"] == img.stat().st_size
    # round-trip the base64 to confirm it's actually decodable
    decoded = base64.b64decode(payload["base64"])
    assert decoded == img.read_bytes()


@pytest.mark.asyncio
async def test_image_read_missing_file(tmp_path: Path) -> None:
    r = await ContentTools().invoke(_call(
        "image_read", {"path": str(tmp_path / "nope.png")},
    ))
    assert r.ok is False
    assert "not found" in (r.error or "")


@pytest.mark.asyncio
async def test_image_read_rejects_oversized(tmp_path: Path) -> None:
    big = tmp_path / "big.png"
    big.write_bytes(b"x" * (9 * 1024 * 1024))  # 9 MB
    r = await ContentTools().invoke(_call("image_read", {"path": str(big)}))
    assert r.ok is False
    assert "8 MB cap" in (r.error or "")


# ── docx_read happy ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_docx_read_extracts_paragraphs(tmp_path: Path) -> None:
    if not importlib.util.find_spec("docx"):
        pytest.skip("python-docx not installed")
    import docx as _docx  # type: ignore
    doc = _docx.Document()
    doc.add_paragraph("第一段 hello")
    doc.add_paragraph("第二段 world")
    p = tmp_path / "demo.docx"
    doc.save(str(p))

    r = await ContentTools().invoke(_call("docx_read", {"path": str(p)}))
    assert r.ok is True
    payload = json.loads(r.content)
    assert payload["paragraphs"] == 2
    assert "第一段 hello" in payload["text"]
    assert "第二段 world" in payload["text"]


# ── xlsx_read happy ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_xlsx_read_extracts_cells(tmp_path: Path) -> None:
    if not importlib.util.find_spec("openpyxl"):
        pytest.skip("openpyxl not installed")
    import openpyxl  # type: ignore
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(["name", "value"])
    ws.append(["foo", 42])
    p = tmp_path / "demo.xlsx"
    wb.save(str(p))

    r = await ContentTools().invoke(_call("xlsx_read", {"path": str(p)}))
    assert r.ok is True
    payload = json.loads(r.content)
    assert "Sheet1" in payload["sheets"]
    assert "foo" in payload["text"]
    assert "42" in payload["text"]


# ── screenshot happy (mss is bundled) ────────────────────────────


@pytest.mark.asyncio
async def test_screenshot_writes_file(tmp_path: Path) -> None:
    if not importlib.util.find_spec("mss"):
        pytest.skip("mss not installed (CI without display)")
    out = tmp_path / "shot.png"
    r = await ContentTools().invoke(_call(
        "screenshot", {"path": str(out)},
    ))
    # Some headless environments (CI Linux without DISPLAY) raise from
    # mss. We accept both "ok=True with file" and "ok=False with
    # diagnostic" — what we DON'T accept is a Python exception leaking
    # out of invoke().
    if r.ok:
        assert out.is_file()
        payload = json.loads(r.content)
        assert payload["bytes"] == out.stat().st_size
        assert payload["width"] > 0
    else:
        assert r.error  # has a human-readable error string


# ── clipboard round-trip ────────────────────────────────────────


@pytest.mark.asyncio
async def test_clipboard_round_trip() -> None:
    if not importlib.util.find_spec("pyperclip"):
        pytest.skip("pyperclip not installed")
    tools = ContentTools()
    payload = "xmclaw clipboard test 123"
    write_r = await tools.invoke(_call("clipboard_write", {"text": payload}))
    if not write_r.ok:
        # Headless / no DISPLAY — pyperclip raises. That's a valid
        # not-installed-on-this-host case; just confirm the error is
        # diagnostic rather than a stack trace.
        pytest.skip(f"clipboard unavailable on this runner: {write_r.error}")

    read_r = await tools.invoke(_call("clipboard_read"))
    assert read_r.ok is True
    payload_back = json.loads(read_r.content)
    assert payload_back["text"] == payload
