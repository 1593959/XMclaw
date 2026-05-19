"""Independent grader signals — Sprint 3 Iron Rule #1.

Iron Rule #1: any promotion needs ≥2 independent signals; never single
LLM-judge. The audit identified the prior HonestGrader's #1 weakness
as 70-80% of scores coming from "tool didn't crash" plus a gameable
LLM self-rating (capped at 0.20 but still gameable). Multiple ICLR /
TACL papers (Huang ICLR 2024; CorrectBench NeurIPS 2025; JudgeBench
ICLR 2025) confirm that self-correction without external ground truth
is an echo chamber and that LLM-as-judge approaches random on hard
tasks. Sprint 3 designs around all of those findings.

This module defines the **independent** signal layer that lives next
to the deterministic check layer (``checks.py``). The deterministic
layer answers "did the tool not crash and produce output of the
declared shape". The independent layer answers "did SOMETHING outside
the tool's own self-report agree that the work was actually useful":

* :class:`UserFollowupSignal` — does the next user turn look like a
  positive continuation (no "redo / undo / revert / 不对") or did the
  user thumbs-up the previous tool result. **Fully implemented** —
  reads existing ``BehavioralEvent`` history without any new
  infrastructure.
* :class:`HoldoutTestSignal` — execute a registered deterministic
  check ("after this skill runs, file X exists with property Y").
  **Stubbed** today (returns ``None`` until skill registry exposes
  ``eval_test_id``); the abstraction is the load-bearing piece.
* :class:`CrossJudgeSignal` — two LLM judges from different families
  score the same artifact. **Disagreement is a NEGATIVE signal**, not
  a positive consensus. Peer research (arxiv 2505.22960) showed
  multi-agent debate ceiling = best single agent, with adversarial
  agents dropping accuracy 10-40%. Disagreement DOES correlate with
  low-quality answers, so we use it as a downweighting signal only.
  **Stubbed** today (returns ``None`` until cross-judge plumbing lands).

Each signal class exposes:

  ``async probe(event, *, history=None) -> tuple[float | None, dict]``

  - First element: confidence ∈ [0.0, 1.0], or ``None`` when the
    signal is **not applicable** to this event (e.g. the user followup
    signal can't fire when there's no user-message history).
  - Second element: machine-readable evidence dict that the verdict
    serializer carries through to the bus payload.

``None`` does NOT mean "score=0". It means "this independent signal
is unavailable for this event, please choose another or fall back to
single-signal-only (which then forbids promotion per Iron Rule #1)".
That distinction is what keeps weak-model loops (GPT-5 / 7B / Kimi)
from spuriously dropping scores: those models simply don't get the
``CrossJudgeSignal`` to fire — they don't get penalised for it.

Per-model capability profile (Iron Rule #3): the deterministic checks
plus the holdout test plus the user-followup signal all run regardless
of model strength. Only the cross-judge signal requires a "strong"
model in the chain (Claude / Opus / GPT-4o) — the other two work for
every model.

Import direction (per ``xmclaw/core/AGENTS.md``):
  ✅ MAY import: stdlib, ``xmclaw.utils.*``, ``xmclaw.security.*``,
     ``xmclaw.core.bus.*`` (own subpackage, allowed).
  ❌ MUST NOT import: ``xmclaw.providers.*`` / ``xmclaw.skills.*`` /
     ``xmclaw.daemon.*`` / ``xmclaw.cli.*``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Protocol

from xmclaw.core.bus.events import BehavioralEvent, EventType


# ── public API ────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class IndependentSignalResult:
    """Result wrapper — the value plus the evidence dict.

    The dataclass is purely a return-shape convenience; signals also
    return a tuple so callers can pattern-match either way.
    """

    score: float | None  # None = signal not applicable
    kind: str            # "user_followup" | "holdout_test" | "cross_judge_agreement" | "none"
    evidence: dict[str, Any]


class IndependentSignal(Protocol):
    """The shape every independent signal must satisfy."""

    name: str

    async def probe(
        self,
        event: BehavioralEvent,
        *,
        history: Iterable[BehavioralEvent] | None = None,
    ) -> tuple[float | None, dict[str, Any]]:
        """Return ``(score, evidence)``.

        ``score`` is ``None`` when the signal is not applicable to this
        event (no signal, not a low signal). ``evidence`` is always a
        dict — ``{}`` when the signal is not applicable, populated when
        the signal fired, even if it fired with low confidence.
        """
        ...


# ── implementations ───────────────────────────────────────────────────────


# Words / phrases that indicate the user's NEXT turn is asking the
# agent to retry, undo, or correct what it just did — a strong negative
# signal that the prior tool/skill output was bad. List is intentionally
# small (high-precision, low-recall): false positives are far more
# damaging than missing some negatives, since this signal can BLOCK
# promotion.
_NEGATIVE_FOLLOWUP_PATTERNS_EN: tuple[str, ...] = (
    "redo",
    "undo",
    "revert",
    "rollback",
    "try again",
    "that's wrong",
    "thats wrong",
    "incorrect",
    "wrong answer",
    "not what i asked",
    "stop",
    "no that",
    "no, that",
    "wrong direction",
    "fix that",
    "this is broken",
)
_NEGATIVE_FOLLOWUP_PATTERNS_ZH: tuple[str, ...] = (
    "不对",
    "重做",
    "撤销",
    "回滚",
    "重新",
    "错了",
    "不是这样",
    "停下",
    "不对劲",
    "修一下",
    "改一下",
    "别这样",
)


class UserFollowupSignal:
    """Signal A — did the user reaction confirm the prior tool succeeded.

    ``probe`` walks ``history`` for the first ``USER_MESSAGE`` event
    that came AFTER ``event.ts`` and scores it:

    * If the user's text contains any negative-followup pattern (English
      or Chinese), score = 0.0 — the user explicitly told us the work
      was bad.
    * If a thumbs-up reaction event is present in the payload, score =
      1.0 (the chat-reaction frame B-???).  When the frame doesn't
      exist (today's reality), this branch is dead code — kept so the
      hookup is a one-line edit when the frame ships.
    * If the user kept conversing for ≥3 more turns without a negative
      pattern, score = 0.7 — sustained engagement is a moderate
      positive.
    * If the user sent ONE follow-up that's neutral (no positive, no
      negative), score = 0.5 — neutral signal, neither penalises nor
      promotes.
    * If there's NO follow-up turn yet (the conversation ended on this
      tool call), the signal is **not applicable** — return ``None``.

    The signal is fully implemented today: it reads ``USER_MESSAGE``
    events that the daemon already publishes for every user turn. No
    new bus event types or chat-reaction infrastructure required.
    """

    name = "user_followup"

    async def probe(
        self,
        event: BehavioralEvent,
        *,
        history: Iterable[BehavioralEvent] | None = None,
    ) -> tuple[float | None, dict[str, Any]]:
        if history is None:
            return None, {}

        # Materialize once so we can iterate twice without repeating
        # the caller's generator. Keep it bounded — typical history
        # passed in is ≤200 events for one session.
        all_events = list(history)
        # Same-session only — UserFollowupSignal must not pull a
        # follow-up from a *different* session and mistake it for a
        # reaction to this event. Defensive even though callers
        # typically pre-filter.
        same_session = [
            e for e in all_events if e.session_id == event.session_id
        ]
        # Find user messages that came AFTER this event (strict >).
        later_user_messages = sorted(
            (
                e for e in same_session
                if e.type == EventType.USER_MESSAGE and e.ts > event.ts
            ),
            key=lambda e: e.ts,
        )

        if not later_user_messages:
            # Conversation ended on this event — signal not applicable.
            return None, {}

        first = later_user_messages[0]
        text = self._extract_text(first.payload).strip()
        text_lower = text.lower()

        # 1. Negative-followup patterns dominate — even one match flips
        #    the signal to 0.0. Accuracy of these patterns is the whole
        #    invariant of this signal; keep the list tight.
        for pat in _NEGATIVE_FOLLOWUP_PATTERNS_EN:
            if pat in text_lower:
                return 0.0, {
                    "pattern": pat,
                    "language": "en",
                    "follow_up_text_preview": text[:100],
                    "later_turns": len(later_user_messages),
                }
        for pat in _NEGATIVE_FOLLOWUP_PATTERNS_ZH:
            if pat in text:
                return 0.0, {
                    "pattern": pat,
                    "language": "zh",
                    "follow_up_text_preview": text[:100],
                    "later_turns": len(later_user_messages),
                }

        # 2. Explicit thumbs-up via chat-reaction frame (future infra).
        #    Today every published event lacks this field, so the
        #    branch is functionally dead but kept for forward-compat.
        reactions: dict[str, Any] = first.payload.get("reactions") or {}
        if reactions.get("thumbs_up"):
            return 1.0, {
                "reaction": "thumbs_up",
                "later_turns": len(later_user_messages),
            }
        if reactions.get("thumbs_down"):
            return 0.0, {
                "reaction": "thumbs_down",
                "later_turns": len(later_user_messages),
            }

        # 3. Sustained engagement (≥3 follow-ups, no negative match).
        if len(later_user_messages) >= 3:
            return 0.7, {
                "reason": "sustained_engagement",
                "later_turns": len(later_user_messages),
            }

        # 4. One follow-up, no signal — neutral.
        return 0.5, {
            "reason": "neutral_followup",
            "later_turns": len(later_user_messages),
        }

    @staticmethod
    def _extract_text(payload: dict[str, Any]) -> str:
        """USER_MESSAGE events use ``text`` consistently, but tolerate
        ``content`` / ``message`` for legacy / test events."""
        for k in ("text", "content", "message"):
            v = payload.get(k)
            if isinstance(v, str):
                return v
        return ""


class HoldoutTestSignal:
    """Signal B — execute a deterministic holdout check.

    Conceptually: a skill registers ``eval_test_id`` pointing at a
    callable that returns ``True`` when the post-state of the world
    matches the skill's claim ("file X exists", "DB row Y has value
    Z"). On invocation, the grader looks up that callable, executes
    it, and the boolean answer becomes the signal score (1.0 / 0.0).

    **Status (Epic #27 sweep #10, 2026-05-19):** fully wired via
    :mod:`xmclaw.core.grader.holdout_registry`. Pre-fix this was a
    stub that always returned ``None`` unless tests passed an
    explicit ``holdout_test_passed`` payload override; the sweep
    audit caught that the docs implied multi-signal grading was
    real while only ``UserFollowupSignal`` actually fired. The
    registry now lets production code register named checks at
    boot time + skill load time; ``eval_test_id`` resolves via
    :func:`holdout_registry.run_check`.

    Resolution order:
      1. ``holdout_test_passed`` payload override (test escape hatch).
      2. ``eval_test_id`` → registry lookup → run → bool / None.
      3. ``None`` (signal not applicable).

    Honest about partial infra: the registry has no automatic
    population — skills that want a holdout check must either
    register one at boot or in their loader hook. Empty registry
    = the signal continues to behave as it did before the fix
    (returns None for events without payload override).
    """

    name = "holdout_test"

    async def probe(
        self,
        event: BehavioralEvent,
        *,
        history: Iterable[BehavioralEvent] | None = None,  # noqa: ARG002
    ) -> tuple[float | None, dict[str, Any]]:
        eval_test_id = event.payload.get("eval_test_id")
        # Test escape hatch: ``holdout_test_passed`` short-circuits
        # the registry lookup so tests can exercise the abstraction
        # without registering a callable. Preserved for backward
        # compat with ``tests.unit.test_v2_signals_holdout_cross``.
        override = event.payload.get("holdout_test_passed")
        if isinstance(override, bool):
            return (1.0 if override else 0.0), {
                "eval_test_id": eval_test_id,
                "passed": override,
                "source": "payload_override",
            }
        if not eval_test_id:
            # No registered holdout — signal not applicable.
            return None, {}

        # Sweep #10 real path: resolve eval_test_id → callable → run.
        from xmclaw.core.grader.holdout_registry import (
            lookup as _lookup, run_check as _run_check,
        )
        if _lookup(eval_test_id) is None:
            return None, {
                "eval_test_id": eval_test_id,
                "status": "unregistered",
            }
        passed = await _run_check(eval_test_id, event.payload)
        if passed is None:
            # Callable raised — keep verdict neutral rather than
            # punishing the skill for a buggy verify hook.
            return None, {
                "eval_test_id": eval_test_id,
                "status": "check_raised",
            }
        return (1.0 if passed else 0.0), {
            "eval_test_id": eval_test_id,
            "passed": passed,
            "source": "registry",
        }


class CrossJudgeSignal:
    """Signal C — disagreement-as-negative cross-LLM judging.

    Two judges from different families (Anthropic + OpenAI / local /
    DashScope) score the same artifact. We compute |score_a - score_b|
    and convert it to a *penalty*:

    * agreement within ``delta`` (default 0.15) → return ``mean(a, b)``
      as a positive consensus signal.
    * disagreement larger than ``delta`` → score = ``0.0`` (signal
      fires negatively; this is the explicit ICLR finding the design
      was built on).

    **Status: stub.** Cross-judge plumbing isn't wired yet — there's
    no second-judge result on the event payload. We honour explicit
    ``cross_judge_a`` / ``cross_judge_b`` payload fields so unit tests
    can exercise the abstraction; production rollout is a follow-up
    that reuses ``CrossJudgeSignal.probe`` once the second judge runs.
    """

    name = "cross_judge"

    def __init__(self, *, delta: float = 0.15) -> None:
        self._delta = delta

    async def probe(
        self,
        event: BehavioralEvent,
        *,
        history: Iterable[BehavioralEvent] | None = None,  # noqa: ARG002
    ) -> tuple[float | None, dict[str, Any]]:
        a = event.payload.get("cross_judge_a")
        b = event.payload.get("cross_judge_b")
        if a is None or b is None:
            # Either judge missing → not applicable.
            return None, {}
        try:
            a_f = float(a)
            b_f = float(b)
        except (TypeError, ValueError):
            return None, {"reason": "non_numeric_judge_score"}

        # Out-of-range guard — clamp to [0, 1] for downstream maths.
        a_f = max(0.0, min(1.0, a_f))
        b_f = max(0.0, min(1.0, b_f))

        diff = abs(a_f - b_f)
        if diff > self._delta:
            # Disagreement = negative signal. This is the whole point
            # of the design: peer research showed multi-judge debate
            # doesn't fix bias, but disagreement DOES correlate with
            # low-quality answer. Score = 0 not None.
            return 0.0, {
                "judge_a": a_f,
                "judge_b": b_f,
                "diff": diff,
                "delta": self._delta,
                "verdict": "disagreement_penalty",
            }

        # Agreement → mean as positive consensus.
        score = (a_f + b_f) / 2.0
        return score, {
            "judge_a": a_f,
            "judge_b": b_f,
            "diff": diff,
            "delta": self._delta,
            "verdict": "agreement",
        }


# ── helpers ───────────────────────────────────────────────────────────────


async def best_independent_score(
    signals: Iterable[IndependentSignal],
    event: BehavioralEvent,
    *,
    history: Iterable[BehavioralEvent] | None = None,
) -> tuple[float | None, str, dict[str, Any]]:
    """Run signals in order, return the first one that fires.

    Sprint 3 keeps the policy simple: any one independent signal
    firing satisfies Iron Rule #1. We do not aggregate multiple
    independent signals into a single score yet — that's a follow-up
    once at least 2 of the 3 signal classes have real implementations
    and we can study correlations between them. For now, the FIRST
    applicable signal in the iteration order wins; the others are
    never probed (cheap-fast path).

    Returns ``(score, kind, evidence)`` where:
      - ``score`` is ``None`` if NO signal was applicable.
      - ``kind`` is the signal's ``name`` or ``"none"``.
      - ``evidence`` is a dict (empty when no signal fired).
    """
    materialized: list[BehavioralEvent] | None = (
        list(history) if history is not None else None
    )
    for sig in signals:
        score, evidence = await sig.probe(event, history=materialized)
        if score is not None:
            return score, sig.name, evidence
    return None, "none", {}


__all__ = [
    "CrossJudgeSignal",
    "HoldoutTestSignal",
    "IndependentSignal",
    "IndependentSignalResult",
    "UserFollowupSignal",
    "best_independent_score",
]
