"""Sprint 4 Tier-2 — sandboxed grader unit tests.

These tests exercise :mod:`xmclaw.eval.swe_bench_sandbox` without
touching Docker. The grader's docker SDK client is fully mocked via
the ``client=`` constructor injection, so:

* ``import xmclaw.eval.swe_bench_sandbox`` is enough to load the module.
* ``SWEBenchDockerGrader(client=mock).grade(...)`` exercises the full
  spawn → wait → parse pipeline against scripted container outputs.

Coverage:

* Dataclass shape — ``SandboxedGradeResult`` carries the right fields
  and is frozen.
* Script generation — the bash payload contains the expected
  ``git clone`` / ``git checkout`` / ``git apply`` / ``pytest`` steps
  and base64-embeds both the test_patch and the agent patch.
* Pytest result parsing — F2P/P2P verdicts are derived from
  ``--json-report`` outcomes; missing tests count as ``False``.
* End-to-end happy path — mocked container returns a passing report,
  ``grade()`` returns ``passed=True, score=1.0``.
* Patch-apply failure — envelope reports ``patch_applied=False`` →
  result has ``patch_applied=False, score=0.0``.
* Timeout — mocked ``container.wait`` raises a ``Timeout``-named
  exception → result has ``error="timeout: ..."`` and ``score=0.0``.
* Suite wire-up — ``SWEBenchVerifiedSuite.set_sandboxed_grader`` +
  ``grade(case, agent_text, tier="sandboxed")`` routes through the
  grader; default ``tier="heuristic"`` does NOT.
* Env override — ``XMC_SWE_BENCH_GRADER=sandboxed`` auto-promotes the
  default tier.
"""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from xmclaw.eval.harness import TaskCase
from xmclaw.eval.swe_bench_sandbox import (
    SWEBenchDockerGrader,
    SandboxedGradeResult,
    _aggregate_score,
    _parse_grade_envelope,
)
from xmclaw.eval.swe_bench_verified import SWEBenchVerifiedSuite


# ── Result dataclass shape ────────────────────────────────────────────


def test_sandboxed_grade_result_is_frozen_dataclass():
    """The result must be hashable / immutable so callers can stuff
    it into sets / use it as a dict key without surprise."""
    r = SandboxedGradeResult(passed=True, score=1.0)
    with pytest.raises(Exception):
        r.passed = False  # type: ignore[misc]


def test_sandboxed_grade_result_default_fields():
    r = SandboxedGradeResult(passed=False, score=0.0)
    assert r.fail_to_pass_results == {}
    assert r.pass_to_pass_results == {}
    assert r.patch_applied is False
    assert r.error is None
    assert r.container_id is None
    assert r.latency_s == 0.0


def test_sandboxed_grade_result_carries_all_metadata():
    r = SandboxedGradeResult(
        passed=True, score=0.5,
        fail_to_pass_results={"t::a": True, "t::b": False},
        pass_to_pass_results={"t::c": True},
        patch_applied=True,
        error=None,
        container_id="abc123",
        latency_s=12.5,
    )
    assert r.fail_to_pass_results["t::a"] is True
    assert r.fail_to_pass_results["t::b"] is False
    assert r.pass_to_pass_results["t::c"] is True
    assert r.container_id == "abc123"
    assert r.latency_s == 12.5


# ── Aggregate scoring ────────────────────────────────────────────────


def test_aggregate_score_all_pass():
    p, s = _aggregate_score(
        f2p_results={"a": True}, p2p_results={"b": True, "c": True},
    )
    assert p is True
    assert s == 1.0


def test_aggregate_score_partial():
    p, s = _aggregate_score(
        f2p_results={"a": True, "b": False},
        p2p_results={"c": True},
    )
    assert p is False
    assert s == pytest.approx(2 / 3)


def test_aggregate_score_empty_inputs():
    p, s = _aggregate_score(f2p_results={}, p2p_results={})
    assert p is False
    assert s == 0.0


# ── Script generation ────────────────────────────────────────────────


def test_build_grade_script_contains_clone_checkout_and_apply():
    grader = SWEBenchDockerGrader(client=MagicMock())
    script = grader._build_grade_script(
        agent_patch="diff --git a/foo b/foo\n--- a/foo\n+++ b/foo\n@@ -1 +1 @@\n",
        repo="django/django",
        base_commit="abc123def456",
        test_patch="diff --git a/test b/test\n",
        fail_to_pass=["tests/test_x.py::test_a"],
        pass_to_pass=["tests/test_x.py::test_b"],
    )
    assert "git clone" in script
    assert "django/django" in script
    assert "abc123def456" in script
    assert "git apply" in script
    assert "pytest" in script
    assert "--json-report" in script
    # Both patches encoded in base64 — never inlined raw, so quoting
    # never bites.
    import base64
    f2p_b64 = base64.b64encode(b"diff --git a/test b/test\n").decode()
    assert f2p_b64 in script


def test_build_grade_script_includes_test_ids_in_pytest_args():
    grader = SWEBenchDockerGrader(client=MagicMock())
    script = grader._build_grade_script(
        agent_patch="diff",
        repo="o/r",
        base_commit="abc",
        test_patch="",
        fail_to_pass=["tests/test_a.py::test_one"],
        pass_to_pass=["tests/test_b.py::test_two"],
    )
    # pytest line has both test ids quoted.
    assert "tests/test_a.py::test_one" in script
    assert "tests/test_b.py::test_two" in script


def test_build_grade_script_safely_quotes_special_chars():
    """A repo / base_commit with shell-meaningful chars must be quoted
    so the script can't be hijacked by malicious-looking dataset rows.
    (SWE-bench Verified is curated, but defence-in-depth is free here.)"""
    grader = SWEBenchDockerGrader(client=MagicMock())
    script = grader._build_grade_script(
        agent_patch="x",
        repo="o/r",
        # base_commit containing a single quote — must NOT break the
        # generated bash. We assert the script is still valid by
        # checking the closing ``'`` count is even (heuristic).
        base_commit="ab'cd",
        test_patch="",
        fail_to_pass=[],
        pass_to_pass=[],
    )
    # The single-quote escaping pattern '\'' is what _shquote uses —
    # the literal must appear in the script to confirm we didn't
    # interpolate raw.
    assert "'\\''" in script


# ── Pytest result parsing ────────────────────────────────────────────


def test_parse_pytest_results_f2p_passed_yields_true():
    grader = SWEBenchDockerGrader(client=MagicMock())
    report = {
        "tests": [
            {"nodeid": "tests/test_x.py::test_a", "outcome": "passed"},
        ]
    }
    f2p, p2p = grader._parse_pytest_results(
        json.dumps(report),
        fail_to_pass=["tests/test_x.py::test_a"],
        pass_to_pass=[],
    )
    assert f2p == {"tests/test_x.py::test_a": True}
    assert p2p == {}


def test_parse_pytest_results_f2p_failed_yields_false():
    grader = SWEBenchDockerGrader(client=MagicMock())
    report = {
        "tests": [
            {"nodeid": "tests/test_x.py::test_a", "outcome": "failed"},
        ]
    }
    f2p, _ = grader._parse_pytest_results(
        json.dumps(report),
        fail_to_pass=["tests/test_x.py::test_a"],
        pass_to_pass=[],
    )
    assert f2p == {"tests/test_x.py::test_a": False}


def test_parse_pytest_results_missing_test_yields_false():
    """Tests that didn't show up in the pytest report (e.g. collection
    failure) are recorded as False — the grader treats absence as
    'didn't pass', NOT as 'unknown / partial credit'."""
    grader = SWEBenchDockerGrader(client=MagicMock())
    report = {"tests": []}
    f2p, p2p = grader._parse_pytest_results(
        json.dumps(report),
        fail_to_pass=["tests/test_x.py::test_missing"],
        pass_to_pass=["tests/test_y.py::test_other"],
    )
    assert f2p == {"tests/test_x.py::test_missing": False}
    assert p2p == {"tests/test_y.py::test_other": False}


def test_parse_pytest_results_p2p_still_passing_yields_true():
    grader = SWEBenchDockerGrader(client=MagicMock())
    report = {
        "tests": [
            {"nodeid": "tests/test_y.py::test_b", "outcome": "passed"},
        ]
    }
    _, p2p = grader._parse_pytest_results(
        json.dumps(report),
        fail_to_pass=[],
        pass_to_pass=["tests/test_y.py::test_b"],
    )
    assert p2p == {"tests/test_y.py::test_b": True}


def test_parse_pytest_results_handles_malformed_json():
    grader = SWEBenchDockerGrader(client=MagicMock())
    f2p, p2p = grader._parse_pytest_results(
        "not valid json {{{",
        fail_to_pass=["a"],
        pass_to_pass=["b"],
    )
    # Both default to False since the report couldn't be parsed.
    assert f2p == {"a": False}
    assert p2p == {"b": False}


# ── Envelope parsing ─────────────────────────────────────────────────


def test_parse_grade_envelope_finds_marker_line():
    stdout = (
        "pip noise here\n"
        "more noise\n"
        "__SWE_BENCH_GRADE_RESULT__: {\"patch_applied\": true, \"error\": null, "
        "\"pytest_report\": {\"tests\": []}}\n"
    )
    env = _parse_grade_envelope(stdout)
    assert env is not None
    assert env["patch_applied"] is True


def test_parse_grade_envelope_returns_none_when_marker_missing():
    assert _parse_grade_envelope("nothing useful here\n") is None
    assert _parse_grade_envelope("") is None


def test_parse_grade_envelope_returns_none_when_payload_invalid_json():
    stdout = "__SWE_BENCH_GRADE_RESULT__: {not json"
    assert _parse_grade_envelope(stdout) is None


# ── End-to-end grade() flow ──────────────────────────────────────────


def _make_mock_client(
    *,
    stdout: bytes,
    exit_code: int = 0,
    wait_raises: Exception | None = None,
) -> MagicMock:
    """Build a docker.from_env()-shaped mock that returns ``stdout``
    when the grader pulls container logs."""
    client = MagicMock(name="docker_client")
    container = MagicMock(name="container")
    container.id = "mock-container-id"
    if wait_raises is not None:
        container.wait.side_effect = wait_raises
    else:
        container.wait.return_value = {"StatusCode": exit_code}
    container.logs.return_value = stdout
    client.containers.create.return_value = container
    return client


def _grade_envelope_stdout(
    *,
    patch_applied: bool,
    pytest_report: dict[str, Any] | None = None,
    error: str | None = None,
) -> bytes:
    """Build a stdout payload that the grader's parser will accept."""
    payload: dict[str, Any] = {
        "patch_applied": patch_applied,
        "error": error,
        "pytest_report": pytest_report or {},
    }
    body = (
        "pip install ...\n"
        "Cloning into 'repo'...\n"
        f"__SWE_BENCH_GRADE_RESULT__: {json.dumps(payload)}\n"
    )
    return body.encode("utf-8")


@pytest.mark.asyncio
async def test_grade_happy_path_passes_all_tests():
    """All FAIL_TO_PASS pass + all PASS_TO_PASS still pass → grade
    is passed=True, score=1.0."""
    pytest_report = {
        "tests": [
            {"nodeid": "tests/test_x.py::test_a", "outcome": "passed"},
            {"nodeid": "tests/test_y.py::test_b", "outcome": "passed"},
        ]
    }
    client = _make_mock_client(
        stdout=_grade_envelope_stdout(
            patch_applied=True, pytest_report=pytest_report,
        ),
    )
    grader = SWEBenchDockerGrader(client=client)

    result = await grader.grade(
        agent_patch="diff --git a/foo b/foo\n",
        repo="django/django",
        base_commit="abc123def456",
        fail_to_pass=["tests/test_x.py::test_a"],
        pass_to_pass=["tests/test_y.py::test_b"],
    )

    assert result.passed is True
    assert result.score == 1.0
    assert result.patch_applied is True
    assert result.fail_to_pass_results == {"tests/test_x.py::test_a": True}
    assert result.pass_to_pass_results == {"tests/test_y.py::test_b": True}
    assert result.error is None
    # Verify the container was created with the expected image + secure
    # defaults — defence-in-depth check on the spawn call.
    create_call = client.containers.create.call_args
    assert create_call.kwargs["image"] == "python:3.11-slim"
    assert "no-new-privileges:true" in create_call.kwargs["security_opt"]


@pytest.mark.asyncio
async def test_grade_rejects_invalid_repo():
    grader = SWEBenchDockerGrader(client=MagicMock())
    with pytest.raises(ValueError):
        await grader.grade(
            agent_patch="x", repo="no-slash", base_commit="abc",
            fail_to_pass=[], pass_to_pass=[],
        )


@pytest.mark.asyncio
async def test_grade_patch_apply_failure_short_circuits():
    """Envelope reports patch_applied=False → result has score=0.0,
    patch_applied=False, no per-test verdicts."""
    client = _make_mock_client(
        stdout=_grade_envelope_stdout(
            patch_applied=False,
            error="agent_patch_apply_failed",
            pytest_report={},
        ),
    )
    grader = SWEBenchDockerGrader(client=client)

    result = await grader.grade(
        agent_patch="not a real diff",
        repo="o/r",
        base_commit="abc123",
        fail_to_pass=["t::a"],
        pass_to_pass=["t::b"],
    )

    assert result.passed is False
    assert result.score == 0.0
    assert result.patch_applied is False
    assert result.error == "agent_patch_apply_failed"


@pytest.mark.asyncio
async def test_grade_timeout_returns_structured_error():
    """A wait timeout (mocked container.wait raising a Timeout-named
    exception) returns ``error="timeout: ..."`` rather than propagating
    the docker SDK's exception."""
    class MockReadTimeout(Exception):
        pass

    client = _make_mock_client(
        stdout=b"",
        wait_raises=MockReadTimeout("read timed out"),
    )
    grader = SWEBenchDockerGrader(client=client, timeout_s=10)

    result = await grader.grade(
        agent_patch="diff",
        repo="o/r",
        base_commit="abc",
        fail_to_pass=["t::a"],
        pass_to_pass=[],
    )

    assert result.passed is False
    assert result.score == 0.0
    assert result.error is not None
    assert "timeout" in result.error.lower()
    # Container should have been cleaned up (kill/remove called).
    container = client.containers.create.return_value
    assert container.kill.called or container.remove.called


@pytest.mark.asyncio
async def test_grade_no_envelope_in_stdout_surfaces_error():
    """If the script never wrote the marker line (e.g. crashed early),
    the grader reports 'no_grade_envelope' with the stdout tail for
    debugging."""
    client = _make_mock_client(
        stdout=b"some pip output but no envelope\n",
        exit_code=1,
    )
    grader = SWEBenchDockerGrader(client=client)

    result = await grader.grade(
        agent_patch="diff", repo="o/r", base_commit="abc",
        fail_to_pass=["t::a"], pass_to_pass=[],
    )

    assert result.passed is False
    assert result.score == 0.0
    assert result.error is not None
    assert "no_grade_envelope" in result.error


# ── Suite wire-up ────────────────────────────────────────────────────


def _case(
    *,
    repo: str = "django/django",
    base_commit: str = "abc123",
    test_patch: str = "",
    fail_to_pass: list[str] | None = None,
    pass_to_pass: list[str] | None = None,
) -> TaskCase:
    # ``is None`` (not ``or``) so the caller can pass ``[]`` to mean
    # "explicitly empty" — important for the env-var promotion test
    # which exercises the F2P-only path.
    if fail_to_pass is None:
        fail_to_pass = ["t::a"]
    if pass_to_pass is None:
        pass_to_pass = ["t::b"]
    return TaskCase(
        task_id="django__django-1",
        prompt="(prompt)",
        expected_signals={
            "gold_patch": "",
            "fail_to_pass": fail_to_pass,
            "pass_to_pass": pass_to_pass,
            "test_patch": test_patch,
        },
        metadata={"repo": repo, "base_commit": base_commit},
    )


def test_suite_set_sandboxed_grader_changes_has_grader_flag():
    suite = SWEBenchVerifiedSuite()
    assert suite.has_sandboxed_grader() is False

    grader = SWEBenchDockerGrader(client=MagicMock())
    suite.set_sandboxed_grader(grader)
    assert suite.has_sandboxed_grader() is True

    suite.set_sandboxed_grader(None)
    assert suite.has_sandboxed_grader() is False


def test_suite_default_tier_remains_heuristic():
    """Default ``grade()`` (no tier kwarg, no env var) must NOT route
    through the sandboxed grader even if one is wired."""
    suite = SWEBenchVerifiedSuite()
    grader = MagicMock(spec=SWEBenchDockerGrader)
    suite.set_sandboxed_grader(grader)

    diff = (
        "diff --git a/django/db/models/sql/compiler.py "
        "b/django/db/models/sql/compiler.py\n"
        "--- a/django/db/models/sql/compiler.py\n"
        "+++ b/django/db/models/sql/compiler.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-x\n+y\n"
    )
    case = _case(
        test_patch=(
            "--- a/django/db/models/sql/compiler.py\n"
            "+++ b/django/db/models/sql/compiler.py\n"
            "@@ -1,1 +1,1 @@\n"
        ),
    )
    passed, score, meta = suite.grade(case, diff)
    assert meta["tier"] == 1
    assert passed is True
    grader.grade.assert_not_called()


def test_suite_tier_sandboxed_routes_through_grader():
    """``grade(case, agent_text, tier='sandboxed')`` must hit the
    wired Docker grader, NOT the heuristic."""
    suite = SWEBenchVerifiedSuite()

    pytest_report = {
        "tests": [
            {"nodeid": "t::a", "outcome": "passed"},
            {"nodeid": "t::b", "outcome": "passed"},
        ]
    }
    client = _make_mock_client(
        stdout=_grade_envelope_stdout(
            patch_applied=True, pytest_report=pytest_report,
        ),
    )
    grader = SWEBenchDockerGrader(client=client)
    suite.set_sandboxed_grader(grader)

    passed, score, meta = suite.grade(
        _case(), "diff --git a/x b/x\n", tier="sandboxed",
    )
    assert passed is True
    assert score == 1.0
    assert meta["tier"] == 2
    assert meta["patch_applied"] is True
    assert meta["fail_to_pass_results"] == {"t::a": True}
    assert meta["pass_to_pass_results"] == {"t::b": True}


def test_suite_tier_sandboxed_without_grader_raises():
    """``grade(..., tier='sandboxed')`` with no wired grader must
    raise a clear RuntimeError telling the caller to wire one up."""
    suite = SWEBenchVerifiedSuite()
    with pytest.raises(RuntimeError) as exc_info:
        suite.grade(_case(), "diff", tier="sandboxed")
    msg = str(exc_info.value)
    assert "set_sandboxed_grader" in msg


def test_suite_env_var_promotes_default_to_sandboxed(
    monkeypatch: pytest.MonkeyPatch,
):
    """``XMC_SWE_BENCH_GRADER=sandboxed`` makes the default ``grade()``
    (no tier kwarg) route through the sandboxed grader."""
    monkeypatch.setenv("XMC_SWE_BENCH_GRADER", "sandboxed")
    suite = SWEBenchVerifiedSuite()

    pytest_report = {"tests": [{"nodeid": "t::a", "outcome": "passed"}]}
    client = _make_mock_client(
        stdout=_grade_envelope_stdout(
            patch_applied=True, pytest_report=pytest_report,
        ),
    )
    grader = SWEBenchDockerGrader(client=client)
    suite.set_sandboxed_grader(grader)

    case = _case(fail_to_pass=["t::a"], pass_to_pass=[])
    passed, score, meta = suite.grade(case, "diff --git a/x b/x\n")
    assert meta["tier"] == 2
    assert passed is True


# ── Client resolution ────────────────────────────────────────────────


def test_grader_uses_runtime_get_client_when_provided():
    """When constructed with ``runtime=...`` (a DockerSkillRuntime-shape
    object), the grader resolves its docker client via
    ``runtime._get_client()`` instead of ``docker.from_env()``."""
    fake_client = MagicMock(name="from_runtime")
    runtime = MagicMock()
    runtime._get_client.return_value = fake_client

    grader = SWEBenchDockerGrader(runtime=runtime)
    assert grader._get_client() is fake_client
    runtime._get_client.assert_called_once()


def test_grader_explicit_client_wins_over_runtime():
    """``client=`` constructor injection takes precedence over
    ``runtime=``. Used by tests so we never accidentally hit a real
    docker daemon."""
    fake_client = MagicMock(name="explicit")
    runtime = MagicMock()
    runtime._get_client.return_value = MagicMock(name="from_runtime")

    grader = SWEBenchDockerGrader(runtime=runtime, client=fake_client)
    assert grader._get_client() is fake_client
    runtime._get_client.assert_not_called()
