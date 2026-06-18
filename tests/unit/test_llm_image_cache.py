"""Unit tests for LLM image block caching (Anthropic + OpenAI)."""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from xmclaw.providers.llm import anthropic as anthropic_module
from xmclaw.providers.llm import openai as openai_module
from xmclaw.providers.llm.anthropic import AnthropicLLM, _img_to_anthropic_block
from xmclaw.providers.llm.openai import OpenAILLM, _img_to_data_url
from xmclaw.providers.llm.base import Message


def _make_fake_image(width: int, height: int):
    """Return a mock PIL Image that survives convert/resize/save."""
    img = MagicMock()
    img.mode = "RGB"
    img.width = width
    img.height = height
    # convert("RGB") returns self so width/height survive
    img.convert.return_value = img
    # resize returns a new mock with the same dimensions
    resized = MagicMock()
    resized.mode = "RGB"
    resized.width = width
    resized.height = height
    img.resize.return_value = resized
    return img


def _clear_caches() -> None:
    anthropic_module._IMAGE_BLOCK_CACHE.clear()
    openai_module._IMAGE_DATA_URL_CACHE.clear()


@pytest.fixture(autouse=True)
def clear_caches_fixture():
    """Ensure module-level caches are empty before each test."""
    _clear_caches()
    yield
    _clear_caches()


# ─── Anthropic ───────────────────────────────────────────────────────────────


class TestAnthropicImageCache:
    def test_cache_hit_same_image(self, tmp_path: Path) -> None:
        """Second call with same path must hit cache — PIL not called again."""
        img_path = tmp_path / "screenshot.png"
        img_path.write_bytes(b"fake_png_bytes")

        fake_img = _make_fake_image(width=2000, height=1000)
        mock_buf = MagicMock()
        mock_buf.getvalue.return_value = b"fake_jpeg"

        with patch(
            "PIL.Image.open", return_value=fake_img
        ) as mock_open:
            with patch(
                "io.BytesIO", return_value=mock_buf
            ):
                result1 = _img_to_anthropic_block(str(img_path))
                assert result1 is not None
                assert mock_open.call_count == 1

                result2 = _img_to_anthropic_block(str(img_path))
                assert result2 is not None
                assert mock_open.call_count == 1  # cache hit!
                assert result1 == result2

    def test_cache_different_paths(self, tmp_path: Path) -> None:
        """Different file paths must have independent cache entries."""
        img1 = tmp_path / "a.png"
        img2 = tmp_path / "b.png"
        img1.write_bytes(b"fake_a")
        img2.write_bytes(b"fake_b")

        fake_img = _make_fake_image(width=2000, height=1000)
        mock_buf = MagicMock()
        mock_buf.getvalue.return_value = b"fake_jpeg"

        with patch(
            "PIL.Image.open", return_value=fake_img
        ) as mock_open:
            with patch(
                "io.BytesIO", return_value=mock_buf
            ):
                _img_to_anthropic_block(str(img1))
                _img_to_anthropic_block(str(img2))
                assert mock_open.call_count == 2

    def test_cache_different_sizes(self, tmp_path: Path) -> None:
        """Changing _VISION_MAX_WIDTH must produce a different cache key."""
        img_path = tmp_path / "screenshot.png"
        img_path.write_bytes(b"fake_png_bytes")

        fake_img = _make_fake_image(width=2000, height=1000)
        mock_buf = MagicMock()
        mock_buf.getvalue.return_value = b"fake_jpeg"

        with patch(
            "PIL.Image.open", return_value=fake_img
        ) as mock_open:
            with patch(
                "io.BytesIO", return_value=mock_buf
            ):
                with patch.object(anthropic_module, "_VISION_MAX_WIDTH", 1280):
                    _img_to_anthropic_block(str(img_path))
                    assert mock_open.call_count == 1

                with patch.object(anthropic_module, "_VISION_MAX_WIDTH", 1920):
                    _img_to_anthropic_block(str(img_path))
                    # Different max_width → different cache key → cache miss
                    assert mock_open.call_count == 2

    def test_cache_ttl_expires(self, tmp_path: Path) -> None:
        """After TTL expires, the image must be re-processed."""
        img_path = tmp_path / "screenshot.png"
        img_path.write_bytes(b"fake_png_bytes")

        fake_img = _make_fake_image(width=2000, height=1000)
        mock_buf = MagicMock()
        mock_buf.getvalue.return_value = b"fake_jpeg"

        with patch(
            "PIL.Image.open", return_value=fake_img
        ) as mock_open:
            with patch(
                "io.BytesIO", return_value=mock_buf
            ):
                with patch("xmclaw.providers.llm.anthropic.time") as mock_time:
                    mock_time.time.return_value = 0.0
                    _img_to_anthropic_block(str(img_path))
                    assert mock_open.call_count == 1

                    # Within TTL — cache hit
                    mock_time.time.return_value = 100.0
                    _img_to_anthropic_block(str(img_path))
                    assert mock_open.call_count == 1

                    # Past TTL — cache miss, re-process
                    mock_time.time.return_value = 301.0
                    _img_to_anthropic_block(str(img_path))
                    assert mock_open.call_count == 2

    def test_cache_cleared_on_close(self, tmp_path: Path) -> None:
        """Provider.close() must clear the module-level cache."""
        img_path = tmp_path / "screenshot.png"
        img_path.write_bytes(b"fake_png_bytes")

        fake_img = _make_fake_image(width=2000, height=1000)
        mock_buf = MagicMock()
        mock_buf.getvalue.return_value = b"fake_jpeg"

        with patch(
            "PIL.Image.open", return_value=fake_img
        ) as mock_open:
            with patch(
                "io.BytesIO", return_value=mock_buf
            ):
                _img_to_anthropic_block(str(img_path))
                assert mock_open.call_count == 1

                llm = AnthropicLLM(api_key="x")
                llm.close()

                # After close, cache is gone → cold path
                _img_to_anthropic_block(str(img_path))
                assert mock_open.call_count == 2

    def test_messages_to_anthropic_result_consistent(self, tmp_path: Path) -> None:
        """Caching must not change the wire shape produced by _messages_to_anthropic."""
        img_path = tmp_path / "screenshot.png"
        img_path.write_bytes(b"fake_png_bytes")

        fake_img = _make_fake_image(width=2000, height=1000)
        mock_buf = MagicMock()
        mock_buf.getvalue.return_value = b"fake_jpeg"

        msgs = [Message(role="user", content="look", images=(str(img_path),))]

        with patch(
            "PIL.Image.open", return_value=fake_img
        ):
            with patch(
                "io.BytesIO", return_value=mock_buf
            ):
                result_first = AnthropicLLM._messages_to_anthropic(msgs)

        _clear_caches()

        with patch(
            "PIL.Image.open", return_value=fake_img
        ):
            with patch(
                "io.BytesIO", return_value=mock_buf
            ):
                result_second = AnthropicLLM._messages_to_anthropic(msgs)

        assert result_first == result_second


# ─── OpenAI ───────────────────────────────────────────────────────────────────


class TestOpenAIImageCache:
    def test_cache_hit_same_image(self, tmp_path: Path) -> None:
        img_path = tmp_path / "screenshot.png"
        img_path.write_bytes(b"fake_png_bytes")

        fake_img = _make_fake_image(width=2000, height=1000)
        mock_buf = MagicMock()
        mock_buf.getvalue.return_value = b"fake_jpeg"

        with patch(
            "PIL.Image.open", return_value=fake_img
        ) as mock_open:
            with patch(
                "io.BytesIO", return_value=mock_buf
            ):
                result1 = _img_to_data_url(str(img_path))
                assert result1 is not None
                assert mock_open.call_count == 1

                result2 = _img_to_data_url(str(img_path))
                assert result2 is not None
                assert mock_open.call_count == 1
                assert result1 == result2

    def test_cache_different_paths(self, tmp_path: Path) -> None:
        img1 = tmp_path / "a.png"
        img2 = tmp_path / "b.png"
        img1.write_bytes(b"fake_a")
        img2.write_bytes(b"fake_b")

        fake_img = _make_fake_image(width=2000, height=1000)
        mock_buf = MagicMock()
        mock_buf.getvalue.return_value = b"fake_jpeg"

        with patch(
            "PIL.Image.open", return_value=fake_img
        ) as mock_open:
            with patch(
                "io.BytesIO", return_value=mock_buf
            ):
                _img_to_data_url(str(img1))
                _img_to_data_url(str(img2))
                assert mock_open.call_count == 2

    def test_cache_different_sizes(self, tmp_path: Path) -> None:
        """Changing _VISION_MAX_WIDTH must produce a different cache key."""
        img_path = tmp_path / "screenshot.png"
        img_path.write_bytes(b"fake_png_bytes")

        fake_img = _make_fake_image(width=2000, height=1000)
        mock_buf = MagicMock()
        mock_buf.getvalue.return_value = b"fake_jpeg"

        with patch(
            "PIL.Image.open", return_value=fake_img
        ) as mock_open:
            with patch(
                "io.BytesIO", return_value=mock_buf
            ):
                with patch.object(openai_module, "_VISION_MAX_WIDTH", 1920):
                    _img_to_data_url(str(img_path))
                    assert mock_open.call_count == 1

                with patch.object(openai_module, "_VISION_MAX_WIDTH", 1280):
                    _img_to_data_url(str(img_path))
                    assert mock_open.call_count == 2

    def test_cache_ttl_expires(self, tmp_path: Path) -> None:
        """After TTL expires, the image must be re-processed."""
        img_path = tmp_path / "screenshot.png"
        img_path.write_bytes(b"fake_png_bytes")

        fake_img = _make_fake_image(width=2000, height=1000)
        mock_buf = MagicMock()
        mock_buf.getvalue.return_value = b"fake_jpeg"

        with patch(
            "PIL.Image.open", return_value=fake_img
        ) as mock_open:
            with patch(
                "io.BytesIO", return_value=mock_buf
            ):
                with patch("xmclaw.providers.llm.openai.time") as mock_time:
                    mock_time.time.return_value = 0.0
                    _img_to_data_url(str(img_path))
                    assert mock_open.call_count == 1

                    # Within TTL
                    mock_time.time.return_value = 100.0
                    _img_to_data_url(str(img_path))
                    assert mock_open.call_count == 1

                    # Past TTL
                    mock_time.time.return_value = 301.0
                    _img_to_data_url(str(img_path))
                    assert mock_open.call_count == 2

    def test_cache_cleared_on_close(self, tmp_path: Path) -> None:
        """Provider.close() must clear the module-level cache."""
        img_path = tmp_path / "screenshot.png"
        img_path.write_bytes(b"fake_png_bytes")

        fake_img = _make_fake_image(width=2000, height=1000)
        mock_buf = MagicMock()
        mock_buf.getvalue.return_value = b"fake_jpeg"

        with patch(
            "PIL.Image.open", return_value=fake_img
        ) as mock_open:
            with patch(
                "io.BytesIO", return_value=mock_buf
            ):
                _img_to_data_url(str(img_path))
                assert mock_open.call_count == 1

                llm = OpenAILLM(api_key="x")
                llm.close()

                _img_to_data_url(str(img_path))
                assert mock_open.call_count == 2

    def test_messages_to_openai_result_consistent(self, tmp_path: Path) -> None:
        """Caching must not change the wire shape produced by _messages_to_openai."""
        img_path = tmp_path / "screenshot.png"
        img_path.write_bytes(b"fake_png_bytes")

        fake_img = _make_fake_image(width=2000, height=1000)
        mock_buf = MagicMock()
        mock_buf.getvalue.return_value = b"fake_jpeg"

        msgs = [Message(role="user", content="look", images=(str(img_path),))]

        with patch(
            "PIL.Image.open", return_value=fake_img
        ):
            with patch(
                "io.BytesIO", return_value=mock_buf
            ):
                result_first = OpenAILLM._messages_to_openai(
                    msgs, model="gpt-4o"
                )

        _clear_caches()

        with patch(
            "PIL.Image.open", return_value=fake_img
        ):
            with patch(
                "io.BytesIO", return_value=mock_buf
            ):
                result_second = OpenAILLM._messages_to_openai(
                    msgs, model="gpt-4o"
                )

        assert result_first == result_second
