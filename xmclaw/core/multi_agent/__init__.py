"""Multi-agent runtime — port of QwenPaw's 4 conventions.

QwenPaw's working multi-agent rests on exactly four pieces (see
``docs/DEV_PLAN.md`` §1.5 + appendix A):

  1. **`X-Agent-Id` header** on every HTTP / WS request — the identity
     bundle. Middleware reads it; ContextVar plumbs it everywhere.
  2. **ContextVar plumbing** so an inner async stack frame knows which
     agent it is running as.
  3. **Lazy-locked agent dict** with concurrent-start dedup via
     `_pending_starts: dict[str, asyncio.Event]` — protects against
     two requests trying to spin up the same agent at once.
  4. **Identity-prefix loop guard** on inter-agent calls — every
     synthesized message gets a `[Agent X requesting]` prefix so the
     receiver can't be tricked into calling back the source.

Source files in the QwenPaw repo (paths under
``.claude/scratch/competitor-code/qwenpaw/``):

* ``src/qwenpaw/app/agent_context.py`` — ContextVar pattern
* ``src/qwenpaw/app/multi_agent_manager.py:22-130`` — manager + dedup
* ``src/qwenpaw/agents/tools/agent_management.py:18-200, 107-121`` —
  4-tool inter-agent surface + identity-prefix
* ``src/qwenpaw/app/routers/agent_scoped.py`` — middleware

For Phase 3 we land the Python infra; existing primary-AgentLoop wiring
stays as-is (the daemon still hands out one agent today). Phase 3.5
follow-up rewires WS gateway to pick agent by header.
"""
from xmclaw.core.multi_agent.context import (
    AgentContext,
    async_agent_context,
    current_agent_id,
    reset_current_agent_id,
    set_current_agent_id,
)
from xmclaw.core.multi_agent.manager import (
    AgentNotFound,
    MultiAgentManager,
)

__all__ = [
    "AgentContext",
    "AgentNotFound",
    "MultiAgentManager",
    "async_agent_context",
    "current_agent_id",
    "reset_current_agent_id",
    "set_current_agent_id",
]
