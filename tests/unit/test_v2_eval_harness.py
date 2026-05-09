"""Sprint 4 base — tests for the A/B benchmark harness.

Coverage targets:

* TaskCase / TaskResult / SuiteResult dataclass shape (frozen, defaults).
* ``Runner.run`` with a fake suite + mocked agent — pass_rate, mean_score,
  cost / latency aggregation, ``limit`` honoured.
* ``Runner`` resilience — per-task agent failures + per-task grader
  failures both surface as ``TaskResult(passed=False, error=...)``
  without aborting the suite.
* LongMemEval mini suite — ≥5 tasks, grader's case-insensitive substring
  semantics, multi-answer "all-must-match" mode for "two/both/and"
  questions, empty-text and missing-answer paths.
* ``xmclaw eval`` CLI smoke (``list``, ``run --limit --out``, bad suite id).
"""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from xmclaw.cli.eval import eval_app
from xmclaw.cli.main import app as root_app
from xmclaw.eval import (
    SUITE_REGISTRY,
    BenchmarkSuite,
    LongMemEvalMiniSuite,
    Runner,
    SuiteResult,
    TaskCase,
    TaskResult,
)


# ── dataclass shape ────────────────────────────────────────────────────


def test_taskcase_is_frozen():
    case = TaskCase(task_id="x", prompt="p", expected_signals={})
    assert dataclasses.is_dataclass(case)
    with pytest.raises(dataclasses.FrozenInstanceError):
        case.task_id = "y"  # type: ignore[misc]


def test_taskcase_metadata_defaults_to_empty_dict():
    case = TaskCase(task_id="x", prompt="p", expected_signals={"a": 1})
    assert case.metadata == {}
    # Each instance gets its own dict (no shared default mutable).
    case2 = TaskCase(task_id="y", prompt="q", expected_signals={})
    assert case.metadata is not case2.metadata


def test_taskresult_required_fields_and_defaults():
    r = TaskResult(
        task_id="t1",
        agent_text="hi",
        passed=True,
        score=0.75,
        turns=3,
        cost_usd=0.001,
        latency_s=1.2,
    )
    assert r.error is None
    assert r.metadata == {}
    assert r.passed is True
    assert r.score == 0.75


def test_suiteresult_to_dict_round_trips_via_json():
    r = TaskResult("t1", "hi", True, 1.0, 1, 0.0, 0.5)
    s = SuiteResult(
        suite_id="demo",
        n_tasks=1,
        n_passed=1,
        pass_rate=1.0,
        mean_score=1.0,
        total_cost_usd=0.0,
        total_latency_s=0.5,
        results=(r,),
    )
    payload = json.dumps(s.to_dict())
    parsed = json.loads(payload)
    assert parsed["suite_id"] == "demo"
    assert parsed["n_tasks"] == 1
    assert parsed["results"][0]["task_id"] == "t1"


# ── Fake suite + mock agents for Runner tests ──────────────────────────


class _FakeSuite(BenchmarkSuite):
    """3-task suite. The agent's response includes the task id; the
    grader passes when the response contains 'win-' for that id."""

    @property
    def suite_id(self) -> str:
        return "fake"

    def load_tasks(self, limit=None):
        cases = [
            TaskCase("a", "prompt-a", {"want": "win-a"}),
            TaskCase("b", "prompt-b", {"want": "win-b"}),
            TaskCase("c", "prompt-c", {"want": "win-c"}),
        ]
        return cases if limit is None else cases[:limit]

    def grade(self, case, agent_text, **extra):
        want = case.expected_signals["want"]
        passed = want in agent_text
        return passed, (1.0 if passed else 0.0), {"want": want}


def _stub_agent_factory(canned: dict[str, str], cost_per_call: float = 0.001):
    """Returns an agent factory whose ``arun`` looks up canned text
    by the prompt suffix (e.g. 'prompt-a' → canned['a'])."""
    def _factory():
        async def _arun(prompt: str) -> dict:
            tail = prompt.rsplit("-", 1)[-1]
            return {"text": canned.get(tail, ""), "turns": 2, "cost_usd": cost_per_call}
        return type("Agent", (), {"arun": staticmethod(_arun)})()
    return _factory


@pytest.mark.asyncio
async def test_runner_aggregates_pass_rate_and_mean_score():
    # 2 of 3 tasks should pass.
    factory = _stub_agent_factory({"a": "win-a", "b": "loss", "c": "win-c"})
    runner = Runner(factory, _FakeSuite())
    res = await runner.run()
    assert res.n_tasks == 3
    assert res.n_passed == 2
    assert res.pass_rate == pytest.approx(2 / 3)
    assert res.mean_score == pytest.approx(2 / 3)
    assert res.total_cost_usd == pytest.approx(0.003)
    assert res.total_latency_s >= 0.0


@pytest.mark.asyncio
async def test_runner_limit_kwarg_honoured():
    factory = _stub_agent_factory({"a": "win-a", "b": "win-b", "c": "win-c"})
    res = await Runner(factory, _FakeSuite()).run(limit=2)
    assert res.n_tasks == 2
    # Only the first two tasks ran.
    assert {r.task_id for r in res.results} == {"a", "b"}


@pytest.mark.asyncio
async def test_runner_limit_zero_is_noop():
    factory = _stub_agent_factory({})
    # limit=None means "all"; the CLI passes None when --limit is 0.
    res = await Runner(factory, _FakeSuite()).run(limit=None)
    assert res.n_tasks == 3


@pytest.mark.asyncio
async def test_runner_per_task_agent_exception_is_caught():
    """Agent raising on task 'b' must not poison tasks 'a' and 'c'."""
    def _factory():
        async def _arun(prompt: str) -> str:
            tail = prompt.rsplit("-", 1)[-1]
            if tail == "b":
                raise RuntimeError("simulated agent crash")
            return f"win-{tail}"
        return type("Agent", (), {"arun": staticmethod(_arun)})()

    res = await Runner(_factory, _FakeSuite()).run()
    assert res.n_tasks == 3
    assert res.n_passed == 2  # a and c still pass
    failed = [r for r in res.results if r.task_id == "b"][0]
    assert failed.passed is False
    assert failed.error is not None
    assert "simulated agent crash" in failed.error


@pytest.mark.asyncio
async def test_runner_factory_exception_is_caught():
    """A factory that raises mid-suite must not abort remaining tasks."""
    counter = {"n": 0}

    def _factory():
        counter["n"] += 1
        if counter["n"] == 2:
            raise RuntimeError("factory blew up on task 2")
        async def _arun(prompt: str) -> str:
            tail = prompt.rsplit("-", 1)[-1]
            return f"win-{tail}"
        return type("Agent", (), {"arun": staticmethod(_arun)})()

    res = await Runner(_factory, _FakeSuite()).run()
    assert res.n_tasks == 3
    failed = [r for r in res.results if r.error is not None]
    assert len(failed) == 1
    assert "factory blew up" in failed[0].error


@pytest.mark.asyncio
async def test_runner_grader_exception_is_caught():
    class _BoomGrader(_FakeSuite):
        def grade(self, case, agent_text, **extra):
            raise ValueError("grader logic error")

    factory = _stub_agent_factory({"a": "x", "b": "y", "c": "z"})
    res = await Runner(factory, _BoomGrader()).run()
    # All three tasks ran; all three failed via grader.
    assert res.n_tasks == 3
    assert res.n_passed == 0
    assert all("grader" in (r.error or "") for r in res.results)


@pytest.mark.asyncio
async def test_runner_score_is_clamped_to_0_1():
    class _OverScoringSuite(_FakeSuite):
        def grade(self, case, agent_text, **extra):
            return True, 1.7, {}

    factory = _stub_agent_factory({"a": "x", "b": "y", "c": "z"})
    res = await Runner(factory, _OverScoringSuite()).run()
    for r in res.results:
        assert r.score == 1.0  # clamped
    assert res.mean_score == 1.0


@pytest.mark.asyncio
async def test_runner_supports_run_turn_shape():
    """Real ``AgentLoop`` exposes ``run_turn(session_id, prompt) -> AgentTurnResult``."""
    class _FakeTurn:
        def __init__(self, text, hops, cost_usd):
            self.text, self.hops, self.cost_usd = text, hops, cost_usd

    def _factory():
        async def _run_turn(session_id, prompt):
            tail = prompt.rsplit("-", 1)[-1]
            return _FakeTurn(f"win-{tail}", 4, 0.002)
        return type("Loop", (), {"run_turn": staticmethod(_run_turn)})()

    res = await Runner(_factory, _FakeSuite()).run()
    assert res.n_passed == 3
    assert all(r.turns == 4 for r in res.results)
    assert res.total_cost_usd == pytest.approx(0.006)


@pytest.mark.asyncio
async def test_runner_supports_callable_agent_stub():
    """A bare callable that returns text is accepted (test-stub path)."""
    def _factory():
        return lambda prompt: f"win-{prompt.rsplit('-', 1)[-1]}"
    res = await Runner(_factory, _FakeSuite()).run()
    assert res.n_passed == 3


def test_runner_rejects_non_callable_factory():
    with pytest.raises(TypeError):
        Runner("not a callable", _FakeSuite())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_runner_rejects_bad_agent_shape():
    """A factory producing an object with no arun / run_turn / __call__
    must surface a typed error, not a confusing AttributeError."""
    def _factory():
        return object()  # bare object — nothing to call

    res = await Runner(_factory, _FakeSuite()).run()
    # All tasks failed with a typed error.
    assert all(r.passed is False for r in res.results)
    assert all(r.error and "Runner cannot drive" in r.error for r in res.results)


# ── LongMemEval mini suite ─────────────────────────────────────────────


def test_longmemeval_mini_suite_id_and_registry():
    s = LongMemEvalMiniSuite()
    assert s.suite_id == "longmemeval-mini"
    assert "longmemeval-mini" in SUITE_REGISTRY


def test_longmemeval_mini_loads_at_least_five_cases():
    cases = LongMemEvalMiniSuite().load_tasks()
    assert len(cases) >= 5
    for c in cases:
        assert c.task_id.startswith("lme-mini-")
        assert "Question" not in c.prompt or "answer" in c.prompt.lower()
        assert isinstance(c.expected_signals.get("answers"), list)
        assert c.expected_signals["answers"]


def test_longmemeval_mini_load_respects_limit():
    cases = LongMemEvalMiniSuite().load_tasks(limit=2)
    assert len(cases) == 2


def test_longmemeval_mini_load_zero_limit():
    cases = LongMemEvalMiniSuite().load_tasks(limit=0)
    assert cases == []


def test_longmemeval_mini_load_negative_limit_raises():
    with pytest.raises(ValueError):
        LongMemEvalMiniSuite().load_tasks(limit=-1)


def test_longmemeval_mini_grader_passes_on_substring():
    suite = LongMemEvalMiniSuite()
    case = suite.load_tasks(limit=1)[0]  # dog Bowser case
    passed, score, meta = suite.grade(case, "the answer is BOWSER, my dog")
    assert passed is True
    assert score == 1.0
    assert meta["matched"] in ("bowser", ["bowser"])


def test_longmemeval_mini_grader_fails_on_absence():
    suite = LongMemEvalMiniSuite()
    case = suite.load_tasks(limit=1)[0]
    passed, score, meta = suite.grade(case, "I have no idea")
    assert passed is False
    assert score == 0.0


def test_longmemeval_mini_grader_handles_empty_text():
    suite = LongMemEvalMiniSuite()
    case = suite.load_tasks(limit=1)[0]
    passed, score, meta = suite.grade(case, "")
    assert passed is False
    assert score == 0.0
    assert "empty" in (meta.get("reason") or "")


def test_longmemeval_mini_grader_all_must_match_for_two_question():
    """Case 7 ('which two things am I allergic to?') needs BOTH answers
    to count as a pass; ANY-match would be too lenient."""
    suite = LongMemEvalMiniSuite()
    cases = suite.load_tasks()
    allergy_case = [c for c in cases if "allergic" in c.expected_signals.get("question", "")][0]
    passed_partial, score_partial, _ = suite.grade(
        allergy_case, "you are allergic to penicillin",
    )
    assert passed_partial is False
    assert 0.0 < score_partial < 1.0
    passed_full, score_full, _ = suite.grade(
        allergy_case, "you are allergic to penicillin and shellfish",
    )
    assert passed_full is True
    assert score_full == 1.0


@pytest.mark.asyncio
async def test_longmemeval_mini_runner_end_to_end():
    """Drive the mini suite with a stub agent that 'cheats' by reading the
    expected_signals — proves the harness wires up clean."""
    suite = LongMemEvalMiniSuite()

    def _factory():
        async def _arun(prompt: str) -> str:
            # Cheat: extract the question from the prompt and answer
            # 'bowser' (matches the first case). For the others, return
            # empty so we get a mixed pass/fail.
            return "the answer is bowser"
        return type("Agent", (), {"arun": staticmethod(_arun)})()

    res = await Runner(_factory, suite).run(limit=3)
    assert res.n_tasks == 3
    assert res.n_passed >= 1  # bowser case passes


# ── CLI smoke ──────────────────────────────────────────────────────────


def test_cli_eval_list_lists_longmemeval():
    runner = CliRunner()
    out = runner.invoke(eval_app, ["list"])
    assert out.exit_code == 0
    assert "longmemeval-mini" in out.stdout


def test_cli_eval_run_unknown_suite_id_exits_nonzero():
    runner = CliRunner()
    out = runner.invoke(eval_app, ["run", "no-such-suite", "--limit", "1"])
    assert out.exit_code != 0
    assert "unknown suite" in out.stdout or "unknown suite" in (out.stderr or "")


def test_cli_eval_run_emits_valid_json(tmp_path: Path):
    runner = CliRunner()
    out_file = tmp_path / "result.json"
    res = runner.invoke(
        eval_app,
        [
            "run", "longmemeval-mini",
            "--limit", "2",
            "--out", str(out_file),
            "--config", str(tmp_path / "nonexistent.json"),  # forces stub agent
        ],
    )
    assert res.exit_code == 0, res.stdout
    assert out_file.exists()
    payload = json.loads(out_file.read_text(encoding="utf-8"))
    assert payload["suite_id"] == "longmemeval-mini"
    assert payload["n_tasks"] == 2
    assert isinstance(payload["results"], list)
    assert len(payload["results"]) == 2


def test_cli_eval_run_stdout_when_no_out(tmp_path: Path):
    runner = CliRunner()
    res = runner.invoke(
        eval_app,
        [
            "run", "longmemeval-mini",
            "--limit", "1",
            "--config", str(tmp_path / "nonexistent.json"),
        ],
    )
    assert res.exit_code == 0
    # stdout contains a JSON object with the suite_id. The CLI prints
    # any preamble (warnings about a missing config) before the JSON,
    # so locate the first '{' and parse from there.
    assert "longmemeval-mini" in res.stdout
    brace = res.stdout.index("{")
    payload = json.loads(res.stdout[brace:])
    assert payload["suite_id"] == "longmemeval-mini"


def test_cli_eval_ab_requires_config(tmp_path: Path):
    """A/B against the stub agent yields no signal — the CLI should
    bail out with a clear error rather than emit zeroes."""
    runner = CliRunner()
    res = runner.invoke(
        eval_app,
        [
            "ab", "claude-haiku", "claude-sonnet",
            "--suite", "longmemeval-mini",
            "--config", str(tmp_path / "nonexistent.json"),
        ],
    )
    assert res.exit_code != 0


def test_cli_eval_wired_into_root_app():
    """Sanity: ``xmclaw eval`` is reachable from the root typer app."""
    runner = CliRunner()
    out = runner.invoke(root_app, ["eval", "list"])
    assert out.exit_code == 0
    assert "longmemeval-mini" in out.stdout
