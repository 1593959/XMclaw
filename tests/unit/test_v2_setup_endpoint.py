"""B-81: GET /api/v2/setup — onboarding checklist for the Web UI banner.

Pins:
  * Empty config → all checks fail, "ready"=false, all in missing[]
  * LLM key under llm.anthropic.api_key counts as configured
  * LLM key under llm.profiles[0].api_key also counts (named profile)
  * Persona ready when SOUL.md OR IDENTITY.md exists in active dir
  * Embedding requires both model AND dimensions (just one isn't enough)
  * Fully-set config → ready=true, missing=[]
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from xmclaw.daemon.app import create_app


def _setup(client: TestClient) -> dict:
    r = client.get("/api/v2/setup")
    assert r.status_code == 200
    return r.json()


def test_empty_config_shows_all_missing(tmp_path: Path, monkeypatch) -> None:
    # Point the persona resolver at an empty tmp dir so the test isn't
    # contaminated by ~/.xmclaw/persona/profiles/default left over from
    # prior runs on the dev machine.
    empty = tmp_path / "no_profile"
    monkeypatch.setattr(
        "xmclaw.daemon.factory._resolve_persona_profile_dir",
        lambda _cfg: empty,
    )
    app = create_app(config={})
    with TestClient(app) as client:
        body = _setup(client)
    assert body["ready"] is False
    assert "llm" in body["missing"]
    assert "persona" in body["missing"]
    assert "embedding" in body["missing"]
    assert body["llm_configured"] is False
    assert body["persona_ready"] is False
    assert body["embedding_configured"] is False


def test_llm_key_under_provider_block_counts(tmp_path: Path) -> None:
    cfg = {"llm": {"anthropic": {"api_key": "sk-real"}}}
    app = create_app(config=cfg)
    with TestClient(app) as client:
        body = _setup(client)
    assert body["llm_configured"] is True
    assert body["llm_provider"] == "anthropic"
    assert "llm" not in body["missing"]


def test_llm_key_under_named_profile_counts() -> None:
    cfg = {
        "llm": {
            "anthropic": {"api_key": ""},  # empty
            "profiles": [
                {"id": "main", "provider": "openai", "api_key": "sk-prof"},
            ],
        },
    }
    app = create_app(config=cfg)
    with TestClient(app) as client:
        body = _setup(client)
    assert body["llm_configured"] is True
    # Provider taken from the profile entry.
    assert body["llm_provider"] == "openai"


def test_empty_api_keys_dont_count() -> None:
    cfg = {
        "llm": {
            "anthropic": {"api_key": ""},
            "openai": {"api_key": "  "},  # whitespace
        },
    }
    app = create_app(config=cfg)
    with TestClient(app) as client:
        body = _setup(client)
    assert body["llm_configured"] is False
    assert "llm" in body["missing"]


def test_embedding_requires_model_and_dimensions() -> None:
    # Just model set → not enough.
    cfg = {
        "evolution": {
            "memory": {"embedding": {"model": "qwen3-embedding:0.6b"}},
        },
    }
    app = create_app(config=cfg)
    with TestClient(app) as client:
        body = _setup(client)
    assert body["embedding_configured"] is False

    # Both → counts.
    cfg2 = {
        "evolution": {
            "memory": {"embedding": {
                "model": "qwen3-embedding:0.6b", "dimensions": 1024,
            }},
        },
    }
    app2 = create_app(config=cfg2)
    with TestClient(app2) as client:
        body2 = _setup(client)
    assert body2["embedding_configured"] is True


def test_ready_true_when_everything_set(tmp_path, monkeypatch) -> None:
    """Persona check needs an actual filesystem dir. Wire the persona
    resolver to a writable tmp path with a stub SOUL.md."""
    pdir = tmp_path / "profile"
    pdir.mkdir()
    (pdir / "SOUL.md").write_text("# soul stub", encoding="utf-8")

    monkeypatch.setattr(
        "xmclaw.daemon.factory._resolve_persona_profile_dir",
        lambda _cfg: pdir,
    )

    cfg = {
        "llm": {"anthropic": {"api_key": "sk-real"}},
        "evolution": {
            "memory": {"embedding": {"model": "x", "dimensions": 1024}},
        },
    }
    app = create_app(config=cfg)
    with TestClient(app) as client:
        body = _setup(client)
    assert body["llm_configured"] is True
    assert body["persona_ready"] is True
    assert body["embedding_configured"] is True
    assert body["missing"] == []
    assert body["ready"] is True
