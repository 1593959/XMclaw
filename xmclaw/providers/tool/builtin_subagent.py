"""Ephemeral subagent fanout — Kimi K2.6 ``Agent Swarm`` runtime pattern.

Background
==========

Kimi K2.6's ``Agent Swarm`` mode can spawn up to 300 ephemeral
sub-agents in parallel. Each receives an isolated context window
(no parent history pollution), runs a short tool-using loop, and
returns its leaf result for synthesis. This is *runtime fanout*,
not a model capability — the only thing baked into K2.6's weights
is the *judgement* of when to fan out.

This module externalises the fanout: we expose a single tool
``parallel_subagents`` that takes 2-8 independent subtask strings
and runs them concurrently using:

1. A fresh ``messages = [system, user=subtask]`` list per subtask —
   no shared history.
2. The same underlying LLM (passed at construction).
3. *Optionally* a stripped-down ToolProvider (passed at construction)
   for the subagents. If absent, subagents are pure-reasoning.
4. A hard hop cap per subagent (default 6) — sub-agents are leaves,
   not full agent loops.
5. A wall-clock timeout for the whole fanout.

Composition with the rest of XMclaw
===================================

This deliberately does NOT use ``MultiAgentManager`` — that's for
*long-lived registered workers*. Fanout is for *ephemeral leaves*
that vanish after returning. Different lifecycle, different cost
profile, different sweet spot. The two can coexist; the LLM picks
based on its task.

Does NOT use ``SwarmOrchestrator`` either — that requires an
HTNPlanner round-trip which the caller has already done implicitly
when it produced the subtask list. Fanout assumes the *caller* knows
how to slice the goal; this tool just executes the slices.

Safety
======

* Hard cap on subtask count (max 8) to prevent runaway fanouts.
* Per-subagent hop cap (default 6) to prevent infinite loops.
* Wall-clock timeout (default 90s for the whole fanout).
* Subagent failures don't poison the result — they're rolled up as
  ``[subagent N error: ...]`` so partial progress is visible.
* No nested fanout: subagent tools don't include this provider
  (caller's responsibility to compose).
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any

from xmclaw.core.ir import ToolCall, ToolResult, ToolSpec
from xmclaw.providers.tool.base import ToolProvider
from xmclaw.utils.log import get_logger

logger = get_logger(__name__)


# 2026-05-25: WorkerSwarm retired — its specialty + per-worker hop
# budget are absorbed here as optional per-call params so the LLM
# can request the same shaping inside a single observable fanout.
_VALID_ROLES = ("general", "code", "research", "ops", "comm")

_ROLE_HINTS = {
    "general": (
        "You are a focused generalist sub-agent. Stay tight; return a "
        "leaf answer the parent can integrate."
    ),
    "code": (
        "You are a code-focused sub-agent. Read files before editing, "
        "keep diffs minimal, and report file:line for any change."
    ),
    "research": (
        "You are a research sub-agent. Search the web / docs / repo, "
        "cite specific sources, and prefer primary evidence."
    ),
    "ops": (
        "You are an ops/shell sub-agent. Prefer dry-run / read-only "
        "commands first; never run anything destructive without a "
        "clear signal from the parent."
    ),
    "comm": (
        "You are a communications sub-agent. Compose / format the "
        "message; do not actually send anything — return draft text."
    ),
}

_PARALLEL_SUBAGENTS_SPEC = ToolSpec(
    name="parallel_subagents",
    description=(
        "Spawn 2-8 ephemeral sub-agents IN PARALLEL, each working on "
        "one independent subtask string. Returns a synthesised summary "
        "of all leaf results. Use when the goal naturally splits into "
        "INDEPENDENT pieces (e.g. \"summarise these 3 files\", "
        "\"compare options A/B/C\"). Do NOT use when subtasks depend on "
        "each other — sub-agents share no context with each other. "
        "Sub-agents have a 6-hop cap (raise via max_hops up to 12 if a "
        "task is heavier) and no further fanout capability."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "subtasks": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 2,
                "maxItems": 8,
                "description": (
                    "List of 2 to 8 independent subtask prompts. Each "
                    "becomes one sub-agent's user message. Keep each "
                    "subtask self-contained — include any context "
                    "from the parent turn the subagent will need."
                ),
            },
            "roles": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": list(_VALID_ROLES),
                },
                "description": (
                    "Optional. One role per subtask (same length as "
                    "subtasks). Shapes the sub-agent's system prompt. "
                    "general (default) | code | research | ops | comm."
                ),
            },
            "max_hops": {
                "type": "integer",
                "minimum": 1,
                "maximum": 12,
                "description": (
                    "Optional. Per-subagent hop budget override "
                    "(default 6, cap 12). Use higher for sub-tasks "
                    "that need multiple tool round-trips."
                ),
            },
            "goal": {
                "type": "string",
                "description": (
                    "Optional high-level goal these subtasks contribute "
                    "to. Used to synthesise the final summary if an LLM "
                    "is available."
                ),
            },
            "synthesis": {
                "type": "string",
                "enum": ["concat", "llm"],
                "description": (
                    "How to merge the leaf results. 'concat' (default) "
                    "joins them with separators. 'llm' calls the LLM "
                    "once to produce a coherent unified answer — costs "
                    "one extra LLM round-trip."
                ),
            },
        },
        "required": ["subtasks"],
    },
)


@dataclass(slots=True)
class _SubResult:
    index: int
    subtask: str
    ok: bool
    content: str = ""
    error: str = ""
    elapsed_s: float = 0.0
    hops: int = 0


class SubagentToolProvider(ToolProvider):
    """Exposes one tool: ``parallel_subagents``.

    Construction params:

    * ``llm`` — async-completing LLM used both for the leaf reasoning
      and (optionally) for synthesis. Same shape AgentLoop uses.
    * ``tools`` — optional inner ``ToolProvider`` for sub-agents to
      use. None means leaf sub-agents are pure reasoning.
    * ``max_hops_per_subagent`` — hard cap on tool-use loop length
      inside one sub-agent. Default 6.
    * ``max_concurrency`` — semaphore to throttle the asyncio.gather.
      Default 4 — keeps pressure off the LLM endpoint.
    * ``fanout_timeout_s`` — wall-clock cap for the whole fanout.
      Default 120s.
    * ``per_subagent_timeout_s`` — wall-clock cap per sub-agent.
      Default 45s.
    * ``enabled`` — kill switch. Default True.
    """

    def __init__(
        self,
        *,
        llm: Any | None = None,
        tools: ToolProvider | None = None,
        max_hops_per_subagent: int = 6,
        max_concurrency: int = 4,
        fanout_timeout_s: float = 120.0,
        per_subagent_timeout_s: float = 45.0,
        enabled: bool = True,
        bus: Any | None = None,
    ) -> None:
        self._llm = llm
        self._tools = tools
        self._max_hops = max(1, int(max_hops_per_subagent))
        self._sem = asyncio.Semaphore(max(1, int(max_concurrency)))
        self._fanout_timeout = max(10.0, float(fanout_timeout_s))
        self._per_timeout = max(5.0, float(per_subagent_timeout_s))
        self._enabled = bool(enabled)
        # 2026-05-25: optional bus + parent session id are late-bound
        # so we can publish per-subagent lifecycle events for the UI
        # (replaces the WorkerSwarm worker_started/completed stream).
        self._bus = bus
        self._parent_session_id: str | None = None

    def set_llm(self, llm: Any) -> None:
        """Late-binding hook used by ``build_agent_from_config`` after
        the LLM is constructed."""
        self._llm = llm

    def set_tools(self, tools: ToolProvider | None) -> None:
        """Late-binding hook so the subagent can share the parent's
        tool catalogue."""
        self._tools = tools

    def set_bus(self, bus: Any) -> None:
        """Late-binding hook so the fanout can publish per-subagent
        events onto the parent session bus. Optional — when unset,
        fanout is silent and only the final ToolResult is visible."""
        self._bus = bus

    def bind_session(self, session_id: str | None) -> None:
        """Per-invocation hook. The agent_loop sets this before each
        tool call so fanout events land on the right session."""
        self._parent_session_id = session_id

    def list_tools(self) -> list[ToolSpec]:
        if not self._enabled:
            return []
        return [_PARALLEL_SUBAGENTS_SPEC]

    async def invoke(self, call: ToolCall) -> ToolResult:
        if call.name != "parallel_subagents":
            return ToolResult(
                call_id=call.id, ok=False, content=None,
                error=f"unknown tool: {call.name}",
            )
        if not self._enabled or self._llm is None:
            return ToolResult(
                call_id=call.id, ok=False, content=None,
                error="parallel_subagents disabled or LLM not wired",
            )

        # Bind session for per-subagent event publishing. hop_loop
        # stamps the call with the parent session id before invoke.
        self._parent_session_id = getattr(call, "session_id", None) or None

        subtasks = call.args.get("subtasks")
        if (
            not isinstance(subtasks, list)
            or not (2 <= len(subtasks) <= 8)
            or not all(isinstance(s, str) and s.strip() for s in subtasks)
        ):
            return ToolResult(
                call_id=call.id, ok=False, content=None,
                error="subtasks must be a list of 2-8 non-empty strings",
            )

        goal = str(call.args.get("goal", "")).strip()
        synthesis = str(call.args.get("synthesis", "concat")).lower()
        if synthesis not in ("concat", "llm"):
            synthesis = "concat"

        # Per-call role hints. Pad / truncate to len(subtasks).
        raw_roles = call.args.get("roles") or []
        if not isinstance(raw_roles, list):
            raw_roles = []
        roles: list[str] = []
        for i in range(len(subtasks)):
            r = raw_roles[i] if i < len(raw_roles) else "general"
            r = str(r).lower() if isinstance(r, str) else "general"
            roles.append(r if r in _VALID_ROLES else "general")

        # Per-call max_hops override (capped at 12; default to instance value).
        raw_hops = call.args.get("max_hops")
        if isinstance(raw_hops, int) and raw_hops > 0:
            effective_hops = min(12, raw_hops)
        else:
            effective_hops = self._max_hops

        t0 = time.perf_counter()
        try:
            results = await asyncio.wait_for(
                self._fanout(subtasks, roles=roles, max_hops=effective_hops),
                timeout=self._fanout_timeout,
            )
        except asyncio.TimeoutError:
            return ToolResult(
                call_id=call.id, ok=False, content=None,
                error=(
                    f"fanout exceeded {self._fanout_timeout}s wall-clock "
                    f"cap with {len(subtasks)} subtasks"
                ),
            )

        merged = await self._synthesise(results, goal=goal, mode=synthesis)
        elapsed = round(time.perf_counter() - t0, 2)

        ok_count = sum(1 for r in results if r.ok)
        fail_count = len(results) - ok_count
        summary = {
            "result": merged,
            "completed": ok_count,
            "failed": fail_count,
            "total": len(results),
            "elapsed_s": elapsed,
            "per_subagent": [
                {
                    "index": r.index,
                    "ok": r.ok,
                    "hops": r.hops,
                    "elapsed_s": round(r.elapsed_s, 2),
                    "error": r.error if not r.ok else None,
                }
                for r in results
            ],
        }
        logger.info(
            "subagent.fanout_done total=%d ok=%d fail=%d elapsed=%.1fs",
            len(results), ok_count, fail_count, elapsed,
        )
        return ToolResult(
            call_id=call.id, ok=True,
            content=json.dumps(summary, ensure_ascii=False),
            error=None,
        )

    # ── Internals ─────────────────────────────────────────────────

    async def _fanout(
        self,
        subtasks: list[str],
        *,
        roles: list[str],
        max_hops: int,
    ) -> list[_SubResult]:
        async def _one(i: int, s: str) -> _SubResult:
            async with self._sem:
                return await self._run_one(
                    i, s, role=roles[i], max_hops=max_hops,
                )

        return await asyncio.gather(
            *(_one(i, s) for i, s in enumerate(subtasks)),
        )

    async def _run_one(
        self, index: int, subtask: str,
        *, role: str = "general", max_hops: int | None = None,
    ) -> _SubResult:
        """Mini tool-use loop for one sub-agent.

        Each sub-agent has its own messages list (no shared history),
        runs at most ``max_hops`` LLM round-trips, returns either
        the final assistant text or a structured error.
        """
        t0 = time.perf_counter()
        await self._publish_subagent_event(
            "subagent_started",
            index=index, subtask=subtask, role=role,
        )
        try:
            res = await asyncio.wait_for(
                self._do_run_one(
                    index, subtask, t0,
                    role=role, max_hops=max_hops or self._max_hops,
                ),
                timeout=self._per_timeout,
            )
            await self._publish_subagent_event(
                "subagent_completed",
                index=index, subtask=subtask, role=role,
                ok=res.ok, output=res.content, error=res.error,
                hops=res.hops, elapsed_s=res.elapsed_s,
            )
            return res
        except asyncio.TimeoutError:
            err = f"subagent timed out after {self._per_timeout}s"
            await self._publish_subagent_event(
                "subagent_completed",
                index=index, subtask=subtask, role=role,
                ok=False, output="", error=err,
                hops=0, elapsed_s=time.perf_counter() - t0,
            )
            return _SubResult(
                index=index, subtask=subtask, ok=False,
                error=err,
                elapsed_s=time.perf_counter() - t0,
            )
        except Exception as exc:  # noqa: BLE001
            err = f"subagent failed: {type(exc).__name__}: {exc}"
            await self._publish_subagent_event(
                "subagent_completed",
                index=index, subtask=subtask, role=role,
                ok=False, output="", error=err,
                hops=0, elapsed_s=time.perf_counter() - t0,
            )
            return _SubResult(
                index=index, subtask=subtask, ok=False,
                error=err,
                elapsed_s=time.perf_counter() - t0,
            )

    async def _publish_subagent_event(
        self, kind: str, **payload: Any,
    ) -> None:
        """Fire a subagent lifecycle event onto the parent session bus.

        Best-effort — never raises. When bus or session is unset
        (e.g. unit tests), this is a no-op.
        """
        if self._bus is None or not self._parent_session_id:
            return
        try:
            from xmclaw.core.bus.events import EventType, make_event
            etype = getattr(EventType, kind.upper(), None)
            if etype is None:
                return
            await self._bus.publish(make_event(
                session_id=self._parent_session_id,
                agent_id=f"subagent:{payload.get('index', '?')}",
                type=etype,
                payload=payload,
            ))
        except Exception:  # noqa: BLE001
            pass

    async def _do_run_one(
        self, index: int, subtask: str, t0: float,
        *, role: str = "general", max_hops: int | None = None,
    ) -> _SubResult:
        from xmclaw.providers.llm.base import Message
        sys_prompt = (
            "You are an ephemeral sub-agent. You were given ONE small "
            "subtask by a parent agent. Use available tools if needed. "
            "Keep responses focused and concise — return a clear leaf "
            "answer the parent can integrate, NOT a verbose narrative."
        )
        role_hint = _ROLE_HINTS.get(role)
        if role_hint:
            sys_prompt = f"{sys_prompt}\n\n{role_hint}"
        hop_cap = max(1, int(max_hops or self._max_hops))
        messages: list[Any] = [
            Message(role="system", content=sys_prompt),
            Message(role="user", content=subtask),
        ]
        tool_specs = (
            self._tools.list_tools() if self._tools is not None else None
        )

        for hop in range(hop_cap):
            resp = await self._llm.complete(messages, tools=tool_specs)
            content = (getattr(resp, "content", "") or "").strip()
            tool_calls = getattr(resp, "tool_calls", None) or []

            if not tool_calls:
                return _SubResult(
                    index=index, subtask=subtask, ok=True,
                    content=content, hops=hop + 1,
                    elapsed_s=time.perf_counter() - t0,
                )

            messages.append(
                Message(
                    role="assistant",
                    content=content,
                    tool_calls=tuple(tool_calls),
                ),
            )
            if self._tools is None:
                return _SubResult(
                    index=index, subtask=subtask, ok=False,
                    error="subagent issued tool calls but no tools wired",
                    hops=hop + 1,
                    elapsed_s=time.perf_counter() - t0,
                )

            for tc in tool_calls:
                name = getattr(tc, "name", "")
                args = getattr(tc, "args", None) or {}
                call_id = getattr(tc, "id", "") or f"sub-{index}-h{hop}"
                if not name:
                    continue
                # Block recursive fanout from inside a sub-agent.
                if name == "parallel_subagents":
                    messages.append(
                        Message(
                            role="tool",
                            content="(nested parallel_subagents blocked)",
                            tool_call_id=call_id,
                        ),
                    )
                    continue
                sub_call = ToolCall(
                    id=call_id, name=name, args=dict(args),
                    provenance="synthetic",
                )
                sub_res = await self._tools.invoke(sub_call)
                messages.append(
                    Message(
                        role="tool",
                        content=str(sub_res.content or sub_res.error or ""),
                        tool_call_id=call_id,
                    ),
                )

        # Hop cap exhausted.
        return _SubResult(
            index=index, subtask=subtask, ok=False,
            error=f"subagent exhausted {self._max_hops} hops",
            hops=self._max_hops,
            elapsed_s=time.perf_counter() - t0,
        )

    async def _synthesise(
        self,
        results: list[_SubResult],
        *,
        goal: str,
        mode: str,
    ) -> str:
        # Always provide the raw concat — used as fallback even in 'llm' mode.
        raw_lines: list[str] = []
        for r in results:
            if r.ok:
                raw_lines.append(
                    f"--- subagent {r.index} ---\n{r.content}"
                )
            else:
                raw_lines.append(
                    f"--- subagent {r.index} (failed) ---\n[{r.error}]"
                )
        concat = "\n\n".join(raw_lines)

        if mode != "llm" or self._llm is None:
            return concat
        if not any(r.ok for r in results):
            return concat  # nothing to synthesise

        try:
            from xmclaw.providers.llm.base import Message
            prompt = _SYNTH_PROMPT.format(
                goal=goal or "(no explicit goal — produce a coherent merge)",
                results=concat[:8000],
            )
            resp = await asyncio.wait_for(
                self._llm.complete([Message(role="user", content=prompt)]),
                timeout=20.0,
            )
            out = (getattr(resp, "content", "") or "").strip()
            return out or concat
        except Exception as exc:  # noqa: BLE001
            logger.warning("subagent.synthesis_failed err=%s", exc)
            return concat


_SYNTH_PROMPT = """\
You will receive partial leaf-results from sub-agents that worked
in parallel on independent slices of a larger goal. Produce ONE
coherent unified answer that integrates them.

GOAL:
{goal}

PARTIAL RESULTS:
{results}

Rules:
  * Merge, don't just concatenate — remove redundancy, resolve
    contradictions if any.
  * Cite which sub-agent contributed each fact if it'd confuse the
    reader otherwise; otherwise present a clean answer.
  * If some sub-agents failed (marked `(failed)`), note their absence
    briefly and continue with what's available.
  * No prose like "Here is the unified answer:" — go straight to it.
"""


__all__ = ["SubagentToolProvider"]
