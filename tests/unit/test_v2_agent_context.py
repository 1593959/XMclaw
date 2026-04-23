"""ContextVar + ASGI middleware — Epic #17 Phase 4.

Covers the four contracts the Phase 5 agent-to-agent tools rely on:

1. ``get_current_agent_id()`` is ``None`` outside any scope.
2. ``use_current_agent_id()`` scopes cleanly and nests.
3. The middleware seeds the var from ``X-Agent-Id`` or ``agent_id``
   (header wins), touches only http/websocket scopes, and unwinds on
   exception.
4. Each asyncio task sees its own value — no cross-agent bleed when
   two turns run concurrently.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from xmclaw.daemon.agent_context import (
    AgentContextMiddleware,
    _extract_agent_id_from_scope,
    get_current_agent_id,
    use_current_agent_id,
)


# ── get / use_current_agent_id ───────────────────────────────────────────


def test_default_is_none() -> None:
    # Outside any scope the var is None so tools can branch rather than
    # crash when called from non-WS paths (CLI, scheduler, tests).
    assert get_current_agent_id() is None


def test_use_current_agent_id_scopes_and_restores() -> None:
    assert get_current_agent_id() is None
    with use_current_agent_id("worker"):
        assert get_current_agent_id() == "worker"
    assert get_current_agent_id() is None


def test_use_current_agent_id_nests() -> None:
    # Nested scopes with different ids — inner overrides, outer
    # restores on exit. Phase 7's evolution-agent scenario relies on
    # this (outer is main, inner is the evolver).
    with use_current_agent_id("outer"):
        assert get_current_agent_id() == "outer"
        with use_current_agent_id("inner"):
            assert get_current_agent_id() == "inner"
        assert get_current_agent_id() == "outer"
    assert get_current_agent_id() is None


def test_use_current_agent_id_accepts_none() -> None:
    # Explicit None lets a test "unset" inside a nested scope without
    # ambiguity about whether the context manager supports it.
    with use_current_agent_id("outer"):
        with use_current_agent_id(None):
            assert get_current_agent_id() is None
        assert get_current_agent_id() == "outer"


def test_use_current_agent_id_restores_on_exception() -> None:
    # Exception out of the block must NOT leak the inner value.
    assert get_current_agent_id() is None
    with pytest.raises(RuntimeError):
        with use_current_agent_id("transient"):
            assert get_current_agent_id() == "transient"
            raise RuntimeError("boom")
    assert get_current_agent_id() is None


@pytest.mark.asyncio
async def test_async_task_isolation() -> None:
    # Contextvars propagate INTO child tasks at creation time, but each
    # task's mutations don't leak back. This is the whole reason we
    # use a ContextVar instead of a module-global: two concurrent turns
    # must not cross-contaminate.
    results: dict[str, str | None] = {}

    async def run_as(label: str) -> None:
        with use_current_agent_id(label):
            await asyncio.sleep(0)  # yield to the scheduler
            results[label] = get_current_agent_id()

    await asyncio.gather(run_as("alpha"), run_as("beta"), run_as("gamma"))
    assert results == {"alpha": "alpha", "beta": "beta", "gamma": "gamma"}
    # Parent scope stays None — child tasks don't mutate caller's var.
    assert get_current_agent_id() is None


# ── _extract_agent_id_from_scope ─────────────────────────────────────────


def _scope_with_headers(headers: list[tuple[bytes, bytes]], qs: bytes = b"") -> dict:
    return {"type": "http", "headers": headers, "query_string": qs}


def test_extract_from_header() -> None:
    scope = _scope_with_headers([(b"x-agent-id", b"worker-7")])
    assert _extract_agent_id_from_scope(scope) == "worker-7"


def test_extract_from_query_string() -> None:
    scope = _scope_with_headers([], qs=b"agent_id=qa&foo=bar")
    assert _extract_agent_id_from_scope(scope) == "qa"


def test_extract_header_beats_query_string() -> None:
    # Header is canonical, query string is the browser-ergonomics
    # fallback. Documented precedence — keep it locked in.
    scope = _scope_with_headers(
        [(b"x-agent-id", b"header-win")], qs=b"agent_id=query-lose",
    )
    assert _extract_agent_id_from_scope(scope) == "header-win"


def test_extract_none_when_absent() -> None:
    assert _extract_agent_id_from_scope({"type": "http"}) is None
    assert _extract_agent_id_from_scope(_scope_with_headers([])) is None


def test_extract_ignores_empty_header_and_query() -> None:
    # "empty after strip" == "not provided" — don't let a stray
    # ``X-Agent-Id:`` clear the default.
    scope = _scope_with_headers([(b"x-agent-id", b"   ")], qs=b"agent_id=")
    assert _extract_agent_id_from_scope(scope) is None


def test_extract_trims_whitespace() -> None:
    scope = _scope_with_headers([(b"x-agent-id", b"  trimmed  ")])
    assert _extract_agent_id_from_scope(scope) == "trimmed"


def test_extract_skips_non_matching_headers() -> None:
    scope = _scope_with_headers(
        [(b"content-type", b"application/json"), (b"x-agent-id", b"keep")],
    )
    assert _extract_agent_id_from_scope(scope) == "keep"


# ── AgentContextMiddleware ───────────────────────────────────────────────


class _SpyApp:
    """Inner ASGI app that records the agent id it saw.

    Also asserts via ``observed`` whether any inner behavior needs to
    run under the middleware-set value — we can inspect it across
    invocations from the test.
    """

    def __init__(self) -> None:
        self.observed: list[str | None] = []

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        self.observed.append(get_current_agent_id())


@pytest.mark.asyncio
async def test_middleware_sets_var_for_http() -> None:
    inner = _SpyApp()
    mw = AgentContextMiddleware(inner)
    await mw(
        _scope_with_headers([(b"x-agent-id", b"worker")]),
        lambda: None,  # type: ignore[arg-type]
        lambda m: None,  # type: ignore[arg-type]
    )
    assert inner.observed == ["worker"]
    # Post-call the var is reset.
    assert get_current_agent_id() is None


@pytest.mark.asyncio
async def test_middleware_sets_var_for_websocket() -> None:
    inner = _SpyApp()
    mw = AgentContextMiddleware(inner)
    scope = {
        "type": "websocket",
        "headers": [(b"x-agent-id", b"qa-bot")],
        "query_string": b"",
    }
    await mw(scope, lambda: None, lambda m: None)  # type: ignore[arg-type]
    assert inner.observed == ["qa-bot"]


@pytest.mark.asyncio
async def test_middleware_passes_through_lifespan() -> None:
    # lifespan / other scope types must NOT touch the var — at boot
    # time there is no agent, and setting it to None via token-reset
    # cycles is harmless but noisy in profiles.
    inner = _SpyApp()
    mw = AgentContextMiddleware(inner)
    await mw({"type": "lifespan"}, lambda: None, lambda m: None)  # type: ignore[arg-type]
    assert inner.observed == [None]  # default was never set


@pytest.mark.asyncio
async def test_middleware_restores_var_on_exception() -> None:
    class _Boom:
        async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
            # Middleware has already set the var to "doomed" at this
            # point. Raising from the inner app must still unwind the
            # reset-token so subsequent requests get a clean default.
            assert get_current_agent_id() == "doomed"
            raise RuntimeError("handler failed")

    mw = AgentContextMiddleware(_Boom())
    with pytest.raises(RuntimeError):
        await mw(
            _scope_with_headers([(b"x-agent-id", b"doomed")]),
            lambda: None,  # type: ignore[arg-type]
            lambda m: None,  # type: ignore[arg-type]
        )
    assert get_current_agent_id() is None


@pytest.mark.asyncio
async def test_middleware_when_no_agent_id_leaves_var_none() -> None:
    inner = _SpyApp()
    mw = AgentContextMiddleware(inner)
    await mw(_scope_with_headers([]), lambda: None, lambda m: None)  # type: ignore[arg-type]
    assert inner.observed == [None]
    assert get_current_agent_id() is None
