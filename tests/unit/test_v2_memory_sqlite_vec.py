"""SqliteVecMemory unit tests.

Anti-req #2 is proven in code here: vector retrieval works, hierarchical
layers are separated, memory is never auto-injected (this module only
stores and retrieves — caller controls prompt stitching).
"""
from __future__ import annotations

import asyncio
import sqlite3

import pytest

from xmclaw.providers.memory.base import MemoryItem
from xmclaw.providers.memory.sqlite_vec import SqliteVecMemory


def _sqlite_extension_supported() -> bool:
    """Whether this Python build's sqlite3 can load extensions.

    macOS system Python and some Homebrew/pyenv builds disable it;
    all Windows builds and most Linux distros enable it.
    """
    try:
        conn = sqlite3.connect(":memory:")
        has_attr = hasattr(conn, "enable_load_extension")
        conn.close()
        return has_attr
    except Exception:  # noqa: BLE001
        return False


requires_vec = pytest.mark.skipif(
    not _sqlite_extension_supported(),
    reason=(
        "sqlite3 extension loading not available on this Python build — "
        "vector retrieval skipped; non-vector paths still tested"
    ),
)


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

@requires_vec
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


@requires_vec
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


# ── evict() — Epic #5 cap-based LRU (+ pinned bypass) ──────────────────────

@pytest.mark.asyncio
async def test_evict_both_caps_none_is_noop() -> None:
    mem = SqliteVecMemory(":memory:")
    for i in range(3):
        await mem.put("short", _item(f"n{i}", ts=float(i + 1)))
    removed = await mem.evict("short")
    assert removed == 0
    assert len(await mem.query("short")) == 3
    mem.close()


@pytest.mark.asyncio
async def test_evict_max_items_trims_oldest_first() -> None:
    mem = SqliteVecMemory(":memory:")
    for i in range(5):
        await mem.put("short", _item(f"n{i}", ts=float(i + 1)))
    removed = await mem.evict("short", max_items=2)
    assert removed == 3
    survivors = sorted(r.text for r in await mem.query("short"))
    assert survivors == ["n3", "n4"]
    mem.close()


@pytest.mark.asyncio
async def test_evict_noop_when_under_cap() -> None:
    mem = SqliteVecMemory(":memory:")
    await mem.put("short", _item("a", ts=1.0))
    await mem.put("short", _item("b", ts=2.0))
    removed = await mem.evict("short", max_items=5)
    assert removed == 0
    assert len(await mem.query("short")) == 2
    mem.close()


@pytest.mark.asyncio
async def test_evict_max_bytes_trims_oldest_first() -> None:
    mem = SqliteVecMemory(":memory:")
    # 10-byte texts each; keep 25 bytes → should retain 2 newest.
    for i, txt in enumerate(["aaaaaaaaaa", "bbbbbbbbbb", "cccccccccc", "dddddddddd"]):
        await mem.put("short", _item(txt, ts=float(i + 1)))
    removed = await mem.evict("short", max_bytes=25)
    assert removed == 2
    survivors = sorted(r.text for r in await mem.query("short"))
    assert survivors == ["cccccccccc", "dddddddddd"]
    mem.close()


@pytest.mark.asyncio
async def test_evict_max_bytes_zero_drops_all_non_pinned() -> None:
    mem = SqliteVecMemory(":memory:")
    await mem.put("short", _item("keep", metadata={"pinned": True}, ts=1.0))
    await mem.put("short", _item("drop_a", ts=2.0))
    await mem.put("short", _item("drop_b", ts=3.0))
    removed = await mem.evict("short", max_bytes=0)
    assert removed == 2
    survivors = [r.text for r in await mem.query("short")]
    assert survivors == ["keep"]
    mem.close()


@pytest.mark.asyncio
async def test_evict_pinned_items_are_exempt() -> None:
    mem = SqliteVecMemory(":memory:")
    await mem.put("short", _item("pin",  metadata={"pinned": True}, ts=1.0))
    await mem.put("short", _item("old",  ts=2.0))
    await mem.put("short", _item("mid",  ts=3.0))
    await mem.put("short", _item("new",  ts=4.0))
    # Cap at 1 non-pinned item — should drop "old" and "mid", keep "new" + pin.
    removed = await mem.evict("short", max_items=1)
    assert removed == 2
    survivors = sorted(r.text for r in await mem.query("short"))
    assert survivors == ["new", "pin"]
    mem.close()


@pytest.mark.asyncio
async def test_evict_pinned_does_not_count_against_item_cap() -> None:
    """Pinning 3 items + cap of 2 shouldn't trigger any eviction — caps
    govern only non-pinned rows."""
    mem = SqliteVecMemory(":memory:")
    for i in range(3):
        await mem.put(
            "short",
            _item(f"p{i}", metadata={"pinned": True}, ts=float(i + 1)),
        )
    removed = await mem.evict("short", max_items=2)
    assert removed == 0
    assert len(await mem.query("short")) == 3
    mem.close()


@pytest.mark.asyncio
async def test_evict_combined_caps_take_union() -> None:
    mem = SqliteVecMemory(":memory:")
    # 5 items, each 10 bytes.
    for i, c in enumerate("abcde"):
        await mem.put("short", _item(c * 10, ts=float(i + 1)))
    # max_items=3 alone would drop 2; max_bytes=30 alone would drop 2
    # (keep 3 newest). Both together still drop 2 — union is the same.
    removed = await mem.evict("short", max_items=3, max_bytes=30)
    assert removed == 2
    survivors = sorted(r.text for r in await mem.query("short"))
    assert survivors == ["c" * 10, "d" * 10, "e" * 10]
    mem.close()


@pytest.mark.asyncio
async def test_evict_combined_caps_picks_tighter_bound() -> None:
    mem = SqliteVecMemory(":memory:")
    # 5 items, 10 bytes each. max_items=4 drops 1; max_bytes=20 drops 3.
    # Union-of-evicted = the 3 oldest (bytes cap is tighter).
    for i, c in enumerate("abcde"):
        await mem.put("short", _item(c * 10, ts=float(i + 1)))
    removed = await mem.evict("short", max_items=4, max_bytes=20)
    assert removed == 3
    survivors = sorted(r.text for r in await mem.query("short"))
    assert survivors == ["d" * 10, "e" * 10]
    mem.close()


@pytest.mark.asyncio
async def test_evict_isolates_by_layer() -> None:
    mem = SqliteVecMemory(":memory:")
    for i in range(4):
        await mem.put("short",   _item(f"s{i}", ts=float(i + 1)))
    for i in range(4):
        await mem.put("working", _item(f"w{i}", ts=float(i + 1)))
    removed = await mem.evict("short", max_items=1)
    assert removed == 3
    assert len(await mem.query("short"))   == 1
    assert len(await mem.query("working")) == 4  # untouched
    mem.close()


@requires_vec
@pytest.mark.asyncio
async def test_evict_also_drops_embedding_rows(tmp_path) -> None:
    """Eviction must clean the sqlite-vec row too — otherwise dim-frozen
    vec tables leak orphan vectors."""
    db = tmp_path / "mem.db"
    mem = SqliteVecMemory(db, embedding_dim=2)
    if not mem._vec_supported:
        mem.close()
        pytest.skip("sqlite-vec extension not loadable here")
    await mem.put("short", _item("a", embedding=[1.0, 0.0], ts=1.0))
    await mem.put("short", _item("b", embedding=[0.0, 1.0], ts=2.0))
    removed = await mem.evict("short", max_items=1)
    assert removed == 1
    # Inspect via the same connection (it has the vec0 extension loaded).
    n = mem._conn.execute("SELECT COUNT(*) FROM memory_vec").fetchone()[0]
    assert n == 1
    mem.close()


@pytest.mark.asyncio
async def test_evict_pinned_tags_exempt_by_tag_scalar() -> None:
    mem = SqliteVecMemory(":memory:", pinned_tags=["identity", "promise"])
    await mem.put("short", _item("who-i-am",  metadata={"tag": "identity"}, ts=1.0))
    await mem.put("short", _item("random-a",  metadata={"tag": "chatter"},  ts=2.0))
    await mem.put("short", _item("random-b",  metadata={"tag": "chatter"},  ts=3.0))
    removed = await mem.evict("short", max_items=0)
    assert removed == 2
    survivors = [r.text for r in await mem.query("short")]
    assert survivors == ["who-i-am"]
    mem.close()


@pytest.mark.asyncio
async def test_evict_pinned_tags_exempt_by_tags_list() -> None:
    mem = SqliteVecMemory(":memory:", pinned_tags=["promise"])
    await mem.put("short", _item("a", metadata={"tags": ["promise", "later"]}, ts=1.0))
    await mem.put("short", _item("b", metadata={"tags": ["chatter"]},          ts=2.0))
    removed = await mem.evict("short", max_items=0)
    assert removed == 1
    survivors = [r.text for r in await mem.query("short")]
    assert survivors == ["a"]
    mem.close()


@pytest.mark.asyncio
async def test_evict_pinned_tags_exempt_by_category() -> None:
    mem = SqliteVecMemory(":memory:", pinned_tags=["system"])
    await mem.put("short", _item("sys", metadata={"category": "system"}, ts=1.0))
    await mem.put("short", _item("usr", metadata={"category": "user"},   ts=2.0))
    removed = await mem.evict("short", max_items=0)
    assert removed == 1
    assert [r.text for r in await mem.query("short")] == ["sys"]
    mem.close()


@pytest.mark.asyncio
async def test_evict_pinned_tags_none_keeps_default_behaviour() -> None:
    """Without pinned_tags, only the explicit metadata.pinned flag counts."""
    mem = SqliteVecMemory(":memory:")
    await mem.put("short", _item("t", metadata={"tag": "identity"}, ts=1.0))
    await mem.put("short", _item("u", metadata={"pinned": True},    ts=2.0))
    removed = await mem.evict("short", max_items=0)
    # Only "u" survives — "t" has no pinned flag and pinned_tags is empty.
    assert removed == 1
    survivors = [r.text for r in await mem.query("short")]
    assert survivors == ["u"]
    mem.close()


@pytest.mark.asyncio
async def test_evict_malformed_metadata_not_treated_as_pinned() -> None:
    """Corrupt metadata JSON must not accidentally immortalize a row."""
    mem = SqliteVecMemory(":memory:")
    # Insert a row directly with invalid JSON in the metadata column.
    mem._conn.execute(
        "INSERT INTO memory_items (id, layer, text, metadata, ts, has_embedding) "
        "VALUES (?, ?, ?, ?, ?, 0)",
        ("bad", "short", "garbage", "{not valid json", 1.0),
    )
    await mem.put("short", _item("new", ts=2.0))
    mem._conn.commit()
    removed = await mem.evict("short", max_items=1)
    assert removed == 1
    survivors = [r.text for r in await mem.query("short")]
    assert survivors == ["new"]
    mem.close()


# ── scale bench (Epic #5 exit criterion) ──────────────────────────────────

@pytest.mark.asyncio
async def test_evict_at_10k_items_is_fast(tmp_path) -> None:
    """Epic #5 exit criterion: ``evict()`` on 10k items must return in
    <100ms.

    This guards against a regression to an O(n²) or full-table-rewrite
    implementation. The ceiling here is 500ms (5x the target) so CI
    noise doesn't flake the build, but an order-of-magnitude slowdown
    still trips.

    Note: this is on-disk, not ``:memory:``, because insert throughput
    matters for setup and we want to measure a realistic file-backed
    path — the one the daemon actually uses.
    """
    import time

    mem = SqliteVecMemory(tmp_path / "mem.db")
    try:
        N = 10_000
        # Tight insert path: one transaction, then one commit.
        cur = mem._conn.cursor()
        cur.execute("BEGIN")
        for i in range(N):
            cur.execute(
                "INSERT INTO memory_items "
                "(id, layer, text, metadata, ts, has_embedding) "
                "VALUES (?, ?, ?, ?, ?, 0)",
                (f"id{i}", "short", f"note {i}", None, float(i + 1)),
            )
        mem._conn.commit()

        # Half the table is over the cap → realistic eviction workload.
        t0 = time.perf_counter()
        removed = await mem.evict("short", max_items=5_000)
        elapsed_ms = (time.perf_counter() - t0) * 1000

        assert removed == 5_000
        assert elapsed_ms < 500, (
            f"evict(10k items, max_items=5000) took {elapsed_ms:.1f}ms "
            f"(exit criterion is <100ms, guard is 5x headroom)"
        )
    finally:
        mem.close()


# ── stats() (Epic #5 phase 3 data surface) ────────────────────────────────

@pytest.mark.asyncio
async def test_stats_empty_db_returns_all_three_layers_zeroed() -> None:
    """CLI renders a stable three-row table even when nothing is stored."""
    mem = SqliteVecMemory(":memory:")
    try:
        s = await mem.stats()
        assert set(s) == {"short", "working", "long"}
        for layer in ("short", "working", "long"):
            assert s[layer] == {
                "count": 0,
                "bytes": 0,
                "pinned_count": 0,
                "oldest_ts": None,
                "newest_ts": None,
            }
    finally:
        mem.close()


@pytest.mark.asyncio
async def test_stats_counts_bytes_and_ts_range_per_layer() -> None:
    mem = SqliteVecMemory(":memory:")
    try:
        await mem.put("short", _item("aa", id="s1", ts=100.0))         # 2 bytes
        await mem.put("short", _item("bbbb", id="s2", ts=200.0))       # 4 bytes
        await mem.put("working", _item("xyz", id="w1", ts=150.0))      # 3 bytes
        s = await mem.stats()
        assert s["short"]["count"] == 2
        assert s["short"]["bytes"] == 6
        assert s["short"]["oldest_ts"] == 100.0
        assert s["short"]["newest_ts"] == 200.0
        assert s["working"]["count"] == 1
        assert s["working"]["bytes"] == 3
        assert s["working"]["oldest_ts"] == 150.0
        assert s["working"]["newest_ts"] == 150.0
        assert s["long"]["count"] == 0
        assert s["long"]["oldest_ts"] is None
    finally:
        mem.close()


@pytest.mark.asyncio
async def test_stats_bytes_counts_utf8_not_chars() -> None:
    mem = SqliteVecMemory(":memory:")
    try:
        # Three-byte CJK glyphs — 2 chars → 6 UTF-8 bytes.
        await mem.put("short", _item("你好", id="s1", ts=1.0))
        s = await mem.stats()
        assert s["short"]["count"] == 1
        assert s["short"]["bytes"] == 6
    finally:
        mem.close()


@pytest.mark.asyncio
async def test_stats_pinned_count_uses_same_rules_as_evict() -> None:
    """Operators reconcile 'what's protected' before tuning caps."""
    mem = SqliteVecMemory(":memory:", pinned_tags=["identity"])
    try:
        await mem.put("short", _item("a", id="a1", ts=1.0))
        await mem.put("short", _item("b", id="a2", ts=2.0,
                                      metadata={"pinned": True}))
        await mem.put("short", _item("c", id="a3", ts=3.0,
                                      metadata={"tag": "identity"}))
        await mem.put("short", _item("d", id="a4", ts=4.0,
                                      metadata={"category": "other"}))
        s = await mem.stats()
        assert s["short"]["count"] == 4
        assert s["short"]["pinned_count"] == 2   # a2 + a3
    finally:
        mem.close()


@pytest.mark.asyncio
async def test_stats_does_not_mutate() -> None:
    mem = SqliteVecMemory(":memory:")
    try:
        await mem.put("short", _item("hello", id="h1", ts=5.0))
        before = await mem.stats()
        # Call multiple times — no side effects.
        for _ in range(3):
            await mem.stats()
        after = await mem.stats()
        assert before == after
        # And the actual row is still there.
        rows = await mem.query("short")
        assert len(rows) == 1
        assert rows[0].id == "h1"
    finally:
        mem.close()


@pytest.mark.asyncio
async def test_stats_reflects_eviction() -> None:
    """After an evict() call, stats reports the reduced count/bytes."""
    mem = SqliteVecMemory(":memory:")
    try:
        for i in range(5):
            await mem.put("short", _item("x" * 10, id=f"e{i}", ts=float(i + 1)))
        s0 = await mem.stats()
        assert s0["short"]["count"] == 5
        assert s0["short"]["bytes"] == 50

        await mem.evict("short", max_items=2)
        s1 = await mem.stats()
        assert s1["short"]["count"] == 2
        assert s1["short"]["bytes"] == 20
        # Newest two (e3, e4) survive.
        assert s1["short"]["oldest_ts"] == 4.0
        assert s1["short"]["newest_ts"] == 5.0
    finally:
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
