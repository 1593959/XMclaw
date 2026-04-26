"""Skill mutation constraint validators.

Direct port of ``hermes-self-evolution/evolution/skills/constraints.py:30-174``
— size / growth / structure gates. Hermes uses these to reject GEPA
proposals that would balloon a skill or strip required sections; we lift
them verbatim so XMclaw's mutator can never promote a degenerate
candidate (e.g. a 50-line skill that GEPA "optimized" into 5,000 lines
of redundant prose).

Public API:
    * :func:`validate_candidate(baseline, candidate)` →
      :class:`ConstraintReport`. ``ok=True`` when every gate passes.

Anti-req #4 corollary: even when the mutator's fitness function approves
a candidate, structural checks have veto power. A candidate that passes
fitness but fails ``validate_candidate`` is still rejected.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Bytes (not chars) — UTF-8 worst-case 4 bytes per char keeps the limit
# reasonable for Chinese / mixed-language skills. Hermes uses chars in
# their TS port; we use bytes so a CJK-heavy skill isn't artificially
# capped at the same limit as an ASCII one.
_DEFAULT_MIN_BYTES = 200          # below this, the skill body is too thin
_DEFAULT_MAX_BYTES = 60_000       # above this, GEPA is bloating
_DEFAULT_MAX_GROWTH = 3.0         # candidate may not exceed 3× baseline
_DEFAULT_MIN_RETAIN_RATIO = 0.10  # at least 10% of baseline content must
                                  # share textual ancestry with candidate


@dataclass(frozen=True, slots=True)
class ConstraintReport:
    ok: bool
    failures: list[str] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)


def _byte_len(s: str) -> int:
    return len(s.encode("utf-8"))


def _ngram_set(s: str, n: int = 8) -> set[str]:
    """Word-level n-gram set used for retention-ratio. Mirrors hermes
    ``_ngram_overlap`` (``constraints.py:66-94``) — coarse but cheap.
    """
    tokens = re.findall(r"\S+", s.lower())
    if len(tokens) < n:
        return {" ".join(tokens)} if tokens else set()
    return {" ".join(tokens[i : i + n]) for i in range(len(tokens) - n + 1)}


def _retention_ratio(baseline: str, candidate: str) -> float:
    """Fraction of baseline n-grams that survive in the candidate."""
    base = _ngram_set(baseline)
    cand = _ngram_set(candidate)
    if not base:
        return 1.0  # no n-grams to compare; treat as preserved
    overlap = base & cand
    return len(overlap) / len(base)


def _has_required_section(text: str, header_pattern: str) -> bool:
    return re.search(header_pattern, text, re.MULTILINE) is not None


def validate_candidate(
    baseline: str,
    candidate: str,
    *,
    min_bytes: int = _DEFAULT_MIN_BYTES,
    max_bytes: int = _DEFAULT_MAX_BYTES,
    max_growth: float = _DEFAULT_MAX_GROWTH,
    min_retain_ratio: float = _DEFAULT_MIN_RETAIN_RATIO,
    required_sections: list[str] | None = None,
) -> ConstraintReport:
    """Validate a mutated skill body against structural gates.

    Args:
        baseline: the prior promoted skill body
        candidate: the proposed new body from the mutator
        min_bytes / max_bytes: absolute size band
        max_growth: candidate may be at most ``max_growth × baseline_bytes``
        min_retain_ratio: 0..1, fraction of baseline 8-gram set that must
            survive in the candidate (rejects total-rewrites that drop
            the user's intent entirely — use 0.0 to allow any rewrite)
        required_sections: optional list of regex header patterns
            (e.g. ``r"^## Examples"``). Each must match the candidate.

    Returns:
        :class:`ConstraintReport` with ``ok=False`` and a non-empty
        ``failures`` list when any gate fails.
    """
    failures: list[str] = []
    metrics: dict[str, float] = {}

    base_bytes = _byte_len(baseline)
    cand_bytes = _byte_len(candidate)
    metrics["baseline_bytes"] = float(base_bytes)
    metrics["candidate_bytes"] = float(cand_bytes)
    metrics["growth_ratio"] = (
        float(cand_bytes) / base_bytes if base_bytes > 0 else 0.0
    )

    if cand_bytes < min_bytes:
        failures.append(
            f"size_below_min: candidate {cand_bytes} bytes < {min_bytes}"
        )
    if cand_bytes > max_bytes:
        failures.append(
            f"size_above_max: candidate {cand_bytes} bytes > {max_bytes}"
        )
    if base_bytes > 0 and metrics["growth_ratio"] > max_growth:
        failures.append(
            f"growth_excess: ratio {metrics['growth_ratio']:.2f}× > {max_growth}×"
        )

    retain = _retention_ratio(baseline, candidate)
    metrics["retain_ratio"] = retain
    if retain < min_retain_ratio:
        failures.append(
            f"retain_below_min: {retain:.2f} < {min_retain_ratio:.2f} "
            "(candidate dropped too much of baseline content)"
        )

    if required_sections:
        for pattern in required_sections:
            if not _has_required_section(candidate, pattern):
                failures.append(f"missing_section: pattern={pattern!r}")

    return ConstraintReport(ok=not failures, failures=failures, metrics=metrics)
