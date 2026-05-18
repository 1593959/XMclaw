"""Speculation — pre-execute read-only tool calls during LLM streaming.

Wave-32+ (2026-05-18). Ports the spirit of free-code's
``services/PromptSuggestion/speculation.ts`` to XMclaw's architecture.

The win
=======

Anthropic's stream emits a complete ``tool_use`` block before the
overall ``message_stop`` event. The hop_loop currently waits for
``message_stop`` before dispatching any tool, leaving 0.5–2 seconds
on the table when the LLM keeps streaming text or additional tools
after the first tool_use.

This module wires a callback into ``complete_streaming``: when a
tool_use block completes mid-stream AND the tool is in the
:data:`READ_ONLY_TOOLS` allowlist, kick off the invoke as a
background task and stash the future in a per-turn cache keyed by
``ToolCall.id``. When ``hop_loop`` Phase B walks the response's tool
calls, it checks the cache first — already-running speculation
tasks are awaited instead of re-invoked.

Why only read-only tools
========================

Mutating tools (file_write, bash, etc.) have ordering dependencies:
running them speculatively could interleave with other tool calls or
mutate state the LLM hadn't committed to. Read-only tools (file_read,
glob, grep, list_dir, web_search) have no side effects, so running
them early is always safe — the result is identical regardless of
when we ask.

The allowlist is conservative. Adding a tool to it should require
verifying it has NO observable side effects beyond returning data.

Cancel + cleanup
================

The cache is per-AgentLoop (lives on a single hop). When the hop
finishes (Phase C done), the cache is drained and any still-running
speculation tasks are cancelled. The TurnCancelEvent (B-38) also
cancels everything mid-flight when the user clicks Stop.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from xmclaw.core.ir import ToolCall, ToolResult


# Tools we KNOW have zero side effects beyond returning data. Adding
# to this set requires verifying the handler reads or computes only —
# no filesystem writes, no network mutations, no state changes.
READ_ONLY_TOOLS: frozenset[str] = frozenset({
    # Filesystem reads
    "file_read",
    "list_dir",
    "glob_files",
    "grep_files",
    # Tabular reads
    "sqlite_query",
    # Web (read-only)
    "web_fetch",
    "web_search",
    # Self-introspection
    "agent_status",
    "todo_read",
    # Memory reads
    "memory_search",
    "recall_user_preferences",
    "journal_recall",
    # Undo inspection (the LIST is read-only; undo_recent itself
    # mutates and is NOT in this set)
    "undo_list",
    # Agent inter — list/check are pure reads
    "list_agents",
    "check_agent_task",
    "list_agent_tasks",
})


@dataclass
class SpeculationCache:
    """Per-hop cache of speculatively-dispatched tool calls.

    Keys are :attr:`ToolCall.id` values. Values are the asyncio Task
    running the speculative invoke. Callers ``await`` the task to
    retrieve the :class:`ToolResult`; if the task is already done
    the await returns immediately.
    """

    tasks: dict[str, "asyncio.Task[ToolResult]"] = field(default_factory=dict)

    def add(self, call_id: str, task: "asyncio.Task[ToolResult]") -> None:
        # If the same call_id was speculated twice (defensive),
        # cancel the older one so we don't leak.
        existing = self.tasks.get(call_id)
        if existing is not None and not existing.done():
            existing.cancel()
        self.tasks[call_id] = task

    def take(self, call_id: str) -> "asyncio.Task[ToolResult] | None":
        """Remove + return the task for ``call_id``. Returns ``None``
        if there was no speculation for this id. Used by Phase B
        so the cache is naturally drained as Phase B iterates."""
        return self.tasks.pop(call_id, None)

    def cancel_remaining(self) -> int:
        """Cancel and clear every still-pending speculation task.
        Called when the hop ends so leftover tasks don't keep
        running against state that's about to change."""
        n = 0
        for task in self.tasks.values():
            if not task.done():
                task.cancel()
                n += 1
        self.tasks.clear()
        return n


def is_speculatable(tool_name: str) -> bool:
    """Whether ``tool_name`` is safe to pre-execute speculatively.

    Centralised so the LLM provider's stream hook + hop_loop's
    Phase B agree on the allowlist. Adding to the allowlist should
    go through :data:`READ_ONLY_TOOLS`, not via overrides at the
    call site."""
    return tool_name in READ_ONLY_TOOLS


def make_speculation_callback(
    cache: SpeculationCache,
    invoke: Callable[["ToolCall"], "asyncio.Future[ToolResult]"],
) -> Callable[["ToolCall"], None]:
    """Build the ``on_tool_block`` callback passed to ``complete_streaming``.

    ``invoke`` is a callable that takes a :class:`ToolCall` and
    returns a Future/Task wrapping ``ToolResult``. The returned
    callback is synchronous — the stream loop calls it from inside
    its event handler and shouldn't be blocked. Speculation work
    runs as a background task.
    """

    def _on_tool_block(call: "ToolCall") -> None:
        if not is_speculatable(call.name):
            return
        task = asyncio.ensure_future(invoke(call))
        cache.add(call.id, task)

    return _on_tool_block


async def maybe_await_cached(
    cache: SpeculationCache,
    call: "ToolCall",
    fallback: Callable[[], "asyncio.Future[ToolResult]"],
) -> "ToolResult":
    """Phase-B helper. Returns the speculated result if one is in
    the cache; otherwise dispatches via ``fallback``.

    Cancellation: if the cached task was cancelled mid-flight (user
    Stop, hop end), fall through to the fallback so the LLM still
    sees a result rather than a CancelledError surfaced as a tool
    failure.
    """
    task = cache.take(call.id)
    if task is None:
        return await fallback()
    try:
        return await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        return await fallback()


__all__ = [
    "READ_ONLY_TOOLS",
    "SpeculationCache",
    "is_speculatable",
    "make_speculation_callback",
    "maybe_await_cached",
]
