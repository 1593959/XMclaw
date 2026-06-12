"""Tests for the LLM endpoint discovery API.

Covers:
* POST /api/v2/llm/endpoints/discover — model list fetching
* POST /api/v2/llm/endpoints/apply — bulk profile creation
* POST /api/v2/llm/endpoints/hotload — in-memory registration
"""
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def tmp_config(tmp_path: Path) -> Path:
    """Minimal config.json so _config_path resolves."""
    cfg = tmp_path / "daemon" / "config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({"llm": {"profiles": []}}), encoding="utf-8")
    return cfg


@pytest.fixture()
def client_with_config(tmp_config: Path, monkeypatch: pytest.MonkeyPatch):
    """FastAPI TestClient with config_path set on app.state."""
    from xmclaw.daemon.app import create_app

    app = create_app()
    monkeypatch.setattr(
        app.state, "config_path", tmp_config, raising=False
    )
    # Also set config so list_profiles can read on-disk data
    monkeypatch.setattr(
        app.state, "config", {"llm": {"profiles": []}}, raising=False
    )
    # llm_registry is None → no runtime profiles yet
    monkeypatch.setattr(
        app.state, "llm_registry", None, raising=False
    )
    return TestClient(app)


class TestDiscoverEndpoint:
    """POST /api/v2/llm/endpoints/discover"""

    def test_missing_params_returns_400(self, client_with_config):
        resp = client_with_config.post(
            "/api/v2/llm/endpoints/discover",
            json={},
        )
        assert resp.status_code == 400
        body = resp.json()
        assert body["ok"] is False
        assert "base_url" in body["error"].lower() or "api_key" in body["error"].lower()

    def test_http_connect_error(self, client_with_config):
        """Connection refused → 200 with ok=false."""
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            import httpx
            mock_get.side_effect = httpx.ConnectError("refused")
            resp = client_with_config.post(
                "/api/v2/llm/endpoints/discover",
                json={
                    "base_url": "http://127.0.0.1:9999/v1",
                    "api_key": "test-key",
                    "provider": "openai",
                },
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert "connection" in body["error"].lower() or "refused" in body["error"].lower()
        assert body["models"] == []
        assert body["model_count"] == 0

    def test_success_with_mocked_models(self, client_with_config):
        """Successful /v1/models response."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "data": [
                {"id": "gpt-4o", "name": "GPT-4o", "created": 1700000000},
                {"id": "gpt-4o-mini", "name": "GPT-4o Mini", "created": 1700000000},
            ]
        }

        async def mock_get(*args, **kwargs):
            return mock_response

        with patch("httpx.AsyncClient.get", new=mock_get):
            resp = client_with_config.post(
                "/api/v2/llm/endpoints/discover",
                json={
                    "base_url": "https://api.openai.com/v1",
                    "api_key": "sk-test",
                    "provider": "openai",
                },
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["model_count"] == 2
        assert len(body["models"]) == 2
        assert body["models"][0]["id"] == "gpt-4o"
        assert body["models"][0]["name"] == "GPT-4o"
        assert "elapsed_ms" in body
        assert body["endpoint_id"]  # non-empty


class TestApplyEndpoint:
    """POST /api/v2/llm/endpoints/apply"""

    def test_missing_models_returns_400(self, client_with_config):
        resp = client_with_config.post(
            "/api/v2/llm/endpoints/apply",
            json={"base_url": "https://api.openai.com/v1", "api_key": "sk-test", "provider": "openai", "models": []},
        )
        assert resp.status_code == 400

    def test_creates_profiles_in_config(self, client_with_config, tmp_config: Path):
        resp = client_with_config.post(
            "/api/v2/llm/endpoints/apply",
            json={
                "endpoint_id": "abc123",
                "base_url": "https://api.openai.com/v1",
                "api_key": "sk-test",
                "provider": "openai",
                "models": ["gpt-4o", "gpt-4o-mini"],
                "options": {"max_tokens": 8192, "extended_thinking": True},
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["restart_required"] is True
        assert len(body["created"]) == 2

        # Verify config.json was written
        cfg = json.loads(tmp_config.read_text(encoding="utf-8"))
        profiles = cfg["llm"]["profiles"]
        assert len(profiles) == 2

        # Check first profile has all fields
        first = next(p for p in profiles if p["model"] == "gpt-4o")
        assert first["provider"] == "openai"
        assert first["base_url"] == "https://api.openai.com/v1"
        assert first["api_key"] == "sk-test"
        assert first["max_tokens"] == 8192
        assert first["extended_thinking"] is True

    def test_preserves_existing_profiles(self, client_with_config, tmp_config: Path):
        # Pre-populate config with one profile
        cfg = json.loads(tmp_config.read_text(encoding="utf-8"))
        cfg["llm"]["profiles"].append({
            "id": "existing_gpt35",
            "label": "GPT-3.5",
            "provider": "openai",
            "model": "gpt-3.5-turbo",
            "api_key": "sk-existing",
        })
        tmp_config.write_text(json.dumps(cfg), encoding="utf-8")
        # Also update app.state.config
        client_with_config.app.state.config = cfg

        resp = client_with_config.post(
            "/api/v2/llm/endpoints/apply",
            json={
                "endpoint_id": "abc123",
                "base_url": "https://api.openai.com/v1",
                "api_key": "sk-test",
                "provider": "openai",
                "models": ["gpt-4o"],
            },
        )
        assert resp.json()["ok"] is True

        cfg2 = json.loads(tmp_config.read_text(encoding="utf-8"))
        assert len(cfg2["llm"]["profiles"]) == 2  # 1 existing + 1 new


class TestHotloadEndpoint:
    """POST /api/v2/llm/endpoints/hotload"""

    def test_hotload_creates_in_memory_registry(self, client_with_config, tmp_config: Path):
        """Hot-load registers profiles into the in-memory registry."""
        # Set up a mock registry
        from xmclaw.daemon.llm_registry import LLMRegistry

        registry = LLMRegistry(profiles={}, default_id=None)
        client_with_config.app.state.llm_registry = registry

        resp = client_with_config.post(
            "/api/v2/llm/endpoints/hotload",
            json={
                "profiles": [
                    {
                        "id": "test_gpt4o",
                        "label": "GPT-4o",
                        "provider": "openai",
                        "model": "gpt-4o",
                        "api_key": "sk-hotload",
                        "base_url": "https://api.openai.com/v1",
                    }
                ]
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert "test_gpt4o" in body["hotloaded"]

        # Verify in-memory registry was updated
        assert "test_gpt4o" in registry.profiles
        prof = registry.profiles["test_gpt4o"]
        assert prof.model == "gpt-4o"
        assert prof.provider_name == "openai"

    def test_hotload_validation_rejects_bad_id(self, client_with_config):
        resp = client_with_config.post(
            "/api/v2/llm/endpoints/hotload",
            json={
                "profiles": [
                    {
                        "id": "BAD-ID",
                        "label": "Bad",
                        "provider": "openai",
                        "model": "gpt-4o",
                        "api_key": "sk-test",
                        "base_url": "https://api.openai.com/v1",
                    }
                ]
            },
        )
        assert resp.status_code == 400
        assert resp.json()["ok"] is False

    def test_hotload_rejects_reserved_default_id(self, client_with_config):
        resp = client_with_config.post(
            "/api/v2/llm/endpoints/hotload",
            json={
                "profiles": [
                    {
                        "id": "default",
                        "label": "Default",
                        "provider": "openai",
                        "model": "gpt-4o",
                        "api_key": "sk-test",
                        "base_url": "https://api.openai.com/v1",
                    }
                ]
            },
        )
        assert resp.status_code == 400
        assert "reserved" in resp.json()["error"].lower()

    def test_hotload_rejects_unknown_provider(self, client_with_config):
        resp = client_with_config.post(
            "/api/v2/llm/endpoints/hotload",
            json={
                "profiles": [
                    {
                        "id": "test_fake",
                        "label": "Fake",
                        "provider": "fake_provider_xyz",
                        "model": "fake-model",
                        "api_key": "sk-test",
                    }
                ]
            },
        )
        assert resp.status_code == 400
        assert resp.json()["ok"] is False
