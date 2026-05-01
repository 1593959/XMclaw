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

from pathlib import Path

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


def test_indexer_picks_up_bare_sqlitevec_state_memory(tmp_path, monkeypatch) -> None:
    """B-88 regression guard: ``_app.state.memory`` is a bare
    SqliteVecMemory, NOT a MemoryManager. Lifespan must recognise the
    direct case rather than walking a non-existent ``.providers`` list.

    Reproduces the user-visible "sqlite_vec 未挂载" false negative
    that came from the old ``for p in mgr.providers`` lookup which
    returned zero matches against a bare SqliteVecMemory.
    """
    from xmclaw.providers.memory.sqlite_vec import SqliteVecMemory

    db = tmp_path / "memory.db"
    vec = SqliteVecMemory(db)
    # A bare SqliteVecMemory must NOT be detected as a MemoryManager-
    # like object (it has no ``providers`` attribute). This pins both
    # halves of the fix.
    assert not hasattr(vec, "providers")
    assert isinstance(vec, SqliteVecMemory)


def test_indexer_start_error_propagates_to_setup(tmp_path, monkeypatch) -> None:
    """B-87: when lifespan recorded an indexer-start failure, /api/v2/setup
    surfaces the reason so the UI can show "向量索引启动失败" with the
    actual cause, not the misleading "等待 daemon 重启" fallback."""
    cfg = {
        "evolution": {
            "memory": {"embedding": {"model": "x", "dimensions": 1024}},
        },
    }
    app = create_app(config=cfg)
    with TestClient(app) as client:
        # Inject a synthetic indexer-start error onto app.state, then
        # query /api/v2/setup and expect it back in the payload.
        app.state.indexer_start_error = (
            "indexer 启动抛异常：ConnectionError: Ollama 拒绝连接"
        )
        body = _setup(client)
    assert body["indexer_start_error"] == (
        "indexer 启动抛异常：ConnectionError: Ollama 拒绝连接"
    )
    # embedding_configured stays true (config has it); indexer_running
    # stays false (state.memory_indexer is None) — UI uses both signals
    # plus indexer_start_error to render the right banner row.
    assert body["embedding_configured"] is True
    assert body["indexer_running"] is False


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
