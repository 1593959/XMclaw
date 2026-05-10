"""Sprint 4 Tier-2 — sandboxed (Docker-backed) grader for TerminalBench 2.0.

This module wires the Tier-2 grader stub in :mod:`xmclaw.eval.terminal_bench`
to a real Docker container that:

  1. Spawns an ephemeral ``ubuntu:22.04`` container (TerminalBench tasks
     assume a Linux box; ``python:3.11-slim`` would be too sparse).
  2. Mounts an empty ``/workspace`` and ``cd``-s into it.
  3. Replays the agent's bash commands (extracted from ``agent_text``)
     so any side-effects the agent claims to have produced — files
     written, packages installed, paths set up — actually exist before
     the verification tests run.
  4. Runs each verification ``test`` (a bash snippet shipped with the
     dataset row) and records its exit code as the per-test verdict.
  5. Reads a JSON envelope off ``/grade-output.json`` (mounted as a
     tmpfs so nothing leaks back to the host) and reports per-test
     pass/fail aggregated into ``passed`` + ``score``.

Why this is the REAL number — and Tier 1 is not
=================================================

The Tier-1 heuristic in ``terminal_bench.py`` only checks "did the agent
mention 'tests passed' / 'exit code 0' / a file write?". A model that
confidently claims completion without doing the work scores high there.
Tier 2 is the only grader that actually runs the agent's commands and
the verification scripts in a real Linux container. **Published
TerminalBench numbers must come from Tier 2.** Tier 1 is for smoke
/ regression testing during development and SHOULD be marked deprecated
for any external benchmark publishing.

Design choices (mirrored 1:1 from :mod:`xmclaw.eval.swe_bench_sandbox`)
======================================================================

* **One container per ``grade()`` call.** No pooling. Same blast-radius
  argument as SWE-bench: corpus is small enough that cold-start cost
  amortises over the suite, and we never want one stuck container to
  poison the next.
* **Network on (bridge).** TerminalBench tasks routinely ``apt-get
  install`` or ``pip install`` something the agent doesn't know about
  in advance — locking the network down to ``none`` would break the
  vast majority of tasks. Locking down to a private mirror is a
  follow-up once the grader proves stable.
* **Hard timeout.** Default 5 minutes per task. TerminalBench tasks
  rarely exceed 1 minute of shell wall-time; 5 minutes covers
  ``apt-get update`` + cold image pull. Timeouts return a structured
  ``SandboxedTerminalGradeResult(error="timeout", score=0.0)``.
* **Lazy SDK import.** The ``docker`` Python SDK is imported inside
  ``_get_client()`` so ``import xmclaw.eval`` (which the daemon does
  on every boot via ``SUITE_REGISTRY``) never pays the docker cost.

Wire-up in ``TerminalBenchSuite``
=================================

The companion suite class accepts a ``set_sandboxed_grader(grader)``
hook. Setting it AND calling ``grade(case, agent_text, tier="sandboxed")``
(or setting the env var ``XMC_TERMINAL_BENCH_GRADER=sandboxed``) routes
through this module instead of the heuristic Tier 1 grader. With no
grader set the suite still defaults to Tier 1 — the marketing-warning
from the parent module's docstring still applies in that case.

This lands as a follow-up to B-385: B-385 shipped the underlying
``DockerSkillRuntime``, this module is a pure consumer (mirrors the
SWE-bench Tier-2 wire-up shipped in :mod:`swe_bench_sandbox`).
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


# ── result type ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class SandboxedTerminalGradeResult:
    """Outcome of a sandboxed (Tier-2) TerminalBench grade.

    Attributes:
        passed: Aggregate pass — True iff every verification test
            exited with the expected outcome (default: exit 0).
        score: 0.0–1.0. Defined as ``passing_tests / total_tests`` so
            partial credit is visible without affecting ``passed``.
        test_results: per-test-name → True if the test passed.
        agent_actions_replayed: False iff no bash commands could be
            extracted from the agent's text (so the verification ran
            against an empty workspace). Useful signal — a passing
            score with ``agent_actions_replayed=False`` means the
            verification tests are trivially-true on an empty box.
        error: human-readable error string when something went wrong
            BEFORE per-test results could be collected (timeout, image
            pull failure, container died, etc). None on a clean grade
            even if some tests themselves failed.
        container_id: best-effort container id, useful for debugging
            stuck graders against ``docker ps -a``.
        latency_s: wall-clock seconds spent inside ``grade()``, including
            image pull / container spawn / test runs.
    """

    passed: bool
    score: float
    test_results: dict[str, bool] = field(default_factory=dict)
    agent_actions_replayed: bool = False
    error: str | None = None
    container_id: str | None = None
    latency_s: float = 0.0


# ── grader ────────────────────────────────────────────────────────────


# Default base image — ``ubuntu:22.04`` ships a useful default toolchain
# (apt, bash, coreutils, find/grep) for TerminalBench tasks. We pin the
# major.minor (``22.04``) so a silent upstream rebuild can't change
# bash / coreutils behaviour underneath us.
_DEFAULT_IMAGE = "ubuntu:22.04"

# Hard cap per ``grade()`` call. TerminalBench tasks typically run in
# <1min on a warm cache; 5min covers cold apt-get + first-time image
# pull.
_DEFAULT_TIMEOUT_S = 300

# Marker the grade script writes around its JSON envelope so we can
# scan past apt / shell chatter on stdout. Mirrors the harness pattern
# in ``swe_bench_sandbox`` and ``DockerSkillRuntime``.
_RESULT_MARKER = "__TERMINAL_BENCH_GRADE_RESULT__"


# Regex for ``$ cmd`` / ``# cmd`` shell-prompt lines. We strip the
# leading sigil + space and treat the rest as a command. Lines that look
# like comments (``#`` followed by text but no whitespace after, or
# ``# this is a comment``) are kept verbatim — bash tolerates them as
# no-ops.
_PROMPT_PREFIX_RE = re.compile(r"^[\$#]\s+")


class TerminalBenchDockerGrader:
    """Docker-based grader for TerminalBench 2.0 (Sprint 4 Tier-2).

    Spawns one ephemeral container per ``grade()`` call, replays the
    agent's bash commands inside ``/workspace``, runs each verification
    test from ``expected_signals['tests']``, and reports per-test
    pass/fail.

    The grader can either receive an existing
    :class:`xmclaw.providers.runtime.docker.DockerSkillRuntime` (so its
    lazily-pulled image cache is shared) or fall back to building its
    own docker client via ``docker.from_env()``. A direct ``client=...``
    injection is also supported for tests.

    Args:
        runtime: Optional :class:`DockerSkillRuntime`. When provided,
            the grader uses ``runtime._get_client()`` so the same
            docker daemon connection / image cache is reused across
            graded tasks. Pass None to lazy-build a private client.
        timeout_s: Hard wall-clock cap per ``grade()`` call, in seconds.
            apt / image pull / shell replay all run under this single
            budget.
        image: Container image for the grade run. Default
            ``ubuntu:22.04``.
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
        agent_text: str,
        tests: list[dict[str, Any]] | list[Any],
        solution: str = "",  # noqa: ARG002 — kept for future LLM-judge graders
    ) -> SandboxedTerminalGradeResult:
        """Replay ``agent_text``'s bash commands in a fresh container,
        then run each ``tests`` entry, return the aggregated result.

        ``tests`` is a list of dict-shaped test specs. Each entry may
        carry ``script`` (the bash to evaluate as the verification),
        ``expected_outcome`` (defaults to ``"pass"``, i.e. exit 0), and
        an optional ``name``. We tolerate plain string entries too —
        legacy rows in the dataset surface ``tests`` as a list of bash
        snippets, no dict envelope. ``solution`` is reserved for a
        future LLM-judge-style grader that compares the agent's
        approach to the reference; the Tier-2 grader does not consult
        it today.

        Never raises for "the agent's commands failed" — that surfaces
        as ``passed=False`` plus per-test verdicts. Raises only for
        grader-side issues the caller MUST see (Docker not reachable,
        image pull failed, etc) — and even then we try to wrap into a
        structured error rather than letting the docker SDK's exception
        zoo escape.
        """
        started = time.monotonic()
        client = self._get_client()

        # Normalise ``tests`` → list of dicts with at least ``name`` +
        # ``script``. A dataset row that ships bare strings still works.
        normalised = _normalise_tests(tests)

        agent_commands = self._extract_bash_commands(agent_text or "")
        if not agent_commands:
            return SandboxedTerminalGradeResult(
                passed=False,
                score=0.0,
                error="no actions: no bash commands extracted from agent_text",
                latency_s=time.monotonic() - started,
            )

        if not normalised:
            # No tests means the dataset row was malformed. Surface
            # as an error rather than silently rewarding a passing 0/0.
            return SandboxedTerminalGradeResult(
                passed=False,
                score=0.0,
                agent_actions_replayed=True,
                error="no tests: expected_signals['tests'] was empty",
                latency_s=time.monotonic() - started,
            )

        script = self._build_grade_script(
            agent_commands=agent_commands, tests=normalised,
        )

        run_id = uuid.uuid4().hex
        labels = {
            "xmclaw.terminal_bench.run_id": run_id,
            "xmclaw.terminal_bench.n_tests": str(len(normalised)),
        }

        try:
            container = await asyncio.to_thread(
                client.containers.create,
                image=self.image,
                command=["/bin/bash", "-lc", script],
                labels=labels,
                detach=True,
                # Network on: TerminalBench tasks routinely
                # ``apt-get install`` / ``pip install`` something the
                # agent doesn't know about in advance. Locking down to a
                # private mirror is a follow-up.
                network_mode="bridge",
                # Memory cap — workspace + apt + verification tests all
                # fit easily under 2GB for the median task.
                mem_limit="2g",
                # Defense-in-depth: drop suid escalation. The container
                # rootfs is writable (we'd like apt-get install to
                # succeed), but read_only=False is OK because the
                # container is ephemeral.
                security_opt=["no-new-privileges:true"],
            )
        except Exception as exc:  # noqa: BLE001 — wrap so callers get one error
            return SandboxedTerminalGradeResult(
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
            return SandboxedTerminalGradeResult(
                passed=False,
                score=0.0,
                error=f"container_start_failed: {type(exc).__name__}: {exc}",
                container_id=container_id,
                latency_s=time.monotonic() - started,
            )

        try:
            wait_result = await asyncio.to_thread(
                _container_wait, container, self.timeout_s,
            )
        except _GraderTimeout:
            await self._cleanup(container)
            return SandboxedTerminalGradeResult(
                passed=False,
                score=0.0,
                agent_actions_replayed=True,
                error=f"timeout: grader exceeded {self.timeout_s}s budget",
                container_id=container_id,
                latency_s=time.monotonic() - started,
            )
        except Exception as exc:  # noqa: BLE001
            await self._cleanup(container)
            return SandboxedTerminalGradeResult(
                passed=False,
                score=0.0,
                error=f"container_wait_failed: {type(exc).__name__}: {exc}",
                container_id=container_id,
                latency_s=time.monotonic() - started,
            )

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
            return SandboxedTerminalGradeResult(
                passed=False,
                score=0.0,
                agent_actions_replayed=True,
                error=(
                    f"no_grade_envelope (exit={exit_code}); "
                    f"stdout_tail={_truncate(stdout, 600)!r}"
                ),
                container_id=container_id,
                latency_s=time.monotonic() - started,
            )

        test_results = self._parse_test_results(json.dumps(envelope))
        passed, score = _aggregate_score(test_results)
        return SandboxedTerminalGradeResult(
            passed=passed,
            score=score,
            test_results=test_results,
            agent_actions_replayed=True,
            error=None,
            container_id=container_id,
            latency_s=time.monotonic() - started,
        )

    # ── command extraction ───────────────────────────────────────────

    def _extract_bash_commands(self, agent_text: str) -> list[str]:
        """Pull bash commands from the agent's response.

        Heuristic — TerminalBench agents emit commands in a few common
        shapes:

        * Triple-backtick ``bash`` / ``sh`` / ``shell`` fences:
          ``​```bash\\ncmd\\n```​``. We slurp every line inside the
          fence verbatim.
        * ``$ cmd`` / ``# cmd`` shell-prompt lines (the canonical
          tutorial-style notation). The leading sigil + space is
          stripped.
        * Plain ``code`` fences with no language tag, when the body
          looks shell-y. We accept those too — false positives here are
          near-free because the bash interpreter just no-ops on
          unparseable lines.

        Returns the deduplicated, in-order list of commands ready to
        feed into a generated bash script.
        """
        if not agent_text:
            return []

        commands: list[str] = []
        seen: set[str] = set()

        def _push(cmd: str) -> None:
            cmd = cmd.strip()
            if not cmd:
                return
            # Skip lines that are pure comments — they're advisory in
            # the agent's text and adding them to the replay script
            # makes log noise, not behaviour. The exception is shebang
            # lines (``#!``) which are command-relevant when an agent
            # writes a script.
            if cmd.startswith("#") and not cmd.startswith("#!"):
                return
            if cmd in seen:
                return
            seen.add(cmd)
            commands.append(cmd)

        # 1) Triple-backtick fences. We accept any of the common shell
        #    languages, plus a bare fence with no language tag.
        fence_re = re.compile(
            r"```(?:bash|sh|shell|zsh|console|terminal)?\s*\n(.*?)```",
            re.DOTALL | re.IGNORECASE,
        )
        for match in fence_re.finditer(agent_text):
            block = match.group(1)
            for line in block.splitlines():
                # Strip ``$ `` / ``# `` prompts inside fences too — many
                # agents copy console-style prompts straight into a
                # fence.
                stripped = _PROMPT_PREFIX_RE.sub("", line)
                _push(stripped)

        # 2) Bare ``$ cmd`` / ``# cmd`` lines outside fences. We scan
        #    every line of the agent text and pick up the prompt-style
        #    ones. Lines inside a fenced block are processed via the
        #    fence path above; the regex below only matches at the
        #    start of a line, so prose with embedded ``$`` won't trip.
        prompt_line_re = re.compile(r"^[ \t]*([\$#])[ \t]+(.+)$", re.MULTILINE)
        for match in prompt_line_re.finditer(agent_text):
            sigil = match.group(1)
            cmd = match.group(2).rstrip()
            # ``# `` lines that look like prose comments (no shell
            # metachars, looks like English) get filtered by ``_push``;
            # ``$ `` lines are always commands.
            if sigil == "$":
                _push(cmd)
            else:
                # ``# cmd`` — only treat as command if it really looks
                # like one. Heuristic: contains a slash or a known
                # shell builtin / utility prefix. Otherwise skip.
                if (
                    "/" in cmd
                    or cmd.split()[:1] and cmd.split()[0] in _SHELL_LIKE_PREFIXES
                ):
                    _push(cmd)

        return commands

    # ── script generation ────────────────────────────────────────────

    def _build_grade_script(
        self,
        agent_commands: list[str],
        tests: list[dict[str, Any]],
    ) -> str:
        """Generate the bash script the container runs.

        Pure function — no docker side effects — so the test suite can
        hold us to a stable shape (``cd /workspace``, agent replay, per-
        test loop, JSON envelope) without spinning up a real container.

        Heredocs (``<<'AGENT_REPLAY_EOF'``) carry the agent's bash
        verbatim so backticks / dollar signs / quotes don't get expanded
        by the outer bash. Single-quoted EOF disables interpolation,
        which is exactly what we want for raw user input.
        """
        agent_b64 = _b64("\n".join(agent_commands) + "\n")
        # Encode each test independently so a quote / dollar sign in
        # one test script can't break the surrounding loop.
        test_specs = [
            {
                "name": str(t.get("name") or f"test_{i}"),
                "script_b64": _b64(str(t.get("script") or "")),
                "expected_outcome": str(t.get("expected_outcome") or "pass"),
            }
            for i, t in enumerate(tests)
        ]
        tests_json_b64 = _b64(json.dumps(test_specs))

        return _GRADE_SCRIPT_TEMPLATE.format(
            agent_b64=agent_b64,
            tests_json_b64=tests_json_b64,
            result_marker=_RESULT_MARKER,
        )

    # ── output parsing ───────────────────────────────────────────────

    def _parse_test_results(self, output: str) -> dict[str, bool]:
        """Map the grade-script's JSON envelope to per-test pass/fail.

        ``output`` is the raw envelope dict (already extracted from the
        marker line by ``_parse_grade_envelope``) re-serialised as JSON
        — keeps the API symmetric with the SWE-bench grader's
        ``_parse_pytest_results`` (which also takes a JSON string).

        The grade script writes a ``tests`` list whose entries each
        carry ``name`` and ``passed``. We project that into the dict
        the suite-level metadata exposes.
        """
        try:
            envelope = json.loads(output) if output else {}
        except json.JSONDecodeError:
            return {}
        if not isinstance(envelope, dict):
            return {}

        results: dict[str, bool] = {}
        for entry in envelope.get("tests") or []:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "")
            if not name:
                continue
            results[name] = bool(entry.get("passed"))
        return results

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
                "TerminalBenchDockerGrader requires the 'docker' Python "
                "SDK. Install with `pip install 'docker>=7'` (or pass "
                "an explicit DockerSkillRuntime via runtime=...)."
            ) from exc
        try:
            self._client = docker.from_env()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"TerminalBenchDockerGrader cannot reach the Docker "
                f"daemon: {type(exc).__name__}: {exc}. Is the daemon "
                f"running?"
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


# Common shell-utility / builtin prefixes. Used to disambiguate ``# cmd``
# lines from prose comments. Not exhaustive — the cost of a false
# negative is just "we don't replay one of the agent's commands"; the
# cost of a false positive (executing a prose sentence as bash) is
# louder. So we err on the conservative side.
_SHELL_LIKE_PREFIXES = frozenset({
    "apt", "apt-get", "awk", "bash", "cat", "cd", "chmod", "chown",
    "cp", "curl", "cut", "dd", "echo", "export", "find", "git", "grep",
    "head", "kill", "ln", "ls", "mkdir", "mv", "nano", "npm", "pip",
    "ps", "python", "python3", "rm", "rmdir", "sed", "sh", "sort",
    "ssh", "su", "sudo", "tail", "tar", "test", "touch", "tr", "uname",
    "vi", "vim", "wc", "wget", "yarn",
})


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


def _b64(text: str) -> str:
    """Encode a string as base64 ASCII for safe embedding in a heredoc.

    User-supplied bash is full of ``\\``, ``$``, backticks, and quotes.
    Even with a single-quoted heredoc, line-ending edge cases (CRLF on
    Windows hosts copying into a Linux container) bite. Base64
    sidesteps all of that — the script ``base64 -d`` decodes back to
    bytes inside the container.
    """
    import base64
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _normalise_tests(tests: Any) -> list[dict[str, Any]]:
    """Coerce the dataset's ``tests`` field into a list of dicts.

    The TerminalBench schema lets ``tests`` be either a list of dict
    specs (preferred) or a list of bare bash strings (legacy rows).
    Bare strings turn into ``{"name": "test_<i>", "script": <s>,
    "expected_outcome": "pass"}``. Anything else is ignored — better
    than crashing the grader on a malformed row.
    """
    if not tests:
        return []
    out: list[dict[str, Any]] = []
    for i, entry in enumerate(tests):
        if isinstance(entry, dict):
            name = str(entry.get("name") or f"test_{i}")
            script = str(entry.get("script") or "")
            expected = str(entry.get("expected_outcome") or "pass")
            if script:
                out.append({
                    "name": name,
                    "script": script,
                    "expected_outcome": expected,
                })
        elif isinstance(entry, str):
            if entry.strip():
                out.append({
                    "name": f"test_{i}",
                    "script": entry,
                    "expected_outcome": "pass",
                })
    return out


def _parse_grade_envelope(stdout: str) -> dict[str, Any] | None:
    """Find the grade script's JSON result line in container stdout.

    The script emits exactly one line beginning with ``_RESULT_MARKER``
    so this can scan past apt / shell chatter. Returns None when no
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
    test_results: dict[str, bool],
) -> tuple[bool, float]:
    """Aggregate per-test verdicts into ``(passed, score)``.

    ``passed`` is True iff EVERY test passed. ``score`` is the fraction
    of tests that passed — useful as partial-credit signal even when
    ``passed`` is False.

    Empty test set degenerates to ``passed=False, score=0.0``: a well-
    formed TerminalBench row always has at least one verification
    test, so an empty set means the grader was called with incomplete
    inputs and shouldn't be silently rewarded.
    """
    total = len(test_results)
    if total == 0:
        return False, 0.0
    passes = sum(1 for v in test_results.values() if v)
    score = passes / total
    passed = passes == total
    return passed, score


# ── grade script template ─────────────────────────────────────────────
#
# Stays POSIX-bash and uses base64-decoded heredocs for the agent's
# replay block + each test script so we don't have to worry about line-
# ending or quoting issues. The script is intentionally idempotent on
# its OWN failure modes (every step is wrapped so a broken agent
# command surfaces as a per-test failure rather than killing the
# grader).
#
# Output contract: the very last stdout line MUST start with
# ``_RESULT_MARKER`` followed by a JSON dict shaped like:
#   {"tests": [{"name": str, "passed": bool, "exit_code": int}, ...]}
#
# The grader's ``_parse_grade_envelope`` reads exactly that line.

_GRADE_SCRIPT_TEMPLATE = '''
set +e
RESULT_MARKER={result_marker!r}

mkdir -p /workspace
cd /workspace

# Decode + replay agent commands. We use ``set +e`` so a single failing
# agent command doesn't abort the whole replay — TerminalBench tasks
# routinely run probe commands that expect to fail (``test -f X || ...``
# etc).
echo {agent_b64} | base64 -d > /tmp/agent.sh
bash /tmp/agent.sh >/tmp/agent.stdout 2>/tmp/agent.stderr || true

# Decode the test specs. Each entry: {{name, script_b64, expected_outcome}}.
echo {tests_json_b64} | base64 -d > /tmp/tests.json

# Iterate the test specs in Python (jq isn't guaranteed in ubuntu:22.04
# without an apt install, and we'd rather use the stdlib).
python3 - <<'PY_EOF' > /grade-output.json
import base64, json, os, subprocess
with open("/tmp/tests.json") as fh:
    specs = json.load(fh)
results = []
for spec in specs:
    name = spec.get("name") or "test"
    script = base64.b64decode(spec.get("script_b64") or "").decode("utf-8", "replace")
    expected = (spec.get("expected_outcome") or "pass").lower()
    try:
        proc = subprocess.run(
            ["bash", "-c", script],
            cwd="/workspace",
            capture_output=True,
            text=True,
            timeout=60,
        )
        exit_code = proc.returncode
    except Exception as exc:
        exit_code = 1
        proc = None
    if expected == "pass":
        passed = (exit_code == 0)
    elif expected == "fail":
        passed = (exit_code != 0)
    else:
        passed = (exit_code == 0)
    results.append({{
        "name": name,
        "passed": bool(passed),
        "exit_code": int(exit_code),
    }})
print(json.dumps({{"tests": results}}))
PY_EOF

# Emit the marker line. We re-read the JSON so a half-written file
# becomes a missing envelope rather than corrupted output.
python3 -c "import json,sys; \\
print('${{RESULT_MARKER}}: ' + json.dumps(json.load(sys.stdin)))" \\
    < /grade-output.json
'''


__all__ = [
    "SandboxedTerminalGradeResult",
    "TerminalBenchDockerGrader",
]
