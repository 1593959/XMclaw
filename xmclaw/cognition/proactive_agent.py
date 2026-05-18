"""ProactiveAgent — the difference between a chatbot and a JARVIS.

Sprint 1 of the Jarvis roadmap. A chatbot waits for the user to type.
A real assistant pipes up: "你 3 分钟后有个会议要不要准备?", "刚才 GitHub 来
review 请求", "你电脑温度 95°C 该清理了", "上次你说要写 X，还没写".

This module owns the proactive loop:

  1. Hold a registry of :class:`ProactiveTrigger` instances.
  2. On a fixed tick (default every 30 s) call each trigger's
     ``should_fire()``.
  3. When a trigger fires, publish a :class:`ProposalEvent` onto the
     bus. The chat WS bridges this to the UI as a
     ``proactive_proposal`` event so the user sees a bubble even
     without typing first.
  4. Per-trigger cooldown + global rate limit so the agent doesn't
     spam.

Triggers are pluggable: subclass :class:`ProactiveTrigger`, register
via ``ProactiveAgent.register_trigger(...)``. Daemon lifespan wiring
loads a default trigger set; users can disable individual ones via
config ``cognition.proactive.disabled_triggers``.

Design choices
==============

* **Pure-Python triggers** (no LLM call per tick). LLM only fires
  inside the agent_loop AFTER the trigger fires and the agent
  decides to compose a real response. Triggers are cheap heuristics.
* **No blocking** — every trigger runs inside its own try/except
  and gets a per-tick budget (50 ms). A misbehaving trigger can't
  stall the loop.
* **Quiet hours / DND** — global silencer for "between 23:00 and
  07:00 don't interrupt unless urgency='high'". Configurable.

Composition with the rest of the system
=======================================

* Reads from :class:`PerceptionBus` (window/clipboard/screen events).
* Reads from memory (autobiographical) for "you said you'd do X".
* Reads from cron / calendar adapters for time-based triggers.
* Writes via :func:`publish` so events ride the same WS as regular
  agent output — UI doesn't need a new transport.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

from xmclaw.utils.log import get_logger

logger = get_logger(__name__)


# ── Trigger Protocol + base class ──────────────────────────────────


class _BusLike(Protocol):
    async def publish(self, ev: Any) -> None: ...


@dataclass(frozen=True, slots=True)
class TriggerProposal:
    """One fire of a trigger — what message to surface and how urgent.

    ``message`` is what the user sees as the agent-initiated bubble.
    ``urgency`` controls whether DND / quiet hours suppress it.
    ``follow_through`` is an optional callable invoked after the user
    sees the proposal — used to wire e.g. "after announcing the meeting,
    fetch the agenda" without the trigger having to know about the
    full agent loop.
    """

    trigger_name: str
    message: str
    urgency: str = "normal"   # "low" | "normal" | "high"
    payload: dict[str, Any] = field(default_factory=dict)
    follow_through: Callable[[], Awaitable[None]] | None = None


class ProactiveTrigger:
    """ABC. Subclass + implement ``should_fire`` + ``propose``.

    Subclasses MUST define ``name`` (used for cooldown bookkeeping and
    user-visible disabled-list). ``cooldown_s`` is the minimum gap
    between successive proposals from the same trigger; defaults to
    1 hour to prevent spam.
    """

    name: str = "unnamed"
    cooldown_s: float = 3600.0

    async def should_fire(self, ctx: ProactiveContext) -> bool:
        """Return True if this trigger has something to surface NOW.

        ``ctx`` carries: time of day, last user activity timestamp,
        perception snapshots, memory access, recent agent output.
        Avoid blocking I/O — the tick budget is 50ms per trigger.
        """
        return False

    async def propose(self, ctx: ProactiveContext) -> TriggerProposal | None:
        """Produce the actual message. Called when ``should_fire`` is
        True. Can return None to back out (rare — should_fire should
        already have decided)."""
        return None


# ── Shared context passed to every trigger ────────────────────────


@dataclass(slots=True)
class ProactiveContext:
    """Snapshot passed into every trigger's ``should_fire`` /
    ``propose`` call. Cheap to construct — composed by the agent's
    tick loop each iteration."""

    now: float                              # time.time() at tick start
    last_user_message_ts: float | None      # epoch s, None if never
    last_agent_message_ts: float | None
    quiet_hours_active: bool                # global DND flag
    # Pluggable hooks for triggers to peek at the daemon's state.
    # All optional — a trigger that needs e.g. memory access checks
    # for None first and skips when missing.
    memory: Any | None = None
    perception_bus: Any | None = None
    cron_store: Any | None = None
    agent_loop: Any | None = None


# ── ProactiveAgent ────────────────────────────────────────────────


class ProactiveAgent:
    """Periodic evaluator that fires registered triggers.

    Usage:

        agent = ProactiveAgent(
            publish=bus.publish, agent_id="main",
            tick_interval_s=30.0,
        )
        agent.register_trigger(IdleCheckInTrigger())
        agent.register_trigger(CalendarReminderTrigger(cal=...))
        await agent.start()    # background asyncio.Task
        ...
        await agent.stop()
    """

    def __init__(
        self,
        *,
        publish: Callable[[str, dict[str, Any]], Awaitable[Any]],
        agent_id: str = "main",
        tick_interval_s: float = 30.0,
        global_min_gap_s: float = 60.0,
        quiet_start_hour: int = 23,
        quiet_end_hour: int = 7,
        memory: Any | None = None,
        perception_bus: Any | None = None,
        cron_store: Any | None = None,
        agent_loop: Any | None = None,
    ) -> None:
        self._publish = publish
        self._agent_id = agent_id
        self._tick_interval_s = max(1.0, float(tick_interval_s))
        self._global_min_gap_s = max(0.0, float(global_min_gap_s))
        self._quiet_start_hour = int(quiet_start_hour)
        self._quiet_end_hour = int(quiet_end_hour)
        self._memory = memory
        self._perception_bus = perception_bus
        self._cron_store = cron_store
        self._agent_loop = agent_loop
        self._triggers: list[ProactiveTrigger] = []
        # Per-trigger cooldown end-time (epoch s).
        self._cooldown_until: dict[str, float] = {}
        # Global "no proposals fired more recently than this".
        self._last_proposal_ts: float = 0.0
        self._last_user_message_ts: float | None = None
        self._last_agent_message_ts: float | None = None
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    # ── public lifecycle ──────────────────────────────────────────

    def register_trigger(self, trigger: ProactiveTrigger) -> None:
        """Add a trigger. Idempotent — re-registering the same name
        replaces the old instance."""
        self._triggers = [
            t for t in self._triggers if t.name != trigger.name
        ]
        self._triggers.append(trigger)

    def unregister_trigger(self, name: str) -> bool:
        before = len(self._triggers)
        self._triggers = [t for t in self._triggers if t.name != name]
        return len(self._triggers) < before

    def trigger_names(self) -> list[str]:
        return [t.name for t in self._triggers]

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_loop())
        logger.info(
            "proactive_agent.started tick=%.1fs triggers=%d",
            self._tick_interval_s, len(self._triggers),
        )

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()
        self._task = None
        logger.info("proactive_agent.stopped")

    # ── hooks for the rest of the daemon ──────────────────────────

    def note_user_message(self, ts: float | None = None) -> None:
        """AgentLoop calls this when a user message is observed so
        the IdleCheckIn / other-time-based triggers know when the user
        last interacted."""
        self._last_user_message_ts = ts or time.time()

    def note_agent_message(self, ts: float | None = None) -> None:
        self._last_agent_message_ts = ts or time.time()

    # ── tick loop internals ───────────────────────────────────────

    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._tick_once()
            except Exception as exc:  # noqa: BLE001 — never crash the loop
                logger.warning("proactive_agent.tick_failed err=%s", exc)
            # Sleep with cancel-awareness.
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._tick_interval_s,
                )
                return  # stop_event set → exit cleanly
            except asyncio.TimeoutError:
                continue

    async def _tick_once(self) -> int:
        """Evaluate every trigger. Returns count of proposals fired."""
        ctx = ProactiveContext(
            now=time.time(),
            last_user_message_ts=self._last_user_message_ts,
            last_agent_message_ts=self._last_agent_message_ts,
            quiet_hours_active=self._is_quiet_hours_active(),
            memory=self._memory,
            perception_bus=self._perception_bus,
            cron_store=self._cron_store,
            agent_loop=self._agent_loop,
        )
        fired = 0
        # Global rate limit — at most one proposal per global_min_gap_s.
        if ctx.now - self._last_proposal_ts < self._global_min_gap_s:
            return 0
        for trigger in list(self._triggers):
            cooldown_until = self._cooldown_until.get(trigger.name, 0.0)
            if ctx.now < cooldown_until:
                continue
            try:
                should = await asyncio.wait_for(
                    trigger.should_fire(ctx), timeout=0.05,
                )
            except (asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001
                logger.debug(
                    "trigger.should_fire_failed name=%s err=%s",
                    trigger.name, exc,
                )
                continue
            if not should:
                continue
            try:
                proposal = await asyncio.wait_for(
                    trigger.propose(ctx), timeout=2.0,
                )
            except (asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001
                logger.warning(
                    "trigger.propose_failed name=%s err=%s",
                    trigger.name, exc,
                )
                continue
            if proposal is None:
                continue
            # Respect quiet hours unless urgency='high'.
            if (
                ctx.quiet_hours_active
                and proposal.urgency != "high"
            ):
                logger.debug(
                    "proactive.suppressed_quiet_hours trigger=%s",
                    trigger.name,
                )
                # Cooldown anyway so we don't re-evaluate on every tick.
                self._cooldown_until[trigger.name] = (
                    ctx.now + trigger.cooldown_s
                )
                continue
            await self._dispatch(proposal)
            self._cooldown_until[trigger.name] = (
                ctx.now + trigger.cooldown_s
            )
            self._last_proposal_ts = ctx.now
            fired += 1
            # One-per-tick — don't dump 5 proposals at the same moment.
            break
        return fired

    async def _dispatch(self, proposal: TriggerProposal) -> None:
        payload = {
            "trigger": proposal.trigger_name,
            "message": proposal.message,
            "urgency": proposal.urgency,
            "ts": time.time(),
            **proposal.payload,
        }
        try:
            await self._publish("proactive_proposal", payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning("proactive.publish_failed err=%s", exc)
            return
        # Optional follow-through (fire-and-forget).
        if proposal.follow_through is not None:
            try:
                asyncio.create_task(proposal.follow_through())
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "proactive.follow_through_failed err=%s", exc,
                )

    def _is_quiet_hours_active(self) -> bool:
        """Decide if right now is within the DND window."""
        try:
            hour = time.localtime().tm_hour
        except Exception:  # noqa: BLE001
            return False
        s = self._quiet_start_hour
        e = self._quiet_end_hour
        if s == e:
            return False
        if s < e:
            return s <= hour < e
        # Window crosses midnight (e.g. 23 → 7)
        return hour >= s or hour < e


# ── Built-in triggers ──────────────────────────────────────────────


class IdleCheckInTrigger(ProactiveTrigger):
    """When the user has been silent for ``idle_threshold_s`` after
    actively chatting, gently check in.

    Heuristic — fires ONLY if there was a recent user message
    (otherwise we'd ping every fresh daemon start). Cooldown 30 min
    so we don't bother the user every cycle.

    Wave-32+ (2026-05-18): generate a CONTEXTUAL one-liner instead
    of the same hardcoded "你那边还好吗" every time. Two paths:

      1. If an LLM is reachable via ``ctx.agent_loop._llm`` AND the
         agent has recent history, ask the LLM for a one-sentence
         check-in that references what the user was doing. Keep the
         call cheap (no tools, no streaming, ~30 token output) so
         the user doesn't pay much for the politeness.
      2. Otherwise (no LLM wired, LLM call failed, history empty)
         pick from a small rotating pool of fallback templates so
         even the degraded path doesn't feel robotic.
    """

    # Fallback pool — varied enough that even with no LLM the user
    # doesn't see the exact same line each cycle. Includes minute-
    # interpolation slots ({minutes}).
    _FALLBACK_TEMPLATES: tuple[str, ...] = (
        "{minutes} 分钟没动静了，要继续刚才的事吗？",
        "歇好了？随时叫我。",
        "看你停下来一会了，需要我帮忙整理一下刚才的进度吗？",
        "在忙别的？需要的时候我都在。",
        "刚才的话题还要继续吗？还是先放放？",
    )

    def __init__(
        self,
        *,
        idle_threshold_s: float = 30 * 60,
        cooldown_s: float = 30 * 60,
        message: str | None = None,
        use_llm: bool = True,
    ) -> None:
        self.name = "idle_check_in"
        self.cooldown_s = float(cooldown_s)
        self._idle_threshold_s = float(idle_threshold_s)
        # ``message`` overrides everything — for tests + for operators
        # who want a pinned greeting. None = use the contextual path.
        self._pinned_message = message
        self._use_llm = bool(use_llm)
        # Round-robin index for the fallback pool so consecutive
        # check-ins don't repeat the same template.
        self._fallback_idx = 0

    async def should_fire(self, ctx: ProactiveContext) -> bool:
        last = ctx.last_user_message_ts
        if last is None:
            return False
        # Don't fire if we never had a real conversation (zero history).
        idle = ctx.now - last
        return idle >= self._idle_threshold_s

    async def propose(
        self, ctx: ProactiveContext,
    ) -> TriggerProposal | None:
        idle_minutes = round(
            (ctx.now - (ctx.last_user_message_ts or ctx.now)) / 60, 1,
        )
        # 1) Pinned override wins — test paths + operator overrides.
        if self._pinned_message is not None:
            text = self._pinned_message
        else:
            text = await self._compose_message(ctx, idle_minutes)
        return TriggerProposal(
            trigger_name=self.name,
            message=text,
            urgency="low",
            payload={"idle_minutes": idle_minutes},
        )

    async def _compose_message(
        self, ctx: ProactiveContext, idle_minutes: float,
    ) -> str:
        # Try LLM path first when allowed + a loop with an LLM and
        # history is reachable.
        if self._use_llm:
            llm_text = await self._try_llm(ctx, idle_minutes)
            if llm_text:
                return llm_text
        # Fallback — rotate templates so we don't repeat the same
        # line each cycle.
        tpl = self._FALLBACK_TEMPLATES[
            self._fallback_idx % len(self._FALLBACK_TEMPLATES)
        ]
        self._fallback_idx += 1
        return tpl.format(minutes=int(idle_minutes))

    async def _try_llm(
        self, ctx: ProactiveContext, idle_minutes: float,
    ) -> str | None:
        """Best-effort LLM-generated greeting. Returns None on any
        failure — caller falls back to a template. Bounded by a 10s
        wait_for so a slow LLM doesn't stall the proactive tick."""
        loop = ctx.agent_loop
        llm = getattr(loop, "_llm", None) if loop is not None else None
        if llm is None:
            return None
        # Build a tiny context: last user message + last assistant
        # message (truncated). The trigger doesn't know the session
        # id — sweep ``_histories`` for the most recent one. If the
        # agent runs multiple parallel sessions, "most recent" is a
        # reasonable proxy.
        histories = getattr(loop, "_histories", None)
        if not isinstance(histories, dict) or not histories:
            return None
        last_user = ""
        last_assistant = ""
        for msgs in histories.values():
            for m in reversed(msgs or []):
                role = getattr(m, "role", None)
                content = getattr(m, "content", "") or ""
                if role == "user" and not last_user:
                    last_user = content[:300]
                elif role == "assistant" and not last_assistant:
                    last_assistant = content[:300]
                if last_user and last_assistant:
                    break
            if last_user:
                break
        if not last_user:
            return None
        try:
            # Lazy import keeps cognition/ off the providers/ DAG.
            from xmclaw.core.ir import Message
            prompt = (
                "你是一个主动的桌面助手。用户已经 "
                f"{int(idle_minutes)} 分钟没动了。基于下面的最近对话，"
                "生成一句 15 字以内的主动问候，要：\n"
                "  • 引用具体上下文，不要套话\n"
                "  • 不要重复\"还好吗\"\"需要帮忙吗\"\n"
                "  • 自然、像朋友随口一问\n\n"
                f"用户上一句：{last_user}\n"
                f"你上一句：{last_assistant[:200]}\n\n"
                "直接输出问候，不要前缀，不要引号，不要 emoji。"
            )
            resp = await asyncio.wait_for(
                llm.complete(
                    [Message(role="user", content=prompt)],
                    tools=None,
                ),
                timeout=10.0,
            )
            text = (getattr(resp, "content", None) or "").strip()
            # Defensive trimming — strip wrapping quotes the model
            # sometimes adds despite being told not to.
            text = text.strip("\"'「」『』 ")
            if text and len(text) <= 80:
                return text
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "idle_check_in.llm_compose_failed err=%s", exc,
            )
        return None


class SystemHealthTrigger(ProactiveTrigger):
    """When the daemon's host system shows alarming metrics (high
    temp / low memory / low disk), alert.

    Reads ``psutil`` lazily so installs without it skip the trigger.
    Default thresholds picked conservative — adjust per machine.
    """

    def __init__(
        self,
        *,
        cooldown_s: float = 30 * 60,
        cpu_temp_celsius_threshold: float = 85.0,
        disk_gb_free_threshold: float = 5.0,
        memory_pct_threshold: float = 92.0,
    ) -> None:
        self.name = "system_health"
        self.cooldown_s = float(cooldown_s)
        self._cpu_temp_threshold = float(cpu_temp_celsius_threshold)
        self._disk_threshold = float(disk_gb_free_threshold)
        self._memory_threshold = float(memory_pct_threshold)
        self._last_warning: str | None = None

    async def should_fire(self, ctx: ProactiveContext) -> bool:
        try:
            import psutil
        except ImportError:
            return False
        try:
            mem = psutil.virtual_memory()
            if mem.percent >= self._memory_threshold:
                self._last_warning = (
                    f"内存使用 {mem.percent:.0f}% — 该清一些后台进程了"
                )
                return True
            disk = psutil.disk_usage("/")
            free_gb = disk.free / (1024 ** 3)
            if free_gb <= self._disk_threshold:
                self._last_warning = (
                    f"系统盘只剩 {free_gb:.1f} GB — 找点空间清吧"
                )
                return True
            # CPU temperature — not always exposed on Windows
            try:
                temps = psutil.sensors_temperatures()
                for _, entries in temps.items():
                    for e in entries:
                        if (
                            e.current is not None
                            and e.current >= self._cpu_temp_threshold
                        ):
                            self._last_warning = (
                                f"CPU 温度 {e.current:.0f}°C — 散热"
                                f"先停手让它凉一下"
                            )
                            return True
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            return False
        return False

    async def propose(
        self, ctx: ProactiveContext,
    ) -> TriggerProposal | None:
        if not self._last_warning:
            return None
        return TriggerProposal(
            trigger_name=self.name,
            message=self._last_warning,
            urgency="normal",
            payload={"warning": self._last_warning},
        )


__all__ = [
    "ProactiveAgent",
    "ProactiveContext",
    "ProactiveTrigger",
    "TriggerProposal",
    "IdleCheckInTrigger",
    "SystemHealthTrigger",
]
