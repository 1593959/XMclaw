"""Domain-specific graders — plug-in layer on top of HonestGrader.

Each domain grader inspects the ``result`` field of a tool/skill event and
produces a bounded-[0,1] quality score. The OnlineScheduler consumes the
combined structural-honest + domain-quality signal as its reward.

Domain graders are OPT-IN per task: a caller selects which grader to apply
given the task type. This mirrors Hermes Agent's skills-grader design but
enforces honesty (see HonestGrader docstring) — the domain grader's output
is a numeric score, never an LLM self-judgement.
"""
from xmclaw.core.grader.domain.summary import SummaryQualityGrader

__all__ = ["SummaryQualityGrader"]
