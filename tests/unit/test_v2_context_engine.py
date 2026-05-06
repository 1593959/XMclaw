"""Tests for the ContextEngine ABC + SimpleContextEngine reference impl.

The production AgentLoop currently handles all six lifecycle stages
inline; this test suite just verifies the contract is well-formed and
the in-memory reference impl behaves sensibly on the happy path.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from xmclaw.context import (
    AssembleResult,
    BootstrapResult,
    CompactResult,
    ContextEngine,
    IngestResult,
    SimpleContextEngine,
)


def test_abc_cannot_be_instantiated() -> None:
    """ContextEngine is abstract — direct instantiation must fail."""
    with pytest.raises(TypeError):
        ContextEngine()  # type: ignore[abstract]


@pytest.mark.asyncio
async def test_simple_engine_bootstrap_idempotent() -> None:
    eng = SimpleContextEngine()
    r1 = await eng.bootstrap("s1")
    r2 = await eng.bootstrap("s1")
    assert r1.success and r2.success
    assert "s1" in eng._sessions


@pytest.mark.asyncio
async def test_simple_engine_ingest_then_assemble() -> None:
    eng = SimpleContextEngine()
    await eng.bootstrap("s1")
    msgs = [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]
    for m in msgs:
        ir = await eng.ingest("s1", m)
        assert ir.success
        assert ir.tokens_added > 0

    out = await eng.assemble("s1", token_budget=1000)
    assert isinstance(out, AssembleResult)
    assert len(out.messages) == 3
    assert out.total_tokens > 0


@pytest.mark.asyncio
async def test_simple_engine_assemble_drops_system_when_excluded() -> None:
    eng = SimpleContextEngine()
    await eng.bootstrap("s1")
    await eng.ingest("s1", {"role": "system", "content": "S"})
    await eng.ingest("s1", {"role": "user", "content": "hi"})
    out = await eng.assemble("s1", token_budget=1000, include_system=False)
    roles = [m["role"] for m in out.messages]
    assert roles == ["user"]


@pytest.mark.asyncio
async def test_simple_engine_compact_is_noop() -> None:
    """SimpleContextEngine doesn't actually compact — it just reports
    the message count. Real impls would delegate to ContextCompressor."""
    eng = SimpleContextEngine()
    await eng.bootstrap("s1")
    for i in range(5):
        await eng.ingest("s1", {"role": "user", "content": f"q{i}"})
    r = await eng.compact("s1")
    assert isinstance(r, CompactResult)
    assert r.success
    assert r.messages_before == 5
    assert r.messages_after == 5


@pytest.mark.asyncio
async def test_simple_engine_dispose_clears_state() -> None:
    eng = SimpleContextEngine()
    await eng.bootstrap("s1")
    await eng.ingest("s1", {"role": "user", "content": "x"})
    assert eng._sessions
    await eng.dispose()
    assert not eng._sessions


@pytest.mark.asyncio
async def test_optional_hooks_are_noops_by_default() -> None:
    """The 3 optional hooks should never raise on the base class."""
    eng = SimpleContextEngine()
    await eng.on_file_modified("s1", "/tmp/x.py")
    await eng.on_tool_invoked("s1", "bash", {})
    await eng.on_error("s1", RuntimeError("test"))
