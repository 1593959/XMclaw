"""ContextVar plumbing — convention #2 of QwenPaw's 4-piece multi-agent
infrastructure.

Direct port of ``qwenpaw/src/qwenpaw/app/agent_context.py``. A
``ContextVar`` is the right Python primitive here because it's
asyncio-aware: setting it on one task's stack doesn't leak into
another concurrent task's stack, even when they share an event loop.

Every layer of the runtime that needs to know "which agent am I right
now?" reads :func:`current_agent_id`. The middleware in
:mod:`xmclaw.daemon.middleware.agent_scope` writes it from the
``X-Agent-Id`` HTTP header on every request; the WS gateway writes it
on connection. The :func:`AgentContext` async-context-manager is the
primary user-facing API for the rare in-process call site that needs
to scope a sub-block to a different agent (e.g. inter-agent tool
calls).
"""
from __future__ import annotations

import contextlib
from contextvars import ContextVar, Token
from typing import Iterator

# Default value is the special string ``"main"`` so existing single-
# agent daemons (no header set) behave identically to today.
_current_agent_id: ContextVar[str] = ContextVar(
    "xmclaw_current_agent_id", default="main"
)


def current_agent_id() -> str:
    """Return the agent id of the currently running async stack frame.

    Defaults to ``"main"`` — the legacy single-agent name.
    """
    return _current_agent_id.get()


def set_current_agent_id(agent_id: str) -> Token[str]:
    """Set the contextvar; return the token so caller can reset.

    Typical use is via :func:`AgentContext` rather than this raw API,
    but middleware needs the token for cleanup.
    """
    return _current_agent_id.set(agent_id or "main")


def reset_current_agent_id(token: Token[str]) -> None:
    _current_agent_id.reset(token)


@contextlib.contextmanager
def AgentContext(agent_id: str) -> Iterator[str]:
    """Sync context-manager: ``with AgentContext("coder"): ...``.

    Restores the prior id on exit so nested scopes work cleanly. Use
    :func:`async_agent_context` if you need an async-context-manager
    flavor (importable below).
    """
    token = _current_agent_id.set(agent_id or "main")
    try:
        yield agent_id
    finally:
        _current_agent_id.reset(token)


@contextlib.asynccontextmanager
async def async_agent_context(agent_id: str):
    """Async ``with`` flavor — same semantics, awaitable scope."""
    token = _current_agent_id.set(agent_id or "main")
    try:
        yield agent_id
    finally:
        _current_agent_id.reset(token)
