"""Honest Grader — multi-signal, ground-truth-first scoring.

Sprint 3 Iron Rule #1: any promotion needs ≥2 INDEPENDENT signals;
never single LLM-judge. The HonestGrader composes a deterministic
signal layer (``ran`` / ``returned`` / ``type_matched`` /
``side_effect_observable`` — see ``checks.py``) with an independent
signal layer (``UserFollowupSignal`` / ``HoldoutTestSignal`` /
``CrossJudgeSignal`` — see ``_signals.py``) and produces a
:class:`GraderVerdict` whose ``promote_eligible`` flag is the gate
the EvolutionController consults before any promotion.

Anti-requirement #4 (LLM self-judgement weight ≤ 0.2) is preserved:
LLM self-rating is never positively scored alone — its only path into
the verdict is through :class:`CrossJudgeSignal`, which ENFORCES
disagreement-as-negative semantics.

See ``docs/EVOLUTION_HONEST_STATE.md`` for the design rationale and
the three peer-research findings the design was built on.
"""
from xmclaw.core.grader._signals import (
    CrossJudgeSignal,
    HoldoutTestSignal,
    IndependentSignal,
    IndependentSignalResult,
    UserFollowupSignal,
)
from xmclaw.core.grader.verdict import GraderVerdict, HonestGrader

__all__ = [
    "CrossJudgeSignal",
    "GraderVerdict",
    "HoldoutTestSignal",
    "HonestGrader",
    "IndependentSignal",
    "IndependentSignalResult",
    "UserFollowupSignal",
]
