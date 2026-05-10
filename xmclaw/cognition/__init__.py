"""xmclaw.cognition — Cognitive architecture ("Jarvisification").

This package provides the building blocks for a self-aware agent loop:

  * state.py         — CognitiveState, Goal, AttentionFocus
  * memory_graph.py  — MemoryGraph (SQLite-backed relational memory)
  * task_scheduler.py— TaskScheduler (DAG + priority + retry)
  * file_watcher.py  — FileWatcher (filesystem perception)
  * evolution_loop.py— EvolutionLoop (skill / prompt / pattern evolution)

All modules default to ``enabled: false`` in config so existing installs
are unaffected.  Enable via ``config.cognition.enabled = true``.
"""
from __future__ import annotations

from xmclaw.cognition.state import (
    AttentionFocus,
    CognitiveState,
    Goal,
    SalienceWeights,
)
from xmclaw.cognition.memory_graph import (
    GraphEdge,
    GraphNode,
    MemoryGraph,
)
from xmclaw.cognition.task_scheduler import (
    Task,
    TaskScheduler,
)
from xmclaw.cognition.file_watcher import (
    FilePercept,
    FileWatcher,
)
from xmclaw.cognition.evolution_loop import (
    EvolutionLoop,
    EvolutionProposal,
    PerformanceAnalyzer,
    PatternExtractor,
    SkillPromoter,
    SystemPromptEvolver,
)

__all__ = [
    # state
    "AttentionFocus",
    "CognitiveState",
    "Goal",
    "SalienceWeights",
    # memory graph
    "GraphEdge",
    "GraphNode",
    "MemoryGraph",
    # task scheduler
    "Task",
    "TaskScheduler",
    # file watcher
    "FilePercept",
    "FileWatcher",
    # evolution
    "EvolutionLoop",
    "EvolutionProposal",
    "PerformanceAnalyzer",
    "PatternExtractor",
    "SkillPromoter",
    "SystemPromptEvolver",
]
