"""Sprint 4 follow-up — TerminalBench 2.0 real-corpus suite tests.

These tests exercise the HuggingFace-backed
:class:`xmclaw.eval.terminal_bench.TerminalBenchSuite` without any
network traffic. ``datasets.load_dataset`` is mocked at every call site;
the only real I/O is creating the HF cache directory under
``~/.xmclaw/v2/eval_cache/terminal_bench/`` (we redirect ``$HOME`` to a
tmp_path for tests that care about that side effect).

Coverage targets:

* Lazy-import contract — ``import xmclaw.eval`` must succeed without
  ``datasets`` installed; the error only surfaces inside ``load_tasks``
  with the install hint.
* Schema mapping — ``task_id`` / ``instruction`` / ``tests`` /
  ``solution`` / ``difficulty`` / ``category`` all flow into the right
  ``TaskCase`` slots.
* Limit honoured before iterating the whole dataset.
* Heuristic grader: passes on completion-signal mentions, fails on
  empty/silent text, fails when an explicit failure phrase is present,
  conservative-multiplier on harder difficulties.
* Suite registers under ``"terminal_bench"`` (independent of the
  longmemeval ids).
* HF cache path is set to ``~/.xmclaw/v2/eval_cache/terminal_bench/``.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import MagicMock

import pytest

from xmclaw.eval import (
    SUITE_REGISTRY,
    LongMemEvalMiniSuite,
    LongMemEvalSuite,
    TerminalBenchSuite,
)
from xmclaw.eval.harness import TaskCase


# ── Fake HF dataset rows used across tests ─────────────────────────────


def _row(
    *,
    task_id: str = "tb-001",
    instruction: str = "Create a file named hello.txt containing 'world'.",
    tests: Any = None,
    solution: str = "echo world > hello.txt",
    difficulty: str = "easy",
    category: str = "file-systems",
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "instruction": instruction,
        "tests": tests if tests is not None else [
            "test -f hello.txt",
            "grep -q world hello.txt",
        ],
        "solution": solution,
        "difficulty": difficulty,
        "category": category,
    }


def _install_fake_datasets(
    monkeypatch: pytest.MonkeyPatch, rows: list[dict[str, Any]],
) -> MagicMock:
    """Install a fake ``datasets`` module with ``load_dataset`` returning
    the given rows. Returns the load_dataset mock so tests can assert on
    call args.
    """
    fake_load_dataset = MagicMock(return_value=rows)
    fake_module = ModuleType("datasets")
    fake_module.load_dataset = fake_load_dataset  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "datasets", fake_module)
    return fake_load_dataset


# ── Suite identity & registry ──────────────────────────────────────────


def test_terminal_bench_suite_id():
    assert TerminalBenchSuite().suite_id == "terminal_bench"


def test_terminal_bench_registered_in_suite_registry():
    assert "terminal_bench" in SUITE_REGISTRY
    assert SUITE_REGISTRY["terminal_bench"] is TerminalBenchSuite
    # Sibling suites are still under their own ids and unchanged.
    assert SUITE_REGISTRY["longmemeval-mini"] is LongMemEvalMiniSuite
    assert SUITE_REGISTRY["longmemeval"] is LongMemEvalSuite
    assert TerminalBenchSuite.SUITE_ID != LongMemEvalSuite.SUITE_ID
    assert TerminalBenchSuite.SUITE_ID != LongMemEvalMiniSuite.SUITE_ID


def test_terminal_bench_dataset_constants():
    """Nail down the upstream pointers — if these change, callers
    using cached results may need to re-fetch."""
    assert TerminalBenchSuite.UPSTREAM_DATASET == "laude-institute/terminal-bench"
    assert TerminalBenchSuite.UPSTREAM_SPLIT == "test"


# ── Lazy-import contract ──────────────────────────────────────────────


def test_load_tasks_raises_clear_error_when_datasets_missing(
    monkeypatch: pytest.MonkeyPatch,
):
    """If ``datasets`` isn't installed, the suite must raise ImportError
    with the install hint — and only inside ``load_tasks``, not at
    module import."""
    # Make sure we're not relying on a stub left over from a prior test.
    monkeypatch.delitem(sys.modules, "datasets", raising=False)

    real_import = __import__

    def _fail_for_datasets(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "datasets" or name.startswith("datasets."):
            raise ImportError("No module named 'datasets'")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr("builtins.__import__", _fail_for_datasets)

    suite = TerminalBenchSuite()
    with pytest.raises(ImportError) as exc_info:
        suite.load_tasks(limit=1)

    msg = str(exc_info.value)
    assert "xmclaw[eval-hf]" in msg, f"missing install hint: {msg}"
    assert "pip install" in msg


def test_module_imports_without_datasets(monkeypatch: pytest.MonkeyPatch):
    """``import xmclaw.eval`` must succeed even with datasets missing."""
    monkeypatch.delitem(sys.modules, "datasets", raising=False)
    # Importing the suite class must not trigger any HF import.
    from xmclaw.eval import terminal_bench  # noqa: F401

    assert TerminalBenchSuite.SUITE_ID == "terminal_bench"


# ── load_tasks plumbing ────────────────────────────────────────────────


def test_load_tasks_calls_load_dataset_with_correct_args(
    monkeypatch: pytest.MonkeyPatch,
):
    fake = _install_fake_datasets(monkeypatch, [_row()])
    TerminalBenchSuite().load_tasks(limit=1)
    assert fake.call_count == 1
    call = fake.call_args
    # First positional arg: dataset id; ``split`` kwarg: "test".
    assert call.args[0] == "laude-institute/terminal-bench"
    assert call.kwargs.get("split") == "test"
    # cache_dir points into the HF cache path under the workspace.
    assert "terminal_bench" in call.kwargs.get("cache_dir", "")


def test_load_tasks_returns_one_taskcase_per_row(monkeypatch: pytest.MonkeyPatch):
    rows = [
        _row(task_id="tb-001"),
        _row(task_id="tb-002"),
        _row(task_id="tb-003"),
    ]
    _install_fake_datasets(monkeypatch, rows)
    cases = TerminalBenchSuite().load_tasks()
    assert len(cases) == 3
    assert [c.task_id for c in cases] == ["tb-001", "tb-002", "tb-003"]


def test_load_tasks_taskcase_shape(monkeypatch: pytest.MonkeyPatch):
    _install_fake_datasets(monkeypatch, [_row()])
    cases = TerminalBenchSuite().load_tasks()
    case = cases[0]
    assert case.task_id == "tb-001"
    # Prompt mentions the instruction + the sandbox preamble.
    assert "Linux terminal sandbox" in case.prompt
    assert "Create a file named hello.txt" in case.prompt
    assert "/workspace/tests/" in case.prompt
    # expected_signals carry the verification + reference solution.
    assert case.expected_signals["tests"] == [
        "test -f hello.txt",
        "grep -q world hello.txt",
    ]
    assert case.expected_signals["solution"] == "echo world > hello.txt"
    # metadata carries difficulty + category.
    assert case.metadata["difficulty"] == "easy"
    assert case.metadata["category"] == "file-systems"


def test_load_tasks_falls_back_when_task_id_missing(
    monkeypatch: pytest.MonkeyPatch,
):
    """Some upstream rows may lack ``task_id``; we synthesise one from
    the row index so duplicate task ids don't break the runner."""
    row = _row()
    row.pop("task_id")
    _install_fake_datasets(monkeypatch, [row])
    cases = TerminalBenchSuite().load_tasks()
    assert cases[0].task_id == "terminal_bench-0"


def test_load_tasks_handles_missing_optional_fields(
    monkeypatch: pytest.MonkeyPatch,
):
    """Rows without ``solution`` / ``difficulty`` / ``category`` get
    safe defaults — heuristic grader still works."""
    row = _row()
    row.pop("solution")
    row.pop("difficulty")
    row.pop("category")
    _install_fake_datasets(monkeypatch, [row])
    case = TerminalBenchSuite().load_tasks()[0]
    assert case.expected_signals["solution"] == ""
    assert case.metadata["difficulty"] == "unknown"
    assert case.metadata["category"] == "general"


def test_load_tasks_respects_limit(monkeypatch: pytest.MonkeyPatch):
    rows = [_row(task_id=f"tb-{i:03d}") for i in range(10)]
    _install_fake_datasets(monkeypatch, rows)
    cases = TerminalBenchSuite().load_tasks(limit=3)
    assert len(cases) == 3
    assert [c.task_id for c in cases] == ["tb-000", "tb-001", "tb-002"]


def test_load_tasks_limit_zero_returns_no_cases(
    monkeypatch: pytest.MonkeyPatch,
):
    _install_fake_datasets(monkeypatch, [_row(), _row(task_id="x")])
    assert TerminalBenchSuite().load_tasks(limit=0) == []


def test_load_tasks_negative_limit_raises():
    with pytest.raises(ValueError):
        TerminalBenchSuite().load_tasks(limit=-1)


def test_load_tasks_sets_hf_cache_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    """``HF_DATASETS_CACHE`` is set so the dataset lands under
    ``~/.xmclaw/v2/eval_cache/terminal_bench/`` on first use."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows analogue
    monkeypatch.delenv("HF_DATASETS_CACHE", raising=False)
    _install_fake_datasets(monkeypatch, [_row()])

    TerminalBenchSuite().load_tasks(limit=1)

    cache_value = __import__("os").environ.get("HF_DATASETS_CACHE", "")
    assert cache_value, "HF_DATASETS_CACHE was not set"
    assert "terminal_bench" in cache_value
    assert "eval_cache" in cache_value


def test_load_tasks_uses_iterable_dataset(monkeypatch: pytest.MonkeyPatch):
    """``load_dataset`` may return an iterable wrapper, not a list."""
    rows = [_row(task_id="a"), _row(task_id="b")]

    class _IterableDataset:
        def __init__(self, items):
            self._items = items
        def __iter__(self):
            return iter(self._items)

    fake_load = MagicMock(return_value=_IterableDataset(rows))
    fake_module = ModuleType("datasets")
    fake_module.load_dataset = fake_load  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "datasets", fake_module)

    cases = TerminalBenchSuite().load_tasks()
    assert [c.task_id for c in cases] == ["a", "b"]


# ── Heuristic grader ──────────────────────────────────────────────────


def _case(difficulty: str = "easy") -> TaskCase:
    return TaskCase(
        task_id="t1",
        prompt="(prompt)",
        expected_signals={
            "tests": ["test -f /tmp/x"],
            "solution": "touch /tmp/x",
        },
        metadata={"difficulty": difficulty, "category": "file-systems"},
    )


def test_grade_passes_on_strong_completion_signals():
    """Tests-passed + exit-zero + file-write all in one response → pass."""
    suite = TerminalBenchSuite()
    text = (
        "I created the file using the Write tool, then ran the tests.\n"
        "All tests passed.\n"
        "Exit code: 0.\n"
    )
    passed, score, meta = suite.grade(_case("easy"), text)
    assert passed is True
    assert score > 0.0
    assert "tests_passed" in meta["signals"]
    assert "exit_zero" in meta["signals"]
    assert "file_write" in meta["signals"]
    assert meta["grader"] == "heuristic"


def test_grade_fails_on_no_completion_signals():
    """Generic chatter that mentions nothing concrete → fail with score 0."""
    suite = TerminalBenchSuite()
    passed, score, meta = suite.grade(
        _case("easy"), "I'm thinking about the problem now."
    )
    assert passed is False
    assert score == 0.0
    assert meta["signals"] == []


def test_grade_fails_on_empty_text():
    suite = TerminalBenchSuite()
    passed, score, meta = suite.grade(_case("easy"), "")
    assert passed is False
    assert score == 0.0
    assert "empty" in (meta.get("reason") or "")


def test_grade_failure_phrase_zeroes_score():
    """An explicit 'tests failed' / 'traceback' / 'exit code 1' beats
    any optimistic 'passed' chatter elsewhere."""
    suite = TerminalBenchSuite()
    text = (
        "I wrote the file. Ran the tests. Some tests passed.\n"
        "But finally: exit code 1.\n"
    )
    passed, score, meta = suite.grade(_case("easy"), text)
    assert passed is False
    assert score == 0.0
    assert "failure signal" in meta["reason"]


def test_grade_difficulty_multiplier_makes_hard_more_conservative():
    """Same agent text on an 'easy' task scores higher than on a 'hard'
    one — heuristics are cheaper to game on simple tasks."""
    suite = TerminalBenchSuite()
    # Hit all 3 signal categories.
    text = (
        "Wrote the file via the Edit tool.\n"
        "Tests passed. Exit code 0.\n"
    )
    _, easy_score, easy_meta = suite.grade(_case("easy"), text)
    _, hard_score, hard_meta = suite.grade(_case("hard"), text)
    assert easy_score > hard_score
    assert easy_meta["multiplier"] > hard_meta["multiplier"]
    assert hard_meta["difficulty"] == "hard"


def test_grade_unknown_difficulty_falls_back():
    """A row with no ``difficulty`` field goes through the unknown path
    rather than crashing."""
    suite = TerminalBenchSuite()
    case = TaskCase(
        task_id="t1",
        prompt="(p)",
        expected_signals={"tests": [], "solution": ""},
        metadata={},  # no difficulty key
    )
    text = "Wrote the file. Tests passed. Exit code 0."
    passed, score, meta = suite.grade(case, text)
    assert score > 0.0
    assert meta["difficulty"] == "unknown"
    assert passed is True


def test_grade_partial_signals_below_threshold():
    """One signal alone (file-write only) shouldn't pass the 0.5
    threshold — partial credit, but not a pass."""
    suite = TerminalBenchSuite()
    text = "I wrote the file. Now thinking about next steps."
    passed, score, meta = suite.grade(_case("easy"), text)
    # 1 signal / 3 = 0.333... * 1.0 (easy) ≈ 0.33 — below 0.5
    assert passed is False
    assert 0.0 < score < 0.5
    assert "file_write" in meta["signals"]


def test_grade_metadata_documents_grader_kind():
    """Heuristic disclosure must surface in the metadata so result-
    consumers know the score isn't sandbox-grade."""
    suite = TerminalBenchSuite()
    _, _, meta = suite.grade(
        _case("medium"), "Tests passed; exit code 0; wrote the file."
    )
    assert meta["grader"] == "heuristic"
    assert "B-385" in meta["note"]  # references the follow-up ticket
