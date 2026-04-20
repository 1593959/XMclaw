"""Coherence checks: detect overlap between a proposed concept and live ones.

Phase E6 extends the pre-forge guard pipeline beyond *shape* (E5 safety
policy) to *semantics*: even a well-formed concept should be rejected if
it duplicates or conflicts with something the system already runs.

Two call sites, both in ``EvolutionEngine``:

* ``check_gene_coherence(concept, existing_genes)`` — reject if the
  proposed trigger matches an existing live gene's trigger.
* ``check_skill_coherence(concept, existing_skills)`` — reject if the
  proposed description is near-identical to a live skill.

These are pure functions. They take an in-memory snapshot of live
artifacts and return ``(ok, reason)``. They do **not** hit the database
or the event bus — the engine is responsible for loading the snapshot
and emitting ``EVOLUTION_REJECTED`` on failure.

Design notes
------------
* Gene overlap is trigger-based because the trigger IS the gene's
  effective identity at match time. Two genes with the same trigger
  compete at every turn; whichever loads first silently shadows the
  other. That is the exact failure mode E6 is preventing.
* We do NOT attempt semantic equivalence on regex patterns (proving two
  regexes accept the same language is undecidable in general). We match
  literal pattern strings only. Same for ``action`` text — we compare
  triggers, not actions, because the dead-gene signal is "trigger never
  matches". Two genes with the same trigger but different actions are
  still in conflict: only one runs.
* Skill near-duplicate detection uses token-level Jaccard similarity on
  the description text. Cheap (no LLM, no embeddings) and good enough
  to catch the "re-forge almost the same skill with a slightly reworded
  insight" failure mode that E4's name-only dedup still allows.
* Thresholds are module-level constants rather than DaemonConfig keys
  because they are policy, not ops-tuning, and no deployment has ever
  needed to ship with looser coherence.
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable

# Jaccard similarity threshold for skill-description near-duplicates.
# 0.85 picks up "auto_frequent_web_search_tasks" vs. "auto_frequent_search_tasks"
# where descriptions share most of their word set, while letting genuinely
# different skills through. Tuned empirically against the seed insight set;
# higher and we miss obvious rewords, lower and legitimately distinct skills
# start colliding.
_SKILL_SIM_THRESHOLD = 0.85

# Minimum token count on both sides before similarity is meaningful.
# Two 3-word stubs often Jaccard above 0.85 just by accident.
_SKILL_MIN_TOKENS = 6

# A trivial stop-word set. We strip these before computing Jaccard so
# "the", "a", "to" don't anchor false positives. Kept small on purpose —
# the goal is rough similarity, not search-engine IR.
_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "of", "to", "in", "on", "for", "with",
    "is", "are", "be", "was", "were", "this", "that", "it", "as", "by",
    "at", "from", "but", "if", "then", "than", "into", "over", "via",
    # Chinese particles / very common noise
    "的", "了", "和", "是", "在", "有", "与", "及", "或",
})

_TOKEN_RE = re.compile(r"[\w\u4e00-\u9fff]+", re.UNICODE)


def _tokenize(text: str) -> set[str]:
    """Lowercase-word tokens minus stop words. Handles CJK characters."""
    if not text:
        return set()
    tokens = {m.group(0).lower() for m in _TOKEN_RE.finditer(text)}
    return {t for t in tokens if t not in _STOPWORDS and len(t) > 1}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _coerce_intents(raw: Any) -> list[str]:
    """Accept either a JSON string (DB representation) or a Python list
    (in-memory concept) and return a clean list of non-empty strings.
    Missing/garbage data yields []."""
    if isinstance(raw, list):
        return [i for i in raw if isinstance(i, str) and i.strip()]
    if isinstance(raw, str) and raw.strip():
        try:
            loaded = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if isinstance(loaded, list):
            return [i for i in loaded if isinstance(i, str) and i.strip()]
    return []


def check_gene_coherence(
    concept: dict[str, Any],
    existing_genes: Iterable[dict[str, Any]],
) -> tuple[bool, str | None]:
    """Reject a gene concept if its trigger collides with a live gene.

    Collision rules:
      keyword  same keyword (case-insensitive) on the same trigger_type
      regex    same pattern string on the same trigger_type
      intent   any overlap between the two intent sets
      event    same event name on the same trigger_type

    Legacy genes without a trigger_type default to ``keyword`` for
    comparison purposes (matches the fallback used in GeneManager.match).
    """
    proposed_type = str(concept.get("trigger_type") or "keyword").lower()
    proposed_trigger = str(concept.get("trigger") or "")
    proposed_intents = {i.strip().lower() for i in _coerce_intents(concept.get("intents"))}

    proposed_trigger_norm = proposed_trigger.strip().lower()

    for existing in existing_genes:
        if not existing.get("enabled", True):
            continue
        existing_type = str(existing.get("trigger_type") or "keyword").lower()

        if proposed_type == "intent" and existing_type == "intent":
            existing_intents = {i.strip().lower() for i in _coerce_intents(existing.get("intents"))}
            if proposed_intents and existing_intents & proposed_intents:
                return False, "gene_intent_overlap"
            continue

        # For non-intent triggers, require matching types AND matching triggers.
        if proposed_type != existing_type:
            continue
        existing_trigger = str(existing.get("trigger") or "").strip()
        if not existing_trigger or not proposed_trigger_norm:
            continue
        if proposed_type == "keyword" or proposed_type == "event":
            if existing_trigger.lower() == proposed_trigger_norm:
                return False, f"gene_{proposed_type}_duplicate"
        elif proposed_type == "regex":
            # Literal pattern comparison only. Regex equivalence is
            # undecidable, and in practice the LLM re-emits identical
            # patterns far more often than it invents equivalent ones.
            if existing_trigger == proposed_trigger:
                return False, "gene_regex_duplicate"

    return True, None


def check_skill_coherence(
    concept: dict[str, Any],
    existing_skills: Iterable[dict[str, Any]],
) -> tuple[bool, str | None]:
    """Reject a skill concept if its description looks like a live skill.

    E4 already rejects exact-name collisions via
    ``_find_live_skill_for_concept``. This function catches the softer
    case where the forge produces a near-duplicate under a slightly
    different auto-generated name.
    """
    proposed_desc = concept.get("description") or ""
    proposed_tokens = _tokenize(proposed_desc)
    if len(proposed_tokens) < _SKILL_MIN_TOKENS:
        # Too few tokens for the similarity score to be meaningful.
        # Skill-level E5 policy already rejects empty names; let short
        # descriptions through and let VFM downgrade them.
        return True, None

    proposed_name = str(concept.get("name") or "").strip().lower()

    for existing in existing_skills:
        existing_name = str(existing.get("name") or "").strip().lower()
        # Identity collisions are E4's job — skip so we don't shadow its
        # more informative reject_reason.
        if existing_name and existing_name == proposed_name:
            continue
        existing_desc = existing.get("description") or ""
        existing_tokens = _tokenize(existing_desc)
        if len(existing_tokens) < _SKILL_MIN_TOKENS:
            continue
        sim = _jaccard(proposed_tokens, existing_tokens)
        if sim >= _SKILL_SIM_THRESHOLD:
            return False, "skill_description_near_duplicate"

    return True, None
