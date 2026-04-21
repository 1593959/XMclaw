"""Phase 1 go/no-go: learning curve on read_and_summarize demo skill.

Criteria (V2_DEVELOPMENT.md §8.2):
  1. Grader mean score at turn 50 ≥ 120% of turn 1 (strictly monotonic in
     windowed mean over 50 turns).
  2. Grader ↔ human agreement ≥ 80% on a 50-example labeled sample.

If this fails, Phase 1 fails and we revisit the fallback path in
REWRITE_PLAN.md §10.
"""
from __future__ import annotations

import pytest


@pytest.mark.skip(reason="Phase 1 — implementation pending")
def test_learning_curve_monotonic() -> None:
    raise NotImplementedError


@pytest.mark.skip(reason="Phase 1 — implementation pending")
def test_grader_human_agreement() -> None:
    raise NotImplementedError
