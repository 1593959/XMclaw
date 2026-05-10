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
the test transitions. That stack lives in :mod:`xmclaw.eval.swe_bench_sandbox`
and is wired through :class:`xmclaw.providers.runtime.docker.DockerSkillRuntime`
(B-385).

* **Tier 1 (heuristic)** — runs in-process, no Docker, no network. It
  extracts a unified diff block from the agent's output, verifies it
  parses, and checks that at least one file the diff touches also
  appears in the ground-truth ``test_patch``. Binary 1.0 / 0.0. Useful
  for fast smoke tests and regression detection during development.

* **Tier 2 (sandboxed)** — spawns an ephemeral Docker container,
  clones the repo at ``base_commit``, applies the agent's patch, runs
  pytest scoped to FAIL_TO_PASS + PASS_TO_PASS, and reports per-test
  outcomes. **This is the real SWE-bench number.** Wire it in via
  :meth:`SWEBenchVerifiedSuite.set_sandboxed_grader` (or set
  ``XMC_SWE_BENCH_GRADER=sandboxed`` for auto-wire).

**Honest disclosure** (do not delete; CI tests for this string):
Tier 1 grading is approximate; published SWE-bench numbers come from
Tier 2 sandboxed evaluation. **Do NOT use Tier 1 scores in marketing**
claims, release notes, or competitive comparisons — they overestimate
"file-touch correctness" relative to the real F→P signal. Tier 1 is
**deprecated for benchmark publishing** as of Sprint 4 Tier-2 wire-up;
it remains supported for development-time smoke tests only.
"""
from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from xmclaw.eval.harness import BenchmarkSuite, TaskCase

if TYPE_CHECKING:
    from xmclaw.eval.swe_bench_sandbox import SWEBenchDockerGrader


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

    Grading defaults to **Tier 1 heuristic**: see the module docstring
    for the honest-disclosure note. Tier 1 numbers are NOT comparable
    to published SWE-bench leaderboards. For real SWE-bench numbers,
    wire up Tier 2 via :meth:`set_sandboxed_grader` (which uses the
    Docker runtime shipped in B-385) and call
    ``grade(case, agent_text, tier="sandboxed")``.

    Setting ``XMC_SWE_BENCH_GRADER=sandboxed`` in the environment
    auto-promotes the default tier to sandboxed when a grader is
    available — useful for CI runs where every grade should hit the
    real Docker pipeline. **Do NOT use Tier 1 scores in marketing**
    once Tier 2 is available; Tier 1 is deprecated for benchmark
    publishing.
    """

    SUITE_ID = "swe_bench_verified"
    UPSTREAM_DATASET = "princeton-nlp/SWE-bench_Verified"
    UPSTREAM_SPLIT = "test"

    def __init__(self) -> None:
        # Optional Tier-2 grader — set via ``set_sandboxed_grader``.
        # We keep this as a private attribute (rather than a constructor
        # arg) so the suite continues to satisfy the BenchmarkSuite ABC's
        # zero-arg construction contract used by SUITE_REGISTRY.
        self._sandboxed_grader: SWEBenchDockerGrader | None = None

    @property
    def suite_id(self) -> str:
        return self.SUITE_ID

    # ── Tier 2 wiring ────────────────────────────────────────────────

    def set_sandboxed_grader(
        self, grader: "SWEBenchDockerGrader | None",
    ) -> None:
        """Attach a Docker-backed Tier-2 grader.

        Pass ``None`` to revert to Tier-1-only operation. Once set,
        ``grade(case, agent_text, tier="sandboxed")`` routes through
        :meth:`xmclaw.eval.swe_bench_sandbox.SWEBenchDockerGrader.grade`
        instead of the heuristic. Default tier remains Tier 1 unless
        ``XMC_SWE_BENCH_GRADER=sandboxed`` is set in the environment.
        """
        self._sandboxed_grader = grader

    def has_sandboxed_grader(self) -> bool:
        """True iff a Tier-2 grader is wired and ``tier="sandboxed"``
        will route through it."""
        return self._sandboxed_grader is not None

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
        """Grade an agent's output against the SWE-bench task.

        Two tiers are available:

        * ``tier="heuristic"`` (default) — Tier 1, in-process, no Docker.
          Returns ``(passed, score, meta)`` where ``passed`` is True iff
          the agent emitted a parseable unified diff AND at least one
          file the diff touches also appears in the ground-truth
          ``test_patch``. Binary 0/1 score. See the marketing-warning
          note in the module docstring.

        * ``tier="sandboxed"`` — Tier 2, routes through the
          Docker-backed grader wired via :meth:`set_sandboxed_grader`.
          Spawns a container, applies the patch, runs pytest scoped to
          FAIL_TO_PASS + PASS_TO_PASS, returns per-test verdicts in
          ``meta``. Raises ``RuntimeError`` if no grader is wired.

        Sandboxed mode also auto-engages when the environment variable
        ``XMC_SWE_BENCH_GRADER=sandboxed`` is set AND a grader is
        wired — convenient for CI runs that should always hit the real
        Docker pipeline.

        ``extra`` is reserved for future tiers (e.g. judge-based grading);
        unknown keys are ignored.
        """
        tier = str(extra.get("tier") or "").lower()
        if not tier and os.environ.get("XMC_SWE_BENCH_GRADER", "").lower() == "sandboxed":
            tier = "sandboxed"

        if tier == "sandboxed":
            return self._grade_sandboxed(case, agent_text)

        # tier="heuristic" (default) or any unrecognised tier value.
        return self._grade_heuristic(case, agent_text)

    def _grade_heuristic(
        self, case: TaskCase, agent_text: str,
    ) -> tuple[bool, float, dict[str, Any]]:
        """Tier 1 grader implementation. See ``grade()`` docstring."""
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
        self, case: TaskCase, agent_text: str, **extra: Any,  # noqa: ARG002
    ) -> tuple[bool, float, dict[str, Any]]:
        """Tier 2 sandboxed grader — routes through the wired
        :class:`SWEBenchDockerGrader`.

        Equivalent to ``grade(case, agent_text, tier="sandboxed")`` —
        kept as a separate method so existing callers can continue to
        spell their intent explicitly. Raises ``RuntimeError`` if no
        sandboxed grader has been wired via :meth:`set_sandboxed_grader`.

        Sprint 4 Tier-2 (B-385 wire-up): the underlying Docker runtime
        ships in :mod:`xmclaw.providers.runtime.docker`; the
        :mod:`xmclaw.eval.swe_bench_sandbox` module is the consumer
        that orchestrates clone → patch-apply → pytest.
        """
        return self._grade_sandboxed(case, agent_text)

    def _grade_sandboxed(
        self, case: TaskCase, agent_text: str,
    ) -> tuple[bool, float, dict[str, Any]]:
        """Tier 2 grader implementation.

        Pulls ``repo`` / ``base_commit`` from ``case.metadata`` and the
        ``test_patch`` / FAIL_TO_PASS / PASS_TO_PASS lists from
        ``case.expected_signals``. Runs the wired
        :class:`SWEBenchDockerGrader` synchronously off the caller's
        event loop so the suite-Runner pattern (which calls ``grade``
        from a non-async context) keeps working.
        """
        grader = self._sandboxed_grader
        if grader is None:
            raise RuntimeError(
                "SWE-bench Tier 2 (sandboxed) grading requires a "
                "DockerSkillRuntime-backed grader. Wire one via "
                "SWEBenchVerifiedSuite.set_sandboxed_grader(...). "
                "See xmclaw.eval.swe_bench_sandbox.SWEBenchDockerGrader "
                "(B-385 wire-up). For development-time smoke tests "
                "use grade(...) (Tier 1 heuristic) — but do NOT "
                "publish those as SWE-bench scores."
            )

        repo = str(case.metadata.get("repo") or "")
        base_commit = str(case.metadata.get("base_commit") or "")
        es = case.expected_signals or {}
        test_patch = str(es.get("test_patch") or "")
        fail_to_pass = list(es.get("fail_to_pass") or [])
        pass_to_pass = list(es.get("pass_to_pass") or [])

        coro = grader.grade(
            agent_patch=agent_text,
            repo=repo,
            base_commit=base_commit,
            fail_to_pass=fail_to_pass,
            pass_to_pass=pass_to_pass,
            test_patch=test_patch,
        )
        result = _run_coroutine_sync(coro)

        meta: dict[str, Any] = {
            "tier": 2,
            "patch_applied": result.patch_applied,
            "fail_to_pass_results": result.fail_to_pass_results,
            "pass_to_pass_results": result.pass_to_pass_results,
            "container_id": result.container_id,
            "latency_s": result.latency_s,
        }
        if result.error:
            meta["error"] = result.error
        return result.passed, result.score, meta


def _run_coroutine_sync(coro: Any) -> Any:
    """Run an awaitable in a fresh loop and return its result.

    The Runner / CLI call ``grade()`` from non-async code, but the
    Tier-2 grader's ``grade()`` is async (it ``await``s docker calls
    via ``asyncio.to_thread``). We don't want to require the caller
    to run inside a loop, so we spin up a fresh one here. If the
    caller IS already inside a loop (rare for grading), we fall
    through to ``asyncio.run`` which raises clearly — the right fix
    is then to call the grader's ``grade()`` directly.
    """
    try:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()
    except RuntimeError as exc:
        if "running event loop" in str(exc).lower():
            # Caller is already inside a loop — let asyncio.run raise
            # its own clearer message.
            return asyncio.run(coro)
        raise


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
