"""Neutral home for the "who is the running agent?" contextvar.

Lives in ``core/`` (not ``daemon/``) so providers that sit below the
daemon in the DAG — tool providers in particular, see
``xmclaw/providers/tool/AGENTS.md`` §2 — can read the ambient agent
id without taking on an illegal upward import of ``xmclaw.daemon.*``.

The Phase 4 :class:`xmclaw.daemon.agent_context.AgentContextMiddleware`
still owns seeding the var on HTTP/WS requests, and the WS handler
still wraps ``run_turn`` with :func:`use_current_agent_id`. What moved
here is strictly the variable + accessors, which have no daemon-
specific behavior and no need to live beside an ASGI middleware.

Phase 5 set the convention: a tool that wants to stamp an outgoing
call with "who am I?" imports :func:`get_current_agent_id` from this
module. The daemon-layer module re-exports the same names so existing
callers keep working without an edit storm.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

_current_agent_id: ContextVar[str | None] = ContextVar(
    "xmclaw_current_agent_id", default=None
)


def get_current_agent_id() -> str | None:
    """Return the agent id for the running turn, or ``None`` outside one.

    Tools should treat ``None`` as "no ambient agent" and fall back to
    their own configured default (typically ``"main"``). Crashing on
    ``None`` would make the tool unusable in contexts that don't flow
    through the WS handler (CLI, tests, scheduler jobs).
    """
    return _current_agent_id.get()


@contextmanager
def use_current_agent_id(agent_id: str | None) -> Iterator[None]:
    """Scope the ambient agent id to a ``with`` block.

    Reset-via-token rather than plain set/clear so nesting works: an
    inner scope with a different id still restores the outer on exit.

    Accepts ``None`` explicitly so callers can "unset" inside a nested
    scope (useful in tests that exercise the no-ambient-agent path).
    """
    token = _current_agent_id.set(agent_id)
    try:
        yield
    finally:
        _current_agent_id.reset(token)
