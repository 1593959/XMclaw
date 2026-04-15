"""VFM (Value Function Model) scoring for evolution quality."""
from typing import Any


class VFMScorer:
    """Score the value of an evolutionary artifact (Gene or Skill)."""

    def score_gene(self, gene: dict[str, Any]) -> dict[str, float]:
        """Score a Gene on multiple dimensions."""
        scores = {
            "novelty": self._score_novelty(gene),
            "clarity": self._score_clarity(gene),
            "actionability": self._score_actionability(gene),
            "relevance": self._score_relevance(gene),
        }
        scores["total"] = sum(scores.values()) / len(scores)
        return scores

    def score_skill(self, skill: dict[str, Any]) -> dict[str, float]:
        """Score a Skill on multiple dimensions."""
        scores = {
            "novelty": self._score_novelty(skill),
            "clarity": self._score_clarity(skill),
            "actionability": self._score_actionability(skill),
            "relevance": self._score_relevance(skill),
        }
        scores["total"] = sum(scores.values()) / len(scores)
        return scores

    def _score_novelty(self, artifact: dict[str, Any]) -> float:
        """How unique is this artifact?"""
        desc = artifact.get("description", "")
        trigger = artifact.get("trigger", "")
        # Simple heuristic: longer, more specific descriptions score higher
        length_score = min(len(desc) / 100, 1.0)
        specificity = 0.5
        if any(w in desc.lower() for w in ["when", "if", "after", "before"]):
            specificity = 1.0
        return round((length_score + specificity) / 2 * 10, 1)

    def _score_clarity(self, artifact: dict[str, Any]) -> float:
        """How clear and well-defined?"""
        name = artifact.get("name", "")
        desc = artifact.get("description", "")
        if len(name) < 3 or len(desc) < 10:
            return 2.0
        has_structure = any(c in desc for c in [".", ":", "\n"])
        return 8.0 if has_structure else 6.0

    def _score_actionability(self, artifact: dict[str, Any]) -> float:
        """Can it actually be executed?"""
        action = artifact.get("action", "")
        if not action:
            return 3.0
        # Check for actionable verbs
        verbs = ["create", "run", "execute", "call", "send", "check", "notify", "load"]
        if any(v in action.lower() for v in verbs):
            return 8.0
        return 5.0

    def _score_relevance(self, artifact: dict[str, Any]) -> float:
        """How relevant to observed patterns?"""
        # Base score, can be enhanced with historical matching
        source = artifact.get("source", "")
        if source in ["negative_feedback", "user_request"]:
            return 9.0
        return 6.0

    def should_solidify(self, scores: dict[str, float], threshold: float = 20.0) -> bool:
        """Determine if artifact passes quality threshold."""
        # Each dimension maxes at 10, total maxes at 10
        # Threshold 20 means average of 5/10 per dimension
        return scores.get("total", 0) >= (threshold / 10)
