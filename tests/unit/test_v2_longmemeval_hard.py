"""LongMemEval-Hard grader — deterministic, no LLM. Pins the abstain /
recency / multihop / distractor / all grading so the A/B signal is stable."""
from __future__ import annotations

from xmclaw.eval import SUITE_REGISTRY
from xmclaw.eval.longmemeval_hard import LongMemEvalHardSuite


def _case(suite, task_id):
    return next(c for c in suite.load_tasks() if c.task_id == task_id)


def test_registered() -> None:
    assert "longmemeval-hard" in SUITE_REGISTRY


def test_loads_all_fixtures() -> None:
    suite = LongMemEvalHardSuite()
    cases = suite.load_tasks()
    assert len(cases) >= 10
    assert suite.load_tasks(limit=3) == cases[:3]


def test_abstain_passes_on_idk_fails_on_fabrication() -> None:
    s = LongMemEvalHardSuite()
    c = _case(s, "hard-abstain-bloodtype")
    ok, score, _ = s.grade(c, "Your blood type isn't mentioned in the history.")
    assert ok and score == 1.0
    bad, bscore, meta = s.grade(c, "Your blood type is O+.")
    assert not bad and bscore == 0.0 and meta["fabricated"]


def test_recency_requires_latest_not_stale() -> None:
    s = LongMemEvalHardSuite()
    c = _case(s, "hard-recency-flight")
    ok, _, _ = s.grade(c, "Your flight is at 6pm.")
    assert ok
    stale, _, meta = s.grade(c, "Your flight is at 3pm.")
    assert not stale and meta["used_stale"]
    # Mentioning both the new and stale time is ambiguous → not a clean pass.
    both, _, _ = s.grade(c, "It was 3pm but moved to 6pm.")
    assert not both


def test_multihop_and_all_modes() -> None:
    s = LongMemEvalHardSuite()
    mh = _case(s, "hard-multihop-allergy")
    assert s.grade(mh, "Your sister is allergic to peanuts.")[0]
    allc = _case(s, "hard-all-meds")
    assert s.grade(allc, "metformin and lisinopril")[0]
    assert not s.grade(allc, "metformin")[0]  # missing one → fail


def test_distractor_rejects_wrong_candidate() -> None:
    s = LongMemEvalHardSuite()
    c = _case(s, "hard-distractor-birthplace")
    assert s.grade(c, "You were born in Lisbon.")[0]
    assert not s.grade(c, "You were born in Paris.")[0]
