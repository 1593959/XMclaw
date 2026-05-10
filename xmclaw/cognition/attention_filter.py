"""AttentionFilter — Jarvis Phase 6.1 salience-driven percept gate.

On every tick, drains the PerceptionBus, scores each percept via
``CognitiveState.compute_salience`` (urgency + relevance + novelty -
fatigue), updates the cognitive state's bounded attention focus
(working memory, capped at ~7±2), and returns the **actionable**
subset (final salience >= ``action_threshold``) for downstream
reasoning / planning.

Honest disclosure: the salience score is a heuristic, not ground
truth. ``action_threshold`` is the dial:
- lower (e.g. 0.4) → more reactive, more interrupts, more noise.
- higher (e.g. 0.8) → more conservative, may miss soft signals.
The default 0.6 means "more than half of the available signals must
align before we act on it".

This module is greenfield (Phase 6.1 foundation) — the actual tick
producer (the AgentLoop heartbeat) is wired in a follow-up commit.
See ``docs/JARVIS_PHASE_6_DESIGN.md`` §3.2.
"""
from __future__ import annotations

import logging
import time
from collections import OrderedDict
from typing import Any

from xmclaw.cognition.perception_bus import Percept, PerceptionBus

logger = logging.getLogger(__name__)


# Per-kind / per-source urgency priors. Source first (broad), kind
# second (specific) — kind wins when both match. Values in [0, 1].
_URGENCY_BY_KIND: dict[str, float] = {
    "user_msg": 0.8,           # ws user input — high default
    "user_command": 0.85,
    "process_oom": 0.95,       # process tier — critical
    "process_crash": 0.9,
    "process_signal": 0.7,
    "file_modified": 0.4,
    "file_created": 0.4,
    "file_deleted": 0.45,
    "time_tick": 0.1,          # heartbeat — keep low so cron alone never fires action
    "time_deadline": 0.85,     # but a real deadline does
    "network_pulse": 0.3,
    "internal_goal_completed": 0.5,
    "internal_plan_failed": 0.7,
}

_URGENCY_BY_SOURCE: dict[str, float] = {
    "ws": 0.7,
    "process": 0.8,
    "file": 0.4,
    "time": 0.15,
    "network": 0.3,
    "internal": 0.5,
}


class AttentionFilter:
    """Consume percepts from PerceptionBus on each tick, score them,
    update working memory, and surface actionable ones.

    ``cognitive_state`` is duck-typed: it must expose the async
    ``compute_salience(percept_id, content, *, urgency, novelty)``
    coroutine and the synchronous ``add_focus(focus)`` method. We do
    not import :class:`CognitiveState` directly to keep this module
    decoupled and easy to test with fakes.
    """

    # Cap for the novelty LRU. Substring memory of the most recent N
    # percept-content snapshots. Cheap, dependency-free; embedding-
    # based novelty is a follow-up.
    _NOVELTY_CACHE_SIZE = 128

    def __init__(
        self,
        cognitive_state: Any,
        bus: PerceptionBus,
        action_threshold: float = 0.6,
        top_k_focus: int = 7,
    ) -> None:
        if not 0.0 <= action_threshold <= 1.0:
            raise ValueError("action_threshold must be in [0, 1]")
        if top_k_focus < 1:
            raise ValueError("top_k_focus must be >= 1")

        self._state = cognitive_state
        self._bus = bus
        self._action_threshold = action_threshold
        self._top_k_focus = top_k_focus

        # Honor the host CognitiveState's working-memory cap if it
        # exposes one — otherwise install our own.
        if hasattr(cognitive_state, "attention_capacity"):
            try:
                cognitive_state.attention_capacity = top_k_focus
            except Exception:
                logger.debug(
                    "AttentionFilter: could not set attention_capacity on "
                    "cognitive_state; ignoring",
                )

        # LRU of recent percept content snippets for novelty.
        # value is the timestamp we last saw it.
        self._seen: OrderedDict[str, float] = OrderedDict()

    @property
    def action_threshold(self) -> float:
        return self._action_threshold

    @property
    def top_k_focus(self) -> int:
        return self._top_k_focus

    async def tick(self) -> list[Percept]:
        """Drain bus, score percepts, update working memory, return actionable.

        Steps:
        1. drain bus (atomic).
        2. for each percept compute (urgency, novelty) heuristics.
        3. ask cognitive_state.compute_salience for the final score.
        4. add to attention focus (cognitive_state caps top-K via add_focus).
        5. return percepts whose final score >= action_threshold.
        """
        percepts = await self._bus.drain()
        if not percepts:
            return []

        scored: list[tuple[Percept, float]] = []
        for p in percepts:
            content = self._extract_content(p)
            urgency = self._infer_urgency(p)
            novelty = await self._novelty(p)
            score = await self._state.compute_salience(
                p.id,
                content,
                urgency=urgency,
                novelty=novelty,
            )
            # Clamp defensively; compute_salience already clamps but
            # an unusual fake might not.
            score = max(0.0, min(1.0, float(score)))
            scored.append((p, score))

        # Update working memory. Adding focuses in score-ascending order
        # means top-scoring percepts persist after the host's
        # capacity-eviction logic runs (host evicts lowest first). This
        # works whether the host is the real CognitiveState or a fake
        # that simply appends.
        scored_for_focus = sorted(scored, key=lambda item: item[1])
        for p, score in scored_for_focus:
            content = self._extract_content(p)
            self._add_focus(p, content, score)

        return [p for p, score in scored if score >= self._action_threshold]

    def _infer_urgency(self, p: Percept) -> float:
        """Heuristic urgency in [0, 1].

        Lookup priority: explicit ``payload['urgency']`` > kind > source
        > default 0.5. This lets producers override on a case-by-case
        basis (e.g. a deadline-aware time percept can claim 0.9 even
        though ``time_tick`` defaults to 0.1) without the filter
        having to know every kind in the universe.
        """
        # Explicit override from the producer.
        explicit = p.payload.get("urgency")
        if isinstance(explicit, (int, float)):
            return max(0.0, min(1.0, float(explicit)))

        if p.kind in _URGENCY_BY_KIND:
            return _URGENCY_BY_KIND[p.kind]
        if p.source in _URGENCY_BY_SOURCE:
            return _URGENCY_BY_SOURCE[p.source]
        return 0.5

    async def _novelty(self, p: Percept) -> float:
        """LRU-based novelty score in [0, 1].

        Returns 1.0 for never-seen content. For repeats, decays
        smoothly with recency: the more recently we saw it, the lower
        the novelty (floor 0.1 so a long-stale repeat still has some
        novelty). Substring match on ``payload['content']``; falls back
        to the kind:source pair when no content is present.

        The seen-set is an LRU capped at ``_NOVELTY_CACHE_SIZE`` —
        oldest entries fall out, so a repeat after ~128 unique
        percepts looks novel again.
        """
        key = self._novelty_key(p)
        now = p.timestamp if p.timestamp else time.time()

        if key not in self._seen:
            # New. Record and reply 1.0.
            self._seen[key] = now
            self._evict_seen_if_needed()
            return 1.0

        # Seen. Compute decay vs how recently we saw it.
        last_seen = self._seen[key]
        # Move to most-recently-used end.
        self._seen.move_to_end(key)
        self._seen[key] = now
        delta = max(0.0, now - last_seen)
        # 60s reference window: same percept within 1s ≈ 0.1 novelty;
        # ~60s gap ≈ 0.6; very long gap ≈ 1.0 (but key would have
        # been evicted by then in most cases anyway).
        novelty = min(1.0, 0.1 + delta / 60.0)
        return novelty

    def _evict_seen_if_needed(self) -> None:
        while len(self._seen) > self._NOVELTY_CACHE_SIZE:
            self._seen.popitem(last=False)

    @staticmethod
    def _extract_content(p: Percept) -> str:
        """Best-effort content extraction for salience + novelty."""
        content = p.payload.get("content")
        if isinstance(content, str):
            return content
        # Common alternates seen across producer styles.
        for key in ("text", "message", "summary", "path"):
            v = p.payload.get(key)
            if isinstance(v, str):
                return v
        return f"{p.source}:{p.kind}"

    def _novelty_key(self, p: Percept) -> str:
        """Stable key for the novelty LRU.

        Uses (kind, source, content-snippet) — same wording in the
        same lane is "the same" for novelty purposes. Snippet is
        capped to keep keys cheap and avoid unbounded memory.
        """
        snippet = self._extract_content(p)[:128]
        return f"{p.source}:{p.kind}:{snippet}"

    def _add_focus(self, p: Percept, content: str, score: float) -> None:
        """Push the percept into the cognitive_state's attention focus.

        Falls back gracefully if the host doesn't expose ``add_focus``
        (e.g. a minimal fake in tests) — we still apply our own top-K
        eviction in that case via a small attribute on the host.
        """
        # The real CognitiveState exposes add_focus; use it when
        # available so its fatigue + capacity logic runs.
        focus_obj = _Focus(
            percept_id=p.id,
            content=content,
            salience_score=score,
            timestamp=p.timestamp,
        )
        add_focus = getattr(self._state, "add_focus", None)
        if callable(add_focus):
            try:
                add_focus(focus_obj)
                return
            except Exception:
                logger.exception(
                    "AttentionFilter: cognitive_state.add_focus raised; "
                    "falling back to local focus list",
                )

        # Fallback path for hosts that don't have add_focus.
        focus_list = getattr(self._state, "attention_focus", None)
        if focus_list is None:
            focus_list = []
            try:
                self._state.attention_focus = focus_list
            except Exception:
                return
        focus_list.append(focus_obj)
        # Local top-K cap (lowest-score eviction).
        if len(focus_list) > self._top_k_focus:
            focus_list.sort(key=lambda f: getattr(f, "salience_score", 0.0))
            focus_list.pop(0)


class _Focus:
    """Minimal duck-type stand-in for AttentionFocus.

    Defined locally to avoid an import-time hard dependency on
    :class:`xmclaw.cognition.state.AttentionFocus`. The real
    CognitiveState.add_focus only reads attribute access, so any
    object with the right fields works.
    """

    __slots__ = ("percept_id", "content", "salience_score", "timestamp")

    def __init__(
        self,
        percept_id: str,
        content: str,
        salience_score: float,
        timestamp: float,
    ) -> None:
        self.percept_id = percept_id
        self.content = content
        self.salience_score = salience_score
        self.timestamp = timestamp


__all__ = ["AttentionFilter"]
