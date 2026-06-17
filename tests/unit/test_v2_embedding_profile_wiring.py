"""embedding profile wiring + fingerprint guard.

2026-06-17. Covers the '走偏' fix: an ``embedding``-capability LLM profile
must be consumed by ``build_embedding_provider`` (else a user who added an
embedding model still got keyword-only recall). Plus the fingerprint guard
that catches an embedding-model change instead of silently degrading.
"""
from __future__ import annotations

import pytest

from xmclaw.providers.memory.embedding import (
    OpenAIEmbeddingProvider,
    build_embedding_provider,
)
from xmclaw.providers.memory.embedding_guard import (
    fingerprint_status,
    guard_embedder,
)


@pytest.fixture(autouse=True)
def _clear_embed_env(monkeypatch: pytest.MonkeyPatch):
    for k in ("XMC_EMBEDDING_API_KEY", "XMC_EMBEDDING_BASE_URL",
              "XMC_EMBEDDING_MODEL", "XMC_EMBEDDING_DIMENSIONS"):
        monkeypatch.delenv(k, raising=False)


# ── Part A: profile wiring ──────────────────────────────────────────


def test_explicit_capability_profile_is_used() -> None:
    cfg = {"llm": {"profiles": [
        {"id": "e", "provider": "openai_compat", "model": "my-embedder-v1",
         "api_key": "sk-e", "base_url": "https://api.example.com/v1",
         "capabilities": ["embedding"]},
    ]}}
    p = build_embedding_provider(cfg)
    assert isinstance(p, OpenAIEmbeddingProvider)
    assert p.model == "my-embedder-v1"


def test_name_heuristic_profile_is_used() -> None:
    cfg = {"llm": {"profiles": [
        {"id": "e", "provider": "openai_compat", "model": "bge-m3",
         "api_key": "sk-e", "base_url": "https://api.example.com/v1"},
    ]}}
    p = build_embedding_provider(cfg)
    assert isinstance(p, OpenAIEmbeddingProvider)
    assert p.model == "bge-m3"


def test_local_ollama_embedding_profile_needs_no_key() -> None:
    cfg = {"llm": {"profiles": [
        {"id": "e", "provider": "openai_compat", "model": "nomic-embed-text",
         "base_url": "http://localhost:11434/v1"},
    ]}}
    p = build_embedding_provider(cfg)
    assert isinstance(p, OpenAIEmbeddingProvider)  # local → auth-free


def test_chat_only_profile_is_not_mistaken_for_embedder() -> None:
    cfg = {"llm": {"profiles": [
        {"id": "c", "provider": "openai_compat", "model": "gpt-4o",
         "api_key": "sk-c", "base_url": "https://api.example.com/v1"},
    ]}}
    assert build_embedding_provider(cfg) is None


def test_explicit_embedding_block_takes_priority() -> None:
    cfg = {
        "evolution": {"memory": {"embedding": {
            "api_key": "sk-block", "model": "text-embedding-3-small",
            "base_url": "https://api.openai.com/v1",
        }}},
        "llm": {"profiles": [
            {"id": "e", "provider": "openai_compat", "model": "bge-m3",
             "api_key": "sk-e", "base_url": "https://api.example.com/v1"},
        ]},
    }
    p = build_embedding_provider(cfg)
    assert isinstance(p, OpenAIEmbeddingProvider)
    assert p.model == "text-embedding-3-small"  # block wins


# ── fingerprint identity ────────────────────────────────────────────


def test_fingerprint_folds_model_and_dim() -> None:
    a = OpenAIEmbeddingProvider(api_key="k", model="bge-m3", dimensions=1024)
    b = OpenAIEmbeddingProvider(api_key="k", model="text-embedding-3-small", dimensions=1024)
    c = OpenAIEmbeddingProvider(api_key="k", model="bge-m3", dimensions=512)
    assert a.fingerprint != b.fingerprint  # same dim, diff model
    assert a.fingerprint != c.fingerprint  # same model, diff dim
    assert a.fingerprint == "openai:bge-m3:1024"


# ── Part B: fingerprint guard ───────────────────────────────────────


def test_guard_fresh_then_match_then_mismatch(tmp_path) -> None:
    state, prev = fingerprint_status(tmp_path, "openai:bge-m3:1024")
    assert state == "fresh" and prev is None
    assert (tmp_path / ".embedding_fingerprint").read_text() == "openai:bge-m3:1024"

    state, prev = fingerprint_status(tmp_path, "openai:bge-m3:1024")
    assert state == "match" and prev == "openai:bge-m3:1024"

    state, prev = fingerprint_status(tmp_path, "openai:other:512")
    assert state == "mismatch" and prev == "openai:bge-m3:1024"
    # mismatch must NOT overwrite — the warning persists until a rebuild.
    assert (tmp_path / ".embedding_fingerprint").read_text() == "openai:bge-m3:1024"


def test_guard_embedder_returns_true_only_on_change(tmp_path) -> None:
    e1 = OpenAIEmbeddingProvider(api_key="k", model="bge-m3", dimensions=1024)
    e2 = OpenAIEmbeddingProvider(api_key="k", model="text-embedding-3-small", dimensions=1024)
    assert guard_embedder(tmp_path, e1) is False  # fresh
    assert guard_embedder(tmp_path, e1) is False  # match
    assert guard_embedder(tmp_path, e2) is True   # changed → warn
