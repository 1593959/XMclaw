"""local-coding suite — grade() actually runs pytest (ground truth).

No LLM: we simulate the agent by either leaving the buggy file (expect
FAIL) or writing the fixed file (expect PASS), then call grade().
"""
from __future__ import annotations

from pathlib import Path

from xmclaw.eval import SUITE_REGISTRY
from xmclaw.eval.local_coding import LocalCodingSuite


def test_registered() -> None:
    assert "local-coding" in SUITE_REGISTRY


def test_tasks_seed_real_files_with_failing_tests() -> None:
    suite = LocalCodingSuite()
    cases = suite.load_tasks()
    assert len(cases) >= 5
    for c in cases:
        wd = Path(c.expected_signals["workdir"])
        assert wd.is_dir()
        assert any(wd.glob("test_*.py"))


def test_grade_fails_on_unfixed_bug() -> None:
    suite = LocalCodingSuite()
    case = next(c for c in suite.load_tasks(limit=1))  # fix-sum-off-by-one
    # Agent did nothing — the seeded bug remains → pytest must fail.
    passed, score, meta = suite.grade(case, "I think the loop is wrong.")
    assert passed is False
    assert score == 0.0


def test_grade_passes_when_bug_is_fixed() -> None:
    suite = LocalCodingSuite()
    case = next(c for c in suite.load_tasks(limit=1))  # fix-sum-off-by-one
    wd = Path(case.expected_signals["workdir"])
    # Simulate a correct agent edit.
    (wd / "calc.py").write_text(
        "def sum_to(n):\n"
        "    total = 0\n"
        "    for i in range(1, n + 1):\n"
        "        total += i\n"
        "    return total\n",
        encoding="utf-8",
    )
    passed, score, meta = suite.grade(case, "fixed the range bound")
    assert passed is True
    assert score == 1.0
    assert meta["returncode"] == 0


def test_grade_cleans_up_workdir() -> None:
    suite = LocalCodingSuite()
    case = next(c for c in suite.load_tasks(limit=1))
    wd = Path(case.expected_signals["workdir"])
    assert wd.is_dir()
    suite.grade(case, "noop")
    assert not wd.exists()  # grade() removes the temp dir afterwards
