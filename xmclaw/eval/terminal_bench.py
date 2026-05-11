"""TerminalBench 2.0 real-corpus suite — pulls from HuggingFace.

This is the terminal-task agent benchmark sibling of
:mod:`xmclaw.eval.longmemeval_full`. Where LongMemEval probes long-context
recall, TerminalBench probes whether an agent can drive a Linux shell to
completion: write files, run commands, satisfy verification scripts.

Dataset: https://huggingface.co/datasets/laude-institute/terminal-bench.
Project home: https://www.tbench.ai/.

Two-tier grading
================

Mirrors the SWE-bench Verified tier split:

* **Tier 1 (heuristic)** — runs in-process, no Docker. Scans the
  agent's text for "completion signals" (test pass mention, exit-code-
  zero mention, file-write evidence). Approximate by construction —
  fast smoke / regression tests but NOT comparable to published
  TerminalBench leaderboard numbers.
* **Tier 2 (sandboxed)** — Sprint 4 follow-up to B-385. Spawns an
  ephemeral ``ubuntu:22.04`` container, replays the agent's bash, runs
  the row's verification ``tests``, reports per-test pass/fail. **This
  is the real TerminalBench number.** Lives in
  :mod:`xmclaw.eval.terminal_bench_sandbox`. Wire it in via
  :meth:`TerminalBenchSuite.set_sandboxed_grader` (or set
  ``XMC_TERMINAL_BENCH_GRADER=sandboxed`` for auto-wire).

**Honest disclosure** (do not delete; CI tests for this string):
Tier 1 grading is approximate; published TerminalBench numbers come
from Tier 2 sandboxed evaluation. **Do NOT use Tier 1 scores in
marketing** claims, release notes, or competitive comparisons — the
heuristic overestimates "did the agent claim to finish" relative to
the real per-test signal. Tier 1 is **deprecated for benchmark
publishing** as of Sprint 4 Tier-2 wire-up; it remains supported for
development-time smoke tests only.

Lazy-imports ``datasets`` *inside* ``load_tasks`` so the daemon (which
imports ``xmclaw.eval`` for SUITE_REGISTRY) never pays the import cost —
only ``xmclaw eval run terminal_bench`` does.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from xmclaw.eval.harness import BenchmarkSuite, TaskCase

if TYPE_CHECKING:
    pass


# Where we tell HuggingFace to cache the dataset. Using XMclaw's own
# workspace dir means a fresh laptop only fetches it once and keeps it
# isolated from the user's global ``~/.cache/huggingface``.
# Patch A (2026-05-10): backed by paths.eval_cache_dir() so
# ``XMC_DATA_DIR`` overrides reroute the cache.
def _hf_cache_dir() -> Path:
    from xmclaw.utils.paths import eval_cache_dir
    return eval_cache_dir("terminal_bench")


# Error hint when the optional extra isn't installed. Surfacing this from
# ``load_tasks`` (not at import time) means the daemon and ``xmclaw eval
# list`` keep working without the extra — only ``xmclaw eval run
# terminal_bench`` trips the install prompt.
_INSTALL_HINT = (
    "TerminalBench real-corpus suite requires the 'eval-hf' extra. "
    "Install with: pip install 'xmclaw[eval-hf]'"
)


# ── Heuristic grading constants ───────────────────────────────────────
# Patterns the agent's text might contain that signal *probable* task
# completion. None of these are sufficient on their own — production
# numbers need the sandboxed grader (B-385). We score by counting how
# many of these surface and clamping to [0, 1].

_TEST_PASS_PATTERNS = (
    r"\btests?\s+passed\b",
    r"\ball\s+tests?\s+pass(?:ed|ing)?\b",
    r"\bok\b.*\btests?\b",
    r"\b(\d+)\s+passed\b",
    r"\bpassed\s+(\d+)\s+tests?\b",
)

_EXIT_ZERO_PATTERNS = (
    r"\bexit\s+code\s*[:=]?\s*0\b",
    r"\breturned\s+0\b",
    r"\breturn\s+code\s*[:=]?\s*0\b",
    r"\$\?\s*[:=]?\s*0\b",
)

_FILE_WRITE_PATTERNS = (
    r"\bwrote\s+(?:to\s+)?(?:the\s+)?file\b",
    r"\bcreated\s+(?:the\s+)?file\b",
    r"\bsaved\s+(?:to\s+)?(?:the\s+)?file\b",
    # heredoc / cat-EOF style
    r"<<\s*['\"]?EOF['\"]?",
    # explicit common edit/write tool mentions
    r"\b(?:Write|Edit|fs_write)\b\s+tool",
)

_FAILURE_NEGATIVES = (
    r"\btests?\s+failed\b",
    r"\bfail(?:ure|ed)\b.*\btests?\b",
    r"\bexit\s+code\s*[:=]?\s*[1-9]\d*\b",
    r"\berror\s*:",
    r"\btraceback\b",
)


# Difficulty multipliers — harder tasks get more conservative scores
# from the heuristic, because completion-signal phrases are easier to
# game on simple tasks than on hard ones. The sandboxed grader (B-385)
# will not need this fudge factor.
_DIFFICULTY_MULTIPLIERS: dict[str, float] = {
    "easy": 1.0,
    "medium": 0.85,
    "hard": 0.7,
    "expert": 0.6,
    "unknown": 0.9,
}


class TerminalBenchSuite(BenchmarkSuite):
    """Real-corpus TerminalBench 2.0 suite — pulls from HuggingFace.

    The first ``load_tasks`` call downloads the dataset (cached under
    ``~/.xmclaw/v2/eval_cache/terminal_bench/``); subsequent calls are
    fast. ``limit`` is honoured before any TaskCase construction so a
    smoke run with ``--limit 5`` doesn't iterate the whole corpus.

    NOTE on grading: this class ships a HEURISTIC grader (see
    :meth:`grade`). It scans the agent's text for completion signals
    and never actually executes the verification tests in a sandbox.
    For production benchmark numbers, wait for the sandboxed grader
    follow-up (B-385) which will dispatch to
    ``xmclaw/providers/runtime/docker.py``. The heuristic is good
    enough to spot regressions and exercise the harness wiring; it is
    NOT comparable to published TerminalBench numbers.
    """

    SUITE_ID = "terminal_bench"
    UPSTREAM_DATASET = "laude-institute/terminal-bench"
    UPSTREAM_SPLIT = "test"

    @property
    def suite_id(self) -> str:
        return self.SUITE_ID

    def load_tasks(self, limit: int | None = None) -> list[TaskCase]:
        """Fetch the TerminalBench test split and convert each row to a
        ``TaskCase``.

        Lazy-imports ``datasets`` so missing-extra environments don't
        crash on ``import xmclaw.eval``.
        """
        if limit is not None and limit < 0:
            raise ValueError(f"limit must be non-negative, got {limit}")

        # Point HF at our cache dir BEFORE importing/calling. Setting the
        # env var also affects the underlying ``huggingface_hub`` client.
        _cache = _hf_cache_dir()
        _cache.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault("HF_DATASETS_CACHE", str(_cache))

        try:
            from datasets import load_dataset  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - exercised via mock
            raise ImportError(_INSTALL_HINT) from exc

        ds = load_dataset(
            self.UPSTREAM_DATASET,
            split=self.UPSTREAM_SPLIT,
            cache_dir=str(_cache),
        )

        cases: list[TaskCase] = []
        for idx, row in enumerate(ds):
            if limit is not None and len(cases) >= limit:
                break
            cases.append(self._row_to_case(idx, row))
        return cases

    @staticmethod
    def _row_to_case(idx: int, row: dict[str, Any]) -> TaskCase:
        """Convert one HuggingFace row to a ``TaskCase``.

        Pulled out so tests can construct cases directly without touching
        ``datasets``. Schema reference (from the dataset card / project
        repo at github.com/laude-institute/terminal-bench):

        * ``task_id``: stable string id
        * ``instruction``: the natural-language prompt for the agent
        * ``tests``: verification scripts/commands the sandboxed grader
          will eventually run; opaque blob to the heuristic grader
        * ``solution``: optional reference solution (not used at grade
          time — kept for debugging / future LLM-judge graders)
        * ``difficulty``: easy / medium / hard / expert / unknown
        * ``category``: e.g. file-systems / git / networking / general
        """
        task_id = str(row.get("task_id") or f"terminal_bench-{idx}")
        instruction = str(row.get("instruction") or "")

        prompt = (
            "You are working in a Linux terminal sandbox. Complete this task:\n\n"
            f"{instruction}\n\n"
            "Use bash tools as needed. When done, ensure the verification "
            "tests in /workspace/tests/ pass."
        )

        return TaskCase(
            task_id=task_id,
            prompt=prompt,
            expected_signals={
                "tests": row.get("tests") or [],
                "solution": str(row.get("solution") or ""),
            },
            metadata={
                "difficulty": str(row.get("difficulty") or "unknown"),
                "category": str(row.get("category") or "general"),
            },
        )

    def grade(
        self, case: TaskCase, agent_text: str, **extra: Any,
    ) -> tuple[bool, float, dict[str, Any]]:
        """Heuristic completion-signal grader. **APPROXIMATE — placeholder.**

        Scans the agent's text for typical "task done" phrases:

        * "tests passed" / "all tests passing" / "N passed"
        * "exit code 0" / "$? = 0" / "returned 0"
        * file-write evidence (Write/Edit tool mention, heredoc, "saved
          to file")

        Each category contributes one signal. We sum the signals,
        normalise by 3 (max categories), then multiply by a difficulty
        factor (harder tasks get more conservative scores — completion
        phrasing is cheaper to fake on easy problems than on hard
        ones). Failure phrases ("tests failed", "exit code 1",
        "traceback") zero the score out — even if completion phrases
        also appear, an explicit failure trumps optimism.

        Pass threshold: score >= 0.5. Empty agent text always fails.

        **For production benchmark numbers, wait for the sandboxed
        grader (TODO: wire to ``xmclaw/providers/runtime/docker.py`` —
        tracked as B-385). The current heuristic is suitable for
        spotting regressions and exercising the harness; it is NOT
        comparable to published TerminalBench leaderboard numbers.**
        """
        if not agent_text:
            return False, 0.0, {
                "matched": False,
                "reason": "empty agent text",
                "grader": "heuristic",
            }

        text_lower = agent_text.lower()

        # Failure trumps optimism: if the agent reports a failure,
        # don't let stray "passed" mentions rescue the score.
        for pat in _FAILURE_NEGATIVES:
            if re.search(pat, text_lower):
                return False, 0.0, {
                    "matched": False,
                    "reason": f"failure signal matched: {pat!r}",
                    "grader": "heuristic",
                }

        signals: list[str] = []
        if any(re.search(pat, text_lower) for pat in _TEST_PASS_PATTERNS):
            signals.append("tests_passed")
        if any(re.search(pat, text_lower) for pat in _EXIT_ZERO_PATTERNS):
            signals.append("exit_zero")
        if any(re.search(pat, text_lower) for pat in _FILE_WRITE_PATTERNS):
            signals.append("file_write")

        # 3 signal categories total.
        raw_score = len(signals) / 3.0

        difficulty = str(case.metadata.get("difficulty") or "unknown").lower()
        multiplier = _DIFFICULTY_MULTIPLIERS.get(
            difficulty, _DIFFICULTY_MULTIPLIERS["unknown"],
        )
        score = max(0.0, min(1.0, raw_score * multiplier))
        passed = score >= 0.5

        return passed, score, {
            "matched": passed,
            "signals": signals,
            "raw_score": raw_score,
            "difficulty": difficulty,
            "multiplier": multiplier,
            "grader": "heuristic",
            "note": (
                "Heuristic grader — approximate. For production benchmark "
                "numbers use the sandboxed grader (B-385: wire to "
                "xmclaw/providers/runtime/docker.py)."
            ),
        }


__all__ = ["TerminalBenchSuite"]
