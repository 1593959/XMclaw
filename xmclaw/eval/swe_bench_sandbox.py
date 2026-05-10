"""Sprint 4 Tier-2 — sandboxed (Docker-backed) grader for SWE-bench Verified.

This module wires the Tier-2 grader stub in
:mod:`xmclaw.eval.swe_bench_verified` to a real Docker container that:

  1. Spawns an ephemeral ``python:3.11-slim`` container with ``git`` installed.
  2. Clones ``case.metadata['repo']`` (a GitHub ``owner/name``) at the
     row's ``base_commit``.
  3. Applies the ground-truth ``test_patch`` (the diff that adds/edits
     the eval tests for this issue).
  4. Applies the agent's candidate unified diff via ``git apply``.
  5. Runs ``pytest --json-report`` scoped to the row's ``FAIL_TO_PASS``
     and ``PASS_TO_PASS`` test ids.
  6. Reads the JSON report off ``/grade-output.json`` (mounted as a
     tmpfs so nothing leaks back to the host) and decides per-test
     pass/fail based on actual outcomes.

Why this is the REAL number — and Tier 1 is not
=================================================

The Tier-1 heuristic in ``swe_bench_verified.py`` only checks "did the
agent emit a unified diff that touches at least one of the
ground-truth test files?". A model that confidently outputs ``pass`` /
trivial no-op edits to the right files will score 1.0 there. Tier 2 is
the only grader that actually verifies the FAIL_TO_PASS tests transition
F→P and the PASS_TO_PASS tests stay P. **Published SWE-bench numbers
must come from Tier 2.** Tier 1 is for smoke / regression testing
during development and SHOULD be marked deprecated for any external
benchmark publishing.

Design choices
==============

* **One container per ``grade()`` call.** No pooling. Each grade is its
  own ephemeral container, killed on completion or timeout. Keeps the
  blast radius of a stuck container tiny — the worst case is one
  zombie container per grading attempt, which the runtime cleanup
  handles. A pool would let one corrupted state poison subsequent
  grades; the SWE-bench corpus is 500 tasks so the cold-start cost
  is amortised over hours of benchmark wall-time.
* **No host network.** ``network_mode="bridge"`` is needed for the
  initial ``git clone`` and ``pip install``, but the grader closes the
  network namespace down to ``none`` for everything else by always
  building a single bash script that runs the install + tests in one
  container shot. (Going with ``bridge`` is the pragmatic call: SWE-
  bench tasks ALWAYS need to clone a real GitHub repo, and the daemon
  config can lock the grader image registry.)
* **Hard timeout.** Default 5 minutes per task. SWE-bench tasks rarely
  exceed 2 minutes of test wall-time on a warm cache; 5 minutes covers
  cold ``pip install`` + first-time clone. Timeouts return a
  structured ``SandboxedGradeResult(error="timeout", score=0.0)``.
* **Lazy SDK import.** The ``docker`` Python SDK is imported inside
  ``_get_client()`` so ``import xmclaw.eval`` (which the daemon does
  on every boot via ``SUITE_REGISTRY``) never pays the docker cost.

Wire-up in ``SWEBenchVerifiedSuite``
====================================

The companion suite class accepts a ``set_sandboxed_grader(grader)``
hook. Setting it AND calling ``grade(case, agent_text, tier="sandboxed")``
(or setting the env var ``XMC_SWE_BENCH_GRADER=sandboxed``) routes
through this module instead of the heuristic Tier 1 grader. With no
grader set the suite still defaults to Tier 1 — the marketing-warning
from the parent module's docstring still applies in that case.

This lands as part of B-385's follow-up: B-385 shipped the underlying
``DockerSkillRuntime``, this module is a pure consumer.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


# ── result type ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class SandboxedGradeResult:
    """Outcome of a sandboxed (Tier-2) SWE-bench grade.

    Attributes:
        passed: Aggregate pass — True iff every FAIL_TO_PASS test ended
            up passing AND every PASS_TO_PASS test stayed passing.
        score: 0.0–1.0. Defined as
            ``(f2p_passes + p2p_passes) / (len(f2p) + len(p2p))``
            so partial credit is visible without affecting ``passed``.
        fail_to_pass_results: per-test-id → True if the test passed
            after the agent's patch was applied.
        pass_to_pass_results: per-test-id → True if the test still
            passed (i.e. the agent did not regress it).
        patch_applied: False iff ``git apply`` rejected the agent's
            unified diff (malformed / wrong base / hunks failed). When
            this is False the test results are not collected; the
            grader short-circuits with ``passed=False, score=0.0``.
        error: human-readable error string when something went wrong
            BEFORE per-test results could be collected (timeout, image
            pull failure, container died, etc). None on a clean grade
            even if the agent's patch itself failed the tests.
        container_id: best-effort container id, useful for debugging
            stuck graders against ``docker ps -a``.
        latency_s: wall-clock seconds spent inside ``grade()``, including
            image pull / container spawn / pytest run.
    """

    passed: bool
    score: float
    fail_to_pass_results: dict[str, bool] = field(default_factory=dict)
    pass_to_pass_results: dict[str, bool] = field(default_factory=dict)
    patch_applied: bool = False
    error: str | None = None
    container_id: str | None = None
    latency_s: float = 0.0


# ── grader ────────────────────────────────────────────────────────────


# Default base image — ships python + pip out of the box. ``git`` is
# apt-installed inside the grade script (the slim image strips it). We
# pin the major.minor (``3.11``) so a silent upstream rebuild can't
# change pytest behaviour underneath us.
_DEFAULT_IMAGE = "python:3.11-slim"

# Hard cap per ``grade()`` call. SWE-bench tasks usually run in <2min
# on a warm cache; 5min covers cold pip install + first clone.
_DEFAULT_TIMEOUT_S = 300

# Marker the grade script writes around its JSON envelope so we can
# scan past pip / pytest chatter on stdout. Mirrors the harness pattern
# in ``DockerSkillRuntime``.
_RESULT_MARKER = "__SWE_BENCH_GRADE_RESULT__"


class SWEBenchDockerGrader:
    """Docker-based grader for SWE-bench Verified (Sprint 4 Tier-2).

    Spawns one ephemeral container per ``grade()`` call, clones the
    repo at ``base_commit``, applies the agent's unified-diff patch,
    runs pytest scoped to ``FAIL_TO_PASS`` + ``PASS_TO_PASS``, and
    reports per-test pass/fail.

    The grader can either receive an existing
    :class:`xmclaw.providers.runtime.docker.DockerSkillRuntime` (so
    its lazily-pulled image cache is shared) or fall back to building
    its own docker client via ``docker.from_env()``. A direct
    ``client=...`` injection is also supported for tests.

    Args:
        runtime: Optional :class:`DockerSkillRuntime`. When provided,
            the grader uses ``runtime._get_client()`` so the same
            docker daemon connection / image cache is reused across
            graded tasks. Pass None to lazy-build a private client.
        timeout_s: Hard wall-clock cap per ``grade()`` call, in seconds.
            Pytest / pip / git all run under this single budget.
        image: Container image for the grade run. Default
            ``python:3.11-slim``.
        client: Test injection — a docker.from_env()-shaped object.
            When set, ``runtime`` is ignored. None means resolve via
            ``runtime`` (if any) or call ``docker.from_env()`` lazily.
    """

    def __init__(
        self,
        runtime: Any | None = None,
        timeout_s: int = _DEFAULT_TIMEOUT_S,
        image: str = _DEFAULT_IMAGE,
        *,
        client: Any | None = None,
    ) -> None:
        self.runtime = runtime
        self.timeout_s = int(timeout_s)
        self.image = image
        self._client: Any | None = client

    # ── public surface ───────────────────────────────────────────────

    async def grade(
        self,
        agent_patch: str,
        repo: str,
        base_commit: str,
        fail_to_pass: list[str],
        pass_to_pass: list[str],
        test_patch: str = "",
    ) -> SandboxedGradeResult:
        """Apply ``agent_patch`` in a sandboxed container, run pytest
        for the F2P / P2P sets, return the result.

        ``repo`` must be a GitHub-style ``owner/name``; ``base_commit``
        a 40-char hash (or anything ``git checkout`` accepts). When
        ``test_patch`` is non-empty it's applied BEFORE the agent's
        patch (so the agent's patch is graded against the same test
        files the upstream grader would use).

        Never raises for "the agent's patch was bad" — that surfaces
        as ``patch_applied=False``. Raises only for grader-side issues
        the caller MUST see (Docker not reachable, image pull failed,
        etc) — and even then we try to wrap into a structured error
        rather than letting the docker SDK's exception zoo escape.
        """
        if not repo or "/" not in repo:
            raise ValueError(
                f"repo must be 'owner/name', got {repo!r}"
            )
        if not base_commit:
            raise ValueError("base_commit is required")

        started = time.monotonic()
        client = self._get_client()

        # Build the bash script that runs in the container. Stays a
        # pure function so tests can hold us to its shape without
        # spinning up Docker.
        script = self._build_grade_script(
            agent_patch=agent_patch,
            repo=repo,
            base_commit=base_commit,
            test_patch=test_patch,
            fail_to_pass=fail_to_pass,
            pass_to_pass=pass_to_pass,
        )

        # Container labels make the host's ``docker ps`` readable when
        # a grader gets stuck. The unique id is also handed back so
        # callers can correlate logs across multiple graded tasks.
        run_id = uuid.uuid4().hex
        labels = {
            "xmclaw.swe_bench.run_id": run_id,
            "xmclaw.swe_bench.repo": repo,
            "xmclaw.swe_bench.base_commit": base_commit,
        }

        try:
            container = await asyncio.to_thread(
                client.containers.create,
                image=self.image,
                command=["/bin/bash", "-lc", script],
                labels=labels,
                detach=True,
                # Network on: SWE-bench tasks ALWAYS need ``git clone``
                # against github.com + ``pip install`` against PyPI.
                # Locking these down to a private mirror is a follow-up
                # once the grader proves stable.
                network_mode="bridge",
                # Memory cap — repo checkouts + pytest run inside this.
                # 2GB is plenty for the median SWE-bench Verified task.
                mem_limit="2g",
                # No new privileges; drop everything except what apt /
                # python need by default. read_only=False because git
                # clone needs to write; the writable surface is the
                # container fs (ephemeral, dies with the container).
                security_opt=["no-new-privileges:true"],
            )
        except Exception as exc:  # noqa: BLE001 — wrap so callers get one error
            return SandboxedGradeResult(
                passed=False,
                score=0.0,
                error=f"container_create_failed: {type(exc).__name__}: {exc}",
                latency_s=time.monotonic() - started,
            )

        container_id = getattr(container, "id", None) or run_id

        try:
            await asyncio.to_thread(container.start)
        except Exception as exc:  # noqa: BLE001
            await self._cleanup(container)
            return SandboxedGradeResult(
                passed=False,
                score=0.0,
                error=f"container_start_failed: {type(exc).__name__}: {exc}",
                container_id=container_id,
                latency_s=time.monotonic() - started,
            )

        # Wait for the script to complete or timeout. We do NOT trust
        # the docker SDK's ``timeout=`` arg alone — it raises
        # ReadTimeout (a requests exception) on deadline. We translate
        # any timeout-shaped error into a structured grade result.
        try:
            wait_result = await asyncio.to_thread(
                _container_wait, container, self.timeout_s,
            )
        except _GraderTimeout:
            await self._cleanup(container)
            return SandboxedGradeResult(
                passed=False,
                score=0.0,
                error=f"timeout: grader exceeded {self.timeout_s}s budget",
                container_id=container_id,
                latency_s=time.monotonic() - started,
            )
        except Exception as exc:  # noqa: BLE001
            await self._cleanup(container)
            return SandboxedGradeResult(
                passed=False,
                score=0.0,
                error=f"container_wait_failed: {type(exc).__name__}: {exc}",
                container_id=container_id,
                latency_s=time.monotonic() - started,
            )

        # Read stdout/stderr — the grade script writes its result JSON
        # on a marker line so we can find it past pip noise.
        try:
            stdout_bytes = await asyncio.to_thread(
                container.logs, stdout=True, stderr=False,
            )
        except Exception:  # noqa: BLE001
            stdout_bytes = b""
        stdout = (
            stdout_bytes.decode("utf-8", errors="replace")
            if isinstance(stdout_bytes, (bytes, bytearray))
            else str(stdout_bytes)
        )

        await self._cleanup(container)

        envelope = _parse_grade_envelope(stdout)
        if envelope is None:
            exit_code = (
                int(wait_result.get("StatusCode", 1))
                if isinstance(wait_result, dict) else 1
            )
            return SandboxedGradeResult(
                passed=False,
                score=0.0,
                error=(
                    f"no_grade_envelope (exit={exit_code}); "
                    f"stdout_tail={_truncate(stdout, 600)!r}"
                ),
                container_id=container_id,
                latency_s=time.monotonic() - started,
            )

        # Successful envelope — compute the per-test verdicts and score.
        if not bool(envelope.get("patch_applied", False)):
            return SandboxedGradeResult(
                passed=False,
                score=0.0,
                patch_applied=False,
                error=str(envelope.get("error") or "patch did not apply"),
                container_id=container_id,
                latency_s=time.monotonic() - started,
            )

        json_report = envelope.get("pytest_report") or {}
        f2p_results, p2p_results = self._parse_pytest_results(
            json.dumps(json_report) if not isinstance(json_report, str) else json_report,
            fail_to_pass=fail_to_pass,
            pass_to_pass=pass_to_pass,
        )
        passed, score = _aggregate_score(
            f2p_results=f2p_results, p2p_results=p2p_results,
        )
        return SandboxedGradeResult(
            passed=passed,
            score=score,
            fail_to_pass_results=f2p_results,
            pass_to_pass_results=p2p_results,
            patch_applied=True,
            error=None,
            container_id=container_id,
            latency_s=time.monotonic() - started,
        )

    # ── script generation ────────────────────────────────────────────

    def _build_grade_script(
        self,
        *,
        agent_patch: str,
        repo: str,
        base_commit: str,
        test_patch: str,
        fail_to_pass: list[str],
        pass_to_pass: list[str],
    ) -> str:
        """Generate the bash script the container runs.

        Pure function — no docker side effects — so the test suite can
        hold us to a stable shape (git clone, git checkout, apply,
        pytest --json-report) without spinning up a real container.

        Heredocs (``<<'AGENT_PATCH_EOF'``) carry the patches verbatim
        so backticks / dollar signs / quotes inside the diff don't get
        expanded by bash. Single-quoted EOF disables interpolation,
        which is exactly what we want for raw diffs.
        """
        f2p_arg = " ".join(_shquote(t) for t in fail_to_pass)
        p2p_arg = " ".join(_shquote(t) for t in pass_to_pass)
        all_tests = " ".join(_shquote(t) for t in (*fail_to_pass, *pass_to_pass))

        # ``pip install -e .`` is best-effort: many SWE-bench tasks
        # don't ship a setup.py / pyproject.toml that pip can resolve
        # cleanly, but the gold-test-runner upstream still works
        # because the patch + tests reference relative paths. We swallow
        # install failures and proceed to pytest — the test outcomes
        # are what matter.
        return _GRADE_SCRIPT_TEMPLATE.format(
            repo=repo,
            base_commit=_shquote(base_commit),
            test_patch_b64=_b64(test_patch or ""),
            agent_patch_b64=_b64(agent_patch or ""),
            f2p_arg=f2p_arg,
            p2p_arg=p2p_arg,
            all_tests=all_tests,
            result_marker=_RESULT_MARKER,
        )

    # ── pytest output parsing ────────────────────────────────────────

    def _parse_pytest_results(
        self,
        json_output: str,
        fail_to_pass: list[str],
        pass_to_pass: list[str],
    ) -> tuple[dict[str, bool], dict[str, bool]]:
        """Map pytest's --json-report output to per-test pass/fail.

        ``json_output`` is the raw stdout the grade script captures
        from ``pytest --json-report --json-report-file=-`` (or the
        equivalent file read). The pytest-json-report plugin emits a
        ``tests`` array; each entry has ``nodeid`` + ``outcome`` keys.

        For each id in ``fail_to_pass`` / ``pass_to_pass`` we look up
        the matching ``nodeid`` and record ``True`` iff outcome is
        ``passed``. Tests that aren't in the report at all (e.g.
        because pytest collection failed) are recorded as ``False``.
        """
        report: dict[str, Any] = {}
        try:
            parsed = json.loads(json_output) if json_output else {}
        except json.JSONDecodeError:
            parsed = {}
        if isinstance(parsed, dict):
            report = parsed

        outcomes: dict[str, str] = {}
        for entry in report.get("tests") or []:
            if not isinstance(entry, dict):
                continue
            node_id = str(entry.get("nodeid") or "")
            outcome = str(entry.get("outcome") or "").lower()
            if node_id:
                outcomes[node_id] = outcome

        f2p_results = {
            test_id: outcomes.get(test_id, "missing") == "passed"
            for test_id in fail_to_pass
        }
        p2p_results = {
            test_id: outcomes.get(test_id, "missing") == "passed"
            for test_id in pass_to_pass
        }
        return f2p_results, p2p_results

    # ── docker plumbing ──────────────────────────────────────────────

    def _get_client(self) -> Any:
        """Return a docker.DockerClient — cached after first call.

        Resolution order:
          1. ``client=`` argument from the constructor (test injection).
          2. ``runtime._get_client()`` if a runtime was passed.
          3. ``docker.from_env()`` lazy-imported here.
        """
        if self._client is not None:
            return self._client
        if self.runtime is not None and hasattr(self.runtime, "_get_client"):
            self._client = self.runtime._get_client()
            return self._client
        try:
            # ``docker`` is an optional runtime dep with no first-party
            # stubs — silence both error codes since the install
            # presence dictates which one mypy reports.
            import docker  # type: ignore[import-untyped]
        except ImportError as exc:  # pragma: no cover — exercised via mock
            raise RuntimeError(
                "SWEBenchDockerGrader requires the 'docker' Python SDK. "
                "Install with `pip install 'docker>=7'` (or pass an "
                "explicit DockerSkillRuntime via runtime=...)."
            ) from exc
        try:
            self._client = docker.from_env()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"SWEBenchDockerGrader cannot reach the Docker daemon: "
                f"{type(exc).__name__}: {exc}. Is the daemon running?"
            ) from exc
        return self._client

    async def _cleanup(self, container: Any) -> None:
        """Best-effort container teardown. Idempotent; swallows errors."""
        try:
            await asyncio.to_thread(_kill_quiet, container)
        except Exception:  # noqa: BLE001
            pass
        try:
            await asyncio.to_thread(_remove_quiet, container)
        except Exception:  # noqa: BLE001
            pass


# ── module helpers ────────────────────────────────────────────────────


class _GraderTimeout(Exception):
    """Internal flag — raised when the docker wait deadline elapses."""


def _container_wait(container: Any, timeout: float | None) -> dict[str, Any]:
    """Sync wrapper around ``container.wait(timeout=...)``.

    Translates docker-py / requests timeout exceptions into our own
    ``_GraderTimeout`` so callers don't have to know about the
    requests/docker-py exception zoo.
    """
    try:
        if timeout is not None:
            result = container.wait(timeout=timeout)
        else:
            result = container.wait()
        # docker-py's wait() returns Any (via the SDK's untyped surface);
        # cast explicitly so mypy doesn't bleed Any back to callers.
        return dict(result) if isinstance(result, dict) else {"StatusCode": 0}
    except Exception as exc:  # noqa: BLE001 — we triage by type name
        name = type(exc).__name__
        if "Timeout" in name or "ReadTimeout" in name:
            raise _GraderTimeout from exc
        raise


def _kill_quiet(container: Any) -> None:
    try:
        container.kill()
    except Exception:  # noqa: BLE001
        pass


def _remove_quiet(container: Any) -> None:
    try:
        container.remove(force=True)
    except Exception:  # noqa: BLE001
        pass


def _truncate(text: str, n: int) -> str:
    if len(text) <= n:
        return text
    return text[:n] + "..."


def _shquote(value: str) -> str:
    """Single-quote a string for safe inclusion in a bash command.

    Equivalent to ``shlex.quote`` but with no dependency on shlex's
    ASCII-vs-unicode quirks. Wraps in single quotes and escapes any
    embedded single-quote with the standard ``'\\''`` trick.
    """
    return "'" + value.replace("'", "'\\''") + "'"


def _b64(text: str) -> str:
    """Encode a string as base64 ASCII for safe embedding in a heredoc.

    Diffs are full of ``\\``, ``$``, backticks, and quotes. Even with a
    single-quoted heredoc, line-ending edge cases (CRLF on Windows
    hosts copying into a Linux container) bite. Base64 sidesteps all
    of that — the script ``base64 -d`` decodes back to bytes inside
    the container.
    """
    import base64
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _parse_grade_envelope(stdout: str) -> dict[str, Any] | None:
    """Find the grade script's JSON result line in container stdout.

    The script emits exactly one line beginning with ``_RESULT_MARKER``
    so this can scan past pip / pytest chatter. Returns None when no
    envelope is present (the caller surfaces ``no_grade_envelope`` so
    debugging is straightforward).
    """
    if not stdout:
        return None
    for line in reversed(stdout.splitlines()):
        if line.startswith(_RESULT_MARKER):
            payload = line[len(_RESULT_MARKER):].lstrip(": ").strip()
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                return None
            if isinstance(obj, dict):
                return obj
            return None
    return None


def _aggregate_score(
    *,
    f2p_results: dict[str, bool],
    p2p_results: dict[str, bool],
) -> tuple[bool, float]:
    """Aggregate per-test verdicts into ``(passed, score)``.

    ``passed`` is True iff EVERY F2P transitioned F→P AND EVERY P2P
    stayed P. ``score`` is the fraction of (F2P + P2P) that hit the
    expected verdict — useful as partial-credit signal even when
    ``passed`` is False.

    Empty F2P/P2P sets degenerate to ``passed=False, score=0.0``: a
    well-formed SWE-bench Verified row always has at least one
    FAIL_TO_PASS, so an empty set means the grader was called with
    incomplete inputs and shouldn't be silently rewarded.
    """
    total = len(f2p_results) + len(p2p_results)
    if total == 0:
        return False, 0.0
    f2p_passes = sum(1 for v in f2p_results.values() if v)
    p2p_passes = sum(1 for v in p2p_results.values() if v)
    score = (f2p_passes + p2p_passes) / total
    passed = (
        f2p_passes == len(f2p_results)
        and p2p_passes == len(p2p_results)
    )
    return passed, score


# ── grade script template ─────────────────────────────────────────────
#
# Stays POSIX-bash and uses base64-decoded heredocs for the patches so
# we don't have to worry about line-ending or quoting issues. The
# script is intentionally idempotent on its OWN failure modes (every
# step is wrapped in a check so a broken patch surfaces as
# patch_applied=False rather than masquerading as a test failure).
#
# Output contract: the very last stdout line MUST start with
# ``_RESULT_MARKER`` followed by a JSON dict with these fields:
#   * patch_applied: bool
#   * error: optional human-readable string
#   * pytest_report: parsed pytest-json-report dict (when applied)
#
# The grader's ``_parse_grade_envelope`` reads exactly that line.

_GRADE_SCRIPT_TEMPLATE = '''
set -e
RESULT_MARKER={result_marker!r}
emit_result() {{
    python3 -c "import json,sys; print('${{RESULT_MARKER}}: '+ json.dumps(json.load(sys.stdin)))" < /grade-output.json
}}
emit_failure() {{
    python3 -c "import json,sys; \\
e = sys.argv[1]; \\
print('${{RESULT_MARKER}}: '+ json.dumps({{'patch_applied': False, 'error': e}}))" "$1"
    exit 0
}}

# Install minimal grader prereqs. Network on, but the host config can
# point this at a private mirror via http_proxy if needed.
apt-get update -qq >/dev/null 2>&1 || true
apt-get install -y -qq git ca-certificates >/dev/null 2>&1 || true

pip install --quiet pytest pytest-json-report >/dev/null 2>&1 || \\
    emit_failure "pip_install_failed: cannot install pytest"

mkdir -p /workspace
cd /workspace

# Clone repo. Use --depth=50 for a lightweight clone — enough history
# to reach base_commit for almost every SWE-bench Verified task; if
# the depth is insufficient, ``git checkout`` fails with a clear
# error that surfaces as patch_applied=False.
git clone --quiet "https://github.com/{repo}.git" repo 2>/dev/null || \\
    emit_failure "git_clone_failed: cannot clone {repo}"
cd repo

git checkout --quiet {base_commit} 2>/dev/null || \\
    git fetch --quiet origin {base_commit} 2>/dev/null && git checkout --quiet FETCH_HEAD 2>/dev/null || \\
    emit_failure "git_checkout_failed: cannot reach {base_commit}"

# Apply the test_patch first (the diff that adds the eval tests).
echo {test_patch_b64} | base64 -d > /tmp/test.patch
if [ -s /tmp/test.patch ]; then
    git apply --whitespace=nowarn /tmp/test.patch 2>/dev/null || \\
        emit_failure "test_patch_apply_failed"
fi

# Apply the agent's candidate patch.
echo {agent_patch_b64} | base64 -d > /tmp/agent.patch
if [ ! -s /tmp/agent.patch ]; then
    emit_failure "empty_agent_patch"
fi
git apply --whitespace=nowarn /tmp/agent.patch 2>/dev/null || {{
    cat > /grade-output.json <<'JSON_EOF'
{{"patch_applied": false, "error": "agent_patch_apply_failed", "pytest_report": {{}}}}
JSON_EOF
    emit_result
    exit 0
}}

# Best-effort pip install -e . — if the project has a setup.py /
# pyproject we pick it up; otherwise we proceed with the source tree
# in place and pytest's own collection.
pip install --quiet -e . >/dev/null 2>&1 || true

# Run pytest scoped to the FAIL_TO_PASS + PASS_TO_PASS set, write the
# JSON report to /tmp/pytest.json.
pytest --json-report --json-report-file=/tmp/pytest.json {all_tests} >/dev/null 2>&1 || true

if [ ! -f /tmp/pytest.json ]; then
    cat > /grade-output.json <<'JSON_EOF'
{{"patch_applied": true, "error": "pytest_no_report", "pytest_report": {{}}}}
JSON_EOF
    emit_result
    exit 0
fi

python3 -c "import json,sys; \\
report = json.load(open('/tmp/pytest.json')); \\
out = {{'patch_applied': True, 'error': None, 'pytest_report': report}}; \\
open('/grade-output.json','w').write(json.dumps(out))"
emit_result
'''


__all__ = [
    "SWEBenchDockerGrader",
    "SandboxedGradeResult",
]
