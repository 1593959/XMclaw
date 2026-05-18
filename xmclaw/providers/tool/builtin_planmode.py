"""Plan-mode tools: ``enter_plan_mode`` + ``exit_plan_mode``.

Wave-32+ (2026-05-18). Ports the free-code-main EnterPlanMode /
ExitPlanMode pattern. Plan mode is a session-level state that
persists across turns — the agent flips it on to signal "explore +
design, don't write", reads/searches freely while it's on, and flips
it off with a concrete plan when ready to act.

State model
===========

Plan-mode-active session ids live in a module-level
``_PLAN_MODE_SESSIONS: set[str]`` rather than on any single
``AgentLoop`` instance. Reasons:

  * Session ids are globally unique across the daemon — there is no
    ambiguity about which loop owns which session.
  * Multiple ``AgentLoop`` instances (multi-agent setups, evolution
    workspace) share the gate state without us having to thread the
    set through every constructor.
  * Tests can clear the set between cases with one call.

The contextvar :func:`xmclaw.core.agent_context.get_current_session_id`
(wired by ``AgentLoop.run_turn``, Wave-32+) identifies the running
session. No tool call carries the session_id as an arg — the LLM
shouldn't have to know its own id.

Gate
====

:func:`is_blocked_by_plan_mode` is the public check; mutating tool
handlers call it at the top of their ``_do_*`` body and short-circuit
with :func:`_fail` when it returns True. The set of blocked tools
lives in :data:`PLAN_MODE_BLOCKED_TOOLS` — keep read-only tools out
of it (the whole point of plan mode is to encourage exploration
before mutation).
"""
from __future__ import annotations

import time

from xmclaw.core.agent_context import get_current_session_id
from xmclaw.core.ir import ToolCall, ToolResult
from xmclaw.providers.tool._helpers import _fail


# Process-global set of session ids currently in plan mode. See
# module docstring for why this isn't per-AgentLoop.
_PLAN_MODE_SESSIONS: set[str] = set()


# Names of mutating tools that plan mode blocks. Read-only tools
# (file_read, glob_files, grep_files, web_search, ask_user_question)
# are NOT in this set — the whole point of plan mode is to encourage
# exploration before mutation.
PLAN_MODE_BLOCKED_TOOLS: frozenset[str] = frozenset({
    "file_write",
    "apply_patch",
    "file_delete",
    "bash",
    # `enter_worktree` + `exit_worktree` are filesystem-mutating;
    # block them so plan mode can't side-step itself by spinning up
    # a worktree and writing there.
    "enter_worktree",
    "exit_worktree",
    # Memory writes affect persistent state — gate them too so the
    # agent doesn't quietly memorize wrong-direction notes during
    # exploration.
    "remember",
    "memory_pin",
    "update_persona",
    "note_write",
    "journal_append",
})


def is_blocked_by_plan_mode(tool_name: str) -> bool:
    """Return True iff ``tool_name`` should refuse because the current
    session is in plan mode.

    Outside a running session (no session contextvar) the gate is
    always open — the CLI / test contexts that bypass the WS handler
    don't have a session to be "in plan mode."
    """
    if tool_name not in PLAN_MODE_BLOCKED_TOOLS:
        return False
    sid = get_current_session_id()
    if sid is None:
        return False
    return sid in _PLAN_MODE_SESSIONS


def is_session_in_plan_mode(session_id: str) -> bool:
    """Programmatic check (used by the WS handler when wiring the
    frontend Plan/Act toggle into a fresh session)."""
    return session_id in _PLAN_MODE_SESSIONS


def set_plan_mode(session_id: str, active: bool) -> None:
    """Idempotent setter for the WS handler. Lets the frontend
    Plan/Act toggle drive the gate without going through a tool call.
    """
    if active:
        _PLAN_MODE_SESSIONS.add(session_id)
    else:
        _PLAN_MODE_SESSIONS.discard(session_id)


def clear_plan_mode_sessions() -> None:
    """Test helper — wipe the set between cases."""
    _PLAN_MODE_SESSIONS.clear()


class BuiltinToolsPlanModeMixin:
    """``enter_plan_mode`` + ``exit_plan_mode`` handlers."""

    async def _enter_plan_mode(self, call: ToolCall, t0: float) -> ToolResult:
        sid = get_current_session_id()
        if sid is None:
            return _fail(
                call, t0,
                "enter_plan_mode can only be called from inside a live "
                "session (no session_id in agent context)",
            )
        if sid in _PLAN_MODE_SESSIONS:
            return ToolResult(
                call_id=call.id, ok=True,
                content=(
                    "Already in plan mode for this session. Continue "
                    "exploring; call ``exit_plan_mode`` with a "
                    "concrete plan when ready."
                ),
                latency_ms=(time.perf_counter() - t0) * 1000.0,
            )
        _PLAN_MODE_SESSIONS.add(sid)
        return ToolResult(
            call_id=call.id, ok=True,
            content=(
                "Entered plan mode. While here:\n"
                "  • Read-only tools (file_read, glob_files, "
                "grep_files, web_search, ask_user_question) work "
                "normally.\n"
                "  • Mutating tools (file_write, apply_patch, "
                "file_delete, bash, memory writes) will REFUSE.\n"
                "  • Your job: thoroughly explore the codebase, "
                "identify the right approach, and call "
                "``exit_plan_mode`` with a concrete plan when ready."
            ),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _exit_plan_mode(self, call: ToolCall, t0: float) -> ToolResult:
        plan = call.args.get("plan")
        if not isinstance(plan, str) or not plan.strip():
            return _fail(
                call, t0,
                "exit_plan_mode requires a non-empty ``plan`` string",
            )
        sid = get_current_session_id()
        if sid is None or sid not in _PLAN_MODE_SESSIONS:
            # Lenient: allow exit-when-not-in. The agent may have lost
            # track of its mode state — cheaper to no-op than to make
            # the agent track it precisely.
            return ToolResult(
                call_id=call.id, ok=True,
                content=(
                    "Not currently in plan mode (no-op). Plan "
                    "recorded but no state change. Proceed with "
                    "implementation."
                ),
                latency_ms=(time.perf_counter() - t0) * 1000.0,
            )
        _PLAN_MODE_SESSIONS.discard(sid)
        return ToolResult(
            call_id=call.id, ok=True,
            content=(
                "Exited plan mode. Mutating tools are available "
                "again. Plan presented to user:\n\n"
                f"{plan.strip()}"
            ),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )


__all__ = [
    "BuiltinToolsPlanModeMixin",
    "PLAN_MODE_BLOCKED_TOOLS",
    "is_blocked_by_plan_mode",
    "is_session_in_plan_mode",
    "set_plan_mode",
    "clear_plan_mode_sessions",
]
