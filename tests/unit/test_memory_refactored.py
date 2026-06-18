"""Memory tool unification -- 2026-06-18.

Covers the 10->1 merge: ``memory`` is the only tool advertised in
``list_tools()``, but every legacy name (memory_search, memory_compact,
...) still works via backward-compat shim with DeprecationWarning.
"""
from __future__ import annotations

import warnings
from unittest.mock import AsyncMock, MagicMock

import pytest

from xmclaw.core.ir import ToolCall
from xmclaw.providers.tool.builtin import BuiltinTools


def _call(name: str, args: dict) -> ToolCall:
    return ToolCall(name=name, args=args, provenance="synthetic")


@pytest.fixture
def tools_with_mock_svc(monkeypatch, tmp_path):
    svc = MagicMock()
    svc.remember = AsyncMock()
    svc.forget = AsyncMock(return_value=True)
    svc.correct = AsyncMock(return_value={"matched": True})
    svc.recall = AsyncMock(return_value=[])
    tools = BuiltinTools()
    monkeypatch.setattr(
        BuiltinTools, "_resolve_memory_v2_service",
        staticmethod(lambda: svc),
    )
    _isolated_store = MagicMock()
    _isolated_store.add = MagicMock(return_value=None)
    _isolated_store.remove = MagicMock(return_value=True)
    _isolated_store.list_jobs = MagicMock(return_value=[])
    monkeypatch.setattr(
        "xmclaw.core.scheduler.cron.default_cron_store",
        lambda: _isolated_store,
    )
    return tools, svc


# -- 1. list_tools() only advertises one memory tool ------------------


def test_list_tools_has_exactly_one_memory_tool():
    """After unification, ``memory`` must appear exactly once and none
    of the 10 legacy names must be present."""
    tools = BuiltinTools()
    specs = tools.list_tools()
    names = [s.name for s in specs]
    assert names.count("memory") == 1
    legacy = {
        "memory_search", "memory_compact", "memory_forget",
        "memory_correct", "memory_dedup", "memory_inspect",
        "memory_get", "memory_graph_neighbors", "memory_pin",
    }
    for old in legacy:
        assert old not in names, f"legacy tool {old!r} still advertised"


# -- 2. memory(action="search") works --------------------------------


@pytest.mark.asyncio
async def test_memory_search_routes_to_legacy_handler(tools_with_mock_svc):
    """``memory(action='search')`` delegates to the existing
    ``_memory_search`` handler.  We only verify the wiring here --
    semantic search logic is covered by the dedicated search suite."""
    tools, _ = tools_with_mock_svc
    # _memory_search needs a wired MemoryManager to avoid the
    # "not configured" gate, so we wire one and patch the handler.
    from unittest.mock import AsyncMock
    tools._memory_manager = MagicMock()
    tools._memory_search = AsyncMock(return_value=MagicMock(ok=True, content="hits"))
    r = await tools.invoke(_call("memory", {"action": "search", "query": "user preferences"}))
    assert r.ok is True
    tools._memory_search.assert_awaited_once()


# -- 3. memory(action="forget") works --------------------------------


@pytest.mark.asyncio
async def test_memory_forget_routes_to_v3_handler(tools_with_mock_svc):
    """``memory(action='forget')`` delegates to the v3
    ``_memory_multi_action`` handler (which uses old_fid / query)."""
    tools, svc = tools_with_mock_svc
    r = await tools.invoke(_call("memory", {
        "action": "forget",
        "old_fid": "deadbeef",
        "reason": "test",
    }))
    assert r.ok is True
    svc.forget.assert_awaited_once_with(
        fact_id="deadbeef", reason="test",
    )


# -- 4. Legacy tool names emit DeprecationWarning ----------------------


@pytest.mark.parametrize("old_name, expected_action", [
    ("memory_search", "search"),
    ("memory_compact", "compact"),
    ("memory_forget", "forget"),
    ("memory_correct", "correct"),
    ("memory_dedup", "dedup"),
    ("memory_inspect", "inspect"),
    ("memory_get", "get"),
    ("memory_graph_neighbors", "graph_neighbors"),
    ("memory_pin", "pin"),
])
@pytest.mark.asyncio
async def test_legacy_memory_tool_names_emit_deprecation_warning(
    old_name, expected_action, tools_with_mock_svc, monkeypatch,
):
    """Each of the 9 legacy names (excluding memory_multi_action which
    was the internal handler for the v3 ``memory`` tool itself) should
    trigger a DeprecationWarning and be rewritten to ``memory(action=...)``."""
    tools, _ = tools_with_mock_svc
    # Patch the unified dispatcher so we can spy on the rewritten call.
    from unittest.mock import AsyncMock
    tools._memory = AsyncMock(
        return_value=MagicMock(ok=True, content="ok"),
    )
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        r = await tools.invoke(_call(old_name, {}))
    assert r.ok is True
    # DeprecationWarning emitted.
    assert len(w) == 1
    assert issubclass(w[0].category, DeprecationWarning)
    assert old_name in str(w[0].message)
    assert expected_action in str(w[0].message)
    # Rewritten call has the unified name and the action parameter.
    call_arg = tools._memory.await_args.args[0]
    assert call_arg.name == "memory"
    assert call_arg.args.get("action") == expected_action


# -- 5. Unknown action returns error -----------------------------------


@pytest.mark.asyncio
async def test_unknown_memory_action_returns_error():
    tools = BuiltinTools()
    r = await tools.invoke(_call("memory", {"action": "not_a_real_action"}))
    assert r.ok is False
    assert "unknown memory action" in r.error
    assert "not_a_real_action" in r.error


# -- 6. multi_action sub_action works ----------------------------------


@pytest.mark.asyncio
async def test_memory_multi_action_with_sub_action(tools_with_mock_svc):
    """``action="multi_action"`` + ``sub_action="add"`` routes to the
    v3 handler just like ``action="add"``."""
    tools, svc = tools_with_mock_svc
    fake_fact = MagicMock(id="f1", bucket="misc", confidence=0.85)
    svc.remember.return_value = fake_fact
    r = await tools.invoke(_call("memory", {
        "action": "multi_action",
        "sub_action": "add",
        "text": "test fact",
        "bucket": "misc",
    }))
    assert r.ok is True
    svc.remember.assert_awaited_once()


@pytest.mark.asyncio
async def test_memory_multi_action_missing_sub_action_rejected():
    tools = BuiltinTools()
    r = await tools.invoke(_call("memory", {
        "action": "multi_action",
    }))
    assert r.ok is False
    assert "sub_action" in r.error


# -- 7. Backward compat: old invoke paths still work -------------------


@pytest.mark.asyncio
async def test_direct_memory_inspect_still_works(tools_with_mock_svc):
    """Invoking the old ``memory_inspect`` tool name directly should
    still reach the handler (no 500 / unknown tool)."""
    tools, svc = tools_with_mock_svc
    svc.recall = AsyncMock(return_value=[])
    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        r = await tools.invoke(_call("memory_inspect", {}))
    # The call is rewritten to memory(action='inspect') and then
    # dispatched to _memory_inspect, which checks svc.
    assert r.ok is True
