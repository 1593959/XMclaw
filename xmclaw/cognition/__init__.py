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
from xmclaw.cognition.graph_runtime import (
    GraphInspection,
    GraphState,
    NodePolicy,
    ReducerRegistry,
    apply_updates,
    inspect_graph_state,
)
from xmclaw.cognition.graph_executor import (
    GraphExecutionResult,
    GraphExecutor,
)
from xmclaw.cognition.tool_history import (
    ToolHistoryEntry,
    ToolHistoryProcessor,
)
from xmclaw.cognition.tool_review import (
    ToolFailureStrategy,
    ToolReview,
)
from xmclaw.cognition.self_critique import (
    CRITIQUE_DIMENSIONS,
    SELF_CRITIQUE_JSON_SCHEMA,
    SelfCritique,
    SelfCritiqueEngine,
    SelfCritiqueMaterializationResult,
    SelfCritiqueMaterializer,
    SelfCritiqueMemoryCandidate,
    SelfCritiqueMemoryPolicy,
    SelfCritiquePromptBuilder,
    SelfCritiqueRequest,
    TrajectoryEvent,
    parse_self_critique_json,
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
    # graph runtime
    "GraphInspection",
    "GraphState",
    "NodePolicy",
    "ReducerRegistry",
    "apply_updates",
    "inspect_graph_state",
    "GraphExecutionResult",
    "GraphExecutor",
    "ToolHistoryEntry",
    "ToolHistoryProcessor",
    "ToolFailureStrategy",
    "ToolReview",
    "CRITIQUE_DIMENSIONS",
    "SELF_CRITIQUE_JSON_SCHEMA",
    "SelfCritique",
    "SelfCritiqueEngine",
    "SelfCritiqueMaterializationResult",
    "SelfCritiqueMaterializer",
    "SelfCritiqueMemoryCandidate",
    "SelfCritiqueMemoryPolicy",
    "SelfCritiquePromptBuilder",
    "SelfCritiqueRequest",
    "TrajectoryEvent",
    "parse_self_critique_json",
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
