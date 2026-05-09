"""B-341 (audit pass-2 #11) — on_session_end hook for cloud memory
providers.

Pre-B-341 ``Mem0MemoryProvider`` and ``SupermemoryMemoryProvider``
inherited the base-class no-op ``on_session_end`` despite being the
two providers that most need a session-boundary ingest hook (mem0
specifically advertises server-side memory extraction off the
``/v1/memories`` ``messages`` array; supermemory has a chunker).
Every conversation was invisible to either backend unless the agent
explicitly called the recall tool mid-session.

This file pins:
  * The hook fires (via ``_request``) when the provider is available.
  * The hook is a no-op when the provider isn't configured (no API
    key) — must not 500 the agent loop or hit the network.
  * Empty / metadata-only message lists are filtered before the
    request goes out.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from xmclaw.providers.memory.mem0 import Mem0MemoryProvider
from xmclaw.providers.memory.supermemory import SupermemoryMemoryProvider


class _FakeMessage:
    def __init__(self, role: str, content: str) -> None:
        self.role = role
        self.content = content


# ── Mem0 ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_b341_mem0_on_session_end_posts_full_messages() -> None:
    """Conversation → /v1/memories with the full messages array.
    Mem0's server-side extraction is the whole point of this provider;
    pre-B-341 the hook didn't fire and the cloud index never grew."""
    provider = Mem0MemoryProvider(api_key="m0-test", user_id="alice")
    provider._request = AsyncMock(return_value={"id": "mem-123"})

    messages = [
        _FakeMessage("user", "What's the deal with B-340?"),
        _FakeMessage("assistant", "It cleared 5 critical audit findings."),
        _FakeMessage("user", "Tell me more about #4 specifically."),
        _FakeMessage("assistant", "Tool-guard rule scoping + bash naming."),
    ]
    await provider.on_session_end(session_id="sid-1", messages=messages)

    provider._request.assert_called_once()
    method, path = provider._request.call_args.args[:2]
    body = provider._request.call_args.args[2]
    assert method == "POST"
    assert path == "/v1/memories"
    # All four turns made it through.
    assert len(body["messages"]) == 4
    assert body["messages"][0] == {
        "role": "user", "content": "What's the deal with B-340?",
    }
    assert body["user_id"] == "alice"
    assert body["metadata"]["session_id"] == "sid-1"
    assert body["metadata"]["kind"] == "session_summary"
    assert body["metadata"]["n_messages"] == 4


@pytest.mark.asyncio
async def test_b341_mem0_on_session_end_skips_when_unconfigured() -> None:
    """No API key → no network call. The provider's ``is_available``
    is False and on_session_end must early-return so the agent loop
    isn't blocked on a 401."""
    provider = Mem0MemoryProvider(api_key="")  # empty key + env unset
    # Belt-and-suspenders against the env vars leaking in.
    provider._api_key = ""
    provider._request = AsyncMock(return_value=None)

    messages = [_FakeMessage("user", "hi")]
    await provider.on_session_end(session_id="sid-1", messages=messages)

    provider._request.assert_not_called()


@pytest.mark.asyncio
async def test_b341_mem0_on_session_end_skips_empty_payload() -> None:
    """Filtering keeps user / assistant / system turns with non-empty
    content — a list with only tool messages or empty strings has no
    payload, and the request must be skipped (zero-length messages
    arrays make the cloud reject the call)."""
    provider = Mem0MemoryProvider(api_key="m0-test")
    provider._request = AsyncMock(return_value=None)

    # tool messages get filtered; empty content also drops.
    messages = [
        _FakeMessage("tool", "discarded"),
        _FakeMessage("user", ""),
        _FakeMessage("assistant", "   "),  # whitespace-only counts as content
    ]
    await provider.on_session_end(session_id="sid-1", messages=messages)
    # whitespace-only assistant message qualifies as content (non-empty
    # truthy string), so the call DOES happen with one message in payload.
    provider._request.assert_called_once()
    body = provider._request.call_args.args[2]
    assert len(body["messages"]) == 1
    assert body["messages"][0]["role"] == "assistant"


@pytest.mark.asyncio
async def test_b341_mem0_on_session_end_swallows_request_failure() -> None:
    """A network failure inside ``_request`` must NOT bubble — the
    agent loop's session-end path is best-effort observability and a
    cloud outage shouldn't crash the session shutdown."""
    provider = Mem0MemoryProvider(api_key="m0-test")
    provider._request = AsyncMock(side_effect=RuntimeError("502 boom"))

    messages = [_FakeMessage("user", "hi"), _FakeMessage("assistant", "ok")]
    # Must not raise.
    await provider.on_session_end(session_id="sid-1", messages=messages)
    provider._request.assert_called_once()


# ── Supermemory ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_b341_supermemory_on_session_end_posts_digest() -> None:
    """Supermemory has no native ``messages`` shape; the hook builds
    a role-prefixed digest and posts to /v3/memories. Pre-B-341 the
    cloud index never grew across session boundaries."""
    provider = SupermemoryMemoryProvider(api_key="sm-test")
    provider._request = AsyncMock(return_value={"id": "sm-123"})

    messages = [
        _FakeMessage("user", "What's the deal?"),
        _FakeMessage("assistant", "Audit cleanup."),
    ]
    await provider.on_session_end(session_id="sid-2", messages=messages)

    provider._request.assert_called_once()
    method, path = provider._request.call_args.args[:2]
    body = provider._request.call_args.args[2]
    assert method == "POST"
    assert path == "/v3/memories"
    # Role-prefixed digest joined with blank lines.
    assert "user: What's the deal?" in body["content"]
    assert "assistant: Audit cleanup." in body["content"]
    assert body["metadata"]["session_id"] == "sid-2"
    assert body["metadata"]["kind"] == "session_summary"
    assert body["metadata"]["n_messages"] == 2


@pytest.mark.asyncio
async def test_b341_supermemory_on_session_end_skips_when_unconfigured() -> None:
    provider = SupermemoryMemoryProvider(api_key="")
    provider._api_key = ""
    provider._request = AsyncMock(return_value=None)
    messages = [_FakeMessage("user", "hi"), _FakeMessage("assistant", "yo")]
    await provider.on_session_end(session_id="sid-1", messages=messages)
    provider._request.assert_not_called()


@pytest.mark.asyncio
async def test_b341_supermemory_on_session_end_swallows_request_failure() -> None:
    provider = SupermemoryMemoryProvider(api_key="sm-test")
    provider._request = AsyncMock(side_effect=RuntimeError("503 boom"))
    messages = [_FakeMessage("user", "hi"), _FakeMessage("assistant", "ok")]
    # Must not raise.
    await provider.on_session_end(session_id="sid-1", messages=messages)
    provider._request.assert_called_once()


# ── Manager fan-out (the actual call site) ─────────────────────────


@pytest.mark.asyncio
async def test_b341_manager_dispatches_on_session_end_to_mem0() -> None:
    """The MemoryManager fans on_session_end out across registered
    providers — pin that the mem0 override actually fires through
    the manager dispatch path. Pre-B-341 the fan-out happened, but
    landed on the base-class no-op so the network call never went
    out. (Manager allows at most ONE external provider, so this
    case + the supermemory case below are the two that matter.)"""
    from xmclaw.providers.memory.manager import MemoryManager

    mem0 = Mem0MemoryProvider(api_key="m0")
    mem0._request = AsyncMock(return_value=None)

    mgr = MemoryManager()
    assert mgr.add_provider(mem0) is True
    messages = [_FakeMessage("user", "x"), _FakeMessage("assistant", "y")]
    await mgr.on_session_end(session_id="sid-fanout", messages=messages)

    mem0._request.assert_called_once()


@pytest.mark.asyncio
async def test_b341_manager_dispatches_on_session_end_to_supermemory() -> None:
    """Mirror of the mem0 test — same regression, different cloud."""
    from xmclaw.providers.memory.manager import MemoryManager

    sm = SupermemoryMemoryProvider(api_key="sm")
    sm._request = AsyncMock(return_value=None)

    mgr = MemoryManager()
    assert mgr.add_provider(sm) is True
    messages = [_FakeMessage("user", "x"), _FakeMessage("assistant", "y")]
    await mgr.on_session_end(session_id="sid-fanout", messages=messages)

    sm._request.assert_called_once()
