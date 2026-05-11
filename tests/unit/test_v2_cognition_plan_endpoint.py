"""POST /api/v2/cognition/goals/plan — R2 front-back contract test.

Per CLAUDE.md (2026-05-09 standing rule), tests for routes the
frontend exercises must hit the real ``create_app`` via TestClient,
not just unit-test the handler.  This file pins:

  * The route is registered (no route-order regression).
  * Body validation: missing/empty description → 400.
  * No-LLM-wired case → 503 with structured hint.
  * Happy path with a fake LLM: returns plan tree + estimated_cost.
  * Materialize-mode without scheduler → 503 (informative).
  * Materialize-mode with fake scheduler → returns task_ids.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from fastapi.testclient import TestClient

from xmclaw.core.bus import InProcessEventBus
from xmclaw.daemon.app import create_app


# ── Fakes ────────────────────────────────────────────────────────


@dataclass
class _FakeLLMResp:
    content: str


@dataclass
class _ScriptedLLM:
    scripts: list[str] = field(default_factory=list)
    calls: int = 0

    async def complete(self, messages: list, tools: Any = None) -> Any:  # noqa: ARG002
        if self.calls >= len(self.scripts):
            raise RuntimeError("LLM script exhausted")
        out = self.scripts[self.calls]
        self.calls += 1
        return _FakeLLMResp(content=out)


@dataclass
class _NopAgent:
    """Agent stand-in exposing the ``_llm`` attribute the endpoint
    looks for. Doesn't run turns — the endpoint never invokes
    anything beyond ``_llm.complete`` (via HTNPlanner)."""
    _llm: Any = None


@dataclass
class _FakeScheduler:
    submitted: list[Any] = field(default_factory=list)

    async def submit(self, task: Any) -> str:
        self.submitted.append(task)
        return task.id


class _CtxClient:
    """A TestClient wrapper that injects fakes AFTER lifespan startup
    so the lifespan can't overwrite our test wiring. Without this the
    real lifespan builds a real TaskScheduler + sets app.state.task_
    scheduler, which clobbers the fake we tried to inject."""

    def __init__(self, *, llm: Any = None, scheduler: Any = None) -> None:
        bus = InProcessEventBus()
        self._app = create_app(
            bus=bus, config={"cognition": {"enabled": True}},
        )
        self._llm = llm
        self._scheduler = scheduler
        self._tc: TestClient | None = None

    def __enter__(self) -> TestClient:
        self._tc = TestClient(self._app)
        self._tc.__enter__()
        # NOW inject — lifespan startup ran, app.state is settled.
        if self._llm is not None:
            self._app.state.agent = _NopAgent(_llm=self._llm)
        if self._scheduler is not None:
            self._app.state.task_scheduler = self._scheduler
        else:
            # Force absence so the "no_scheduler_wired" 503 case can
            # be exercised even when lifespan built a real one.
            self._app.state.task_scheduler = None
        return self._tc

    def __exit__(self, *exc) -> None:
        if self._tc is not None:
            self._tc.__exit__(*exc)
            self._tc = None


def _client_with(*, llm: Any = None, scheduler: Any = None) -> _CtxClient:
    return _CtxClient(llm=llm, scheduler=scheduler)


# ── Route presence + 400s ────────────────────────────────────────


def test_route_registered() -> None:
    """Pin route registration — front-back rule: route order matters
    (a stray /goals/{id} catch-all could shadow /goals/plan)."""
    bus = InProcessEventBus()
    app = create_app(bus=bus, config={"cognition": {"enabled": True}})
    paths = [r.path for r in app.routes]
    assert "/api/v2/cognition/goals/plan" in paths


def test_empty_description_returns_400() -> None:
    with _client_with(llm=_ScriptedLLM(scripts=[])) as tc:
        r = tc.post("/api/v2/cognition/goals/plan", json={
            "description": "",
        })
        assert r.status_code == 400
        assert "description" in r.json()["error"]


def test_missing_description_returns_400() -> None:
    with _client_with(llm=_ScriptedLLM(scripts=[])) as tc:
        r = tc.post("/api/v2/cognition/goals/plan", json={})
        assert r.status_code == 400


# ── 503: missing wires ────────────────────────────────────────────


def test_no_llm_wired_returns_structured_503() -> None:
    """Endpoint surfaces a friendly 503 with hint when no agent
    LLM is wired (default test app has no agent)."""
    bus = InProcessEventBus()
    app = create_app(bus=bus, config={"cognition": {"enabled": True}})
    with TestClient(app) as tc:
        r = tc.post("/api/v2/cognition/goals/plan", json={
            "description": "anything",
        })
    assert r.status_code == 503
    body = r.json()
    assert body["error"] == "no_llm_wired"
    assert "agent.LLM" in body["hint"]


def test_materialize_without_scheduler_returns_503() -> None:
    """Even with LLM wired, materialize=true without a scheduler
    should 503 with the plan attached — caller can still see the
    decomposition."""
    llm = _ScriptedLLM(scripts=[json.dumps({
        "kind": "atomic",
        "task_prompt": "single step",
        "estimated_cost_usd": 0.05,
    })])
    with _client_with(llm=llm) as tc:
        r = tc.post("/api/v2/cognition/goals/plan", json={
            "description": "x", "materialize": True,
        })
    assert r.status_code == 503
    body = r.json()
    assert body["error"] == "no_scheduler_wired"
    # Plan still rendered so the user can see what would have run.
    assert body["plan"]["kind"] == "atomic"


# ── Happy paths ──────────────────────────────────────────────────


def test_atomic_plan_dry_run() -> None:
    llm = _ScriptedLLM(scripts=[json.dumps({
        "kind": "atomic",
        "task_prompt": "do the thing",
        "estimated_cost_usd": 0.07,
    })])
    with _client_with(llm=llm) as tc:
        r = tc.post("/api/v2/cognition/goals/plan", json={
            "description": "the thing",
        })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["plan"]["kind"] == "atomic"
    assert body["plan"]["task_prompt"] == "do the thing"
    assert body["estimated_cost_usd"] == 0.07
    assert len(body["leaves"]) == 1
    assert body["task_ids"] == []  # dry-run


def test_compound_plan_dry_run() -> None:
    llm = _ScriptedLLM(scripts=[
        json.dumps({
            "kind": "compound",
            "sub_goals": [
                {"description": "step A", "priority": 5},
                {"description": "step B", "priority": 5},
            ],
            "edges": [[1, 0]],   # B after A
        }),
        json.dumps({"kind": "atomic", "task_prompt": "A!",
                    "estimated_cost_usd": 0.05}),
        json.dumps({"kind": "atomic", "task_prompt": "B!",
                    "estimated_cost_usd": 0.05}),
    ])
    with _client_with(llm=llm) as tc:
        r = tc.post("/api/v2/cognition/goals/plan", json={
            "description": "two-step task",
        })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["plan"]["kind"] == "compound"
    assert len(body["plan"]["children"]) == 2
    assert body["plan"]["edges"] == [[1, 0]]
    assert len(body["leaves"]) == 2


def test_materialize_submits_tasks() -> None:
    """materialize=true with scheduler wired → tasks submitted +
    task_ids returned in topo order."""
    llm = _ScriptedLLM(scripts=[
        json.dumps({
            "kind": "compound",
            "sub_goals": [
                {"description": "S1", "priority": 5},
                {"description": "S2", "priority": 5},
            ],
            "edges": [[1, 0]],
        }),
        json.dumps({"kind": "atomic", "task_prompt": "do S1",
                    "estimated_cost_usd": 0.05}),
        json.dumps({"kind": "atomic", "task_prompt": "do S2",
                    "estimated_cost_usd": 0.05}),
    ])
    sched = _FakeScheduler()
    with _client_with(llm=llm, scheduler=sched) as tc:
        r = tc.post("/api/v2/cognition/goals/plan", json={
            "description": "ship X",
            "materialize": True,
        })
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["task_ids"]) == 2
    assert len(sched.submitted) == 2
    assert {t.prompt for t in sched.submitted} == {"do S1", "do S2"}
    # Topo: dep before dependent.
    prompts = [t.prompt for t in sched.submitted]
    assert prompts.index("do S1") < prompts.index("do S2")
