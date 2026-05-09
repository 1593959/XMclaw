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


# ── B-395 (Sprint 1): memory build error captured + cause-specific hint ─


def test_b395_memory_build_database_locked_error_classified() -> None:
    """Pre-B-395 the bare except at app.py:389 swallowed the
    SqliteVecMemory construction exception and the indexer block fell
    back to ``memory.enabled=false 或构造失败 — 检查 memory.* 节``,
    which is wrong when memory.enabled IS true. The user spent hours
    deleting memory.db (the suggested fix) without realising the
    actual cause was process lock contention. Now: capture the
    exception, classify by string match (locked / sqlite_vec / load_extension /
    other), and surface a cause-specific fix.

    Test injects a fake exception via monkeypatch on
    ``build_memory_from_config`` so the indexer block sees ``memory=None``
    + ``memory_build_error="OperationalError: database is locked"`` and
    must produce the locked-specific hint, NOT the legacy generic.
    """
    import xmclaw.daemon.app as app_mod

    def _fake_build(_cfg, bus=None):
        raise __import__("sqlite3").OperationalError("database is locked")

    # Patch BEFORE create_app reads the symbol via the local import.
    orig = app_mod.create_app
    # The import inside create_app is ``from xmclaw.daemon.factory
    # import build_memory_from_config`` — patch that module symbol.
    import xmclaw.daemon.factory as factory_mod
    real = factory_mod.build_memory_from_config
    factory_mod.build_memory_from_config = _fake_build
    # Also stub the embedding-provider check so the indexer block
    # doesn't trip the "embedder 未构造" branch (which fires BEFORE
    # the vec_provider branch where B-395 lives).
    import xmclaw.providers.memory.embedding as emb_mod
    real_emb = emb_mod.build_embedding_provider
    emb_mod.build_embedding_provider = lambda _cfg: object()  # truthy stub
    try:
        cfg = {
            "memory": {"enabled": True},
            "evolution": {
                "memory": {"embedding": {"model": "x", "dimensions": 1024}},
            },
        }
        app = orig(config=cfg)
        with TestClient(app) as client:
            body = _setup(client)
    finally:
        factory_mod.build_memory_from_config = real
        emb_mod.build_embedding_provider = real_emb

    err = body["indexer_start_error"] or ""
    # Must include the literal exception text (so user sees ground truth).
    assert "database is locked" in err
    # Must NOT use the legacy generic (which the user followed wrong).
    assert "memory.enabled=false 或构造失败" not in err
    # Must include the locked-specific hint (don't delete memory.db).
    assert "不要" in err or "不要删" in err
    assert "xmclaw stop" in err


def test_b395_memory_build_sqlite_vec_missing_classified() -> None:
    """When the exception text mentions ``sqlite_vec`` (package import
    failure), the hint should say ``pip install sqlite-vec`` instead of
    the database-locked guidance."""
    import xmclaw.daemon.factory as factory_mod

    def _fake_build(_cfg, bus=None):
        raise ImportError("No module named 'sqlite_vec'")

    real = factory_mod.build_memory_from_config
    factory_mod.build_memory_from_config = _fake_build
    import xmclaw.providers.memory.embedding as emb_mod
    real_emb = emb_mod.build_embedding_provider
    emb_mod.build_embedding_provider = lambda _cfg: object()
    try:
        cfg = {
            "memory": {"enabled": True},
            "evolution": {
                "memory": {"embedding": {"model": "x", "dimensions": 1024}},
            },
        }
        from xmclaw.daemon.app import create_app as _ca
        app = _ca(config=cfg)
        with TestClient(app) as client:
            body = _setup(client)
    finally:
        factory_mod.build_memory_from_config = real
        emb_mod.build_embedding_provider = real_emb

    err = body["indexer_start_error"] or ""
    assert "sqlite_vec" in err.lower() or "sqlite-vec" in err.lower()
    assert "pip install sqlite-vec" in err


# ── B-350 (Sprint 1): last_config_reload exposed in /api/v2/setup ─


def test_b350_setup_includes_last_config_reload_field() -> None:
    """Pre-B-350 the /api/v2/setup payload had no signal that a
    config edit happened since startup. Users saved a new LLM key,
    saw no UI feedback, and were left guessing whether the daemon
    had picked it up. Now the endpoint surfaces the latest
    CONFIG_RELOADED summary (or None when none happened yet).
    """
    app = create_app(config={})
    with TestClient(app) as client:
        body = _setup(client)
    # Field is always present (None when no reload has fired yet).
    assert "last_config_reload" in body
    assert body["last_config_reload"] is None


def test_b350_last_config_reload_populated_after_state_set() -> None:
    """The setup endpoint reads ``app.state.last_config_reload``;
    the watcher's CONFIG_RELOADED subscriber stashes the summary
    there. Verify that injecting state surfaces correctly without
    spinning up a real watcher (which needs a config file path).
    """
    app = create_app(config={})
    summary = {
        "changed_keys": ["llm.anthropic.api_key"],
        "top_changed": ["llm"],
        "restart_required": True,
        "runtime_only": False,
        "mtime": 1234567890.0,
    }
    app.state.last_config_reload = summary
    with TestClient(app) as client:
        body = _setup(client)
    assert body["last_config_reload"] == summary
    assert body["last_config_reload"]["restart_required"] is True
    assert body["last_config_reload"]["top_changed"] == ["llm"]
