"""Regression tests for the WS user-frame image intake helper.

Locks in the fix for chat-b3c614bc (2026-05-26): a missing ``time``
import in the WS handler's inline save loop made every chat image
upload silently fail with ``NameError`` for weeks, swallowed by a
broad ``except Exception`` and surfaced only as "image save failed"
in the daemon log. Each subsequent fix attempt addressed downstream
symptoms (orchestrator passing user_images, persona-poisoning, etc.)
without anyone checking the uploads_dir was actually being written.

These tests exercise the data: URL → file-on-disk path end-to-end
without an LLM or a WS connection, so a future copy-paste mistake
that drops ``import time`` from the helper module turns into a
red test instead of weeks of silent failure.
"""
from __future__ import annotations

import base64
from pathlib import Path

import pytest

from xmclaw.daemon.ws_image_intake import (
    MAX_IMAGE_BYTES,
    MAX_IMAGES_PER_FRAME,
    save_user_frame_images,
)


# 1x1 transparent PNG, base64'd. Same bytes Chrome composes when
# FileReader.readAsDataURL runs on a tiny PNG file. Used everywhere
# below — small enough that test runs are instant.
_PNG_1X1 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4nGNgYAAA"
    "AAMAASsJTYQAAAAASUVORK5CYII="
)
_PNG_DATA_URL = f"data:image/png;base64,{_PNG_1X1}"


def test_saves_single_data_url_to_disk(tmp_path: Path) -> None:
    """The flagship case: one PNG attached, one file on disk,
    returned path points at it."""
    out = save_user_frame_images([_PNG_DATA_URL], tmp_path)
    assert len(out) == 1
    saved = Path(out[0])
    assert saved.exists(), f"file not written to {saved}"
    assert saved.suffix == ".png"
    # Round-trip the bytes — the saved file should decode to
    # exactly what the data URL encoded.
    assert saved.read_bytes() == base64.b64decode(_PNG_1X1)


def test_saves_multiple_images_in_order(tmp_path: Path) -> None:
    out = save_user_frame_images([_PNG_DATA_URL, _PNG_DATA_URL], tmp_path)
    assert len(out) == 2
    for p in out:
        assert Path(p).exists()


def test_non_list_input_returns_empty(tmp_path: Path) -> None:
    """The WS handler accepts any shape from the JSON frame; non-
    list inputs (None, missing key, malformed) must yield ``[]``
    without raising."""
    assert save_user_frame_images(None, tmp_path) == []
    assert save_user_frame_images("not a list", tmp_path) == []
    assert save_user_frame_images({"oops": "dict"}, tmp_path) == []


def test_non_data_url_entries_skipped(tmp_path: Path) -> None:
    """The frontend should always send ``data:`` URLs, but if an
    HTTP URL or stray string slips through we drop it (we can't
    fetch arbitrary URLs from the WS handler) rather than crash."""
    out = save_user_frame_images(
        [_PNG_DATA_URL, "https://example.com/img.png", "", 12345],
        tmp_path,
    )
    assert len(out) == 1  # only the data: URL


def test_oversized_image_rejected(tmp_path: Path) -> None:
    """Per-attachment cap protects disk + downstream LLM token
    budget. The check uses decoded bytes, not the base64 length."""
    big_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * (MAX_IMAGE_BYTES + 1024)
    big_url = f"data:image/png;base64,{base64.b64encode(big_bytes).decode()}"
    assert save_user_frame_images([big_url], tmp_path) == []


def test_caps_at_max_images_per_frame(tmp_path: Path) -> None:
    """A user can stage up to 8 images per turn; entries beyond
    that cap are silently dropped."""
    out = save_user_frame_images(
        [_PNG_DATA_URL] * (MAX_IMAGES_PER_FRAME + 3),
        tmp_path,
    )
    assert len(out) == MAX_IMAGES_PER_FRAME


def test_no_nameerror_on_time_import(tmp_path: Path) -> None:
    """Direct regression for the NameError that caused chat-b3c614bc.

    The helper must not depend on ``time`` being magically in scope
    from the caller — it imports the stdlib module itself. If a
    future refactor drops the import, the very first call here
    raises NameError + this test goes red.
    """
    # Just calling save_user_frame_images with a valid input
    # exercises ``int(time.time())`` inside the helper. If the
    # import is missing, this raises NameError instead of writing
    # a file, and the assertion below fails loudly.
    out = save_user_frame_images([_PNG_DATA_URL], tmp_path)
    assert out, (
        "save_user_frame_images returned empty — the NameError "
        "regression has come back. Check that ws_image_intake.py "
        "imports ``time`` at the module level."
    )


@pytest.mark.parametrize("mime,expected_ext", [
    ("image/png", ".png"),
    ("image/jpeg", ".jpg"),
    ("image/webp", ".webp"),
    ("image/gif", ".gif"),
    ("application/unknown", ".bin"),  # unknown mime falls through
])
def test_extension_inferred_from_mime(
    mime: str, expected_ext: str, tmp_path: Path,
) -> None:
    url = f"data:{mime};base64,{_PNG_1X1}"
    out = save_user_frame_images([url], tmp_path)
    assert len(out) == 1
    assert Path(out[0]).suffix == expected_ext
