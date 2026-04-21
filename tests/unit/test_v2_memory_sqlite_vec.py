"""SqliteVecMemory unit tests.

Anti-req #2 is proven in code here: vector retrieval works, hierarchical
layers are separated, memory is never auto-injected (this module only
stores and retrieves — caller controls prompt stitching).
"""
from __future__ import annotations

import asyncio

import pytest

from xmclaw.providers.memory.base import MemoryItem
from xmclaw.providers.memory.sqlite_vec import SqliteVecMemory


def _item(
    text: str,
    *,
    layer: str = "short",
    id: str = "",  # noqa: A002
    metadata: dict | None = None,
    embedding: list[float] | None = None,
    ts: float = 0.0,
) -> MemoryItem:
    return MemoryItem(
        id=id, layer=layer, text=text, metadata=metadata or {},
        embedding=tuple(embedding) if embedding else None, ts=ts,
    )


# ── basics ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_put_assigns_id_when_empty() -> None:
    mem = SqliteVecMemory(":memory:")
    new_id = await mem.put("short", _item("hello"))
    assert new_id  # non-empty
    results = await mem.query("short")
    assert len(results) == 1
    assert results[0].text == "hello"
    assert results[0].id == new_id
    mem.close()


@pytest.mark.asyncio
async def test_put_respects_caller_id() -> None:
    mem = SqliteVecMemory(":memory:")
    returned = await mem.put("short", _item("a", id="caller-chosen"))
    assert returned == "caller-chosen"
    mem.close()


@pytest.mark.asyncio
async def test_put_stores_metadata_roundtrip() -> None:
    mem = SqliteVecMemory(":memory:")
    await mem.put("short", _item("x", metadata={"tag": "note", "score": 0.5}))
    [got] = await mem.query("short")
    assert got.metadata == {"tag": "note", "score": 0.5}
    mem.close()


# ── hierarchical layers (anti-req #2) ─────────────────────────────────────

@pytest.mark.asyncio
async def test_layers_are_isolated() -> None:
    mem = SqliteVecMemory(":memory:")
    await mem.put("short",   _item("short-note"))
    await mem.put("working", _item("working-note"))
    await mem.put("long",    _item("long-note"))

    assert len(await mem.query("short")) == 1
    assert len(await mem.query("working")) == 1
    assert len(await mem.query("long")) == 1
    assert (await mem.query("short"))[0].text == "short-note"
    assert (await mem.query("long"))[0].text == "long-note"
    mem.close()


@pytest.mark.asyncio
async def test_query_most_recent_ordering() -> None:
    mem = SqliteVecMemory(":memory:")
    await mem.put("short", _item("old",    ts=1.0))
    await mem.put("short", _item("middle", ts=2.0))
    await mem.put("short", _item("new",    ts=3.0))
    results = await mem.query("short")
    assert [r.text for r in results] == ["new", "middle", "old"]
    mem.close()


# ── k cap + text fallback ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_k_caps_result_count() -> None:
    mem = SqliteVecMemory(":memory:")
    for i in range(25):
        await mem.put("short", _item(f"note {i}", ts=float(i)))
    out = await mem.query("short", k=5)
    assert len(out) == 5
    mem.close()


@pytest.mark.asyncio
async def test_text_substring_fallback() -> None:
    mem = SqliteVecMemory(":memory:")
    await mem.put("short", _item("the fox runs"))
    await mem.put("short", _item("a dog barks"))
    await mem.put("short", _item("fox and dog meet"))
    results = await mem.query("short", text="fox")
    texts = [r.text for r in results]
    assert "the fox runs" in texts
    assert "fox and dog meet" in texts
    assert "a dog barks" not in texts
    mem.close()


# ── semantic (anti-req #2 core) ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_vector_retrieval_returns_nearest_first() -> None:
    """Vector query: caller supplies embeddings, provider returns by distance.

    Uses a toy 3-D space where similarity is geometrically obvious.
    """
    mem = SqliteVecMemory(":memory:", embedding_dim=3)

    # Three items in 3-D space
    await mem.put("long", _item("red",   embedding=[1.0, 0.0, 0.0]))
    await mem.put("long", _item("green", embedding=[0.0, 1.0, 0.0]))
    await mem.put("long", _item("blue",  embedding=[0.0, 0.0, 1.0]))

    # Query near "red"
    results = await mem.query("long", embedding=[0.9, 0.1, 0.0], k=3)
    assert results[0].text == "red"
    # the other two should follow; ordering between them is secondary
    assert {r.text for r in results[1:]} == {"green", "blue"}
    mem.close()


@pytest.mark.asyncio
async def test_embedding_dim_frozen_after_first_put() -> None:
    """Lazy vec-table creation freezes dim on the first put with embedding."""
    mem = SqliteVecMemory(":memory:")  # dim unspecified
    await mem.put("long", _item("a", embedding=[1.0, 0.0, 0.0]))
    with pytest.raises(ValueError, match="embedding dim"):
        await mem.put("long", _item("b", embedding=[1.0, 0.0]))
    mem.close()


@pytest.mark.asyncio
async def test_query_without_embedding_falls_back_when_no_vec_table() -> None:
    """If no embeddings have been stored, a query w/ embedding should still
    produce results (timestamp-ordered or text-matched), not crash."""
    mem = SqliteVecMemory(":memory:")  # dim unspecified, no vec table
    await mem.put("short", _item("hi", ts=1.0))
    results = await mem.query("short", embedding=[0.1, 0.2, 0.3])
    assert len(results) == 1
    assert results[0].text == "hi"
    mem.close()


# ── filters ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_metadata_filter_exact_match() -> None:
    mem = SqliteVecMemory(":memory:")
    await mem.put("short", _item("one",   metadata={"kind": "note"}))
    await mem.put("short", _item("two",   metadata={"kind": "task"}))
    await mem.put("short", _item("three", metadata={"kind": "note"}))
    results = await mem.query("short", filters={"kind": "note"})
    assert {r.text for r in results} == {"one", "three"}
    mem.close()


# ── forget + prune (explicit never silent) ────────────────────────────────

@pytest.mark.asyncio
async def test_forget_removes_item() -> None:
    mem = SqliteVecMemory(":memory:")
    item_id = await mem.put("short", _item("x"))
    assert len(await mem.query("short")) == 1
    await mem.forget(item_id)
    assert len(await mem.query("short")) == 0
    mem.close()


@pytest.mark.asyncio
async def test_prune_by_ttl_removes_old_items_only() -> None:
    mem = SqliteVecMemory(":memory:", ttl={"short": 100.0})
    import time as _time
    now = _time.time()
    # 'old' was written 1000s ago — past TTL
    # 'fresh' was written just now — within TTL
    await mem.put("short", _item("old",   ts=now - 1000.0))
    await mem.put("short", _item("fresh", ts=now))
    removed = await mem.prune("short")
    assert removed == 1
    surviving = await mem.query("short")
    assert [r.text for r in surviving] == ["fresh"]
    mem.close()


@pytest.mark.asyncio
async def test_prune_long_layer_is_noop_by_default() -> None:
    mem = SqliteVecMemory(":memory:")  # default long TTL = None
    await mem.put("long", _item("perma", ts=0.0))
    removed = await mem.prune("long")
    assert removed == 0
    assert len(await mem.query("long")) == 1
    mem.close()


# ── anti-req #2: no silent prompt injection ───────────────────────────────

def test_memory_provider_has_no_auto_inject_method() -> None:
    """Anti-req #2: the provider MUST NOT expose any method that secretly
    stuffs memory into a caller's prompt or system message. RAG-style
    retrieval is caller-driven.

    This test fails if someone adds a method like ``stuff_into_prompt`` /
    ``inject_context`` / ``build_system_prefix`` etc. to the provider.
    """
    import xmclaw.providers.memory.sqlite_vec as mod
    cls = mod.SqliteVecMemory
    banned = {
        "inject", "inject_into_prompt", "stuff_into_prompt",
        "build_system_prefix", "auto_context", "freeze_into_system",
    }
    methods = {m for m in dir(cls) if not m.startswith("_")}
    overlap = methods & banned
    assert not overlap, f"anti-req #2 violated: found auto-inject methods {overlap}"
