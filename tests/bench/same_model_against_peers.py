"""Anti-req #11 bench: XMclaw shell must not make the model dumber.

50 tasks × {Anthropic Opus, OpenAI GPT-4o}. Compare:
  (a) naked provider SDK + minimal loop
  (b) XMclaw v2 agent on same provider

CI fails release if (b) mean score < (a) mean score × 0.95.

Phase 2 deliverable.
"""
from __future__ import annotations

import pytest


@pytest.mark.skip(reason="Phase 2 — needs working v2 agent loop")
def test_same_model_bench_vs_naked_sdk() -> None:
    raise NotImplementedError
