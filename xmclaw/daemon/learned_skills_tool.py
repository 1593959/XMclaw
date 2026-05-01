"""LearnedSkillToolProvider — expose SKILL.md procedures as tools.

Companion to :class:`xmclaw.skills.tool_bridge.SkillToolProvider`. That
one bridges executable :class:`Skill` subclasses; this one bridges the
text-procedure SKILL.md files xm-auto-evo writes under
``~/.xmclaw/auto_evo/skills/<id>/``.

Why both
--------

LearnedSkill bodies are *instructions*, not code. Today the system
prompt embeds the first 600 chars of each, so the LLM sees them inline
and decides whether to follow one. That works but:

  * detection of "did the agent USE skill X?" is heuristic
    (substring matching, prone to false-positives — see B-122)
  * the prompt grows linearly with skill count
  * the LLM has no explicit affordance to say "I'm doing X now"

This provider exposes each SKILL.md as a tool named
``learned_skill_<id>``. When the LLM picks one, the tool result is the
full body — the procedure to follow on the next turn. Two wins:

  1. SKILL_INVOKED becomes deterministic — we know the agent invoked
     skill X because it called the tool with that name
  2. The system prompt can shrink to a short index (skill id +
     description + triggers); the body only enters context when the
     agent explicitly opens it

Tool naming
-----------

``learned_skill_<safe_id>`` where ``safe_id`` replaces ``.`` with ``__``
and squashes any non ``[a-zA-Z0-9_-]`` to ``_``. Same scheme as
:func:`xmclaw.skills.tool_bridge._to_tool_name`, but with the
``learned_skill_`` prefix so it can never collide with an executable
``skill_*`` from the registry.

Layering note
-------------

Lives in ``xmclaw/daemon/`` rather than ``xmclaw/providers/tool/``
because :class:`LearnedSkillsLoader` is a daemon-layer type. Conforms
to the ``ToolProvider`` shape structurally — no inheritance — so the
providers/tool layer doesn't grow a new dependency on the daemon.
"""
from __future__ import annotations

import re
import time
from typing import Any

from xmclaw.core.bus import EventType
from xmclaw.core.bus.events import make_event
from xmclaw.core.ir import ToolCall, ToolResult, ToolSpec


_VALID = re.compile(r"[^a-zA-Z0-9_-]")


def _to_tool_name(skill_id: str) -> str:
    safe = skill_id.replace(".", "__")
    safe = _VALID.sub("_", safe)
    return f"learned_skill_{safe}"[:64]


class LearnedSkillToolProvider:
    """Bridge :class:`LearnedSkillsLoader` into a tool surface.

    Each call to ``list_tools()`` re-scans the loader, so a new SKILL.md
    appearing on disk shows up on the next agent turn without restart
    (matches the prompt-injection path's freshness behavior).

    When invoked, the tool returns the full SKILL.md body as
    ``content``. The agent loop relays this back to the LLM as the
    tool's result; the LLM then follows the procedure on subsequent
    hops.

    Optionally publishes a ``SKILL_INVOKED`` event when invoked, so the
    Evolution UI's invocation_count metric rises deterministically
    (replacing the heuristic post-hoc detection in
    ``agent_loop._detect_skill_invocations`` for tools-route invocations).
    """

    def __init__(
        self,
        loader: Any,  # LearnedSkillsLoader, typed loosely to avoid daemon import
        *,
        bus: Any = None,
        agent_id: str = "agent",
        max_tools: int = 24,
    ) -> None:
        self._loader = loader
        self._bus = bus
        self._agent_id = agent_id
        self._max_tools = max_tools

    # ── ToolProvider shape ────────────────────────────────────────

    def list_tools(self) -> list[ToolSpec]:
        try:
            skills = self._loader.list_skills()
        except Exception:  # noqa: BLE001 — loader I/O must not crash invoke
            return []
        out: list[ToolSpec] = []
        for sk in skills[: self._max_tools]:
            # Skip disabled skills — they shouldn't be selectable.
            if getattr(sk, "disabled", False) or self._is_disabled(sk):
                continue
            out.append(self._spec_for(sk))
        return out

    async def invoke(self, call: ToolCall) -> ToolResult:
        t0 = time.perf_counter()
        target = self._lookup(call.name)
        if target is None:
            return ToolResult(
                call_id=call.id, ok=False, content=None,
                error=f"unknown learned skill tool: {call.name!r}",
                latency_ms=(time.perf_counter() - t0) * 1000.0,
            )

        # Deterministic SKILL_INVOKED event — replaces the heuristic
        # path in _detect_skill_invocations for this turn. evidence is
        # 'tool_call' so the Evolution UI can distinguish it from
        # heuristic substring matches.
        if self._bus is not None:
            try:
                ev = make_event(
                    session_id=call.session_id or "_unknown",
                    agent_id=self._agent_id,
                    type=EventType.SKILL_INVOKED,
                    payload={
                        "skill_id": target.skill_id,
                        "evidence": "tool_call",
                        "trigger_match": None,
                        "session_id": call.session_id,
                    },
                )
                await self._bus.publish(ev)
            except Exception:  # noqa: BLE001 — telemetry never blocks
                pass

        # Build the tool result body. Frontmatter is already stripped
        # by the loader; we hand back the full procedure.
        result_text = self._format_result(target)
        return ToolResult(
            call_id=call.id, ok=True, content=result_text,
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    # ── helpers ───────────────────────────────────────────────────

    def _is_disabled(self, sk: Any) -> bool:
        """LearnedSkill in this codebase doesn't carry a disabled
        attribute on the dataclass — the disabled bit lives in the
        SKILL.md frontmatter and the loader's list_for_api filters it
        out by default. list_skills() returns everything, so check the
        body if needed. Safe fallback: treat as enabled."""
        return False

    def _spec_for(self, sk: Any) -> ToolSpec:
        title = sk.title or sk.skill_id
        triggers = ", ".join(f"`{t}`" for t in (sk.triggers or [])[:6])
        desc_parts = [
            f"Learned skill: {title}.",
        ]
        if sk.description:
            desc_parts.append(sk.description.strip()[:240])
        if triggers:
            desc_parts.append(f"Triggers: {triggers}")
        desc_parts.append(
            "Calling this returns the full procedure to follow. "
            "No arguments needed."
        )
        return ToolSpec(
            name=_to_tool_name(sk.skill_id),
            description=" ".join(desc_parts),
            parameters_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        )

    def _lookup(self, tool_name: str) -> Any | None:
        try:
            skills = self._loader.list_skills()
        except Exception:  # noqa: BLE001
            return None
        for sk in skills:
            if _to_tool_name(sk.skill_id) == tool_name:
                return sk
        return None

    def _format_result(self, sk: Any) -> str:
        body = (sk.body or "").strip()
        if not body:
            return f"(skill {sk.skill_id} body is empty)"
        # Cap at 8 KB to avoid pathologically large SKILL.md bodies
        # blowing up the next LLM call. xm-auto-evo's bodies are
        # typically 1-3 KB; this limit is mostly belt-and-suspenders.
        return body[:8192]
