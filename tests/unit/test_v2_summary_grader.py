"""SummaryQualityGrader unit tests."""
from __future__ import annotations

from xmclaw.core.grader.domain.summary import (
    SummaryQualityGrader,
    SummaryTask,
)


_TASK = SummaryTask(
    file_id="t",
    reference_keywords=("alpha", "beta", "gamma"),
    target_words=20,
    target_words_tol=0.5,
)


def test_perfect_length_and_all_keywords() -> None:
    text = (
        "alpha beta gamma are three letters used often in math, physics, "
        "and statistics by many students every year."  # ~20 words
    )
    g = SummaryQualityGrader(require_structure=False)
    v = g.grade(text, _TASK)
    assert v.keyword_score == 1.0
    assert v.length_score >= 0.8
    assert v.score >= 0.9


def test_missing_keywords_drops_score() -> None:
    text = "This summary says nothing useful; " + "filler " * 15
    g = SummaryQualityGrader(require_structure=False)
    v = g.grade(text, _TASK)
    assert v.keyword_score == 0.0
    assert v.score < 0.8  # keyword miss tanks the score


def test_off_target_length_clamps_to_zero() -> None:
    text = "alpha beta gamma"  # 3 words, target is 20 ± 10
    g = SummaryQualityGrader(require_structure=False)
    v = g.grade(text, _TASK)
    assert v.length_score == 0.0  # 17 words off, beyond the 10-word span
    assert v.keyword_score == 1.0


def test_bullets_structure_check() -> None:
    bulleted = "- alpha is important\n- beta covers a lot\n- gamma closes it"
    flat = "alpha and beta and gamma all matter"
    g = SummaryQualityGrader(require_structure=True)
    v_ok = g.grade(bulleted, _TASK, variant_id="bullets")
    v_bad = g.grade(flat, _TASK, variant_id="bullets")
    assert v_ok.structure_score == 1.0
    assert v_bad.structure_score < 1.0


def test_tldr_structure_check() -> None:
    with_prefix = "TL;DR: alpha beta gamma in a short phrase"
    without_prefix = "alpha beta gamma in a short phrase"
    g = SummaryQualityGrader(require_structure=True)
    v_ok = g.grade(with_prefix, _TASK, variant_id="tl;dr")
    v_bad = g.grade(without_prefix, _TASK, variant_id="tl;dr")
    assert v_ok.structure_score == 1.0
    assert v_bad.structure_score == 0.0


def test_score_bounded_0_1() -> None:
    g = SummaryQualityGrader()
    for txt in ["", "x", "x " * 500, "alpha beta gamma"]:
        v = g.grade(txt, _TASK)
        assert 0.0 <= v.score <= 1.0


def test_evidence_is_non_empty() -> None:
    g = SummaryQualityGrader()
    v = g.grade("alpha beta gamma extra words here", _TASK)
    assert len(v.evidence) >= 2
