"""B-276 / B-277 / B-279: pin three memory subsystem fixes.

* B-276: ``MemoryManager.put`` raises ``MemoryPutError`` when ALL
  registered providers fail (vs silently returning None).
* B-277: ``BuiltinFileMemoryProvider.query`` actually applies the
  ``filters`` argument instead of ignoring it.
* B-279: ``MemoryManager.query`` accumulates partial results across
  providers up to ``k`` instead of stopping at the first non-empty
  list.
"""
from __future__ import annotations

import pytest

from xmclaw.providers.memory.base import Layer, MemoryItem, MemoryProvider
from xmclaw.providers.memory.manager import MemoryManager, MemoryPutError


# ── Test fixtures: tiny stub providers ────────────────────────────


class _AlwaysFailingProvider(MemoryProvider):
    """Provider whose put + query both raise.

    Named "builtin" so MemoryManager.add_provider doesn't treat
    several of these as duplicate-external (the manager only allows
    one external; tests stack 2+ stubs and that gate isn't what
    we're testing)."""
    name = "builtin"

    async def put(self, layer, item):
        raise RuntimeError("simulated put failure")

    async def query(self, layer, *, text=None, embedding=None, k=10, filters=None):
        raise RuntimeError("simulated query failure")

    async def forget(self, item_id):
        pass


class _CountingProvider(MemoryProvider):
    """Records calls; returns N predetermined items per query."""
    def __init__(self, name: str, items: list[MemoryItem]):
        self.name = name
        self._items = items
        self.put_calls = 0
        self.query_calls = 0

    async def put(self, layer, item):
        self.put_calls += 1
        return f"{self.name}-{self.put_calls}"

    async def query(self, layer, *, text=None, embedding=None, k=10, filters=None):
        self.query_calls += 1
        return list(self._items[:k])

    async def forget(self, item_id):
        pass


def _mk_item(item_id: str) -> MemoryItem:
    return MemoryItem(
        id=item_id, layer="session", text=item_id,
        metadata={}, ts=0.0,
    )


# ── B-276: put failure raises ───────────────────────────────────────


@pytest.mark.asyncio
async def test_b276_put_raises_when_all_providers_fail() -> None:
    mgr = MemoryManager()
    mgr.add_provider(_AlwaysFailingProvider())
    item = _mk_item("x")
    with pytest.raises(MemoryPutError):
        await mgr.put("session", item)


@pytest.mark.asyncio
async def test_b276_put_returns_none_when_no_providers_registered() -> None:
    """Distinct case: zero registered providers → silent None.
    Useful when memory is intentionally disabled."""
    mgr = MemoryManager()
    item = _mk_item("x")
    result = await mgr.put("session", item)
    assert result is None


@pytest.mark.asyncio
async def test_b276_put_succeeds_when_first_provider_fails_but_second_works() -> None:
    """Existing fallback semantic preserved — if the first provider
    fails but a later one succeeds, no exception."""
    mgr = MemoryManager()
    mgr.add_provider(_AlwaysFailingProvider())
    backup = _CountingProvider("builtin", [])
    mgr.add_provider(backup)
    item = _mk_item("x")
    rid = await mgr.put("session", item)
    assert rid == "builtin-1"


# ── B-279: manager.query accumulates partial results ───────────────


@pytest.mark.asyncio
async def test_b279_query_accumulates_partial_results() -> None:
    """Provider A returns 3 hits, provider B returns 4 more.
    With k=10 we want 7 total, not just A's 3."""
    a = _CountingProvider("builtin", [_mk_item(f"a-{i}") for i in range(3)])
    b = _CountingProvider("builtin", [_mk_item(f"b-{i}") for i in range(4)])
    mgr = MemoryManager()
    # Bypass add_provider's "one external only" gate by registering
    # multiple builtin-named stubs. Production wires 1 external + 1
    # builtin; for testing the fallthrough we just need 2 providers.
    mgr.add_provider(a)
    mgr.add_provider(b)
    out = await mgr.query("session", text="hi", k=10)
    out_ids = {h.id for h in out}
    assert "a-0" in out_ids
    assert "b-0" in out_ids
    assert len(out) == 7
    assert b.query_calls == 1  # B was actually consulted


@pytest.mark.asyncio
async def test_b279_query_stops_at_k() -> None:
    """When first provider returns >= k, second is skipped."""
    a = _CountingProvider("builtin", [_mk_item(f"a-{i}") for i in range(15)])
    b = _CountingProvider("builtin", [_mk_item(f"b-{i}") for i in range(10)])
    mgr = MemoryManager()
    mgr.add_provider(a)
    mgr.add_provider(b)
    out = await mgr.query("session", text="hi", k=10)
    assert len(out) == 10
    assert b.query_calls == 0  # B never consulted; A had enough


@pytest.mark.asyncio
async def test_b279_query_dedup_by_id() -> None:
    """Same id from two providers → only one in output."""
    a = _CountingProvider("builtin", [_mk_item("dup")])
    b = _CountingProvider("builtin", [_mk_item("dup"), _mk_item("uniq")])
    mgr = MemoryManager()
    mgr.add_provider(a)
    mgr.add_provider(b)
    out = await mgr.query("session", text="hi", k=10)
    out_ids = [h.id for h in out]
    assert out_ids.count("dup") == 1
    assert "uniq" in out_ids


@pytest.mark.asyncio
async def test_b279_query_failing_first_provider_falls_through() -> None:
    """Provider A raises; B has hits → return B's hits."""
    a = _AlwaysFailingProvider()
    b = _CountingProvider("builtin", [_mk_item("b-1"), _mk_item("b-2")])
    mgr = MemoryManager()
    mgr.add_provider(a)
    mgr.add_provider(b)
    out = await mgr.query("session", text="hi", k=10)
    assert {h.id for h in out} == {"b-1", "b-2"}


# ── B-277: builtin_file filters honored ────────────────────────────


@pytest.mark.asyncio
async def test_b277_builtin_file_filter_applies(tmp_path) -> None:
    """File-scoped filter only returns matching file's bullets."""
    from xmclaw.providers.memory.builtin_file import BuiltinFileMemoryProvider

    mem_path = tmp_path / "MEMORY.md"
    user_path = tmp_path / "USER.md"
    mem_path.write_text(
        "## Decisions\n- 决定 alpha\n- 决定 beta\n",
        encoding="utf-8",
    )
    user_path.write_text(
        "## Profile\n- 用户喜欢 alpha\n- 用户也喜欢 gamma\n",
        encoding="utf-8",
    )

    prov = BuiltinFileMemoryProvider(persona_dir_provider=lambda: tmp_path)

    # Without filter — both files contribute.
    out_all = await prov.query("session", text="alpha", k=10)
    files_in_all = {h.metadata["file"] for h in out_all}
    assert files_in_all == {"MEMORY.md", "USER.md"}

    # With file=MEMORY.md filter — only that file.
    out_mem = await prov.query(
        "session", text="alpha", k=10,
        filters={"file": "MEMORY.md"},
    )
    assert all(h.metadata["file"] == "MEMORY.md" for h in out_mem)
    assert len(out_mem) >= 1


@pytest.mark.asyncio
async def test_b277_filter_unknown_key_excludes_all(tmp_path) -> None:
    """A filter key the builtin doesn't track (e.g. session_id) →
    zero hits (strict). Pre-B-277 returned all bullets — that was
    the bug: cross-session queries leaked across all sessions."""
    from xmclaw.providers.memory.builtin_file import BuiltinFileMemoryProvider

    (tmp_path / "MEMORY.md").write_text(
        "## D\n- alpha\n- beta\n", encoding="utf-8",
    )
    prov = BuiltinFileMemoryProvider(persona_dir_provider=lambda: tmp_path)
    out = await prov.query(
        "session", text="alpha", k=10,
        filters={"session_id": "chat-123"},  # builtin_file doesn't store this
    )
    assert out == []
