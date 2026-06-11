"""Memory paging tools — Letta/MemGPT-inspired working-context management.

Adds ``memory(action="pin_to_working", ...)`` and
``memory(action="evict_from_working", ...)`` to the agent's memory
tool surface. This lets the LLM actively manage what facts are in
the "working context" (the cacheable system prompt section that the
agent can read verbatim).

Reference: MemGPT/Letta (arXiv:2310.08560) — LLM as memory manager
           through function-calling for context paging.
"""
from __future__ import annotations

import hashlib
import time
from typing import Any

from xmclaw.utils.log import get_logger

_log = get_logger(__name__)

# Working context facts are stored with a dedicated bucket so the
# v2 renderer can project them into a unique section of the system
# prompt — agent_identity.md / AGENTS.md "## Working Context".
_WORKING_BUCKET = "working_context"
_MAX_WORKING_FACTS = 8  # Keep small to avoid cache prefix bloat


class WorkingContextManager:
    """Manages the Working Context — a small, editable section of the
    system prompt that the agent can explicitly control via memory tools."""

    def __init__(self) -> None:
        self._pinned: list[dict[str, Any]] = []
        self._last_access: dict[str, float] = {}

    async def pin(
        self,
        *,
        fact_id: str,
        text: str,
        reason: str | None = None,
    ) -> str:
        """Add a fact to the working context."""
        now = time.time()
        for item in self._pinned:
            if item["id"] == fact_id:
                item["ts"] = now
                item["reason"] = reason or item.get("reason", "")
                self._last_access[fact_id] = now
                return f"fact {fact_id[:16]} already in working context (updated)"

        if len(self._pinned) >= _MAX_WORKING_FACTS:
            # Evict the least recently used fact.
            oldest = min(self._pinned, key=lambda i: self._last_access.get(i["id"], 0.0))
            self._pinned.remove(oldest)
            _log.info("memory_paging.evicted_lru id=%s reason=capacity", oldest["id"][:16])

        self._pinned.append({
            "id": fact_id, "text": text, "ts": now, "reason": reason or "",
        })
        self._last_access[fact_id] = now
        return f"pinned fact {fact_id[:16]} to working context ({len(self._pinned)}/{_MAX_WORKING_FACTS})"

    async def evict(self, fact_id: str) -> str:
        """Remove a fact from the working context."""
        before = len(self._pinned)
        self._pinned = [i for i in self._pinned if i["id"] != fact_id]
        self._last_access.pop(fact_id, None)
        if len(self._pinned) < before:
            return f"evicted fact {fact_id[:16]} from working context"
        return f"fact {fact_id[:16]} was not in working context"

    def render_for_prompt(self) -> str:
        """Project working context facts into a system prompt section."""
        if not self._pinned:
            return ""
        # Sort by recency.
        sorted_pins = sorted(self._pinned, key=lambda i: i.get("ts", 0.0), reverse=True)
        lines = ["## Working Context (agent-managed)", ""]
        for item in sorted_pins:
            lines.append(f"  - {item['text']}  <!-- fid:{item['id']} -->")
        lines.append("")
        return "\n".join(lines)

    @staticmethod
    def compute_fact_id(fact_id: str) -> str:
        return f"wc_{hashlib.sha1(fact_id.encode()).hexdigest()[:12]}"

    def memory_tool_spec_extension(self) -> dict[str, Any]:
        """Return additional JSON Schema properties for the memory tool
        to support ``pin_to_working`` and ``evict_from_working`` actions."""
        return {
            "pin_to_working": {
                "type": "object",
                "description": (
                    "Pin a fact to the Working Context — a small, editable "
                    "section of the system prompt the agent can read directly "
                    "every turn. Use for facts you need constant access to "
                    "(active project paths, current task state, key user "
                    "preferences for the current conversation).  Max "
                    f"{_MAX_WORKING_FACTS} facts; LRU eviction on overflow."
                ),
                "properties": {
                    "fact_id": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["fact_id"],
            },
            "evict_from_working": {
                "type": "object",
                "description": (
                    "Remove a fact from the Working Context when it is no "
                    "longer needed for the current task."
                ),
                "properties": {
                    "fact_id": {"type": "string"},
                },
                "required": ["fact_id"],
            },
        }
