"""B-76: POST /api/v2/memory/embedding/configure writes the
``evolution.memory.embedding`` config section.

Pins:
  * happy path writes the section to disk + bumps in-memory config
  * missing model / non-positive dimensions → 400
  * api_key omitted means the section omits ``api_key`` (rather than
    storing an empty string and forcing every embed call to send it)
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
    r = client.post("/api/v2/memory/embedding/configure", json=body)
    return r.status_code, r.json()


def test_configure_embedding_happy_path(app, tmp_path) -> None:
    cfg_path = tmp_path / "config.json"
    with TestClient(app) as client:
        status, body = _post(client, {
            "provider": "openai",
            "base_url": "http://127.0.0.1:11434/v1",
            "model": "qwen3-embedding:0.6b",
            "dimensions": 1024,
        })
    assert status == 200, body
    assert body["ok"] is True
    assert body["restart_required"] is True
    sec = body["embedding"]
    assert sec["provider"] == "openai"
    assert sec["model"] == "qwen3-embedding:0.6b"
    assert sec["dimensions"] == 1024
    assert sec["base_url"] == "http://127.0.0.1:11434/v1"
    assert "api_key" not in sec  # omitted because user didn't provide

    # Disk reflects the change.
    on_disk = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert on_disk["evolution"]["memory"]["embedding"]["model"] == "qwen3-embedding:0.6b"


def test_configure_embedding_rejects_missing_model(app) -> None:
    with TestClient(app) as client:
        status, body = _post(client, {
            "provider": "openai", "dimensions": 1024,
        })
    assert status == 400
    assert "model" in body.get("error", "")


def test_configure_embedding_rejects_zero_dimensions(app) -> None:
    with TestClient(app) as client:
        status, body = _post(client, {
            "provider": "openai", "model": "x", "dimensions": 0,
        })
    assert status == 400
    assert "dimensions" in body.get("error", "")


def test_configure_embedding_keeps_api_key_when_provided(app) -> None:
    with TestClient(app) as client:
        status, body = _post(client, {
            "provider": "openai",
            "model": "text-embedding-3-small",
            "dimensions": 1536,
            "api_key": "sk-secret",
        })
    assert status == 200
    assert body["embedding"]["api_key"] == "sk-secret"
