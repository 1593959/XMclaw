"""Evolution — streaming observer-driven promotion/rollback loop.

Phase 3.3 ships ``EvolutionController``: the glue between scheduler
arm stats and ``SkillRegistry`` promotion. It decides WHEN a candidate
has earned a promotion based on grader evidence, and refuses to
promote without it (anti-req #12 at the autonomous-loop layer).

Phase 4+ extends this with cross-session signal aggregation and
online hot-reload of promoted skill versions.
"""
from xmclaw.core.evolution.constraints import (
    ConstraintReport,
    validate_candidate,
)
from xmclaw.core.evolution.controller import (
    EvolutionController,
    EvolutionDecision,
    PromotionThresholds,
)
from xmclaw.core.evolution.dataset import (
    EvalDataset,
    EvalExample,
    build_dataset_from_history,
)
from xmclaw.core.evolution.mutator import (
    MutationResult,
    SkillMutator,
    xmclaw_fitness,
)
from xmclaw.core.evolution.proposer import (
    ProposedSkill,
    SkillProposer,
    noop_extractor,
)

__all__ = [
    "ConstraintReport",
    "EvalDataset",
    "EvalExample",
    "EvolutionController",
    "EvolutionDecision",
    "MutationResult",
    "ProposedSkill",
    "PromotionThresholds",
    "SkillMutator",
    "SkillProposer",
    "build_dataset_from_history",
    "noop_extractor",
    "validate_candidate",
    "xmclaw_fitness",
]
