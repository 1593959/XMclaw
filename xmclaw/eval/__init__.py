"""Sprint 4 — A/B benchmark harness.

Provides the framework for running an XMclaw agent against a benchmark
suite (LongMemEval / TerminalBench / SWE-bench Verified subset) and
producing comparable A/B numbers. Real dataset adapters are stubbed —
this commit ships the harness skeleton + a hand-coded LongMemEval
mini-suite so the CLI surface is exercisable end-to-end without any
network or HuggingFace dependency.

See ``docs/EVOLUTION_HONEST_STATE.md`` "Sprint 4: publish numbers".

Modules:
  * ``harness`` — TaskCase / TaskResult / SuiteResult dataclasses,
    the BenchmarkSuite ABC, and the Runner that orchestrates a suite
    against an agent factory.
  * ``longmemeval`` — concrete BenchmarkSuite with 7 hand-coded
    multi-turn dialogue recall tasks (no dataset download).

Layering: ``xmclaw/eval/`` may import ``xmclaw.core``,
``xmclaw.providers``, ``xmclaw.utils``. It must NOT import
``xmclaw.daemon`` (the daemon is the long-running runtime; eval is a
batch one-shot tool that constructs its own throwaway agent context).
"""
from __future__ import annotations

from xmclaw.eval.harness import (
    BenchmarkSuite,
    Runner,
    SuiteResult,
    TaskCase,
    TaskResult,
)
from xmclaw.eval.longmemeval import LongMemEvalMiniSuite
from xmclaw.eval.longmemeval_full import LongMemEvalSuite

# Public registry of suites the CLI can list / run by id. Keep this
# small and explicit — adding a real LongMemEval / SWE-bench / Terminal
# Bench adapter later means appending a new entry, not touching the CLI.
SUITE_REGISTRY: dict[str, type[BenchmarkSuite]] = {
    LongMemEvalMiniSuite.SUITE_ID: LongMemEvalMiniSuite,
    LongMemEvalSuite.SUITE_ID: LongMemEvalSuite,
}


__all__ = [
    "BenchmarkSuite",
    "LongMemEvalMiniSuite",
    "LongMemEvalSuite",
    "Runner",
    "SUITE_REGISTRY",
    "SuiteResult",
    "TaskCase",
    "TaskResult",
]
