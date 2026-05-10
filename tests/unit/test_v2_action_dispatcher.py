"""Unit tests for Jarvis Phase 6 wiring B: real ActionDispatcher routing.

Covers the four real routes (``llm_turn`` / ``skill_invoke`` /
``tool_call`` / ``wait_for_percept``), the ``stub`` fallback when the
matching collaborator is absent, and the result-dataclass shapes.
"""
from __future__ import annotations

import time
from dataclasses import FrozenInstanceError
from typing import Any

import pytest

from xmclaw.cognition.action_dispatcher import (
    ActionDispatcher,
    PlanExecutionResult,
    StepExecutionResult,
)


# ── helpers ────────────────────────────────────────────────────────────


def make_step(
    *,
    id: str = "s1",
    action_kind: str = "llm_turn",
    payload: dict[str, Any] | None = None,
    expected_outcome: str = "ok",
    retry_policy: dict[str, Any] | None = None,
) -> Any:
    """Build a minimal step duck — matches PlanStep's public surface."""
    return type(
        "Step",
        (),
        {
            "id": id,
            "action_kind": action_kind,
            "payload": dict(payload or {}),
            "expected_outcome": expected_outcome,
            "retry_policy": dict(retry_policy or {"max_retries": 2, "backoff_s": 0.0}),
            "depends_on": (),
        },
    )()


def make_plan(*, id: str = "p1", steps: list[Any] | None = None) -> Any:
    return type("Plan", (), {"id": id, "steps": tuple(steps or [])})()


class FakeAgentLoop:
    """Captures `run_turn` calls and returns a canned answer."""

    def __init__(self, answer: Any | None = None, raises: Exception | None = None) -> None:
        self.answer = answer if answer is not None else {"text": "hello"}
        self.raises = raises
        self.calls: list[dict[str, Any]] = []

    async def run_turn(self, *, session_id: str, user_message: str) -> Any:
        self.calls.append({"session_id": session_id, "user_message": user_message})
        if self.raises is not None:
            raise self.raises
        return self.answer


class FakeSkill:
    """Minimal Skill duck — exposes `.run(SkillInput) -> SkillOutput`."""

    def __init__(
        self,
        *,
        ok: bool = True,
        result: Any = "skill-result",
        raises: Exception | None = None,
        side_effects: tuple[str, ...] = (),
    ) -> None:
        self.ok = ok
        self.result = result
        self.raises = raises
        self.side_effects = side_effects
        self.calls: list[Any] = []

    async def run(self, inp: Any) -> Any:
        self.calls.append(inp)
        if self.raises is not None:
            raise self.raises
        return type(
            "SkillOutput",
            (),
            {"ok": self.ok, "result": self.result, "side_effects": list(self.side_effects)},
        )()


class FakeSkillRegistry:
    """Skill lookup duck — supports both `find` and `get`."""

    def __init__(self, skills: dict[str, FakeSkill] | None = None) -> None:
        self.skills = skills or {}
        self.find_calls: list[str] = []
        self.get_calls: list[str] = []

    def find(self, intent: str) -> FakeSkill | None:
        self.find_calls.append(intent)
        return self.skills.get(intent)

    def get(self, skill_id: str, version: int | None = None) -> FakeSkill:
        self.get_calls.append(skill_id)
        if skill_id not in self.skills:
            raise LookupError(f"unknown skill: {skill_id}")
        return self.skills[skill_id]


class FakeToolProvider:
    """Captures invoke calls and returns a canned ToolResult-shape."""

    def __init__(
        self,
        *,
        ok: bool = True,
        content: Any = "tool-content",
        error: str | None = None,
        side_effects: tuple[str, ...] = (),
        raises: Exception | None = None,
    ) -> None:
        self.ok = ok
        self.content = content
        self.error = error
        self.side_effects = side_effects
        self.raises = raises
        self.calls: list[Any] = []

    async def invoke(self, call: Any) -> Any:
        self.calls.append(call)
        if self.raises is not None:
            raise self.raises
        return type(
            "ToolResult",
            (),
            {
                "call_id": getattr(call, "id", "x"),
                "ok": self.ok,
                "content": self.content,
                "error": self.error,
                "side_effects": self.side_effects,
                "latency_ms": 1.0,
            },
        )()


# ── result dataclass shapes ────────────────────────────────────────────


def test_step_execution_result_default_fields() -> None:
    r = StepExecutionResult(step_id="s1", route="stub", ok=True)
    assert r.step_id == "s1"
    assert r.route == "stub"
    assert r.ok is True
    assert r.output == {}
    assert r.error is None
    assert r.latency_ms == 0.0
    assert r.pending is False


def test_step_execution_result_is_frozen() -> None:
    r = StepExecutionResult(step_id="s1", route="stub", ok=True)
    with pytest.raises(FrozenInstanceError):
        r.ok = False  # type: ignore[misc]


def test_plan_execution_result_default_fields() -> None:
    p = PlanExecutionResult(plan_id="p1", step_results=(), all_ok=True)
    assert p.plan_id == "p1"
    assert p.step_results == ()
    assert p.all_ok is True
    assert p.error is None


def test_plan_execution_result_is_frozen() -> None:
    p = PlanExecutionResult(plan_id="p1", step_results=(), all_ok=True)
    with pytest.raises(FrozenInstanceError):
        p.all_ok = False  # type: ignore[misc]


# ── execute_step routing ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_llm_turn_routes_through_agent_loop() -> None:
    al = FakeAgentLoop(answer={"reply": "world"})
    disp = ActionDispatcher(agent_loop=al)
    step = make_step(
        action_kind="llm_turn",
        payload={"prompt": "hi", "goal_id": "g1"},
    )
    out = await disp.execute_step(step)
    assert out.ok is True
    assert out.route == "llm_turn"
    assert out.step_id == "s1"
    # AgentLoop received goal_id-as-session_id and the prompt.
    assert al.calls == [{"session_id": "g1", "user_message": "hi"}]
    assert out.output["session_id"] == "g1"
    assert out.output["agent_result"] == {"reply": "world"}


@pytest.mark.asyncio
async def test_llm_turn_falls_back_to_stub_when_agent_loop_missing() -> None:
    disp = ActionDispatcher(agent_loop=None)
    step = make_step(action_kind="llm_turn", expected_outcome="hello")
    out = await disp.execute_step(step)
    assert out.route == "stub"
    assert out.ok is True
    assert out.output["expected_outcome"] == "hello"
    assert out.output["stub"] is True


@pytest.mark.asyncio
async def test_llm_turn_uses_step_id_when_goal_id_missing() -> None:
    al = FakeAgentLoop()
    disp = ActionDispatcher(agent_loop=al)
    step = make_step(id="step-xyz", action_kind="llm_turn", payload={"prompt": "go"})
    await disp.execute_step(step)
    assert al.calls[0]["session_id"] == "step-xyz"


@pytest.mark.asyncio
async def test_llm_turn_captures_agent_loop_exception() -> None:
    al = FakeAgentLoop(raises=RuntimeError("agent boom"))
    disp = ActionDispatcher(agent_loop=al)
    step = make_step(action_kind="llm_turn", payload={"prompt": "hi"})
    out = await disp.execute_step(step)
    assert out.ok is False
    assert out.route == "llm_turn"
    assert "agent boom" in (out.error or "")
    # Latency captured (monotonic clock; may round to 0 on a fast path
    # where the exception surfaces in well under a microsecond, so we
    # only require it be non-negative — no assert > 0 here).
    assert out.latency_ms >= 0.0


@pytest.mark.asyncio
async def test_skill_invoke_routes_through_registry() -> None:
    skill = FakeSkill(result={"answer": 42}, side_effects=("/tmp/x",))
    reg = FakeSkillRegistry({"summarize": skill})
    disp = ActionDispatcher(skill_registry=reg)
    step = make_step(
        action_kind="skill_invoke",
        payload={"skill_id": "summarize", "skill_args": {"text": "hello"}},
    )
    out = await disp.execute_step(step)
    assert out.ok is True
    assert out.route == "skill_invoke"
    assert out.output["skill_id"] == "summarize"
    assert out.output["result"] == {"answer": 42}
    assert out.output["side_effects"] == ["/tmp/x"]
    # Skill received SkillInput-shape with `args`.
    assert len(skill.calls) == 1
    assert skill.calls[0].args == {"text": "hello"}


@pytest.mark.asyncio
async def test_skill_invoke_unknown_skill_returns_error() -> None:
    reg = FakeSkillRegistry({})
    disp = ActionDispatcher(skill_registry=reg)
    step = make_step(
        action_kind="skill_invoke",
        payload={"skill_id": "nonexistent"},
    )
    out = await disp.execute_step(step)
    assert out.ok is False
    assert out.route == "skill_invoke"
    assert "nonexistent" in (out.error or "")


@pytest.mark.asyncio
async def test_skill_invoke_missing_skill_id_returns_error() -> None:
    reg = FakeSkillRegistry({"x": FakeSkill()})
    disp = ActionDispatcher(skill_registry=reg)
    step = make_step(action_kind="skill_invoke", payload={})
    out = await disp.execute_step(step)
    assert out.ok is False
    assert "skill_id" in (out.error or "")


@pytest.mark.asyncio
async def test_skill_invoke_falls_back_to_stub_when_registry_missing() -> None:
    disp = ActionDispatcher(skill_registry=None)
    step = make_step(action_kind="skill_invoke", expected_outcome="will-stub")
    out = await disp.execute_step(step)
    assert out.route == "stub"
    assert out.ok is True
    assert out.output["expected_outcome"] == "will-stub"


@pytest.mark.asyncio
async def test_skill_invoke_propagates_skill_ok_false() -> None:
    skill = FakeSkill(ok=False, result="bad")
    reg = FakeSkillRegistry({"bad_skill": skill})
    disp = ActionDispatcher(skill_registry=reg)
    step = make_step(
        action_kind="skill_invoke",
        payload={"skill_id": "bad_skill"},
    )
    out = await disp.execute_step(step)
    assert out.ok is False
    assert out.error is not None


@pytest.mark.asyncio
async def test_skill_invoke_captures_skill_exception() -> None:
    skill = FakeSkill(raises=ValueError("skill boom"))
    reg = FakeSkillRegistry({"x": skill})
    disp = ActionDispatcher(skill_registry=reg)
    step = make_step(action_kind="skill_invoke", payload={"skill_id": "x"})
    out = await disp.execute_step(step)
    assert out.ok is False
    assert "skill boom" in (out.error or "")


@pytest.mark.asyncio
async def test_skill_invoke_uses_intent_when_skill_id_missing() -> None:
    skill = FakeSkill(result="found-via-intent")
    reg = FakeSkillRegistry({"my_intent": skill})
    disp = ActionDispatcher(skill_registry=reg)
    step = make_step(
        action_kind="skill_invoke",
        payload={"intent": "my_intent"},
    )
    out = await disp.execute_step(step)
    assert out.ok is True
    assert out.output["result"] == "found-via-intent"


@pytest.mark.asyncio
async def test_tool_call_routes_through_provider() -> None:
    tp = FakeToolProvider(content={"data": [1, 2, 3]}, side_effects=("/tmp/file",))
    disp = ActionDispatcher(tool_provider=tp)
    step = make_step(
        action_kind="tool_call",
        payload={"tool_name": "fs.read", "tool_args": {"path": "/tmp/x"}},
    )
    out = await disp.execute_step(step)
    assert out.ok is True
    assert out.route == "tool_call"
    assert out.output["tool_name"] == "fs.read"
    assert out.output["content"] == {"data": [1, 2, 3]}
    assert out.output["side_effects"] == ["/tmp/file"]
    # Provider received ToolCall-shape.
    call = tp.calls[0]
    assert call.name == "fs.read"
    assert call.args == {"path": "/tmp/x"}
    assert call.id  # uuid hex is non-empty


@pytest.mark.asyncio
async def test_tool_call_missing_name_returns_error() -> None:
    tp = FakeToolProvider()
    disp = ActionDispatcher(tool_provider=tp)
    step = make_step(action_kind="tool_call", payload={})
    out = await disp.execute_step(step)
    assert out.ok is False
    assert "tool_name" in (out.error or "")


@pytest.mark.asyncio
async def test_tool_call_falls_back_to_stub_when_provider_missing() -> None:
    disp = ActionDispatcher(tool_provider=None)
    step = make_step(action_kind="tool_call", expected_outcome="stubbed")
    out = await disp.execute_step(step)
    assert out.route == "stub"
    assert out.ok is True


@pytest.mark.asyncio
async def test_tool_call_propagates_provider_ok_false() -> None:
    tp = FakeToolProvider(ok=False, error="permission denied")
    disp = ActionDispatcher(tool_provider=tp)
    step = make_step(
        action_kind="tool_call",
        payload={"tool_name": "fs.write", "tool_args": {}},
    )
    out = await disp.execute_step(step)
    assert out.ok is False
    assert "permission denied" in (out.error or "")


@pytest.mark.asyncio
async def test_tool_call_captures_provider_exception() -> None:
    tp = FakeToolProvider(raises=RuntimeError("tool boom"))
    disp = ActionDispatcher(tool_provider=tp)
    step = make_step(
        action_kind="tool_call",
        payload={"tool_name": "fs.read"},
    )
    out = await disp.execute_step(step)
    assert out.ok is False
    assert "tool boom" in (out.error or "")


@pytest.mark.asyncio
async def test_wait_for_percept_returns_pending_immediately() -> None:
    disp = ActionDispatcher()
    step = make_step(
        action_kind="wait_for_percept",
        payload={"percept_kind": "user_msg"},
        expected_outcome="user replies",
    )
    t0 = time.monotonic()
    out = await disp.execute_step(step)
    elapsed = time.monotonic() - t0
    assert elapsed < 0.05  # truly non-blocking
    assert out.pending is True
    assert out.ok is True
    assert out.route == "wait_for_percept"
    assert out.output["percept_kind"] == "user_msg"
    assert out.output["expected_outcome"] == "user replies"


@pytest.mark.asyncio
async def test_wait_for_percept_uses_default_kind() -> None:
    disp = ActionDispatcher()
    step = make_step(action_kind="wait_for_percept", payload={})
    out = await disp.execute_step(step)
    assert out.pending is True
    assert out.output["percept_kind"] == "any"


@pytest.mark.asyncio
async def test_unknown_action_kind_falls_through_to_stub() -> None:
    disp = ActionDispatcher()
    step = make_step(action_kind="space_travel", expected_outcome="moon")
    out = await disp.execute_step(step)
    assert out.route == "stub"
    assert out.ok is True
    assert out.output["expected_outcome"] == "moon"


@pytest.mark.asyncio
async def test_step_id_preserved_across_routes() -> None:
    al = FakeAgentLoop()
    skill = FakeSkill()
    reg = FakeSkillRegistry({"s": skill})
    tp = FakeToolProvider()
    disp = ActionDispatcher(agent_loop=al, skill_registry=reg, tool_provider=tp)

    for kind, payload in (
        ("llm_turn", {"prompt": "x"}),
        ("skill_invoke", {"skill_id": "s"}),
        ("tool_call", {"tool_name": "t"}),
        ("wait_for_percept", {}),
    ):
        step = make_step(id=f"id-{kind}", action_kind=kind, payload=payload)
        out = await disp.execute_step(step)
        assert out.step_id == f"id-{kind}"


@pytest.mark.asyncio
async def test_latency_captured_for_executed_routes() -> None:
    al = FakeAgentLoop()
    disp = ActionDispatcher(agent_loop=al)
    step = make_step(action_kind="llm_turn", payload={"prompt": "hi"})
    out = await disp.execute_step(step)
    assert out.latency_ms >= 0  # monotonic clock; rounded near-zero acceptable


# ── execute_plan orchestration ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_plan_runs_every_step_in_order() -> None:
    al = FakeAgentLoop()
    disp = ActionDispatcher(agent_loop=al)
    steps = [
        make_step(id=f"s{i}", action_kind="llm_turn", payload={"prompt": f"p{i}"})
        for i in range(3)
    ]
    plan = make_plan(id="plan-multi", steps=steps)
    result = await disp.execute_plan(plan)
    assert isinstance(result, PlanExecutionResult)
    assert result.plan_id == "plan-multi"
    assert result.all_ok is True
    assert len(result.step_results) == 3
    assert all(r.ok for r in result.step_results)
    assert [c["user_message"] for c in al.calls] == ["p0", "p1", "p2"]


@pytest.mark.asyncio
async def test_execute_plan_stops_on_first_failure_by_default() -> None:
    al = FakeAgentLoop(raises=RuntimeError("boom"))
    disp = ActionDispatcher(agent_loop=al)
    steps = [
        make_step(id="s0", action_kind="llm_turn", payload={"prompt": "p0"}),
        make_step(id="s1", action_kind="llm_turn", payload={"prompt": "p1"}),
        make_step(id="s2", action_kind="llm_turn", payload={"prompt": "p2"}),
    ]
    plan = make_plan(steps=steps)
    result = await disp.execute_plan(plan)
    assert result.all_ok is False
    # Only the first step ran (then halted).
    assert len(result.step_results) == 1
    assert result.step_results[0].ok is False
    assert "s0" in (result.error or "")


@pytest.mark.asyncio
async def test_execute_plan_continues_when_retry_policy_says_so() -> None:
    al = FakeAgentLoop(raises=RuntimeError("boom"))
    disp = ActionDispatcher(agent_loop=al)
    steps = [
        make_step(
            id="s0",
            action_kind="llm_turn",
            payload={"prompt": "p0"},
            retry_policy={"max_retries": 0, "continue_on_failure": True},
        ),
        make_step(
            id="s1",
            action_kind="llm_turn",
            payload={"prompt": "p1"},
            retry_policy={"max_retries": 0, "continue_on_failure": True},
        ),
    ]
    plan = make_plan(steps=steps)
    result = await disp.execute_plan(plan)
    # Both ran; both failed; all_ok=False but error remains None at plan
    # level because we did NOT halt.
    assert len(result.step_results) == 2
    assert all(not r.ok for r in result.step_results)
    assert result.all_ok is False


@pytest.mark.asyncio
async def test_execute_plan_returns_pending_when_step_suspends() -> None:
    disp = ActionDispatcher()
    steps = [
        make_step(id="s0", action_kind="wait_for_percept", payload={}),
        make_step(id="s1", action_kind="llm_turn", payload={"prompt": "x"}),
    ]
    plan = make_plan(steps=steps)
    result = await disp.execute_plan(plan)
    # First step pending → halt without error; second step did not run.
    assert len(result.step_results) == 1
    assert result.step_results[0].pending is True
    assert result.all_ok is False
    assert result.error is None


@pytest.mark.asyncio
async def test_execute_plan_handles_empty_steps() -> None:
    disp = ActionDispatcher()
    plan = make_plan(id="p-empty", steps=[])
    result = await disp.execute_plan(plan)
    assert result.plan_id == "p-empty"
    assert result.step_results == ()
    assert result.all_ok is True


@pytest.mark.asyncio
async def test_execute_plan_never_propagates_step_exception() -> None:
    """Even if execute_step is monkey-patched to raise, execute_plan absorbs."""
    disp = ActionDispatcher()

    async def boom(_step: Any) -> StepExecutionResult:
        raise RuntimeError("inner boom")

    disp.execute_step = boom  # type: ignore[assignment]

    steps = [make_step(id="s0", action_kind="llm_turn", payload={"prompt": "x"})]
    plan = make_plan(steps=steps)
    result = await disp.execute_plan(plan)  # Must not raise.
    assert result.all_ok is False
    assert any("inner boom" in (r.error or "") for r in result.step_results)


# ── dispatch alias (Planner contract) ──────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_returns_dict_for_planner_compat() -> None:
    al = FakeAgentLoop()
    disp = ActionDispatcher(agent_loop=al)
    step = make_step(action_kind="llm_turn", payload={"prompt": "x"})
    out = await disp.dispatch(step)
    # Planner's contract: dispatch returns a dict on success.
    assert isinstance(out, dict)
    assert out["ok"] is True
    assert out["step_id"] == "s1"
    assert out["route"] == "llm_turn"


@pytest.mark.asyncio
async def test_dispatch_raises_on_step_failure_for_planner_compat() -> None:
    al = FakeAgentLoop(raises=RuntimeError("agent boom"))
    disp = ActionDispatcher(agent_loop=al)
    step = make_step(action_kind="llm_turn", payload={"prompt": "x"})
    # Planner.execute uses raises as its failure signal — dispatch must
    # honour that contract even though execute_step does not.
    with pytest.raises(RuntimeError):
        await disp.dispatch(step)


@pytest.mark.asyncio
async def test_dispatch_does_not_raise_for_pending_step() -> None:
    disp = ActionDispatcher()
    step = make_step(action_kind="wait_for_percept", payload={})
    out = await disp.dispatch(step)
    # Pending is NOT a failure — Planner.execute can keep going.
    assert isinstance(out, dict)
    assert out["pending"] is True
    assert out["ok"] is True


# ── stub fallback identity ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stub_fallback_marks_route_stub_and_carries_expected_outcome() -> None:
    """The v0 stub semantics are preserved exactly when no executor wired."""
    disp = ActionDispatcher()
    step = make_step(
        action_kind="llm_turn",
        expected_outcome="42 is the answer",
    )
    out = await disp.execute_step(step)
    assert out.route == "stub"
    assert out.ok is True
    assert out.output["expected_outcome"] == "42 is the answer"
    assert out.output["stub"] is True


@pytest.mark.asyncio
async def test_dict_steps_supported_too() -> None:
    """Steps may be plain dicts (Planner uses dataclass; tests vary)."""
    al = FakeAgentLoop()
    disp = ActionDispatcher(agent_loop=al)
    step = {
        "id": "dict-step",
        "action_kind": "llm_turn",
        "payload": {"prompt": "yo", "goal_id": "g7"},
        "expected_outcome": "ok",
        "retry_policy": {"max_retries": 0},
    }
    out = await disp.execute_step(step)
    assert out.ok is True
    assert out.step_id == "dict-step"
    assert al.calls[0]["session_id"] == "g7"
