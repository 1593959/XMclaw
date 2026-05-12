"""GoalAnchor — runtime trick to make weak models do 100+ hop tool chains.

Background
==========

Kimi K2.6 can string together 200-300 tool calls without drifting from
the original goal — that's partly RL-trained into the weights, but
**a big chunk of it is just runtime state management**: re-inject the
original goal + progress summary every N hops so the model's context
keeps being reminded of "what we were doing".

This module externalises that scaffolding so XMclaw + any model
(Qwen 7B, Llama 8B, Mistral, etc.) can match the long-horizon coherence
of an agentic-trained model without retraining anything.

What it does
============

Every ``anchor_every`` hops (default 5), inject a synthesized
"[GOAL-ANCHOR]" message into the in-flight ``messages`` list summarising:

  * The original user goal (the first ``user`` message of the turn).
  * Plan steps if a planner ran (from Batch B PlanFirstMode wiring).
  * Tools called so far + 1-line success/fail summary each.
  * Current hop / remaining hop budget.
  * Open questions / unresolved errors.

The model sees this RIGHT BEFORE its next LLM call — even with only
4-8 K real attention, the most recent message is the easiest to "look
at" — so it doesn't have to crawl back through 50 tool-result messages
to remember what it was trying to do.

Not persisted to history
========================

These messages carry a literal ``[GOAL-ANCHOR]`` prefix and are
filtered out by ``_sanitize_memory_context`` before history hits disk.
The on-disk chat record stays as the user / assistant actually
exchanged — only the in-flight context window has the anchor.

Anti-overclaim: this is a **scaffold**, not intelligence. The model
still has to reason about what to do next. We're just making it
easier for the model to remember WHY it's doing it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


# Marker prefix the sanitiser strips before persistence. Match exactly
# at content[0:14] — keep it stable; turn_context.py reads the same
# token from _GOAL_ANCHOR_BLOCK_RE.
GOAL_ANCHOR_MARKER = "[GOAL-ANCHOR]"


@dataclass
class GoalAnchorState:
    """Snapshot of where the turn is. Built fresh each anchor injection
    so we never mutate caller-owned data."""

    original_goal: str
    hop: int
    max_hops: int
    tool_calls_made: list[dict[str, Any]]
    plan_steps: list[str] | None = None  # populated by PlanFirstMode (Batch B)
    completed_step_indices: set[int] | None = None
    open_errors: list[str] | None = None  # last N tool errors

    @property
    def hops_remaining(self) -> int:
        return max(0, self.max_hops - self.hop)


class GoalAnchorTracker:
    """Decides when to inject goal anchors + how to format them.

    Stateless — keeps no per-session state itself; the caller (hop_loop)
    holds the canonical state (``tool_calls_made``, ``hop``, etc.).
    This makes the tracker safe to share across sessions and trivially
    testable.

    Parameters
    ----------
    anchor_every : int
        Inject an anchor every N hops. Default 5 — empirically the sweet
        spot for 32 K context models (more often = wasted tokens; less
        often = drift sets in). Increase for long-context models, decrease
        for short-context ones.
    tail_calls_summary : int
        How many recent tool calls to summarise in detail. The rest get
        compressed into a one-line "+ K earlier calls" footer.
    max_error_chars : int
        Truncate each surfaced error to keep the anchor budget bounded.
    """

    def __init__(
        self,
        *,
        anchor_every: int = 5,
        tail_calls_summary: int = 6,
        max_error_chars: int = 160,
    ) -> None:
        self._anchor_every = max(1, int(anchor_every))
        self._tail = max(1, int(tail_calls_summary))
        self._max_err = max(40, int(max_error_chars))

    # ── Decision ───────────────────────────────────────────────────

    def should_anchor(self, hop: int) -> bool:
        """True if hop ``hop`` should trigger an anchor injection.

        Anchors at hop 0 are skipped — the user message + system prompt
        are already fresh, no need to re-paste. After that, every Nth
        hop gets one.
        """
        return hop > 0 and (hop % self._anchor_every) == 0

    # ── Formatting ─────────────────────────────────────────────────

    def format(self, state: GoalAnchorState) -> str:
        """Render the goal-anchor body for one hop.

        Output is a self-contained block prefixed with ``GOAL_ANCHOR_MARKER``
        so the persistence sanitiser can strip it later.
        """
        lines: list[str] = [
            GOAL_ANCHOR_MARKER + " refreshed every "
            f"{self._anchor_every} hops — the agent re-anchors to the "
            "user's original goal + progress so far. Read this BEFORE "
            "deciding the next tool call.\n",
            "## 原始目标 (Original Goal)",
            self._truncate(state.original_goal.strip(), 800),
            "",
        ]

        if state.plan_steps:
            lines.append("## 计划步骤 (Decomposed plan)")
            done = state.completed_step_indices or set()
            for i, step in enumerate(state.plan_steps):
                mark = "[x]" if i in done else "[ ]"
                lines.append(f"  {mark} {i + 1}. {self._truncate(step, 240)}")
            lines.append("")

        tools = state.tool_calls_made or []
        if tools:
            lines.append(f"## 已执行 {len(tools)} 个工具调用")
            head_compressed = max(0, len(tools) - self._tail)
            if head_compressed:
                lines.append(
                    f"  (earlier {head_compressed} calls compressed — "
                    "see hop-level events for detail)"
                )
            for tc in tools[-self._tail:]:
                name = str(tc.get("name", "?"))
                ok = bool(tc.get("ok", True))
                badge = "✓" if ok else "✗"
                err = tc.get("error") or ""
                content_summary = ""
                if ok:
                    c = tc.get("content_preview") or ""
                    if c:
                        content_summary = f" → {self._truncate(str(c), 80)}"
                else:
                    if err:
                        content_summary = f" — error: {self._truncate(err, self._max_err)}"
                lines.append(f"  {badge} {name}{content_summary}")
            lines.append("")

        if state.open_errors:
            lines.append("## 待处理的错误 (open errors)")
            for e in state.open_errors[-5:]:
                lines.append(f"  - {self._truncate(e, self._max_err)}")
            lines.append("")

        lines.append(
            f"## 预算 — hop {state.hop} / {state.max_hops} "
            f"(剩余 {state.hops_remaining})"
        )
        lines.append(
            "如果接近预算上限, 先合成已有结果给用户; "
            "如果还远, 继续推进 ``## 计划步骤`` 里下一个未完成项。"
        )
        return "\n".join(lines)

    # ── Helpers ────────────────────────────────────────────────────

    @staticmethod
    def _truncate(s: str, max_chars: int) -> str:
        if not s:
            return ""
        s = s.replace("\r", "")
        if len(s) <= max_chars:
            return s
        return s[: max_chars - 1] + "…"


def is_anchor_message(content: Any) -> bool:
    """Cheap check used by sanitisers to identify anchor messages."""
    if isinstance(content, str):
        return content.lstrip().startswith(GOAL_ANCHOR_MARKER)
    if isinstance(content, list):
        # Provider-shape: list of {type: text/tool_use/...} blocks.
        for block in content:
            txt = (
                getattr(block, "text", None)
                or (block.get("text") if isinstance(block, dict) else None)
            )
            if isinstance(txt, str) and txt.lstrip().startswith(GOAL_ANCHOR_MARKER):
                return True
    return False


__all__ = [
    "GoalAnchorTracker",
    "GoalAnchorState",
    "GOAL_ANCHOR_MARKER",
    "is_anchor_message",
]
