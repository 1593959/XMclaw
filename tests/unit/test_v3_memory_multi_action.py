"""Memory v3 phase 4.1 — single-tool multi-action ``memory()``.

Pins the dispatch contract: bad action / missing args / each action's
delegation to MemoryService primitives. Spec-only tests (no actual
LanceDB writes) — full integration is covered by
``test_v3_recall_hybrid`` and the existing per-primitive suites.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from xmclaw.core.ir import ToolCall
from xmclaw.providers.tool.builtin import BuiltinTools


def _call(args: dict) -> ToolCall:
    return ToolCall(name="memory", args=args, provenance="synthetic")


@pytest.fixture
def tools_with_mock_svc(monkeypatch):
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
    return tools, svc


# ─── action validation ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_invalid_action_rejected():
    tools = BuiltinTools()
    r = await tools.invoke(_call({"action": "garbage"}))
    assert r.ok is False
    assert "add/replace/forget/pin" in r.error


@pytest.mark.asyncio
async def test_no_service_returns_clean_error(monkeypatch):
    monkeypatch.setattr(
        BuiltinTools, "_resolve_memory_v2_service",
        staticmethod(lambda: None),
    )
    tools = BuiltinTools()
    r = await tools.invoke(_call({"action": "add", "text": "x"}))
    assert r.ok is False
    assert "not wired" in r.error


# ─── action: add ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_add_requires_text(tools_with_mock_svc):
    tools, _ = tools_with_mock_svc
    r = await tools.invoke(_call({"action": "add"}))
    assert r.ok is False
    assert "text" in r.error.lower()


@pytest.mark.asyncio
async def test_add_delegates_to_remember(tools_with_mock_svc):
    tools, svc = tools_with_mock_svc
    fake_fact = MagicMock()
    fake_fact.id = "f123"
    fake_fact.bucket = "user_preference"
    svc.remember.return_value = fake_fact
    r = await tools.invoke(_call({
        "action": "add",
        "text": "用户偏好简洁回复",
        "bucket": "user_preference",
    }))
    assert r.ok is True
    assert r.content["fid"] == "f123"
    assert r.content["rendered_to"] == ["USER.md"]
    svc.remember.assert_awaited_once()
    kwargs = svc.remember.call_args.kwargs
    assert kwargs["bucket"] == "user_preference"


@pytest.mark.asyncio
async def test_add_unknown_bucket_coerces_to_misc(tools_with_mock_svc):
    """v3 phase 1.3 contract — bad bucket name lands in misc rather
    than failing. The multi-action handler must honour the same
    contract since it goes through ``buckets.resolve``."""
    tools, svc = tools_with_mock_svc
    fake_fact = MagicMock()
    fake_fact.id = "f1"
    fake_fact.bucket = "misc"
    svc.remember.return_value = fake_fact
    r = await tools.invoke(_call({
        "action": "add",
        "text": "x",
        "bucket": "not_a_real_bucket",
    }))
    assert r.ok is True
    kwargs = svc.remember.call_args.kwargs
    assert kwargs["bucket"] == "misc"


@pytest.mark.asyncio
async def test_add_commitment_without_due_ts_rejected(tools_with_mock_svc):
    tools, _ = tools_with_mock_svc
    r = await tools.invoke(_call({
        "action": "add",
        "text": "提醒用户开会",
        "bucket": "commitment",
    }))
    assert r.ok is False
    assert "due_ts" in r.error


@pytest.mark.asyncio
async def test_add_commitment_with_due_ts_embeds_marker(tools_with_mock_svc):
    """v3 phase 4.3: due_ts gets inlined as ``[due:YYYY-...]`` prefix
    in the stored text so .md render shows it; scheduling is
    best-effort + reported via the ``cron`` field."""
    tools, svc = tools_with_mock_svc
    fake_fact = MagicMock()
    fake_fact.id = "fc"
    fake_fact.bucket = "commitment"
    svc.remember.return_value = fake_fact
    import time as _t
    future = _t.time() + 60.0
    r = await tools.invoke(_call({
        "action": "add",
        "text": "提醒用户开项目会",
        "bucket": "commitment",
        "due_ts": future,
    }))
    assert r.ok is True
    # remember() takes text as a positional arg.
    stored_text = svc.remember.call_args.args[0]
    assert stored_text.startswith("[due:")
    assert "提醒用户开项目会" in stored_text
    # cron status surfaced — even on a "skipped" outcome this field
    # exists, so the LLM doesn't have to wonder.
    assert "cron" in r.content


# ─── action: pin ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pin_routes_through_remember_with_high_confidence(
    tools_with_mock_svc,
):
    tools, svc = tools_with_mock_svc
    fake_fact = MagicMock(id="fp", bucket="rules", confidence=0.99)
    svc.remember.return_value = fake_fact
    await tools.invoke(_call({
        "action": "pin",
        "text": "永远不直接 push main",
        "bucket": "rules",
    }))
    kwargs = svc.remember.call_args.kwargs
    assert kwargs["confidence"] >= 0.95


# ─── action: replace ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_replace_needs_old_fid_or_old_text(tools_with_mock_svc):
    tools, _ = tools_with_mock_svc
    r = await tools.invoke(_call({
        "action": "replace",
        "text": "new value",
    }))
    assert r.ok is False
    assert "old_fid" in r.error or "old_text" in r.error


@pytest.mark.asyncio
async def test_replace_with_old_fid_uses_correct_with_old_fact_id(
    tools_with_mock_svc,
):
    """2026-05-29 cleanup: replace by old_fid now flows through
    ``service.correct(old_fact_id=...)`` instead of doing a manual
    ``forget+remember`` duet. That keeps the SUPERSEDES edge intact
    and matches the old_text path's pipeline."""
    tools, svc = tools_with_mock_svc
    svc.correct.return_value = {
        "matched": True,
        "old_fact_id": "old42",
        "new_fact_id": "new42",
        "distance": 0.0,
    }
    r = await tools.invoke(_call({
        "action": "replace",
        "old_fid": "old42",
        "text": "用户叫敬宇",
        "bucket": "user_identity",
    }))
    assert r.ok is True
    svc.correct.assert_awaited_once()
    kwargs = svc.correct.call_args.kwargs
    assert kwargs["old_fact_id"] == "old42"
    assert kwargs["new_text"] == "用户叫敬宇"
    assert r.content["via"] == "old_fid"
    assert r.content["new_fact_id"] == "new42"


@pytest.mark.asyncio
async def test_replace_with_old_text_uses_correct(tools_with_mock_svc):
    tools, svc = tools_with_mock_svc
    svc.correct.return_value = {
        "matched": True, "old_fact_id": "x", "new_fact_id": "y",
    }
    r = await tools.invoke(_call({
        "action": "replace",
        "old_text": "用户叫张伟",
        "text": "用户叫敬宇",
    }))
    assert r.ok is True
    assert r.content["via"] == "old_text"
    svc.correct.assert_awaited_once()
    kwargs = svc.correct.call_args.kwargs
    # old_fact_id is None for the text-driven path; old_text carries
    # the semantic-search target.
    assert kwargs["old_fact_id"] is None
    assert kwargs["old_text"] == "用户叫张伟"


# ─── action: forget ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_forget_with_old_fid_direct(tools_with_mock_svc):
    tools, svc = tools_with_mock_svc
    r = await tools.invoke(_call({
        "action": "forget",
        "old_fid": "f8a3",
        "reason": "user retracted",
    }))
    assert r.ok is True
    svc.forget.assert_awaited_once_with(
        fact_id="f8a3", reason="user retracted",
    )
    assert r.content["forgotten"][0]["fid"] == "f8a3"


@pytest.mark.asyncio
async def test_forget_with_query_uses_semantic_search(tools_with_mock_svc):
    tools, svc = tools_with_mock_svc
    h1 = MagicMock(); h1.fact = MagicMock(id="a", text="用户喜欢 Chrome"); h1.distance = 0.1
    h2 = MagicMock(); h2.fact = MagicMock(id="b", text="用户喜欢 Edge"); h2.distance = 0.3
    svc.recall.return_value = [h1, h2]
    svc.forget.return_value = True
    r = await tools.invoke(_call({
        "action": "forget",
        "query": "用户浏览器偏好",
        "max_matches": 2,
    }))
    assert r.ok is True
    assert r.content["forgotten_count"] == 2
    assert svc.forget.await_count == 2


@pytest.mark.asyncio
async def test_forget_needs_old_fid_or_query(tools_with_mock_svc):
    tools, _ = tools_with_mock_svc
    r = await tools.invoke(_call({"action": "forget"}))
    assert r.ok is False
    assert "old_fid" in r.error or "query" in r.error


# ─── spec registration ────────────────────────────────────────────


def test_memory_tool_advertised():
    names = {s.name for s in BuiltinTools().list_tools()}
    assert "memory" in names
    assert "memory_get" in names


def test_memory_spec_actions_enum_exhaustive():
    from xmclaw.providers.tool._specs import _MEMORY_SPEC
    actions = _MEMORY_SPEC.parameters_schema["properties"]["action"]["enum"]
    assert set(actions) == {"add", "replace", "forget", "pin"}
