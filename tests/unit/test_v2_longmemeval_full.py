"""Sprint 4 follow-up — LongMemEval real-corpus suite tests.

These tests exercise the HuggingFace-backed
:class:`xmclaw.eval.longmemeval_full.LongMemEvalSuite` without any
network traffic. ``datasets.load_dataset`` is mocked at every call site;
the only real I/O is creating the HF cache directory under
``~/.xmclaw/v2/eval_cache/longmemeval/`` (we redirect ``$HOME`` to a
tmp_path for tests that care about that side effect).

Coverage targets:

* Lazy-import contract — ``import xmclaw.eval`` must succeed without
  ``datasets`` installed; the error only surfaces inside ``load_tasks``.
* Schema mapping — ``question_id`` / ``question`` / ``answer`` /
  ``haystack_sessions`` / ``evidence_session_ids`` / ``question_type``
  all flow into the right ``TaskCase`` slots.
* Limit honoured before iterating the whole dataset.
* Grader: case-insensitive substring on the ``answer`` field; multi-word
  preserved; empty agent_text fails.
* Suite registers under ``"longmemeval"`` (NOT the same id as the mini
  suite, which keeps ``"longmemeval-mini"``).
* HF cache path is set to ``~/.xmclaw/v2/eval_cache/longmemeval/``.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import MagicMock

import pytest

from xmclaw.eval import SUITE_REGISTRY, LongMemEvalMiniSuite, LongMemEvalSuite
from xmclaw.eval.longmemeval_full import _flatten_sessions
from xmclaw.eval.harness import TaskCase


# ── Fake HF dataset rows used across tests ─────────────────────────────


def _row(
    *,
    question_id: str = "lme-001",
    question: str = "What is my dog's name?",
    answer: str = "Bowser",
    haystack_sessions: Any = None,
    evidence_session_ids: list[str] | None = None,
    question_type: str = "single-session-user",
) -> dict[str, Any]:
    return {
        "question_id": question_id,
        "question": question,
        "answer": answer,
        "haystack_sessions": haystack_sessions
        if haystack_sessions is not None
        else [
            [
                {"role": "user", "content": "My dog's name is Bowser."},
                {"role": "assistant", "content": "Got it."},
            ],
        ],
        "evidence_session_ids": evidence_session_ids or ["s0"],
        "question_type": question_type,
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


def test_longmemeval_full_suite_id():
    assert LongMemEvalSuite().suite_id == "longmemeval"


def test_longmemeval_full_registered_in_suite_registry():
    assert "longmemeval" in SUITE_REGISTRY
    assert SUITE_REGISTRY["longmemeval"] is LongMemEvalSuite
    # Mini suite is still under its own id and unchanged.
    assert SUITE_REGISTRY["longmemeval-mini"] is LongMemEvalMiniSuite
    assert LongMemEvalSuite.SUITE_ID != LongMemEvalMiniSuite.SUITE_ID


def test_longmemeval_full_dataset_constants():
    """Nail down the upstream pointers — if these change, callers
    using cached results may need to re-fetch."""
    assert LongMemEvalSuite.UPSTREAM_DATASET == "OpenMOSS/LongMemEval"
    assert LongMemEvalSuite.UPSTREAM_SPLIT == "test"


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

    suite = LongMemEvalSuite()
    with pytest.raises(ImportError) as exc_info:
        suite.load_tasks(limit=1)

    msg = str(exc_info.value)
    assert "xmclaw[eval-hf]" in msg, f"missing install hint: {msg}"
    assert "pip install" in msg


def test_module_imports_without_datasets(monkeypatch: pytest.MonkeyPatch):
    """``import xmclaw.eval`` must succeed even with datasets missing."""
    monkeypatch.delitem(sys.modules, "datasets", raising=False)
    # If the real module isn't available it's fine; we just need
    # importing the suite class to work.
    from xmclaw.eval import longmemeval_full  # noqa: F401

    # Accessing the class must not trigger any HF import either.
    assert LongMemEvalSuite.SUITE_ID == "longmemeval"


# ── load_tasks plumbing ────────────────────────────────────────────────


def test_load_tasks_calls_load_dataset_with_correct_args(
    monkeypatch: pytest.MonkeyPatch,
):
    fake = _install_fake_datasets(monkeypatch, [_row()])
    LongMemEvalSuite().load_tasks(limit=1)
    assert fake.call_count == 1
    call = fake.call_args
    # First positional arg: dataset id; ``split`` kwarg: "test".
    assert call.args[0] == "OpenMOSS/LongMemEval"
    assert call.kwargs.get("split") == "test"
    # cache_dir points into the HF cache path under the workspace.
    assert "longmemeval" in call.kwargs.get("cache_dir", "")


def test_load_tasks_returns_one_taskcase_per_row(monkeypatch: pytest.MonkeyPatch):
    rows = [
        _row(question_id="lme-001", answer="Paris"),
        _row(question_id="lme-002", answer="Berlin"),
        _row(question_id="lme-003", answer="Rome"),
    ]
    _install_fake_datasets(monkeypatch, rows)
    cases = LongMemEvalSuite().load_tasks()
    assert len(cases) == 3
    assert [c.task_id for c in cases] == ["lme-001", "lme-002", "lme-003"]


def test_load_tasks_taskcase_shape(monkeypatch: pytest.MonkeyPatch):
    _install_fake_datasets(monkeypatch, [_row()])
    cases = LongMemEvalSuite().load_tasks()
    case = cases[0]
    assert case.task_id == "lme-001"
    assert "What is my dog's name?" in case.prompt
    assert "Answer concisely" in case.prompt
    assert case.expected_signals["answer"] == "Bowser"
    assert case.expected_signals["evidence_session_ids"] == ["s0"]
    assert case.metadata["question_type"] == "single-session-user"


def test_load_tasks_falls_back_when_question_id_missing(
    monkeypatch: pytest.MonkeyPatch,
):
    """Some upstream rows may lack ``question_id``; we synthesise one
    from the row index so duplicate task ids don't break the runner."""
    row = _row()
    row.pop("question_id")
    _install_fake_datasets(monkeypatch, [row])
    cases = LongMemEvalSuite().load_tasks()
    assert cases[0].task_id == "longmemeval-0"


def test_load_tasks_respects_limit(monkeypatch: pytest.MonkeyPatch):
    rows = [_row(question_id=f"lme-{i:03d}") for i in range(10)]
    _install_fake_datasets(monkeypatch, rows)
    cases = LongMemEvalSuite().load_tasks(limit=3)
    assert len(cases) == 3
    assert [c.task_id for c in cases] == ["lme-000", "lme-001", "lme-002"]


def test_load_tasks_limit_zero_returns_no_cases(
    monkeypatch: pytest.MonkeyPatch,
):
    _install_fake_datasets(monkeypatch, [_row(), _row(question_id="x")])
    assert LongMemEvalSuite().load_tasks(limit=0) == []


def test_load_tasks_negative_limit_raises():
    with pytest.raises(ValueError):
        LongMemEvalSuite().load_tasks(limit=-1)


def test_load_tasks_sets_hf_cache_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    """``HF_DATASETS_CACHE`` is set so the dataset lands under
    ``~/.xmclaw/v2/eval_cache/longmemeval/`` on first use."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows analogue
    monkeypatch.delenv("HF_DATASETS_CACHE", raising=False)
    _install_fake_datasets(monkeypatch, [_row()])

    LongMemEvalSuite().load_tasks(limit=1)

    cache_value = __import__("os").environ.get("HF_DATASETS_CACHE", "")
    assert cache_value, "HF_DATASETS_CACHE was not set"
    assert "longmemeval" in cache_value
    assert "eval_cache" in cache_value


# ── Grader semantics ──────────────────────────────────────────────────


def _case(answer: str = "Paris") -> TaskCase:
    return TaskCase(
        task_id="t1",
        prompt="(prompt)",
        expected_signals={"answer": answer, "evidence_session_ids": []},
    )


def test_grade_case_insensitive_substring_passes():
    suite = LongMemEvalSuite()
    passed, score, meta = suite.grade(_case("Paris"), "The answer is paris.")
    assert passed is True
    assert score == 1.0
    assert meta["matched"] is True


def test_grade_case_insensitive_substring_fails():
    suite = LongMemEvalSuite()
    passed, score, meta = suite.grade(_case("Paris"), "It's London")
    assert passed is False
    assert score == 0.0
    assert meta["matched"] is False


def test_grade_preserves_multi_word_answers():
    suite = LongMemEvalSuite()
    case = _case("May 9th 2026")
    text = "Per the conversation, the meeting is on May 9th 2026 at 3pm."
    passed, score, _ = suite.grade(case, text)
    assert passed is True
    assert score == 1.0


def test_grade_partial_multiword_match_fails():
    """Substring is on the FULL answer string — partial overlap doesn't
    pass. (You can match 'May' alone but not the whole answer.)"""
    suite = LongMemEvalSuite()
    case = _case("May 9th 2026")
    passed, _, _ = suite.grade(case, "Sometime in May 2026.")
    assert passed is False


def test_grade_empty_agent_text_fails():
    suite = LongMemEvalSuite()
    passed, score, meta = suite.grade(_case("Bowser"), "")
    assert passed is False
    assert score == 0.0
    assert "empty" in (meta.get("reason") or "")


def test_grade_empty_ground_truth_does_not_silently_pass():
    """If upstream gave us a blank answer, refuse to call it a pass —
    every non-empty agent_text would 'contain' the empty string."""
    suite = LongMemEvalSuite()
    passed, score, meta = suite.grade(_case(""), "anything goes")
    assert passed is False
    assert score == 0.0
    assert "ground truth" in (meta.get("reason") or "").lower()


# ── Prompt construction ───────────────────────────────────────────────


def test_flatten_sessions_renders_role_and_content():
    sessions = [
        [
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello"},
        ],
        [
            {"role": "user", "content": "Bye"},
        ],
    ]
    rendered = _flatten_sessions(sessions)
    assert "Session 1:" in rendered
    assert "Session 2:" in rendered
    assert "User: Hi" in rendered
    assert "Assistant: Hello" in rendered
    assert "User: Bye" in rendered


def test_flatten_sessions_handles_empty_input():
    assert "no prior conversation" in _flatten_sessions([])


def test_load_tasks_prompt_includes_session_text(
    monkeypatch: pytest.MonkeyPatch,
):
    row = _row(
        haystack_sessions=[
            [
                {"role": "user", "content": "Bowser is my dog."},
                {"role": "assistant", "content": "Noted."},
            ],
        ],
    )
    _install_fake_datasets(monkeypatch, [row])
    case = LongMemEvalSuite().load_tasks()[0]
    assert "Bowser is my dog." in case.prompt
    assert "Session 1:" in case.prompt


# ── Defensive coverage ────────────────────────────────────────────────


def test_grade_with_non_string_answer_field():
    """If a buggy upstream row hands us a list/int, fail safe rather
    than crash the whole suite."""
    case = TaskCase(
        task_id="bad",
        prompt="(p)",
        expected_signals={"answer": ["not", "a", "string"]},
    )
    passed, score, meta = LongMemEvalSuite().grade(case, "anything")
    assert passed is False
    assert score == 0.0
    assert "error" in meta or "matched" in meta


def test_load_tasks_uses_iterable_dataset(monkeypatch: pytest.MonkeyPatch):
    """``load_dataset`` may return an iterable wrapper, not a list. The
    suite must still iterate it correctly."""
    rows = [_row(question_id="a"), _row(question_id="b")]

    class _IterableDataset:
        def __init__(self, items):
            self._items = items
        def __iter__(self):
            return iter(self._items)

    fake_load = MagicMock(return_value=_IterableDataset(rows))
    fake_module = ModuleType("datasets")
    fake_module.load_dataset = fake_load  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "datasets", fake_module)

    cases = LongMemEvalSuite().load_tasks()
    assert [c.task_id for c in cases] == ["a", "b"]
