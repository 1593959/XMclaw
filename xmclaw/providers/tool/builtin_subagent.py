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
4. A hard hop cap per subagent (default 50) — sub-agents are leaves,
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
* Per-subagent hop cap (default 50) to prevent infinite loops.
* Wall-clock timeout (default 90s for the whole fanout).
* Subagent failures don't poison the result — they're rolled up as
  ``[subagent N error: ...]`` so partial progress is visible.
* Bounded nested fanout (2026-06-15): a sub-agent MAY call
  parallel_subagents itself, up to ``max_depth`` levels (default 2;
  top-level sub-agents are depth 1). Beyond that it's blocked. The
  depth cap + concurrency semaphore + wall-clock keep a decomposition
  tree from running away.
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


# 派发前编辑拆解（#3）：当用户显式点了「派专家团」时，组长拆完任务先
# 暂停，把拆解方案(角色+子任务)推给前端可编辑，用户改/删/加/确认后再真正
# 派发。复用 builtin_user._PENDING_QUESTIONS 的 Future 暂停/恢复套路：
# 工具侧建 Future 并 await，WS handler 收到 fanout_review_decision 帧后
# 用编辑过的方案 resolve。刷新恢复靠 _PENDING_FANOUT_PAYLOADS 快照。
_PENDING_FANOUT_REVIEWS: dict[str, asyncio.Future] = {}
_PENDING_FANOUT_PAYLOADS: dict[str, dict] = {}


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

# Phase 11: keyword → capability mapping for routing a subtask to a
# specialist CHAT model. 2026-06-15: ONLY vision belongs here — a vision
# model is still a chat model, so swapping a sub-agent's LLM to one lets
# it interpret images. Generation (image/video/audio) is NOT a chat
# capability: a sub-agent generates by CALLING the generate_image /
# generate_video tool (which delegates to the configured backend), so it
# keeps its normal chat model. Swapping its LLM to an image-only endpoint
# would just break its tool-use loop.
_SUBTASK_CAPABILITY_HINTS: dict[str, str] = {
    "vision": "vision",
    "截图": "vision",
    "screenshot": "vision",
    "看图": "vision",
    "识图": "vision",
}

# Capabilities that are NOT usable as a chat/reasoning model — a sub-agent
# must never swap its LLM to one of these (it would lose the ability to
# reason + call tools). Generation goes through tools instead.
_NON_CHAT_CAPABILITIES = frozenset({"image_gen", "video_gen", "audio_out", "embedding"})

_PARALLEL_SUBAGENTS_SPEC = ToolSpec(
    name="parallel_subagents",
    description=(
        "Spawn 2-8 ephemeral sub-agents IN PARALLEL, each working on "
        "one independent subtask string. Returns a synthesised summary "
        "of all leaf results. Use when the goal naturally splits into "
        "INDEPENDENT pieces (e.g. \"summarise these 3 files\", "
        "\"compare options A/B/C\"). Do NOT use when subtasks depend on "
        "each other — sub-agents share no context with each other. "
        "Sub-agents have a 50-hop cap (raise via max_hops up to 100 if a "
        "task is heavier). A sub-agent may itself decompose via a nested "
        "parallel_subagents call, but only a couple levels deep (a deeper "
        "tree is blocked). "
        "A subtask that needs to SEE images can be routed to a vision "
        "model (see specialist_models). To GENERATE an image/video, a "
        "sub-agent just calls the generate_image / generate_video tool — "
        "no special model needed."
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
                "maximum": 100,
                "description": (
                    "Optional. Per-subagent hop budget override "
                    "(default 6, cap 100). Use higher for sub-tasks "
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
            "specialist_models": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional. One CHAT-model capability per subtask (same "
                    "length as subtasks) to route that sub-agent to a "
                    "registered model with that capability — currently only "
                    "'vision' is meaningful (a sub-agent that must read an "
                    "image). Generation capabilities (image_gen / video_gen "
                    "/ audio_out) are ignored here: to generate, the "
                    "sub-agent calls the generate_image / generate_video "
                    "tool, which delegates to the configured backend."
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
      inside one sub-agent. Default 50.
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
        llm_registry: Any | None = None,
        max_hops_per_subagent: int = 100,
        max_concurrency: int = 4,
        # 2026-05-25: both caps bumped to 5 min per user request.
        # Previous 120s fanout cap aborted mid-way through real-world
        # swarm-mode fan-outs (multi-file refactor, parallel research)
        # where individual leaves legitimately needed more than 120s.
        fanout_timeout_s: float = 300.0,
        per_subagent_timeout_s: float = 300.0,
        # 2026-06-15 (#7): allow bounded NESTED fanout. A sub-agent may
        # itself call parallel_subagents up to this depth (1 = the old
        # flat behaviour, no nesting; 2 = sub-agents may spawn one more
        # level; etc.). Bounded by max_concurrency + the wall-clock so a
        # tree can't runaway. Top-level sub-agents are depth 1.
        max_depth: int = 2,
        enabled: bool = True,
        bus: Any | None = None,
    ) -> None:
        self._llm = llm
        self._tools = tools
        self._llm_registry = llm_registry
        self._max_hops = max(1, int(max_hops_per_subagent))
        self._max_depth = max(1, int(max_depth))
        self._max_concurrency = max(1, int(max_concurrency))
        self._sem = asyncio.Semaphore(self._max_concurrency)
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

    def set_llm_registry(self, registry: Any | None) -> None:
        """Late-binding hook so sub-agents can route to specialist
        models (image_gen / video_gen / audio_out) based on subtask
        content."""
        self._llm_registry = registry

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

        # Phase 11: optional per-subtask specialist capability hints.
        raw_specialists = call.args.get("specialist_models") or []
        if not isinstance(raw_specialists, list):
            raw_specialists = []
        specialists: list[str] = []
        for i in range(len(subtasks)):
            s = raw_specialists[i] if i < len(raw_specialists) else ""
            specialists.append(str(s).strip().lower() if isinstance(s, str) else "")

        t0 = time.perf_counter()
        interactive = bool(call.args.get("interactive_review")) and bool(
            self._parent_session_id
        )

        # 派发前编辑拆解（#3）：显式点了「派专家团」时先把拆解方案推给前端
        # 编辑、阻塞等确认，再用最终方案派发；审批卡本身承担组长拆解展示，
        # 故此分支不再发 fanout_started（前端在确认时把审批卡转入运行态）。
        # 普通 LLM 自主 fanout 不打断 —— 照常发 fanout_started 后直接派发。
        if interactive:
            decision = await self._await_fanout_review(
                goal=goal, synthesis=synthesis,
                subtasks=subtasks, roles=roles, specialists=specialists,
            )
            if decision is None or decision.get("cancelled"):
                return ToolResult(
                    call_id=call.id, ok=False, content=None,
                    error="fanout cancelled by user before dispatch",
                )
            subtasks = decision["subtasks"]
            roles = decision["roles"]
            specialists = decision["specialists"]
            synthesis = decision.get("synthesis", synthesis)
        else:
            await self._publish_subagent_event(
                "fanout_started",
                goal=goal,
                total=len(subtasks),
                synthesis=synthesis,
                plan=[
                    {
                        "index": i,
                        "role": roles[i],
                        "subtask": subtasks[i],
                        "specialist": specialists[i],
                    }
                    for i in range(len(subtasks))
                ],
            )

        try:
            results = await asyncio.wait_for(
                self._fanout(
                    subtasks, roles=roles, max_hops=effective_hops,
                    specialists=specialists, depth=1,
                ),
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
        specialists: list[str],
        depth: int = 1,
        sem: "asyncio.Semaphore | None" = None,
    ) -> list[_SubResult]:
        # Nested fanouts get their OWN semaphore (passed by
        # _run_nested_fanout): the parent sub-agent already holds a permit
        # on the top-level ``self._sem`` while it waits for its children,
        # so reusing it would deadlock at low concurrency.
        _sem = sem or self._sem

        async def _one(i: int, s: str) -> _SubResult:
            async with _sem:
                return await self._run_one(
                    i, s, role=roles[i], max_hops=max_hops,
                    specialist=specialists[i] if i < len(specialists) else "",
                    depth=depth,
                )

        return await asyncio.gather(
            *(_one(i, s) for i, s in enumerate(subtasks)),
        )

    async def _run_nested_fanout(self, args: dict[str, Any], *, depth: int) -> str:
        """Run a parallel_subagents call issued from INSIDE a sub-agent.
        Returns a synthesised string for the parent sub-agent's tool result.
        Lenient: bad args come back as an error string, never raise (a
        nested decomposition failing must not crash the parent)."""
        raw_subtasks = args.get("subtasks")
        if not isinstance(raw_subtasks, list):
            return "(nested fanout error: 'subtasks' must be a list)"
        subtasks = [str(s) for s in raw_subtasks if isinstance(s, str) and s.strip()]
        if len(subtasks) < 2:
            return "(nested fanout error: need at least 2 subtasks)"
        subtasks = subtasks[:8]  # same hard cap as the top level
        raw_roles = args.get("roles") if isinstance(args.get("roles"), list) else []
        roles = [
            str(raw_roles[i]).strip().lower() if i < len(raw_roles) and isinstance(raw_roles[i], str) else "general"
            for i in range(len(subtasks))
        ]
        raw_spec = args.get("specialist_models") if isinstance(args.get("specialist_models"), list) else []
        specialists = [
            str(raw_spec[i]).strip().lower() if i < len(raw_spec) and isinstance(raw_spec[i], str) else ""
            for i in range(len(subtasks))
        ]
        try:
            results = await asyncio.wait_for(
                self._fanout(
                    subtasks, roles=roles, max_hops=self._max_hops,
                    specialists=specialists, depth=depth,
                    sem=asyncio.Semaphore(self._max_concurrency),
                ),
                timeout=self._fanout_timeout,
            )
        except asyncio.TimeoutError:
            return f"(nested fanout timed out after {self._fanout_timeout}s)"
        goal = str(args.get("goal") or "")
        synthesis = str(args.get("synthesis") or "concat")
        return await self._synthesise(results, goal=goal, mode=synthesis)

    async def _run_one(
        self, index: int, subtask: str,
        *, role: str = "general", max_hops: int | None = None,
        specialist: str = "", depth: int = 1,
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
            specialist=specialist,
        )
        try:
            res = await asyncio.wait_for(
                self._do_run_one(
                    index, subtask, t0,
                    role=role, max_hops=max_hops or self._max_hops,
                    specialist=specialist, depth=depth,
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

    async def _await_fanout_review(
        self, *, goal: str, synthesis: str,
        subtasks: list[str], roles: list[str], specialists: list[str],
    ) -> dict | None:
        """派发前编辑拆解：发 FANOUT_REVIEW_REQUESTED + 阻塞在 Future 上，
        等 WS handler 收到 fanout_review_decision 帧后用编辑过的方案
        resolve。复用 builtin_user._PENDING_QUESTIONS 的跨边界套路。

        返回 ``{cancelled: True}`` 表示用户取消；否则返回校验补齐后的
        subtasks/roles/specialists/synthesis。无 UI 通道时不阻塞，原样派发。"""
        import uuid

        if self._bus is None or not self._parent_session_id:
            return {
                "subtasks": subtasks, "roles": roles,
                "specialists": specialists, "synthesis": synthesis,
            }

        review_id = uuid.uuid4().hex
        plan = [
            {"index": i, "role": roles[i], "subtask": subtasks[i],
             "specialist": specialists[i]}
            for i in range(len(subtasks))
        ]
        loop = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()
        _PENDING_FANOUT_REVIEWS[review_id] = future
        # 刷新恢复快照（对位 ask_user 的 _PENDING_QUESTION_PAYLOADS）。
        _PENDING_FANOUT_PAYLOADS[review_id] = {
            "review_id": review_id,
            "session_id": self._parent_session_id,
            "goal": goal,
            "synthesis": synthesis,
            "plan": plan,
        }
        await self._publish_subagent_event(
            "fanout_review_requested",
            review_id=review_id, goal=goal, synthesis=synthesis,
            total=len(plan), plan=plan,
        )
        try:
            decision = await future
        except asyncio.CancelledError:
            return {"cancelled": True}
        finally:
            _PENDING_FANOUT_REVIEWS.pop(review_id, None)
            _PENDING_FANOUT_PAYLOADS.pop(review_id, None)
        return self._normalise_review_decision(
            decision, fallback_synthesis=synthesis,
        )

    @staticmethod
    def _normalise_review_decision(
        decision: Any, *, fallback_synthesis: str,
    ) -> dict | None:
        """校验/补齐前端回传的编辑方案。非法(空 / 不在 2-8 范围) → None，
        当取消处理，绝不让坏方案派发。"""
        if not isinstance(decision, dict):
            return None
        if decision.get("cancelled"):
            return {"cancelled": True}
        raw_plan = decision.get("plan")
        if not isinstance(raw_plan, list):
            return None
        subtasks: list[str] = []
        roles: list[str] = []
        specialists: list[str] = []
        for item in raw_plan:
            if not isinstance(item, dict):
                continue
            st = str(item.get("subtask") or "").strip()
            if not st:
                continue
            subtasks.append(st)
            r = str(item.get("role") or "general").lower()
            roles.append(r if r in _VALID_ROLES else "general")
            specialists.append(str(item.get("specialist") or "").strip().lower())
        if not (2 <= len(subtasks) <= 8):
            return None
        syn = str(decision.get("synthesis") or fallback_synthesis).lower()
        if syn not in ("concat", "llm"):
            syn = fallback_synthesis
        return {
            "subtasks": subtasks, "roles": roles,
            "specialists": specialists, "synthesis": syn,
        }

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
        specialist: str = "", depth: int = 1,
    ) -> _SubResult:
        from xmclaw.providers.llm.base import Message

        # Phase 11: resolve the specialist CHAT model for this subtask.
        # Priority: 1) explicit specialist arg  2) keyword detection
        # 3) fallback to the default LLM wired at construction.
        _capability = specialist
        if not _capability:
            _text_lower = subtask.lower()
            for hint, cap in _SUBTASK_CAPABILITY_HINTS.items():
                if hint in _text_lower:
                    _capability = cap
                    break

        # 2026-06-15: only swap the sub-agent's LLM for a CHAT-usable
        # capability (e.g. vision). Generation capabilities (image_gen /
        # video_gen / audio_out) are NOT chat models — a sub-agent that
        # needs to generate keeps its normal chat model and CALLS the
        # generate_image / generate_video tool (which delegates to the
        # configured backend). Swapping to an image-only endpoint here
        # would break the sub-agent's tool-use loop.
        _sub_llm = self._llm
        if (
            _capability
            and _capability not in _NON_CHAT_CAPABILITIES
            and self._llm_registry is not None
        ):
            try:
                _prof = self._llm_registry.pick_by_capability(
                    _capability,
                    prefer_tier=("vision", "strong", "balanced", "fast"),
                )
                if _prof is not None and _prof.llm is not None:
                    _sub_llm = _prof.llm
            except Exception:  # noqa: BLE001
                pass

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
            resp = await _sub_llm.complete(messages, tools=tool_specs)
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
                # 2026-06-15 (#7): bounded nested fanout. A sub-agent may
                # itself decompose, up to self._max_depth. Beyond that we
                # block (a tree this deep is almost always a model going in
                # circles, and the depth cap is the runaway guard).
                if name == "parallel_subagents":
                    if depth + 1 > self._max_depth:
                        messages.append(Message(
                            role="tool",
                            content=(
                                f"(nested parallel_subagents blocked — max "
                                f"depth {self._max_depth} reached; do this "
                                f"subtask directly instead of decomposing "
                                f"further)"
                            ),
                            tool_call_id=call_id,
                        ))
                        continue
                    nested = await self._run_nested_fanout(args, depth=depth + 1)
                    messages.append(Message(
                        role="tool", content=nested, tool_call_id=call_id,
                    ))
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
            error=f"subagent exhausted {self._max_hops} hops — "
                   f"raise max_hops if the task legitimately needs more",
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
