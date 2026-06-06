"""Skills — versioned, manifest-declared, evolvable agent capabilities.

Anti-req #5 + #12: every skill is a version-tagged artifact. Promotion
emits a ``skill_promoted`` event with non-empty ``evidence``. Rollback is
single-call. See V2_DEVELOPMENT.md §3.5.
"""

from xmclaw.skills.base import Skill, SkillInput, SkillOutput
from xmclaw.skills.manifest import SkillManifest, SkillTrustLevel
from xmclaw.skills.registry import SkillRegistry, SkillRef, SkillUsageStats, UnknownSkillError, DangerousPromotionError
from xmclaw.skills.tool_bridge import SkillToolProvider
from xmclaw.skills.user_loader import UserSkillsLoader, LoadResult, resolve_skill_roots
from xmclaw.skills.markdown_skill import MarkdownProcedureSkill
from xmclaw.skills.variant_selector import VariantSelector
from xmclaw.skills.inductor import SkillInductor, Trajectory, TrajectoryStep, InductionProposal
from xmclaw.skills.semantic_index import SkillSemanticIndex
from xmclaw.skills.prefilter import select_relevant_skills
from xmclaw.skills.versioning import PromotionRecord
from xmclaw.skills.orchestrator import EvolutionOrchestrator

__all__ = [
    # Base
    "Skill",
    "SkillInput",
    "SkillOutput",
    # Manifest
    "SkillManifest",
    "SkillTrustLevel",
    # Registry
    "SkillRegistry",
    "SkillRef",
    "SkillUsageStats",
    "UnknownSkillError",
    "DangerousPromotionError",
    # Tool bridge
    "SkillToolProvider",
    # Loader
    "UserSkillsLoader",
    "LoadResult",
    "resolve_skill_roots",
    # Markdown skill
    "MarkdownProcedureSkill",
    # Variant selector
    "VariantSelector",
    # Inductor
    "SkillInductor",
    "Trajectory",
    "TrajectoryStep",
    "InductionProposal",
    # Semantic index
    "SkillSemanticIndex",
    # Prefilter
    "select_relevant_skills",
    # Versioning
    "PromotionRecord",
    # Orchestrator
    "EvolutionOrchestrator",
]
