"""Honest Grader — ground-truth-first scoring.

Anti-requirement #4: LLM self-judgment weight ≤ 0.2. See V2_DEVELOPMENT.md §1.3.
"""
from xmclaw.core.grader.verdict import GraderVerdict, HonestGrader

__all__ = ["GraderVerdict", "HonestGrader"]
