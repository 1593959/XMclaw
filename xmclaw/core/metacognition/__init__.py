"""Metacognition — R3 (2026-05-10).

Agent self-modification driven by **patterns in its own behavior**,
not single-skill grader scores. Where ``HonestGrader`` answers "did
this skill solve the task?", metacognition answers "am I, as an
agent, exhibiting a recurring failure mode?".

Three pieces:

* ``DecisionTrace`` (``trace.py``) — append-only log of "agent made
  decision X with reason Y at step Z of turn T". Stored in events.db
  alongside the rest of the journal so consolidation cycles can see
  it.
* ``MetaCognitionPass`` (``pass_.py``) — periodic LLM-driven scan
  over recent traces. Returns structured ``Pattern`` findings.
* ``Reformer`` (``reformer.py``) — turns Patterns into actionable
  proposals: curriculum_edit, skill_propose, preference_update.
  All proposals route through the existing EvolutionController's
  grader gate so hallucinated patterns can't self-mutate the agent.

Why a separate module instead of extending evolution/:
  * evolution/ is already named-and-shaped around skill mutation
    (mutation orchestrator, candidate vs head, Pareto frontier).
  * Metacognition operates at the **agent** layer (preferences,
    curriculum hints, persona facts). Mixing layers in one module
    would muddy each.
  * The Reformer DOES delegate to EvolutionController for the
    grader gate — composition not inheritance.
"""
from __future__ import annotations

from xmclaw.core.metacognition.pass_ import MetaCognitionPass, Pattern
from xmclaw.core.metacognition.reformer import (
    Reformer,
    ReformProposal,
    ReformKind,
)
from xmclaw.core.metacognition.trace import (
    DecisionTrace,
    DecisionTraceRecorder,
)

__all__ = [
    "DecisionTrace",
    "DecisionTraceRecorder",
    "MetaCognitionPass",
    "Pattern",
    "Reformer",
    "ReformKind",
    "ReformProposal",
]
