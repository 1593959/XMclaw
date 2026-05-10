"""SWE-bench Verified suite — the 500-task human-verified subset.

SWE-bench Verified is the industry-standard benchmark for real-world
coding-agent evaluation: each task is a GitHub issue from a Python OSS
repo, the agent must produce a unified diff that resolves the issue,
and the published score is the fraction of tasks whose patches make
the ``FAIL_TO_PASS`` tests transition F→P while keeping
``PASS_TO_PASS`` tests passing.

Paper: https://arxiv.org/abs/2310.06770.
Verified subset: https://huggingface.co/datasets/princeton-nlp/SWE-bench_Verified.

Sibling of :mod:`xmclaw.eval.longmemeval_full` — same lazy-import +
HF-cache pattern. We never import ``datasets`` at module load so the
daemon (which imports ``xmclaw.eval`` via ``SUITE_REGISTRY``) does not
pay the HuggingFace import cost.

Two-tier grading
================

Real SWE-bench evaluation requires a sandboxed environment: clone the
repo at ``base_commit``, apply the candidate patch, run pytest, check
the test transitions. That stack lives behind backlog item **B-385**
(docker runtime). It is **not** shipped in this commit.

Until B-385 lands we run a **Tier 1 heuristic** grader:

* Extract a unified diff block from ``agent_text``.
* Verify it parses as a valid unified diff.
* Verify it touches at least one of the files mentioned in the
  ground-truth ``test_patch`` (a strong signal the agent at least
  identified the right files).
* Score 1.0 / 0.0 binary; ``passed`` reflects the same.

The Tier 2 grader interface is in place — calling
:meth:`SWEBenchVerifiedSuite.grade_tier2` raises ``NotImplementedError``
with a hint pointing at B-385 and the docker runtime hook the future
implementation will plug into.

**Honest disclosure** (do not delete; CI tests for this string):
Tier 1 grading is approximate; published SWE-bench numbers come from
Tier 2 sandboxed evaluation. **Do NOT use Tier 1 scores in marketing**
claims, release notes, or competitive comparisons — they overestimate
"file-touch correctness" relative to the real F→P signal.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from xmclaw.eval.harness import BenchmarkSuite, TaskCase


# HF cache lives under XMclaw's workspace so it survives across `pip
# uninstall` cycles and stays isolated from the user's global
# ``~/.cache/huggingface``.
_HF_CACHE_DIR = (
    Path.home() / ".xmclaw" / "v2" / "eval_cache" / "swe_bench_verified"
)


# Surface the install hint from ``load_tasks`` (not at import time) so
# the daemon and ``xmclaw eval list`` keep working without the extra.
_INSTALL_HINT = (
    "SWE-bench Verified suite requires the 'eval-hf' extra. "
    "Install with: pip install 'xmclaw[eval-hf]'"
)


# Precompiled regex for unified-diff sniffing. Matches both
# ``diff --git a/foo b/foo`` headers and bare ``--- a/foo`` /
# ``+++ b/foo`` pairs that some agents emit. We intentionally accept
# both because Tier 1 is a coarse "did the agent at least output a
# patch shape" check.
_DIFF_FILE_RE = re.compile(
    r"^(?:diff --git a/(?P<git>\S+) b/\S+|"
    r"\+\+\+ b/(?P<plus>\S+)|"
    r"--- a/(?P<minus>\S+))$",
    re.MULTILINE,
)
_HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@", re.MULTILINE)


class SWEBenchVerifiedSuite(BenchmarkSuite):
    """SWE-bench Verified — 500-task human-verified coding benchmark.

    The first ``load_tasks`` call downloads the dataset (cached under
    ``~/.xmclaw/v2/eval_cache/swe_bench_verified/``); subsequent calls
    are fast. ``limit`` is honoured before any TaskCase construction so
    a smoke run with ``--limit 5`` doesn't iterate the whole corpus.

    Grading is **Tier 1 heuristic** by default. See module docstring
    for the honest-disclosure note: Tier 1 numbers are NOT comparable
    to published SWE-bench leaderboards. **Do NOT use Tier 1 scores in
    marketing** until B-385 wires up Tier 2 sandboxed evaluation.
    """

    SUITE_ID = "swe_bench_verified"
    UPSTREAM_DATASET = "princeton-nlp/SWE-bench_Verified"
    UPSTREAM_SPLIT = "test"

    @property
    def suite_id(self) -> str:
        return self.SUITE_ID

    def load_tasks(self, limit: int | None = None) -> list[TaskCase]:
        """Fetch the SWE-bench Verified split and convert each row to a
        ``TaskCase``.

        Lazy-imports ``datasets`` so missing-extra environments don't
        crash on ``import xmclaw.eval``.
        """
        if limit is not None and limit < 0:
            raise ValueError(f"limit must be non-negative, got {limit}")

        # Point HF at our cache dir BEFORE importing/calling. Setting
        # the env var also affects the underlying ``huggingface_hub``
        # client.
        _HF_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault("HF_DATASETS_CACHE", str(_HF_CACHE_DIR))

        try:
            from datasets import load_dataset  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - exercised via mock
            raise ImportError(_INSTALL_HINT) from exc

        ds = load_dataset(
            self.UPSTREAM_DATASET,
            split=self.UPSTREAM_SPLIT,
            cache_dir=str(_HF_CACHE_DIR),
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

        Pulled out so tests can construct cases directly without
        touching ``datasets``. Schema reference (from the dataset card):

        * ``instance_id``: stable string id (``<repo>-<issue#>``-style)
        * ``repo``: GitHub ``owner/name``
        * ``base_commit``: commit hash the patch will be applied on top of
        * ``problem_statement``: the GitHub issue body
        * ``hints_text``: optional, sometimes-present human notes
        * ``test_patch``: the diff that adds/edits the eval tests
        * ``patch``: the gold (human) patch — held back at eval time
        * ``FAIL_TO_PASS``: list of test ids that should flip F→P
        * ``PASS_TO_PASS``: list of test ids that must stay P
        """
        repo = str(row.get("repo") or "unknown/unknown")
        base_commit = str(row.get("base_commit") or "")
        problem_statement = str(row.get("problem_statement") or "")
        instance_id = str(row.get("instance_id") or f"swe-{idx}")

        prompt = (
            f"Repository: {repo}\n"
            f"Base commit: {base_commit}\n\n"
            f"Issue:\n{problem_statement}\n\n"
            f"Your task: produce a unified diff patch that resolves "
            f"this issue. The patch will be applied to {base_commit} "
            f"and tested. Pass criteria: FAIL_TO_PASS tests transition "
            f"F→P; PASS_TO_PASS tests stay P. Output ONLY the "
            f"unified diff (no prose)."
        )

        return TaskCase(
            task_id=instance_id,
            prompt=prompt,
            expected_signals={
                "gold_patch": str(row.get("patch") or ""),
                "fail_to_pass": list(row.get("FAIL_TO_PASS") or []),
                "pass_to_pass": list(row.get("PASS_TO_PASS") or []),
                "test_patch": str(row.get("test_patch") or ""),
            },
            metadata={
                "repo": repo,
                "base_commit": base_commit,
            },
        )

    def grade(
        self, case: TaskCase, agent_text: str, **extra: Any,
    ) -> tuple[bool, float, dict[str, Any]]:
        """Tier 1 heuristic grader (default).

        See class / module docstring for the honest-disclosure note.
        Tier 1 returns ``(passed, score, meta)`` where ``passed`` is
        True iff (a) the agent emitted a parseable unified diff and
        (b) at least one file the diff touches also appears in the
        ground-truth ``test_patch``. Score is binary 0/1.

        ``extra`` is reserved — Tier 2 will accept ``runtime=...`` to
        receive a docker-backed evaluator.
        """
        if not agent_text or not agent_text.strip():
            return False, 0.0, {
                "tier": 1,
                "reason": "empty agent text",
                "matched_files": [],
            }

        candidate_files = _extract_diff_files(agent_text)
        has_hunks = bool(_HUNK_HEADER_RE.search(agent_text))
        if not candidate_files or not has_hunks:
            return False, 0.0, {
                "tier": 1,
                "reason": "no parseable unified diff in agent_text",
                "matched_files": [],
            }

        test_patch = case.expected_signals.get("test_patch") or ""
        if not isinstance(test_patch, str):
            return False, 0.0, {
                "tier": 1,
                "reason": "expected_signals.test_patch is not a string",
                "matched_files": [],
            }

        ground_truth_files = _extract_diff_files(test_patch)
        # If the ground truth has no test_patch (rare but possible for
        # malformed rows), fall back to "did we at least output a diff".
        # We score it as a half-pass to flag the row instead of silently
        # rewarding any diff. Half-pass still fails ``passed=True``.
        if not ground_truth_files:
            return False, 0.5, {
                "tier": 1,
                "reason": "no test_patch ground truth — diff shape OK",
                "matched_files": list(candidate_files),
            }

        overlap = candidate_files & ground_truth_files
        if overlap:
            return True, 1.0, {
                "tier": 1,
                "matched_files": sorted(overlap),
                "candidate_files": sorted(candidate_files),
                "ground_truth_files": sorted(ground_truth_files),
            }
        return False, 0.0, {
            "tier": 1,
            "reason": "diff touches no test_patch files",
            "matched_files": [],
            "candidate_files": sorted(candidate_files),
            "ground_truth_files": sorted(ground_truth_files),
        }

    def grade_tier2(
        self, case: TaskCase, agent_text: str, **extra: Any,
    ) -> tuple[bool, float, dict[str, Any]]:
        """Tier 2 sandboxed grader — NOT YET IMPLEMENTED.

        The real SWE-bench grader needs to:

        1. Clone ``case.metadata['repo']`` at ``base_commit`` into a
           temp workspace (or pull from a pre-built docker image).
        2. Apply ``case.expected_signals['test_patch']``.
        3. Apply the candidate patch from ``agent_text``.
        4. Run pytest scoped to ``FAIL_TO_PASS`` + ``PASS_TO_PASS``.
        5. Verify FAIL_TO_PASS tests went F→P and PASS_TO_PASS
           stayed P.

        That requires a docker runtime + image cache, which is tracked
        as backlog **B-385**. To wire this up: pass a runtime via
        ``extra['runtime']`` and call its ``run_swebench_eval`` hook.
        """
        # TODO(B-385): wire to docker runtime — see module docstring.
        raise NotImplementedError(
            "SWE-bench Tier 2 grading is not implemented yet. "
            "Wire to the docker runtime tracked as B-385; the candidate "
            "patch must be applied to the repo at base_commit, "
            "FAIL_TO_PASS / PASS_TO_PASS tests run, and transitions "
            "verified. Use grade() (Tier 1) for heuristic numbers in "
            "the meantime — but do NOT publish those as SWE-bench scores."
        )


def _extract_diff_files(text: str) -> set[str]:
    """Return the set of file paths a unified diff touches.

    Accepts both ``diff --git a/foo b/foo`` and bare ``--- a/foo`` /
    ``+++ b/foo`` styles. Strips the ``a/`` / ``b/`` prefix. Returns
    an empty set when the text contains no recognisable diff.
    """
    if not text:
        return set()
    files: set[str] = set()
    for match in _DIFF_FILE_RE.finditer(text):
        for path in match.groupdict().values():
            if path and path != "/dev/null":
                files.add(path)
    return files


__all__ = ["SWEBenchVerifiedSuite"]
