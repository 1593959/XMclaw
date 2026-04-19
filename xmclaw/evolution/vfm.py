"""VFM (Value Function Model) scoring for evolution quality."""
import ast
import re
from pathlib import Path
from typing import Any


# Keywords that indicate genuine, specific behavior (vs generic)
_SOLID_BEHAVIOR_KW = {
    "retry", "fallback", "cache", "validate", "sanitize", "parse",
    "compress", "encrypt", "authenticate", "rate_limit", "throttle",
    "aggregate", "transform", "normalize", "dedupe", "index",
    "notify", "alert", "escalate", "degrade", "circuit_breaker",
    "batch", "stream", "paginate", "watch", "poll", "observe",
    "rollback", "checkpoint", "snapshot", "replay",
}
# Vague/generic words that score low
_GENERIC_KW = {
    "handle", "process", "manage", "do", "perform", "execute task",
    "something", "stuff", "thing", "work", "working", "stuff",
    "whatever", "miscellaneous", "generic",
}
# Actionable verb patterns for executable artifacts
_ACTIONABLE_VERBS = {
    "create", "delete", "update", "upsert", "fetch", "retrieve",
    "send", "receive", "subscribe", "publish", "enqueue", "dequeue",
    "run", "exec", "spawn", "schedule", "cancel",
    "check", "verify", "assert", "validate", "compare",
    "transform", "convert", "encode", "decode", "serialize",
    "aggregate", "count", "sum", "average", "index",
    "notify", "alert", "escalate", "log", "emit",
}


class VFMScorer:
    """Score the value of an evolutionary artifact (Gene or Skill).

    The VFM model evaluates artifacts across 4 dimensions:
    - novelty:    Is this behaviour genuinely new? (vs a trivial variant)
    - clarity:    Is the intent well-specified?
    - actionability: Can it actually do something concrete?
    - relevance:  Does it address a real observed pattern?
    """

    def score_gene(self, gene: dict[str, Any]) -> dict[str, float]:
        scores = {
            "novelty": self._score_novelty(gene),
            "clarity": self._score_clarity(gene),
            "actionability": self._score_actionability_gene(gene),
            "relevance": self._score_relevance(gene),
        }
        scores["total"] = sum(scores.values()) / len(scores)
        return scores

    def score_skill(self, skill: dict[str, Any]) -> dict[str, float]:
        scores = {
            "novelty": self._score_novelty(skill),
            "clarity": self._score_clarity(skill),
            "actionability": self._score_actionability_skill(skill),
            "relevance": self._score_relevance(skill),
        }
        scores["total"] = sum(scores.values()) / len(scores)
        return scores

    def _score_novelty(self, artifact: dict[str, Any]) -> float:
        """Novelty: is this genuinely new behaviour?

        Looks at description length, specificity keywords, and whether
        the description is too generic.
        """
        desc = artifact.get("description", "").lower()
        name = artifact.get("name", "").lower()
        trigger = artifact.get("trigger", "").lower()
        combined = f"{desc} {name} {trigger}"

        # Penalise generic descriptions
        generic_hits = sum(1 for kw in _GENERIC_KW if kw in combined)
        if generic_hits >= 2:
            return 1.5

        # Reward solid behaviour keywords
        solid_hits = sum(1 for kw in _SOLID_BEHAVIOR_KW if kw in combined)
        if solid_hits >= 3:
            score = 7.0 + min(solid_hits, 2)
        elif solid_hits >= 1:
            score = 5.0 + min(solid_hits, 2)
        else:
            # Length-based fallback
            score = min(len(desc) / 60, 1.0) * 5

        # Reward conditional language (specific triggers)
        conditional_kw = ["when", "if", "after", "before", "on ", "during", "unless"]
        if any(kw in combined for kw in conditional_kw):
            score = min(score + 1.5, 10.0)

        return round(score, 1)

    def _score_clarity(self, artifact: dict[str, Any]) -> float:
        """Clarity: is the intent well-defined?

        Checks for:
        - Non-trivial name length
        - Description with sentence structure or bullet points
        - Defined parameters
        - Explicit input/output expectations
        """
        name = artifact.get("name", "")
        desc = artifact.get("description", "")
        params = artifact.get("parameters", {})

        score = 5.0

        # Name should be descriptive (not "auto_skill_abc123")
        auto_pattern = re.compile(r"auto_[a-f0-9]{6,}", re.IGNORECASE)
        if auto_pattern.match(name):
            score -= 1.5

        # Description should be > 30 chars and structured
        if len(desc) >= 30:
            score += 1.0
        if len(desc) >= 100:
            score += 0.5
        if any(p in desc for p in [".", ":", "\n", "- ", "* "]):
            score += 1.0

        # Has explicit parameters → clearer intent
        if params and isinstance(params, dict):
            score += 1.0

        # Vague/empty description
        if len(desc) < 15:
            score -= 2.0

        return round(max(0.5, min(score, 10.0)), 1)

    def _score_actionability_gene(self, artifact: dict[str, Any]) -> float:
        """Actionability for genes: does it have a concrete trigger and action?"""
        trigger = artifact.get("trigger", "")
        action = artifact.get("action", "")

        score = 5.0

        # Must have a trigger pattern
        if not trigger or len(trigger) < 3:
            return 2.0

        # Actionable verb in trigger
        trigger_lower = trigger.lower()
        if any(v in trigger_lower for v in _ACTIONABLE_VERBS):
            score += 2.0

        # Action body exists and is non-trivial
        if action and len(action) > 20:
            score += 1.5

        # Reasonable trigger length (not too short, not too long)
        if 10 <= len(trigger) <= 80:
            score += 1.0

        return round(max(0.5, min(score, 10.0)), 1)

    def _score_actionability_skill(self, skill: dict[str, Any]) -> float:
        """Actionability for skills: can it actually be called and run?

        Checks for: defined parameters, non-trivial description, actionable name.
        """
        name = skill.get("name", "")
        desc = skill.get("description", "")
        params = skill.get("parameters", {})

        score = 5.0

        # Has parameters (can be invoked with inputs)
        if params and isinstance(params, dict) and len(params) > 0:
            score += 2.0

        # Description mentions what it does
        if any(v in desc.lower() for v in _ACTIONABLE_VERBS):
            score += 1.5

        # Name reflects an action (verb prefix)
        if name and name.split("_")[0].lower() in _ACTIONABLE_VERBS:
            score += 1.0

        # Has action_body (pre-generated code)
        if skill.get("action_body"):
            score += 1.5

        return round(max(0.5, min(score, 10.0)), 1)

    def _score_relevance(self, artifact: dict[str, Any]) -> float:
        """Relevance: does it address a real observed pattern?"""
        source = artifact.get("source", "")
        if source == "negative_feedback":
            return 9.5   # Problems reported by users are high priority
        if source == "repeated_request":
            return 8.0
        if source == "user_request":
            return 8.5
        if source == "tool_usage_analysis":
            return 7.0
        if source == "repeated_tool":
            return 6.5
        return 6.0

    def score_file(self, path: Path) -> dict[str, float]:
        """Score an existing Python artifact file by inspecting its AST.

        Used to re-score previously generated genes/skills that lacked
        concept metadata (e.g., old auto-generated files).
        """
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
        except Exception:
            return {"novelty": 1.0, "clarity": 1.0, "actionability": 1.0, "relevance": 5.0, "total": 2.0}

        score = 5.0
        funcs = [n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
        classes = [n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
        has_imports = len(tree.body) > 0 and isinstance(tree.body[0], (ast.Import, ast.ImportFrom))

        if len(funcs) >= 2:
            score += 1.0
        if len(classes) >= 1:
            score += 0.5
        if has_imports:
            score += 0.5
        if any(f in funcs for f in ["execute", "run", "validate", "check"]):
            score += 1.0

        return {
            "novelty": round(score - 1, 1),
            "clarity": round(score, 1),
            "actionability": round(score + 0.5, 1),
            "relevance": 6.0,
            "total": round(score, 1),
        }

    def should_solidify(self, scores: dict[str, float], threshold: float = 5.0) -> bool:
        """Determine if artifact passes quality threshold.

        Total score is 0-10 (average of 4 dimensions). Default threshold 5.0
        means at least an average of 5/10 per dimension.
        """
        return scores.get("total", 0) >= threshold
