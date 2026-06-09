"""Narration enforcement — observer over the hop loop.

Background
==========

Audit batch 1 (2026-05-25): the user complained that long tool
chains produced a wall of tool-call cards with no plain-text
context — "我不知道他在干啥". Soft prompt guidance asked the LLM
to emit short plain-text updates between tool calls, but models
drift silent for N hops anyway.

This module wraps the silent-hop tracking that hop_loop previously
inlined. Observer pattern: hop_loop calls :meth:`observe_hop` once
per LLM response and the enforcer mutates its own state, optionally
returning a system-nudge message to prepend to the next hop's
messages or a synthetic INNER_MONOLOGUE event for the bus.

Why extract this
----------------

Audit G1: lives 100 lines into ``_run_hop_loop`` next to 5 other
unrelated counters (stuck-loop deque, no-progress, B-227 retries,
B-230 max-tokens-continue, B-397 anti-loop). Pulling it out lets
us:

* Unit-test the soft/hard threshold transitions without spinning
  up an LLM.
* Tune the thresholds (or add per-session overrides) without
  touching the 1500-line hop_loop.
* Add similar observers later (audit-batch 3 added the v2_renderer
  toxic-fact filter — same shape, observation + side-effect on
  caller's state).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class NarrationDecision:
    """Returned by :meth:`NarrationEnforcer.observe_hop`.

    ``nudge_message`` is None when no nudge is needed this hop;
    otherwise the caller appends it to the messages list before
    the next LLM call. ``progress_marker_event`` is None unless
    we've crossed the "publish a synthetic INNER_MONOLOGUE so the
    user sees SOMETHING" threshold.

    ``force_text_response`` (strict mode only) tells the caller to
    discard any tool_calls from this hop and re-prompt the LLM with
    a stronger instruction to emit plain text BEFORE making tools.
    """

    nudge_message: str | None = None
    progress_marker: dict[str, Any] | None = None
    force_text_response: bool = False


class NarrationEnforcer:
    """Counts consecutive silent hops and decides when to nudge.

    A "silent" hop = the LLM emitted tool calls but no visible
    plain-text content (think-tool counts as hidden — it's already
    routed through INNER_MONOLOGUE). Two consecutive silent hops →
    inject a nudge prompt on the next hop. Three → also publish a
    progress marker so the user isn't staring at silence.

    **Strict mode** (``strict=True``): when the hard threshold is
    reached, the enforcer returns ``force_text_response=True`` so
    the caller can strip tool calls and force the LLM to produce
    plain text before proceeding. This prevents the model from
    ignoring soft nudges indefinitely.
    """

    SOFT_NUDGE_AFTER: int = 2
    HARD_BUBBLE_AFTER: int = 3

    def __init__(self, *, strict: bool = False) -> None:
        self._silent_hops = 0
        self._strict = strict

    @property
    def silent_hops(self) -> int:
        return self._silent_hops

    def observe_hop(
        self,
        *,
        response_content: str | None,
        has_tool_calls: bool,
        hop: int,
        tool_names: list[str] | None = None,
    ) -> NarrationDecision:
        """Update the silent-hop counter and decide on the response.

        ``response_content`` is the LLM's plain-text output for this
        hop (think-tool text is NOT included here — that's routed
        through INNER_MONOLOGUE separately and doesn't count as a
        user-visible update).
        """
        visible = (response_content or "").strip()
        if has_tool_calls and not visible:
            self._silent_hops += 1
        else:
            self._silent_hops = 0
            return NarrationDecision()

        decision = NarrationDecision()
        if self._silent_hops >= self.SOFT_NUDGE_AFTER:
            decision.nudge_message = (
                "已连续 "
                f"{self._silent_hops} 个步骤没有给用户的进度更新。"
                "下一步先用一句 plain text 告诉用户你刚做了什么、"
                "接下来要做什么，再继续工具调用。"
            )
        if self._silent_hops >= self.HARD_BUBBLE_AFTER:
            tool_names_str = ", ".join((tool_names or [])[:5])[:120]
            decision.progress_marker = {
                "content": (
                    f"已连续 {self._silent_hops} 个工具调用 "
                    f"（{tool_names_str}）无文字汇报，已注入"
                    f"叙述提示。"
                ),
                "kind": "narration_enforcement",
                "hop": hop,
            }
            # Strict mode: force the LLM to produce text before tools.
            if self._strict:
                decision.force_text_response = True
                decision.nudge_message = (
                    "已连续 "
                    f"{self._silent_hops} 个步骤没有文字汇报。"
                    "**本回合禁止调用工具** — 你必须先用 1-2 句 plain "
                    "text 告诉用户当前进展，然后本轮结束。"
                )
        return decision


__all__ = ["NarrationEnforcer", "NarrationDecision"]
