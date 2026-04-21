"""SummaryQualityGrader — heuristic quality score for read-and-summarize.

The goal is NOT to re-invent BLEU/ROUGE. Phase 1 needs a simple, honest
signal that UCB1 can learn from on 50 turns. Three components, each
bounded [0, 1], averaged:

  * length_score   — how close to target word count (triangle kernel)
  * keyword_score  — fraction of required keywords present
  * structure_score — for variants that ask for bullets / TL;DR, check shape

None of these require an LLM. All are deterministic and replay-able — the
verdict carries the component scores as evidence so a reviewer can
inspect why a summary scored what it did.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class SummaryTask:
    """Ground truth for a read-and-summarize target."""

    file_id: str                            # identifier of the source doc
    reference_keywords: tuple[str, ...]     # keywords the summary SHOULD mention
    target_words: int                       # ideal summary length in words
    target_words_tol: float = 0.5           # triangle-kernel half-width (fraction)


@dataclass(frozen=True, slots=True)
class SummaryVerdict:
    score: float                    # ∈ [0, 1]
    length_score: float
    keyword_score: float
    structure_score: float
    evidence: list[str] = field(default_factory=list)


class SummaryQualityGrader:
    """Score a summary against its task. Pure function, no LLM, no state."""

    def __init__(self, *, require_structure: bool = True) -> None:
        self._require_structure = require_structure

    def grade(
        self,
        summary_text: str,
        task: SummaryTask,
        *,
        variant_id: str | None = None,
    ) -> SummaryVerdict:
        length_score, length_ev = self._length(summary_text, task)
        keyword_score, kw_ev = self._keywords(summary_text, task)
        structure_score, struct_ev = self._structure(summary_text, variant_id)

        components = [length_score, keyword_score]
        if self._require_structure:
            components.append(structure_score)
        score = sum(components) / len(components)

        evidence: list[str] = []
        evidence.extend(length_ev)
        evidence.extend(kw_ev)
        evidence.extend(struct_ev)

        return SummaryVerdict(
            score=max(0.0, min(1.0, score)),
            length_score=length_score,
            keyword_score=keyword_score,
            structure_score=structure_score,
            evidence=evidence,
        )

    # ── components ──

    @staticmethod
    def _length(text: str, task: SummaryTask) -> tuple[float, list[str]]:
        words = len(text.split())
        if task.target_words <= 0:
            return 1.0, [f"length={words} (target=any)"]
        span = task.target_words * task.target_words_tol
        diff = abs(words - task.target_words)
        # Triangle kernel: 1.0 at exact target, 0 at target ± span.
        score = max(0.0, 1.0 - diff / max(span, 1.0))
        return score, [f"length={words} target={task.target_words}±{span:.0f} score={score:.2f}"]

    @staticmethod
    def _keywords(text: str, task: SummaryTask) -> tuple[float, list[str]]:
        if not task.reference_keywords:
            return 1.0, ["no reference keywords"]
        hay = text.lower()
        hits: list[str] = []
        misses: list[str] = []
        for kw in task.reference_keywords:
            if kw.lower() in hay:
                hits.append(kw)
            else:
                misses.append(kw)
        score = len(hits) / len(task.reference_keywords)
        ev = [f"keywords hit={len(hits)}/{len(task.reference_keywords)} score={score:.2f}"]
        if misses:
            ev.append(f"missed={misses!r}")
        return score, ev

    @staticmethod
    def _structure(
        text: str, variant_id: str | None,
    ) -> tuple[float, list[str]]:
        """Variant-specific structural check.

        Encodes known expectations (e.g. 'bullets' → contains bullet
        markers, 'tl;dr' → prefixed with TL;DR:). Unknown variants get 1.0.
        """
        if variant_id == "bullets":
            # At least 2 bullet-looking lines
            bullet_re = re.compile(r"^\s*[-*•]\s+", re.MULTILINE)
            count = len(bullet_re.findall(text))
            score = 1.0 if count >= 2 else count / 2
            return score, [f"bullets={count} score={score:.2f}"]
        if variant_id == "tl;dr":
            has_prefix = text.strip().upper().startswith(("TL;DR", "TLDR"))
            score = 1.0 if has_prefix else 0.0
            return score, [f"tl;dr prefix={'yes' if has_prefix else 'no'}"]
        return 1.0, ["no structural requirement"]
