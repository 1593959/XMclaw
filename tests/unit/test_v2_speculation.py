"""Speculation cache — Wave-32+ (2026-05-18).

Pure-function tests for the speculation primitives. End-to-end
streaming-prefetch is verified separately in
``tests/integration/test_v2_speculation_e2e.py`` since it requires
exercising the full hop_loop + Anthropic-mock streaming dance.
"""
from __future__ import annotations

import asyncio

import pytest

from xmclaw.cognition.speculation import (
    SpeculationCache,
    is_speculatable,
    make_speculation_callback,
    maybe_await_cached,
)
from xmclaw.core.ir import ToolCall, ToolResult


def _call(name: str, id: str = "tc-1", **args: object) -> ToolCall:
    return ToolCall(name=name, args=args, id=id, provenance="llm")


# ── is_speculatable ─────────────────────────────────────────────────────


def test_known_read_only_tools_are_speculatable() -> None:
    """Pin core read-only tools so future churn doesn't accidentally
    drop them from the allowlist."""
    for name in [
        "file_read", "list_dir", "glob_files", "grep_files",
        "web_search", "web_fetch", "sqlite_query",
        "memory_search", "agent_status",
    ]:
        assert is_speculatable(name), name


def test_mutating_tools_are_not_speculatable() -> None:
    for name in [
        "file_write", "apply_patch", "file_delete", "bash",
        "remember", "memory_pin", "note_write",
        "enter_worktree", "enter_plan_mode",
        # undo_recent mutates; undo_list reads
        "undo_recent",
    ]:
        assert not is_speculatable(name), name


def test_unknown_tools_default_to_not_speculatable() -> None:
    """Conservative — anything not explicitly on the allowlist is
    blocked. Adding a tool requires editing READ_ONLY_TOOLS."""
    assert not is_speculatable("hypothetical_new_tool")
    assert not is_speculatable("")


# ── SpeculationCache ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cache_add_and_take_round_trip() -> None:
    cache = SpeculationCache()

    async def _run() -> ToolResult:
        return ToolResult(call_id="tc-1", ok=True, content="hello")

    task = asyncio.ensure_future(_run())
    cache.add("tc-1", task)
    fetched = cache.take("tc-1")
    assert fetched is task
    # Second take returns None — the cache is drained on use.
    assert cache.take("tc-1") is None
    res = await fetched
    assert res.content == "hello"


@pytest.mark.asyncio
async def test_cache_add_duplicate_cancels_previous() -> None:
    """Defensive: if the stream emits the same call_id twice the
    older speculation should be cancelled to avoid leaks."""
    cache = SpeculationCache()
    started = asyncio.Event()
    finish = asyncio.Event()

    async def _slow() -> ToolResult:
        started.set()
        await finish.wait()
        return ToolResult(call_id="tc-x", ok=True, content="slow")

    first = asyncio.ensure_future(_slow())
    cache.add("tc-x", first)
    await started.wait()  # ensure it's actually running

    async def _fast() -> ToolResult:
        return ToolResult(call_id="tc-x", ok=True, content="fast")

    second = asyncio.ensure_future(_fast())
    cache.add("tc-x", second)
    # First should now be cancelled.
    await asyncio.sleep(0)  # let cancel propagate
    assert first.cancelled() or first.done()
    finish.set()  # unblock if not cancelled (defensive)
    task = cache.take("tc-x")
    assert task is not None
    res = await task
    assert res.content == "fast"


@pytest.mark.asyncio
async def test_cancel_remaining_clears_pending_tasks() -> None:
    cache = SpeculationCache()

    async def _hang() -> ToolResult:
        await asyncio.sleep(60)
        return ToolResult(call_id="never", ok=True, content="never")

    t = asyncio.ensure_future(_hang())
    cache.add("hang-1", t)
    n = cache.cancel_remaining()
    assert n == 1
    assert cache.tasks == {}
    # Allow cancel to propagate; task should be cancelled.
    await asyncio.sleep(0)
    assert t.cancelled() or t.done()


# ── make_speculation_callback ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_callback_dispatches_only_read_only_tools() -> None:
    """The callback must filter — mutating tools are NOT prefetched
    even if the stream offers them. Pin this so a careless edit
    can't silently start a `bash` invocation early."""
    cache = SpeculationCache()
    invoked: list[str] = []

    def _invoke(call: ToolCall) -> "asyncio.Future[ToolResult]":
        invoked.append(call.name)

        async def _r() -> ToolResult:
            return ToolResult(call_id=call.id, ok=True, content="ok")

        return asyncio.ensure_future(_r())

    cb = make_speculation_callback(cache, _invoke)
    cb(_call("file_read", id="r1"))    # in allowlist → dispatched
    cb(_call("bash", id="m1"))         # mutating → ignored
    cb(_call("file_write", id="m2"))   # mutating → ignored
    cb(_call("grep_files", id="r2"))   # in allowlist → dispatched
    # Drain so we can inspect.
    for task in list(cache.tasks.values()):
        await task
    assert invoked == ["file_read", "grep_files"]


# ── maybe_await_cached ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_phase_b_helper_uses_cached_result_when_present() -> None:
    cache = SpeculationCache()
    fallback_called = [False]

    async def _run() -> ToolResult:
        return ToolResult(call_id="tc-1", ok=True, content="speculated")

    cache.add("tc-1", asyncio.ensure_future(_run()))

    def _fallback() -> "asyncio.Future[ToolResult]":
        fallback_called[0] = True

        async def _r() -> ToolResult:
            return ToolResult(call_id="tc-1", ok=True, content="fallback")

        return asyncio.ensure_future(_r())

    res = await maybe_await_cached(cache, _call("file_read", id="tc-1"), _fallback)
    assert res.content == "speculated"
    assert fallback_called[0] is False


@pytest.mark.asyncio
async def test_phase_b_helper_falls_back_when_uncached() -> None:
    """Unspeculated tool (mutating, or read-only that the LLM
    finalised after stream end) → dispatches via fallback."""
    cache = SpeculationCache()

    def _fallback() -> "asyncio.Future[ToolResult]":
        async def _r() -> ToolResult:
            return ToolResult(call_id="tc-99", ok=True, content="fresh")

        return asyncio.ensure_future(_r())

    res = await maybe_await_cached(cache, _call("file_write", id="tc-99"), _fallback)
    assert res.content == "fresh"


@pytest.mark.asyncio
async def test_phase_b_helper_falls_back_when_speculation_cancelled() -> None:
    """If the cached task was cancelled mid-flight (user Stop, hop
    end), the helper falls through to a fresh dispatch so the LLM
    sees a real result, not a CancelledError."""
    cache = SpeculationCache()

    async def _slow() -> ToolResult:
        await asyncio.sleep(60)
        return ToolResult(call_id="cancelled", ok=True, content="should-not-arrive")

    task = asyncio.ensure_future(_slow())
    cache.add("cancelled", task)
    task.cancel()
    await asyncio.sleep(0)

    def _fallback() -> "asyncio.Future[ToolResult]":
        async def _r() -> ToolResult:
            return ToolResult(call_id="cancelled", ok=True, content="from-fallback")

        return asyncio.ensure_future(_r())

    res = await maybe_await_cached(
        cache, _call("file_read", id="cancelled"), _fallback,
    )
    assert res.content == "from-fallback"


# ── default complete_streaming preserves contract ───────────────────────


@pytest.mark.asyncio
async def test_default_streaming_impl_fires_on_tool_block_for_each_call() -> None:
    """LLM providers without true streaming fall back to complete().
    The default complete_streaming impl still fires on_tool_block
    for every tool_call in the final response, so callers writing
    against the callback don't need a separate code path for
    non-streaming providers."""
    from dataclasses import dataclass

    from xmclaw.providers.llm.base import LLMProvider, LLMResponse, Pricing
    from xmclaw.core.ir import ToolCallShape

    @dataclass
    class _Stub(LLMProvider):
        async def complete(self, messages, tools=None):
            return LLMResponse(
                content="text",
                tool_calls=(
                    _call("file_read", id="a"),
                    _call("file_write", id="b"),
                ),
            )

        def stream(self, messages, tools=None, *, cancel=None):  # pragma: no cover
            raise NotImplementedError

        @property
        def tool_call_shape(self) -> ToolCallShape:
            return ToolCallShape.NATIVE

        @property
        def pricing(self) -> Pricing:
            return Pricing()

    seen: list[str] = []
    stub = _Stub()
    await stub.complete_streaming(
        [], tools=None,
        on_tool_block=lambda tc: seen.append(tc.name),
    )
    assert seen == ["file_read", "file_write"]
