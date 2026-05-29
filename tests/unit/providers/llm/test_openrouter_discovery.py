"""Tests for OpenRouter model directory auto-discovery (B-387)."""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from xmclaw.providers.llm._openrouter_discovery import (
    _OpenRouterCache,
    _cache_path,
    get_context_length,
    get_pricing,
    refresh_cache,
    is_cache_stale,
    list_models,
    warm_cache,
)
from xmclaw.utils.cost import Pricing


class TestOpenRouterCache:
    def test_parse_valid_response(self):
        cache = _OpenRouterCache()
        raw = {
            "data": [
                {
                    "id": "anthropic/claude-sonnet-4",
                    "name": "Claude Sonnet 4",
                    "context_length": 200000,
                    "pricing": {"prompt": "0.000003", "completion": "0.000015"},
                },
                {
                    "id": "openai/gpt-4o",
                    "name": "GPT-4o",
                    "context_length": 128000,
                    "pricing": {"prompt": "0.0000025", "completion": "0.00001"},
                },
            ]
        }
        parsed = cache._parse(raw)
        assert "anthropic/claude-sonnet-4" in parsed
        assert parsed["anthropic/claude-sonnet-4"]["context_length"] == 200000

    def test_context_length_from_entry(self):
        cache = _OpenRouterCache()
        cache._data = {
            "anthropic/claude-sonnet-4": {
                "context_length": 200000,
                "pricing": {},
            }
        }
        assert cache.context_length("anthropic/claude-sonnet-4") == 200000
        assert cache.context_length("unknown/model") is None

    def test_pricing_from_entry(self):
        cache = _OpenRouterCache()
        cache._data = {
            "anthropic/claude-sonnet-4": {
                "pricing": {"prompt": "0.000003", "completion": "0.000015"},
            }
        }
        pricing = cache.pricing("anthropic/claude-sonnet-4")
        assert pricing is not None
        assert pricing.input_per_mtok == 3.0
        assert pricing.output_per_mtok == 15.0

    def test_pricing_zero_returns_none(self):
        cache = _OpenRouterCache()
        cache._data = {
            "free/model": {"pricing": {"prompt": "0", "completion": "0"}},
        }
        assert cache.pricing("free/model") is None

    def test_is_stale_when_empty(self):
        cache = _OpenRouterCache()
        cache._data = {}
        assert cache.is_stale() is True

    def test_is_stale_after_ttl(self):
        cache = _OpenRouterCache()
        cache._data = {"x": {}}
        cache._last_refresh_ts = time.time() - 25 * 3600
        assert cache.is_stale() is True

    def test_is_fresh_within_ttl(self):
        cache = _OpenRouterCache()
        cache._data = {"x": {}}
        cache._last_refresh_ts = time.time() - 1 * 3600
        assert cache.is_stale() is False

    def test_save_and_load_roundtrip(self, tmp_path):
        cache = _OpenRouterCache()
        cache._data = {"test/model": {"context_length": 12345}}
        cache._last_refresh_ts = 1234567890.0
        # Override cache path to tmp_path
        with patch("xmclaw.providers.llm._openrouter_discovery._cache_path", return_value=tmp_path / "cache.json"):
            cache._save_to_disk()
            loaded = _OpenRouterCache()
            with patch("xmclaw.providers.llm._openrouter_discovery._cache_path", return_value=tmp_path / "cache.json"):
                loaded._load_from_disk()
        assert "test/model" in loaded._data
        assert loaded._last_refresh_ts == 1234567890.0


class TestPublicAPI:
    def test_get_context_length_miss_returns_none(self):
        # Ensure cache is empty for this test
        from xmclaw.providers.llm import _openrouter_discovery as _mod
        with patch.object(_mod._CACHE, "_data", {}):
            assert get_context_length("nonexistent/model") is None

    def test_get_pricing_miss_returns_none(self):
        from xmclaw.providers.llm import _openrouter_discovery as _mod
        with patch.object(_mod._CACHE, "_data", {}):
            assert get_pricing("nonexistent/model") is None

    def test_list_models(self):
        from xmclaw.providers.llm import _openrouter_discovery as _mod
        with patch.object(_mod._CACHE, "_data", {"a/b": {}, "c/d": {}}):
            models = list_models()
            assert "a/b" in models
            assert "c/d" in models

    def test_refresh_cache_success(self):
        from xmclaw.providers.llm import _openrouter_discovery as _mod
        raw_data = {
            "data": [
                {
                    "id": "test/model",
                    "context_length": 64000,
                    "pricing": {"prompt": "0.000001", "completion": "0.000002"},
                }
            ]
        }

        with patch.object(_mod._CACHE, "_fetch", return_value=raw_data):
            ok = refresh_cache()
            assert ok is True
            assert get_context_length("test/model") == 64000
            pricing = get_pricing("test/model")
            assert pricing is not None
            assert pricing.input_per_mtok == 1.0
            assert pricing.output_per_mtok == 2.0

    def test_refresh_cache_http_error(self):
        from xmclaw.providers.llm import _openrouter_discovery as _mod
        with patch.object(_mod._CACHE, "_fetch", side_effect=Exception("HTTP 500")):
            ok = refresh_cache()
            assert ok is False

    def test_warm_cache_triggers_refresh_when_stale(self):
        from xmclaw.providers.llm import _openrouter_discovery as _mod
        with patch.object(_mod._CACHE, "is_stale", return_value=True):
            with patch.object(_mod._CACHE, "refresh", return_value=True) as mock_refresh:
                warm_cache()
                mock_refresh.assert_called_once()


class TestIntegrationWithStaticTables:
    """Verify that OpenRouter discovery is consulted BEFORE static tables."""

    def test_openrouter_context_length_takes_precedence(self):
        from xmclaw.providers.llm._provider_profiles import get_model_context_length

        # The static table has "gpt-4o" → 128_000.
        # If we mock OpenRouter to return 200_000 for "gpt-4o",
        # the discovered value should win.
        with patch(
            "xmclaw.providers.llm._openrouter_discovery.get_context_length",
            return_value=200_000,
        ):
            result = get_model_context_length("gpt-4o")
            assert result == 200_000

    def test_fallback_to_static_table_when_discovery_misses(self):
        from xmclaw.providers.llm._provider_profiles import get_model_context_length

        with patch(
            "xmclaw.providers.llm._openrouter_discovery.get_context_length",
            return_value=None,
        ):
            result = get_model_context_length("gpt-4o")
            # Static table value
            assert result == 128_000

    def test_openrouter_pricing_takes_precedence(self):
        from xmclaw.utils.cost import lookup_pricing

        with patch(
            "xmclaw.providers.llm._openrouter_discovery.get_pricing",
            return_value=Pricing(99.0, 99.0),
        ):
            result = lookup_pricing("gpt-4o")
            assert result.input_per_mtok == 99.0
            assert result.output_per_mtok == 99.0

    def test_fallback_to_static_pricing_when_discovery_misses(self):
        from xmclaw.utils.cost import lookup_pricing

        with patch(
            "xmclaw.providers.llm._openrouter_discovery.get_pricing",
            return_value=None,
        ):
            result = lookup_pricing("gpt-4o")
            # Static table: gpt-4o → 2.5 / 10.0
            assert result.input_per_mtok == 2.5
            assert result.output_per_mtok == 10.0
