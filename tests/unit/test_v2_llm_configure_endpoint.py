"""B-83: POST /api/v2/llm/configure — focused single-section LLM
endpoint used by SetupBanner's first-time API-key form.

Pins:
  * happy path writes llm.<provider>.api_key + reflects to disk
  * provider must be anthropic|openai
  * api_key required
  * default_provider auto-set when none was configured before
  * default_provider preserved when one is already set
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from xmclaw.daemon.app import create_app


@pytest.fixture
def app(tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"llm": {}}, indent=2), encoding="utf-8")
    return create_app(config={"llm": {}}, config_path=cfg_path)


def _post(client: TestClient, body: dict) -> tuple[int, dict]:
    r = client.post("/api/v2/llm/configure", json=body)
    return r.status_code, r.json()


def test_configure_llm_anthropic_happy(app, tmp_path) -> None:
    cfg_path = tmp_path / "config.json"
    with TestClient(app) as client:
        status, body = _post(client, {
            "provider": "anthropic",
            "api_key": "sk-ant-secret-key",
            "default_model": "claude-sonnet-4",
        })
    assert status == 200, body
    assert body["ok"] is True
    assert body["provider"] == "anthropic"
    assert body["restart_required"] is True
    on_disk = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert on_disk["llm"]["anthropic"]["api_key"] == "sk-ant-secret-key"
    assert on_disk["llm"]["anthropic"]["default_model"] == "claude-sonnet-4"
    # default_provider auto-set since cfg started empty.
    assert on_disk["llm"]["default_provider"] == "anthropic"


def test_configure_llm_rejects_unknown_provider(app) -> None:
    with TestClient(app) as client:
        status, body = _post(client, {
            "provider": "totally_made_up", "api_key": "x",
        })
    assert status == 400
    assert "provider" in body["error"]


def test_configure_llm_rejects_missing_api_key(app) -> None:
    with TestClient(app) as client:
        status, body = _post(client, {"provider": "openai"})
    assert status == 400
    assert "api_key" in body["error"]


def test_configure_llm_preserves_existing_default_provider(tmp_path) -> None:
    """If config already names anthropic as default, configuring openai
    should add openai's key but NOT change default_provider — the user
    explicitly set that and we don't want to silently override it."""
    cfg_path = tmp_path / "config.json"
    cfg = {
        "llm": {
            "default_provider": "anthropic",
            "anthropic": {"api_key": "old-key"},
        },
    }
    cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    app = create_app(config=cfg, config_path=cfg_path)
    with TestClient(app) as client:
        status, body = _post(client, {
            "provider": "openai", "api_key": "sk-openai-fresh",
        })
    assert status == 200
    on_disk = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert on_disk["llm"]["default_provider"] == "anthropic"  # unchanged
    assert on_disk["llm"]["openai"]["api_key"] == "sk-openai-fresh"
    # Old anthropic key untouched.
    assert on_disk["llm"]["anthropic"]["api_key"] == "old-key"


def test_configure_llm_overwrites_existing_provider_key(tmp_path) -> None:
    """User rotating their API key should be able to just submit the
    new value and have it land on disk."""
    cfg_path = tmp_path / "config.json"
    cfg = {"llm": {"openai": {"api_key": "old-key"}}}
    cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    app = create_app(config=cfg, config_path=cfg_path)
    with TestClient(app) as client:
        status, body = _post(client, {
            "provider": "openai", "api_key": "new-key",
        })
    assert status == 200
    on_disk = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert on_disk["llm"]["openai"]["api_key"] == "new-key"
