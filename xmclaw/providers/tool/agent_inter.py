"""Agent-to-agent tools — Epic #17 Phase 5 + 6.

Four tools the LLM running in one agent can call to inspect or delegate
to other agents running in the same daemon:

* ``list_agents`` — enumerate registered agent ids + readiness.
* ``chat_with_agent`` — blocking: await the callee's ``run_turn`` and
  return the last assistant message as a string.
* ``submit_to_agent`` — fire-and-forget: kick off a turn in the
  background, return a task_id the caller can poll later. Lets a
  long-running task (multi-file refactor, background research) run in
  parallel to the caller's own reasoning.
* ``check_agent_task`` — poll a task_id, return status (pending /
  running / done / error) and, when done, the reply.

This provider is constructed with:

* a ``MultiAgentManager``-like object — duck-typed via the
  :class:`_ManagerLike` Protocol so ``providers/tool/`` stays free of
  ``xmclaw.daemon.*`` imports (see :file:`AGENTS.md` §2).
* an optional primary agent loop — routed to when the caller sends
  ``agent_id="main"``. ``MultiAgentManager`` does NOT track the
  primary; it lives on ``app.state.agent``. We accept a reference
  here so the tool surface presents a single unified namespace to the
  LLM ("agents I can talk to" == primary + every registered worker).

Coupling to ``AgentLoop._histories``: ``chat_with_agent`` / the
background submit task both read ``agent_loop._histories[session_id]``
to fish out the last assistant message. Yes, it's a private attr —
but exposing a public accessor means editing ``AgentLoop`` whose
contract every translator + test already depends on. The alternative
(subscribing to the bus for ``TEXT_STREAM`` events and re-assembling
the response) is more code and duplicates information the agent loop
already has structured. Revisit if the history shape changes.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from xmclaw.core.agent_context import get_current_agent_id
from xmclaw.core.ir import ToolCall, ToolResult, ToolSpec
from xmclaw.providers.tool.base import ToolProvider


# ── Duck-typed Protocols (keep providers/tool free of daemon imports) ───


class _AgentLoopLike(Protocol):
    """Minimal surface we need from :class:`xmclaw.daemon.agent_loop.AgentLoop`."""

    _histories: dict[str, Any]

    async def run_turn(self, session_id: str, content: str) -> None: ...


class _WorkspaceLike(Protocol):
    agent_id: str
    agent_loop: _AgentLoopLike | None
    # Phase 7: workspaces with ``kind != "llm"`` (e.g. evolution
    # observers) carry no agent_loop. Kept as a plain attribute so
    # ``list_agents`` can surface the discriminator without duck-typing
    # around a missing field.
    kind: str

    def is_ready(self) -> bool: ...


class _ManagerLike(Protocol):
    def list_ids(self) -> list[str]: ...

    def get(self, agent_id: str) -> _WorkspaceLike | None: ...

    def __contains__(self, agent_id: str) -> bool: ...


# ── Tool specs advertised to the LLM ─────────────────────────────────────

_LIST_AGENTS_SPEC = ToolSpec(
    name="list_agents",
    description=(
        "List every other agent running on this daemon. Returns a "
        "JSON string with one entry per agent: {agent_id, ready, "
        "primary, kind}. 'kind' is 'llm' for a chatty agent you can "
        "delegate to via chat_with_agent / submit_to_agent, or "
        "'evolution' for a headless observer you should NOT send "
        "prompts to."
    ),
    parameters_schema={"type": "object", "properties": {}},
)

_CHAT_WITH_AGENT_SPEC = ToolSpec(
    name="chat_with_agent",
    description=(
        "Send a message to another agent and BLOCK until it responds. "
        "Use for short, focused questions where you need the reply "
        "before continuing. For long-running work use submit_to_agent."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "agent_id": {
                "type": "string",
                "description": (
                    "Target agent's id. Use 'main' for the primary "
                    "agent; otherwise an id returned by list_agents."
                ),
            },
            "content": {
                "type": "string",
                "description": "The user-style prompt to send.",
            },
        },
        "required": ["agent_id", "content"],
    },
)

_SUBMIT_TO_AGENT_SPEC = ToolSpec(
    name="submit_to_agent",
    description=(
        "Dispatch a turn to another agent IN THE BACKGROUND. Returns "
        "immediately with a task_id. Poll it with check_agent_task. "
        "Use when the delegated work may take a while and you want to "
        "continue reasoning in parallel."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "agent_id": {
                "type": "string",
                "description": "Target agent id (see chat_with_agent).",
            },
            "content": {"type": "string"},
        },
        "required": ["agent_id", "content"],
    },
)

_CHECK_AGENT_TASK_SPEC = ToolSpec(
    name="check_agent_task",
    description=(
        "Look up a task by the id returned from submit_to_agent. "
        "Returns {status, reply, error} — status is one of 'pending', "
        "'running', 'done', 'error'. Reply is populated only when "
        "status=='done'; error only when status=='error'."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "Id from a prior submit_to_agent call.",
            },
        },
        "required": ["task_id"],
    },
)


# ── submit / check task bookkeeping ──────────────────────────────────────


@dataclass
class _TaskRecord:
    """In-memory record for a submit_to_agent dispatch.

    Not persisted — daemon restart drops everything. Acceptable for
    Phase 5 because these tasks are ephemeral LLM-triggered delegations,
    not user-authored work items. Phase 6+ can promote to SQLite if a
    real durability requirement shows up.
    """

    task_id: str
    agent_id: str
    session_id: str
    content: str
    status: str = "pending"  # pending | running | done | error
    reply: str | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    completed_at: float | None = None


_MAX_TASKS = 256  # bounded dict — drop oldest when full


class AgentInterTools(ToolProvider):
    """The four agent-to-agent tools as a single ``ToolProvider``."""

    def __init__(
        self,
        *,
        manager: _ManagerLike,
        primary_loop: _AgentLoopLike | None = None,
        primary_id: str = "main",
    ) -> None:
        self._manager = manager
        self._primary_loop = primary_loop
        self._primary_id = primary_id
        self._tasks: dict[str, _TaskRecord] = {}

    # ── ToolProvider surface ─────────────────────────────────────────

    def list_tools(self) -> list[ToolSpec]:
        return [
            _LIST_AGENTS_SPEC,
            _CHAT_WITH_AGENT_SPEC,
            _SUBMIT_TO_AGENT_SPEC,
            _CHECK_AGENT_TASK_SPEC,
        ]

    async def invoke(self, call: ToolCall) -> ToolResult:
        t0 = time.perf_counter()
        try:
            if call.name == "list_agents":
                content = self._do_list_agents()
            elif call.name == "chat_with_agent":
                content = await self._do_chat_with_agent(call)
            elif call.name == "submit_to_agent":
                content = self._do_submit_to_agent(call)
            elif call.name == "check_agent_task":
                content = self._do_check_agent_task(call)
            else:
                return ToolResult(
                    call_id=call.id, ok=False, content=None,
                    error=f"unknown tool: {call.name!r}",
                    latency_ms=(time.perf_counter() - t0) * 1000.0,
                )
            return ToolResult(
                call_id=call.id, ok=True, content=content,
                latency_ms=(time.perf_counter() - t0) * 1000.0,
            )
        except _ToolError as exc:
            return ToolResult(
                call_id=call.id, ok=False, content=None,
                error=str(exc),
                latency_ms=(time.perf_counter() - t0) * 1000.0,
            )

    # ── list_agents ──────────────────────────────────────────────────

    def _do_list_agents(self) -> str:
        import json
        rows: list[dict[str, Any]] = []
        if self._primary_loop is not None:
            rows.append({
                "agent_id": self._primary_id, "ready": True, "primary": True,
                "kind": "llm",
            })
        for aid in self._manager.list_ids():
            if aid == self._primary_id:
                # Defensive: manager shouldn't hold the primary id, but
                # if a future merge lets it, don't double-emit.
                continue
            ws = self._manager.get(aid)
            ready = ws.is_ready() if ws is not None else False
            # ``getattr`` rather than ``ws.kind`` directly so older
            # workspace objects that predate Phase 7 still round-trip
            # cleanly — treat the absence as an LLM workspace.
            kind = getattr(ws, "kind", "llm") if ws is not None else "llm"
            rows.append({
                "agent_id": aid, "ready": ready, "primary": False,
                "kind": kind,
            })
        return json.dumps({"agents": rows})

    # ── chat_with_agent (sync) ───────────────────────────────────────

    async def _do_chat_with_agent(self, call: ToolCall) -> str:
        agent_id, content = self._extract_target_and_content(call)
        loop = self._resolve_loop(agent_id)
        caller = self._resolve_caller_id()
        session_id = _make_a2a_session_id(caller=caller, callee=agent_id)
        stamped = _prepend_caller_marker(content, caller)
        await loop.run_turn(session_id, stamped)
        reply = _extract_last_assistant(loop, session_id)
        return reply

    # ── submit_to_agent (async) ──────────────────────────────────────

    def _do_submit_to_agent(self, call: ToolCall) -> str:
        import json
        agent_id, content = self._extract_target_and_content(call)
        loop = self._resolve_loop(agent_id)
        caller = self._resolve_caller_id()
        task_id = uuid.uuid4().hex[:16]
        session_id = _make_a2a_session_id(caller=caller, callee=agent_id)
        stamped = _prepend_caller_marker(content, caller)
        record = _TaskRecord(
            task_id=task_id, agent_id=agent_id,
            session_id=session_id, content=stamped,
        )
        self._store_task(record)
        # Kick off the real work. We intentionally do NOT await the
        # coroutine — the whole point of submit is that the caller can
        # proceed in parallel. The task's exceptions are captured into
        # ``record.error`` via the wrapper.
        asyncio.create_task(self._run_background(record, loop))
        return json.dumps({"task_id": task_id, "agent_id": agent_id})

    async def _run_background(
        self, record: _TaskRecord, loop: _AgentLoopLike,
    ) -> None:
        record.status = "running"
        try:
            await loop.run_turn(record.session_id, record.content)
            record.reply = _extract_last_assistant(loop, record.session_id)
            record.status = "done"
        except Exception as exc:  # noqa: BLE001 — must land in record, not crash daemon
            record.error = f"{type(exc).__name__}: {exc}"
            record.status = "error"
        finally:
            record.completed_at = time.time()

    def _store_task(self, record: _TaskRecord) -> None:
        # Bounded: drop oldest by insertion order once the cap is hit.
        # dict preserves insertion order in CPython 3.7+.
        if len(self._tasks) >= _MAX_TASKS:
            oldest = next(iter(self._tasks))
            del self._tasks[oldest]
        self._tasks[record.task_id] = record

    # ── check_agent_task ─────────────────────────────────────────────

    def _do_check_agent_task(self, call: ToolCall) -> str:
        import json
        raw = call.args.get("task_id")
        if not isinstance(raw, str) or not raw.strip():
            raise _ToolError("task_id required")
        task_id = raw.strip()
        record = self._tasks.get(task_id)
        if record is None:
            raise _ToolError(f"unknown task_id: {task_id!r}")
        payload: dict[str, Any] = {
            "task_id": record.task_id,
            "agent_id": record.agent_id,
            "status": record.status,
        }
        if record.status == "done":
            payload["reply"] = record.reply or ""
        elif record.status == "error":
            payload["error"] = record.error or "unknown error"
        return json.dumps(payload)

    # ── helpers ──────────────────────────────────────────────────────

    def _extract_target_and_content(self, call: ToolCall) -> tuple[str, str]:
        raw_id = call.args.get("agent_id")
        if not isinstance(raw_id, str) or not raw_id.strip():
            raise _ToolError("agent_id required")
        agent_id = raw_id.strip()
        raw_content = call.args.get("content")
        if not isinstance(raw_content, str):
            raise _ToolError("content required")
        return agent_id, raw_content

    def _resolve_caller_id(self) -> str:
        """Return the id of the agent currently running the turn.

        Phase 6: agent-to-agent naming keys off *agent ids*, not WS
        session ids. The ambient id is populated by the Phase 4
        :class:`AgentContextMiddleware` + the WS handler's
        ``use_current_agent_id`` wrap, so any turn that reached a tool
        via a real WS frame has a value. The ``primary_id`` fallback
        covers contexts outside that flow — the CLI, tests that call
        ``invoke`` directly, scheduler / cron jobs — where the only
        sensible default is "the primary is talking".
        """
        return get_current_agent_id() or self._primary_id

    def _resolve_loop(self, agent_id: str) -> _AgentLoopLike:
        """Map ``agent_id`` → concrete :class:`AgentLoop`.

        ``main`` (or whatever ``primary_id`` is configured as) routes to
        the primary loop. Anything else goes through the manager.
        """
        if agent_id == self._primary_id:
            if self._primary_loop is None:
                raise _ToolError("primary agent is not wired")
            return self._primary_loop
        ws = self._manager.get(agent_id)
        if ws is None:
            raise _ToolError(f"unknown agent_id: {agent_id!r}")
        if ws.agent_loop is None:
            raise _ToolError(f"agent {agent_id!r} is not ready")
        return ws.agent_loop


class _ToolError(Exception):
    """Sentinel for tool-invocation validation errors.

    Raised inside ``_do_*`` helpers; caught by :meth:`AgentInterTools.invoke`
    and surfaced as ``ToolResult(ok=False, error=...)``. Kept module-private
    because the error-payload contract is "string blob in ToolResult.error",
    not an exception type the caller should import.
    """


def _make_a2a_session_id(*, caller: str, callee: str) -> str:
    """Format: ``{caller}:to:{callee}:{ts}:{uuid8}`` — Phase 6 convention.

    The literal ``to`` separator is there on purpose: a single token
    split on ``:`` yields ``[caller, "to", callee, ts, uuid]`` which is
    trivially parseable by log viewers and event-dump tooling. It also
    makes agent-to-agent session ids visually distinct from the raw
    user-WS session ids (which are free-form and rarely contain ``:to:``).

    ``ts`` is milliseconds since epoch; ``uuid8`` is 8 hex chars from a
    fresh ``uuid4``. Collisions would require the same caller→callee
    edge firing twice in the same millisecond AND the uuid's first 32
    bits colliding — cheap enough to live with.
    """
    stamp = int(time.time() * 1000)
    suffix = uuid.uuid4().hex[:8]
    return f"{caller}:to:{callee}:{stamp}:{suffix}"


_CALLER_MARKER_PREFIX = "[Agent "


def _prepend_caller_marker(content: str, caller: str) -> str:
    """Tag ``content`` with ``[Agent {caller} requesting]`` so the callee knows.

    The receiving agent's LLM sees this text as the next user message.
    Without the tag, a delegated prompt looks indistinguishable from a
    human user's — the callee might misroute ("the user wants X") or
    miss that cross-agent trust rules apply. Idempotent: if ``content``
    already starts with ``[Agent `` we assume the caller already stamped
    it (nested delegation, retry) and leave it alone — double-tagging
    would confuse the callee about chain depth.

    Format choice: brackets + blank line separator means the tag is
    lexically distinct from the body even when the body starts with a
    heading or code fence. The trailing ``\\n\\n`` is important — some
    translators collapse single newlines into spaces.
    """
    if content.startswith(_CALLER_MARKER_PREFIX):
        return content
    return f"[Agent {caller} requesting]\n\n{content}"


def _extract_last_assistant(
    loop: _AgentLoopLike, session_id: str,
) -> str:
    """Read the last assistant message's ``content`` from the loop's history.

    Returns empty string (not None) when there is no assistant message,
    so callers — who stuff this into a JSON result the LLM reads — don't
    have to special-case. An agent that produced no assistant response
    is a weird edge case (max_hops exceeded, tool-only turn) but not an
    error by this layer's definition.
    """
    history = loop._histories.get(session_id) or []
    for msg in reversed(history):
        if getattr(msg, "role", None) == "assistant":
            return getattr(msg, "content", "") or ""
    return ""
