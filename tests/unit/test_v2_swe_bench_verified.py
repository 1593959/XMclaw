"""Sprint 4 follow-up — SWE-bench Verified suite tests.

These tests exercise the HuggingFace-backed
:class:`xmclaw.eval.swe_bench_verified.SWEBenchVerifiedSuite` without
any network traffic. ``datasets.load_dataset`` is mocked at every call
site; the only real I/O is creating the HF cache directory under
``~/.xmclaw/v2/eval_cache/swe_bench_verified/`` (we redirect ``$HOME``
to a tmp_path for tests that care about that side effect).

Coverage targets:

* Lazy-import contract — ``import xmclaw.eval`` must succeed without
  ``datasets`` installed; ImportError surfaces only inside ``load_tasks``.
* Schema mapping — ``instance_id`` / ``repo`` / ``base_commit`` /
  ``problem_statement`` / ``patch`` / ``test_patch`` /
  ``FAIL_TO_PASS`` / ``PASS_TO_PASS`` all flow into the right
  ``TaskCase`` slots.
* Limit honoured before iterating the whole dataset.
* Tier 1 grader — accepts unified diffs that touch test_patch files;
  rejects diffless / malformed / empty / off-target outputs.
* Tier 2 grader — raises NotImplementedError with B-385 hint.
* Suite registers under ``"swe_bench_verified"`` in SUITE_REGISTRY.
* HF cache path is set to ``~/.xmclaw/v2/eval_cache/swe_bench_verified/``.
* **Honest disclosure** — the suite docstring contains the marketing
  warning, so a grep-style audit can keep the contract intact.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import MagicMock

import pytest

from xmclaw.eval import SUITE_REGISTRY, SWEBenchVerifiedSuite
from xmclaw.eval.harness import TaskCase
from xmclaw.eval.swe_bench_verified import _extract_diff_files


# ── Fake HF dataset rows used across tests ─────────────────────────────


_TEST_PATCH = (
    "diff --git a/django/db/models/sql/compiler.py b/django/db/models/sql/compiler.py\n"
    "--- a/django/db/models/sql/compiler.py\n"
    "+++ b/django/db/models/sql/compiler.py\n"
    "@@ -100,6 +100,7 @@ class SQLCompiler:\n"
    "     def compile(self, node):\n"
    "+        # new line\n"
    "         return node\n"
)


_GOLD_PATCH = (
    "diff --git a/django/db/models/sql/compiler.py b/django/db/models/sql/compiler.py\n"
    "--- a/django/db/models/sql/compiler.py\n"
    "+++ b/django/db/models/sql/compiler.py\n"
    "@@ -50,6 +50,7 @@\n"
    "     def some_fix(self):\n"
    "+        return True\n"
)


def _row(
    *,
    instance_id: str = "django__django-12345",
    repo: str = "django/django",
    base_commit: str = "abc123def456",
    problem_statement: str = "QuerySet.compile() drops a clause when ...",
    test_patch: str = _TEST_PATCH,
    patch: str = _GOLD_PATCH,
    fail_to_pass: list[str] | None = None,
    pass_to_pass: list[str] | None = None,
    hints_text: str = "",
) -> dict[str, Any]:
    return {
        "instance_id": instance_id,
        "repo": repo,
        "base_commit": base_commit,
        "problem_statement": problem_statement,
        "hints_text": hints_text,
        "test_patch": test_patch,
        "patch": patch,
        "FAIL_TO_PASS": fail_to_pass
        or ["tests/test_compiler.py::test_compile_clause"],
        "PASS_TO_PASS": pass_to_pass
        or ["tests/test_compiler.py::test_basic_compile"],
    }


def _install_fake_datasets(
    monkeypatch: pytest.MonkeyPatch, rows: list[dict[str, Any]],
) -> MagicMock:
    """Install a fake ``datasets`` module with ``load_dataset`` returning
    the given rows. Returns the load_dataset mock so tests can assert
    on call args.
    """
    fake_load_dataset = MagicMock(return_value=rows)
    fake_module = ModuleType("datasets")
    fake_module.load_dataset = fake_load_dataset  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "datasets", fake_module)
    return fake_load_dataset


# ── Suite identity & registry ──────────────────────────────────────────


def test_swe_bench_verified_suite_id():
    assert SWEBenchVerifiedSuite().suite_id == "swe_bench_verified"


def test_swe_bench_verified_registered_in_suite_registry():
    assert "swe_bench_verified" in SUITE_REGISTRY
    assert SUITE_REGISTRY["swe_bench_verified"] is SWEBenchVerifiedSuite


def test_swe_bench_verified_dataset_constants():
    """Nail down the upstream pointers — if these change, callers
    using cached results may need to re-fetch."""
    assert (
        SWEBenchVerifiedSuite.UPSTREAM_DATASET
        == "princeton-nlp/SWE-bench_Verified"
    )
    assert SWEBenchVerifiedSuite.UPSTREAM_SPLIT == "test"


# ── Honest-disclosure contract ─────────────────────────────────────────


def test_module_docstring_carries_marketing_warning():
    """The honest-disclosure phrase must be present in the suite's
    docstring so a grep-based audit catches accidental deletion."""
    import xmclaw.eval.swe_bench_verified as mod

    docstring = (mod.__doc__ or "") + (SWEBenchVerifiedSuite.__doc__ or "")
    assert "Do NOT use Tier 1 scores in marketing" in docstring, (
        "honest-disclosure phrase missing — see swe_bench_verified.py "
        "module/class docstrings"
    )


# ── Lazy-import contract ──────────────────────────────────────────────


def test_load_tasks_raises_clear_error_when_datasets_missing(
    monkeypatch: pytest.MonkeyPatch,
):
    """If ``datasets`` isn't installed, the suite must raise ImportError
    with the install hint — and only inside ``load_tasks``, not at
    module import."""
    monkeypatch.delitem(sys.modules, "datasets", raising=False)

    real_import = __import__

    def _fail_for_datasets(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "datasets" or name.startswith("datasets."):
            raise ImportError("No module named 'datasets'")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr("builtins.__import__", _fail_for_datasets)

    suite = SWEBenchVerifiedSuite()
    with pytest.raises(ImportError) as exc_info:
        suite.load_tasks(limit=1)

    msg = str(exc_info.value)
    assert "xmclaw[eval-hf]" in msg, f"missing install hint: {msg}"
    assert "pip install" in msg


def test_module_imports_without_datasets(monkeypatch: pytest.MonkeyPatch):
    """``import xmclaw.eval`` must succeed even with datasets missing."""
    monkeypatch.delitem(sys.modules, "datasets", raising=False)
    from xmclaw.eval import swe_bench_verified  # noqa: F401

    # Accessing the class must not trigger any HF import either.
    assert SWEBenchVerifiedSuite.SUITE_ID == "swe_bench_verified"


# ── load_tasks plumbing ────────────────────────────────────────────────


def test_load_tasks_calls_load_dataset_with_correct_args(
    monkeypatch: pytest.MonkeyPatch,
):
    fake = _install_fake_datasets(monkeypatch, [_row()])
    SWEBenchVerifiedSuite().load_tasks(limit=1)
    assert fake.call_count == 1
    call = fake.call_args
    assert call.args[0] == "princeton-nlp/SWE-bench_Verified"
    assert call.kwargs.get("split") == "test"
    assert "swe_bench_verified" in call.kwargs.get("cache_dir", "")


def test_load_tasks_taskcase_shape(monkeypatch: pytest.MonkeyPatch):
    _install_fake_datasets(monkeypatch, [_row()])
    cases = SWEBenchVerifiedSuite().load_tasks()
    assert len(cases) == 1
    case = cases[0]
    # task_id == instance_id (carries repo prefix as upstream provides).
    assert case.task_id == "django__django-12345"
    assert case.task_id.startswith("django__django")

    # Prompt must contain the issue body and the unified-diff cue.
    assert "QuerySet.compile() drops a clause" in case.prompt
    assert "unified diff" in case.prompt.lower()
    assert "django/django" in case.prompt
    assert "abc123def456" in case.prompt

    # expected_signals carries the full grading context.
    es = case.expected_signals
    assert es["gold_patch"] == _GOLD_PATCH
    assert es["test_patch"] == _TEST_PATCH
    assert es["fail_to_pass"] == [
        "tests/test_compiler.py::test_compile_clause"
    ]
    assert es["pass_to_pass"] == [
        "tests/test_compiler.py::test_basic_compile"
    ]

    # metadata carries the repo + base_commit for sandboxed Tier 2.
    assert case.metadata["repo"] == "django/django"
    assert case.metadata["base_commit"] == "abc123def456"


def test_load_tasks_respects_limit(monkeypatch: pytest.MonkeyPatch):
    rows = [_row(instance_id=f"repo__repo-{i:04d}") for i in range(20)]
    _install_fake_datasets(monkeypatch, rows)
    cases = SWEBenchVerifiedSuite().load_tasks(limit=5)
    assert len(cases) == 5
    assert [c.task_id for c in cases] == [
        f"repo__repo-{i:04d}" for i in range(5)
    ]


def test_load_tasks_negative_limit_raises():
    with pytest.raises(ValueError):
        SWEBenchVerifiedSuite().load_tasks(limit=-1)


def test_load_tasks_falls_back_when_instance_id_missing(
    monkeypatch: pytest.MonkeyPatch,
):
    row = _row()
    row.pop("instance_id")
    _install_fake_datasets(monkeypatch, [row])
    cases = SWEBenchVerifiedSuite().load_tasks()
    assert cases[0].task_id == "swe-0"


def test_load_tasks_sets_hf_cache_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    """``HF_DATASETS_CACHE`` is set so the dataset lands under
    ``~/.xmclaw/v2/eval_cache/swe_bench_verified/`` on first use."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows analogue
    monkeypatch.delenv("HF_DATASETS_CACHE", raising=False)
    _install_fake_datasets(monkeypatch, [_row()])

    SWEBenchVerifiedSuite().load_tasks(limit=1)

    cache_value = __import__("os").environ.get("HF_DATASETS_CACHE", "")
    assert cache_value, "HF_DATASETS_CACHE was not set"
    assert "swe_bench_verified" in cache_value
    assert "eval_cache" in cache_value


# ── Tier 1 grader ──────────────────────────────────────────────────────


def _case(
    *,
    test_patch: str = _TEST_PATCH,
    gold_patch: str = _GOLD_PATCH,
) -> TaskCase:
    return TaskCase(
        task_id="django__django-12345",
        prompt="(prompt)",
        expected_signals={
            "gold_patch": gold_patch,
            "fail_to_pass": ["tests/test_compiler.py::test_compile_clause"],
            "pass_to_pass": ["tests/test_compiler.py::test_basic_compile"],
            "test_patch": test_patch,
        },
        metadata={
            "repo": "django/django",
            "base_commit": "abc123def456",
        },
    )


def test_tier1_grade_valid_diff_touching_test_files_passes():
    """Agent emits a unified diff that modifies a file present in the
    ground-truth ``test_patch`` → Tier 1 marks it as passed."""
    suite = SWEBenchVerifiedSuite()
    agent_text = (
        "diff --git a/django/db/models/sql/compiler.py "
        "b/django/db/models/sql/compiler.py\n"
        "--- a/django/db/models/sql/compiler.py\n"
        "+++ b/django/db/models/sql/compiler.py\n"
        "@@ -100,6 +100,7 @@ class SQLCompiler:\n"
        "     def compile(self, node):\n"
        "+        node = self._fix(node)\n"
        "         return node\n"
    )
    passed, score, meta = suite.grade(_case(), agent_text)
    assert passed is True
    assert score == 1.0
    assert meta["tier"] == 1
    assert "django/db/models/sql/compiler.py" in meta["matched_files"]


def test_tier1_grade_no_diff_in_text_fails():
    """Plain prose (no diff headers) fails."""
    suite = SWEBenchVerifiedSuite()
    passed, score, meta = suite.grade(
        _case(), "I think the bug is in compiler.py — you should fix it."
    )
    assert passed is False
    assert score == 0.0
    assert meta["tier"] == 1
    assert "no parseable" in meta["reason"]


def test_tier1_grade_malformed_diff_fails():
    """Diff headers without hunks (no ``@@``) is malformed."""
    suite = SWEBenchVerifiedSuite()
    agent_text = (
        "--- a/django/db/models/sql/compiler.py\n"
        "+++ b/django/db/models/sql/compiler.py\n"
        "(no hunks here, just a header)\n"
    )
    passed, score, meta = suite.grade(_case(), agent_text)
    assert passed is False
    assert score == 0.0
    assert meta["tier"] == 1


def test_tier1_grade_empty_agent_text_fails():
    suite = SWEBenchVerifiedSuite()
    passed, score, meta = suite.grade(_case(), "")
    assert passed is False
    assert score == 0.0
    assert meta["tier"] == 1
    assert "empty" in meta["reason"]


def test_tier1_grade_diff_touching_unrelated_files_fails():
    """Agent emits a valid diff but touches a file not in
    ``test_patch`` — Tier 1 marks it failed (overlap empty)."""
    suite = SWEBenchVerifiedSuite()
    agent_text = (
        "diff --git a/django/utils/something_else.py "
        "b/django/utils/something_else.py\n"
        "--- a/django/utils/something_else.py\n"
        "+++ b/django/utils/something_else.py\n"
        "@@ -1,3 +1,4 @@\n"
        " def fn():\n"
        "+    pass\n"
        "     return 1\n"
    )
    passed, score, meta = suite.grade(_case(), agent_text)
    assert passed is False
    assert score == 0.0
    assert meta["tier"] == 1
    assert "no test_patch files" in meta["reason"]
    assert "django/utils/something_else.py" in meta["candidate_files"]


def test_tier1_grade_missing_test_patch_returns_half_score():
    """If the upstream row has no ``test_patch`` ground truth, we
    can't confirm file overlap — flag with score 0.5 (still
    ``passed=False``)."""
    suite = SWEBenchVerifiedSuite()
    agent_text = (
        "diff --git a/foo.py b/foo.py\n"
        "--- a/foo.py\n"
        "+++ b/foo.py\n"
        "@@ -1,1 +1,2 @@\n"
        " x = 1\n"
        "+y = 2\n"
    )
    passed, score, meta = suite.grade(_case(test_patch=""), agent_text)
    assert passed is False
    assert score == 0.5
    assert meta["tier"] == 1


# ── Tier 2 grader ──────────────────────────────────────────────────────


def test_tier2_grade_without_grader_raises_runtime_error():
    """Tier 2 must raise a clear RuntimeError pointing at the
    Sprint 4 Tier-2 wire-up when no grader has been attached.

    The previous behaviour was ``NotImplementedError`` while B-385
    (docker runtime) was pending; once Sprint 4 Tier-2 lands, the
    grader is wired through :class:`SWEBenchDockerGrader` and the
    "not yet" error is replaced with a "wire one up" RuntimeError.
    """
    suite = SWEBenchVerifiedSuite()
    assert suite.has_sandboxed_grader() is False
    with pytest.raises(RuntimeError) as exc_info:
        suite.grade_tier2(_case(), "any agent text")
    msg = str(exc_info.value)
    assert "set_sandboxed_grader" in msg
    assert "docker" in msg.lower() or "Docker" in msg


# ── Diff parsing helper (covered indirectly above; explicit smoke) ────


def test_extract_diff_files_handles_git_style_headers():
    text = (
        "diff --git a/foo/bar.py b/foo/bar.py\n"
        "--- a/foo/bar.py\n"
        "+++ b/foo/bar.py\n"
        "@@ -1,1 +1,1 @@\n"
    )
    assert _extract_diff_files(text) == {"foo/bar.py"}


def test_extract_diff_files_handles_bare_minus_plus_headers():
    text = (
        "--- a/x.py\n"
        "+++ b/x.py\n"
        "@@ -1,1 +1,1 @@\n"
    )
    assert _extract_diff_files(text) == {"x.py"}


def test_extract_diff_files_skips_dev_null():
    """``/dev/null`` shows up for new-file or deleted-file diffs but
    isn't a real path — must not appear in the result set."""
    text = (
        "--- /dev/null\n"
        "+++ b/new_file.py\n"
        "@@ -0,0 +1,1 @@\n"
    )
    assert _extract_diff_files(text) == {"new_file.py"}


def test_extract_diff_files_returns_empty_for_non_diff_text():
    assert _extract_diff_files("nothing diff-y here") == set()
    assert _extract_diff_files("") == set()
