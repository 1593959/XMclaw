"""Hook lifecycle event taxonomy.

Borrows the Claude Code event vocabulary (25 lifecycle moments)
adapted to XMclaw's loop shape. Events fall into three buckets:

* **Per-session** — fire once when a session is created / destroyed
  (``session_start`` / ``session_end``).
* **Per-turn** — fire on user input / LLM round-trip boundaries
  (``user_prompt_submit``, ``pre_llm``, ``post_llm``, ``stop``).
* **Per-tool** — fire around each individual tool invocation
  (``pre_tool_use``, ``post_tool_use``, ``tool_blocked``).

Plus sub-agent + notification points for parity with the multi-agent
manager and channel adapters.

Why an enum (not str literals): typo on a hook-event name in
config.json would silently never fire pre-fix. Enum validation at
load time turns the typo into a config error.
"""
from __future__ import annotations

from enum import Enum


class HookEvent(str, Enum):
    """Stable identifier for a lifecycle moment.

    Values are the canonical config-file string for the event
    (e.g. ``"UserPromptSubmit"``). Matches Claude Code naming so
    operator muscle memory carries over.
    """

    # ── Session lifecycle ──────────────────────────────────────
    SESSION_START = "SessionStart"
    SESSION_END = "SessionEnd"
    SESSION_RESUME = "SessionResume"  # reconnect after disconnect

    # ── Turn boundaries ────────────────────────────────────────
    USER_PROMPT_SUBMIT = "UserPromptSubmit"  # before AgentLoop.run_turn
    PRE_LLM = "PreLLM"                       # before each LLM call (every hop)
    POST_LLM = "PostLLM"                     # after each LLM call
    STOP = "Stop"                            # turn about to end (last hop)
    TURN_FINISHED = "TurnFinished"           # turn cleanly closed

    # ── Tool boundaries ────────────────────────────────────────
    PRE_TOOL_USE = "PreToolUse"
    POST_TOOL_USE = "PostToolUse"
    TOOL_BLOCKED = "ToolBlocked"  # ToolGuard / hook denied
    TOOL_FAILED = "ToolFailed"    # tool errored at runtime

    # ── Sub-agent + multi-agent ────────────────────────────────
    SUBAGENT_START = "SubagentStart"
    SUBAGENT_STOP = "SubagentStop"

    # ── Memory / persona ───────────────────────────────────────
    MEMORY_WRITE = "MemoryWrite"        # remember() / update_persona
    PERSONA_UPDATED = "PersonaUpdated"  # bump_prompt_freeze_generation

    # ── Channel / IM ───────────────────────────────────────────
    CHANNEL_INBOUND = "ChannelInbound"      # IM message arrived
    CHANNEL_OUTBOUND = "ChannelOutbound"    # adapter about to send

    # ── Cognitive daemon ───────────────────────────────────────
    PERCEPT_OBSERVED = "PerceptObserved"
    REFLECTION_RAN = "ReflectionRan"
    GOAL_SPAWNED = "GoalSpawned"

    # ── Notifications / observability ──────────────────────────
    NOTIFICATION = "Notification"  # outgoing user-visible note
    ERROR_RAISED = "ErrorRaised"   # any errors event downstream

    # ── Skill lifecycle ────────────────────────────────────────
    SKILL_INVOKED = "SkillInvoked"
    SKILL_PROMOTED = "SkillPromoted"


# Set used for fast membership checks when validating config.
ALL_EVENTS: frozenset[str] = frozenset(e.value for e in HookEvent)


def parse_event(name: str) -> HookEvent | None:
    """Case-insensitive lookup, returns None on unknown.

    Accepts both ``"UserPromptSubmit"`` (canonical) and
    ``"user_prompt_submit"`` (snake_case) since operators vary.
    """
    if not name:
        return None
    canonical = name.strip()
    if canonical in ALL_EVENTS:
        return HookEvent(canonical)
    # snake_case → PascalCase fallback.
    parts = canonical.replace("-", "_").split("_")
    pascal = "".join(p[:1].upper() + p[1:].lower() for p in parts if p)
    if pascal in ALL_EVENTS:
        return HookEvent(pascal)
    return None


__all__ = ["HookEvent", "ALL_EVENTS", "parse_event"]
