"""local-coding — a self-contained AGENTIC code-fix benchmark.

The recall suites (longmemeval-*) saturate at 100% on a capable model
because they're single-turn Q&A. To *drive loop tuning* you need a
benchmark with headroom that exercises the AGENTIC loop end-to-end:
read code → locate a bug → edit a file → (ideally) verify → iterate.

This is a local, dependency-free "mini SWE-bench": each task seeds a
throwaway temp repo with a buggy module + a pytest that fails until the
bug is fixed. The agent is told the paths and must FIX THE CODE using
its real tools (file_read / apply_patch / file_write). Grading is
ground truth — we actually run ``pytest`` against the dir the agent
edited; a task passes iff the test suite goes green. No Docker, no
HuggingFace, no network — runs anywhere the agent runs.

Why this is the right tuning signal:
  * It measures the loop's ability to ACT (call tools, edit files),
    not just talk — a loop that narrates a fix without editing scores 0.
  * It has a difficulty gradient (off-by-one → multi-file → subtle
    recursion) so a mediocre loop lands below 100%.
  * Grading is deterministic (test pass/fail), so A/B deltas are real.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from xmclaw.eval.harness import BenchmarkSuite, TaskCase


# Each task: id, difficulty, {filename: content}, the module the agent
# must fix, and a one-line description of the bug's symptom.
_TASKS: list[dict[str, Any]] = [
    {
        "id": "fix-sum-off-by-one",
        "difficulty": "easy",
        "fix_file": "calc.py",
        "files": {
            "calc.py": (
                "def sum_to(n):\n"
                "    # inclusive sum 1..n\n"
                "    total = 0\n"
                "    for i in range(1, n):\n"  # BUG: should be n+1
                "        total += i\n"
                "    return total\n"
            ),
            "test_calc.py": (
                "from calc import sum_to\n\n"
                "def test_sum_to():\n"
                "    assert sum_to(5) == 15\n"
                "    assert sum_to(1) == 1\n"
            ),
        },
        "symptom": "sum_to(5) returns 10, expected 15 (the loop excludes n).",
    },
    {
        "id": "fix-add-wrong-op",
        "difficulty": "easy",
        "fix_file": "ops.py",
        "files": {
            "ops.py": (
                "def add(a, b):\n"
                "    return a - b\n"  # BUG
            ),
            "test_ops.py": (
                "from ops import add\n\n"
                "def test_add():\n"
                "    assert add(2, 3) == 5\n"
                "    assert add(10, 0) == 10\n"
            ),
        },
        "symptom": "add() subtracts instead of adding.",
    },
    {
        "id": "fix-empty-list-edgecase",
        "difficulty": "medium",
        "fix_file": "stats.py",
        "files": {
            "stats.py": (
                "def average(nums):\n"
                "    return sum(nums) / len(nums)\n"  # BUG: ZeroDivisionError on []
            ),
            "test_stats.py": (
                "from stats import average\n\n"
                "def test_average():\n"
                "    assert average([2, 4, 6]) == 4\n"
                "    assert average([]) == 0\n"  # must guard empty
            ),
        },
        "symptom": "average([]) raises ZeroDivisionError; should return 0.",
    },
    {
        "id": "fix-fizzbuzz-order",
        "difficulty": "medium",
        "fix_file": "fizz.py",
        "files": {
            "fizz.py": (
                "def fizzbuzz(n):\n"
                "    if n % 3 == 0:\n"
                "        return 'Fizz'\n"
                "    if n % 5 == 0:\n"
                "        return 'Buzz'\n"
                "    if n % 15 == 0:\n"  # BUG: unreachable — 15 must be checked first
                "        return 'FizzBuzz'\n"
                "    return str(n)\n"
            ),
            "test_fizz.py": (
                "from fizz import fizzbuzz\n\n"
                "def test_fizzbuzz():\n"
                "    assert fizzbuzz(15) == 'FizzBuzz'\n"
                "    assert fizzbuzz(3) == 'Fizz'\n"
                "    assert fizzbuzz(5) == 'Buzz'\n"
                "    assert fizzbuzz(7) == '7'\n"
            ),
        },
        "symptom": "fizzbuzz(15) returns 'Fizz' — the n%15 branch is unreachable.",
    },
    {
        "id": "fix-multifile-discount",
        "difficulty": "hard",
        "fix_file": "(find it)",
        "files": {
            "pricing.py": (
                "from rates import discount_rate\n\n"
                "def final_price(price, tier):\n"
                "    return round(price * (1 - discount_rate(tier)), 2)\n"
            ),
            "rates.py": (
                "def discount_rate(tier):\n"
                "    # gold=20%, silver=10%, none=0%\n"
                "    if tier == 'gold':\n"
                "        return 0.10\n"  # BUG: gold should be 0.20
                "    if tier == 'silver':\n"
                "        return 0.10\n"
                "    return 0.0\n"
            ),
            "test_pricing.py": (
                "from pricing import final_price\n\n"
                "def test_final_price():\n"
                "    assert final_price(100, 'gold') == 80.0\n"
                "    assert final_price(100, 'silver') == 90.0\n"
                "    assert final_price(100, 'none') == 100.0\n"
            ),
        },
        "symptom": "gold tier is priced as 10% off; should be 20%. The bug is in a helper module, not pricing.py.",
    },
    {
        "id": "fix-recursion-basecase",
        "difficulty": "hard",
        "fix_file": "fact.py",
        "files": {
            "fact.py": (
                "def factorial(n):\n"
                "    if n == 1:\n"  # BUG: missing n==0 base case → factorial(0) recurses negative
                "        return 1\n"
                "    return n * factorial(n - 1)\n"
            ),
            "test_fact.py": (
                "from fact import factorial\n\n"
                "def test_factorial():\n"
                "    assert factorial(5) == 120\n"
                "    assert factorial(1) == 1\n"
                "    assert factorial(0) == 1\n"  # base case
            ),
        },
        "symptom": "factorial(0) never terminates / errors — the base case only handles n==1.",
    },
]


class LocalCodingSuite(BenchmarkSuite):
    """Self-contained agentic code-fix suite. Graded by running pytest."""

    SUITE_ID = "local-coding"

    @property
    def suite_id(self) -> str:
        return self.SUITE_ID

    def load_tasks(self, limit: int | None = None) -> list[TaskCase]:
        rows = _TASKS
        if limit is not None:
            if limit < 0:
                raise ValueError(f"limit must be non-negative, got {limit}")
            rows = rows[:limit]
        out: list[TaskCase] = []
        root = Path(tempfile.mkdtemp(prefix="xmc_localcode_"))
        for spec in rows:
            workdir = root / spec["id"]
            workdir.mkdir(parents=True, exist_ok=True)
            for name, content in spec["files"].items():
                (workdir / name).write_text(content, encoding="utf-8")
            test_files = [n for n in spec["files"] if n.startswith("test_")]
            src_files = [n for n in spec["files"] if not n.startswith("test_")]
            prompt = (
                f"You are working in the directory:\n  {workdir}\n\n"
                f"It is a small Python project with these files: "
                f"{', '.join(sorted(spec['files']))}.\n"
                f"Running the tests ({', '.join(test_files)}) currently FAILS.\n"
                f"Symptom: {spec['symptom']}\n\n"
                f"Fix the bug by editing the source file(s) ({', '.join(src_files)}) "
                f"so the tests pass. Use your file tools (file_read, then "
                f"apply_patch or file_write) to actually edit the files on disk. "
                f"Do NOT edit the test files. Do not just describe the fix — "
                f"make the edit.\n"
            )
            out.append(TaskCase(
                task_id=spec["id"],
                prompt=prompt,
                expected_signals={"workdir": str(workdir)},
                metadata={
                    "difficulty": spec["difficulty"],
                    "workdir": str(workdir),
                },
            ))
        return out

    def grade(
        self, case: TaskCase, agent_text: str, **extra: Any,
    ) -> tuple[bool, float, dict[str, Any]]:
        """Ground truth: run pytest in the workdir the agent edited. Pass
        iff every test passes (exit code 0)."""
        workdir = case.expected_signals.get("workdir")
        if not workdir or not Path(workdir).is_dir():
            return False, 0.0, {"error": "workdir missing — task setup lost"}
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", str(workdir)],
                cwd=str(workdir),
                capture_output=True,
                text=True,
                timeout=120,
            )
            passed = proc.returncode == 0
            tail = (proc.stdout or proc.stderr or "").strip().splitlines()[-3:]
            meta = {
                "difficulty": case.metadata.get("difficulty"),
                "returncode": proc.returncode,
                "pytest_tail": " | ".join(tail),
            }
            return passed, (1.0 if passed else 0.0), meta
        except subprocess.TimeoutExpired:
            return False, 0.0, {"error": "pytest timed out"}
        except Exception as exc:  # noqa: BLE001
            return False, 0.0, {"error": f"grader run failed: {exc}"}
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


__all__ = ["LocalCodingSuite"]
