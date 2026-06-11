"""Phase 10.M1 — Mission Control 任务聚合 + /ui-next/ 挂载测试。

按 2026-05-09 测试规则双层覆盖：
  1. 状态推导纯函数单测（_derive 启发式）
  2. TestClient 端到端 — 打前端真实会请求的 URL：
     GET /api/v2/tasks（任务栏水化）+ GET /ui-next/（新 UI 壳可达）
"""
from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from xmclaw.core.bus import InProcessEventBus
from xmclaw.daemon.app import create_app
from xmclaw.daemon.routers.tasks import _derive


def _ev(t: str, ts: float, **payload: object) -> SimpleNamespace:
    return SimpleNamespace(type=t, ts=ts, payload=payload)


NOW = time.time()


# ── Layer 1: 状态推导 ──────────────────────────────────────────────


def test_derive_awaiting_input_when_question_unanswered() -> None:
    out = _derive([
        _ev("user_message", NOW - 30),
        _ev("agent_asked_question", NOW - 10, question_id="q1"),
    ], NOW)
    assert out["status"] == "awaiting_input"


def test_derive_answered_question_not_awaiting() -> None:
    out = _derive([
        _ev("agent_asked_question", NOW - 20, question_id="q1"),
        _ev("user_answered_question", NOW - 10, question_id="q1"),
        _ev("llm_response", NOW - 5, ok=True, tool_calls_count=0),
    ], NOW)
    assert out["status"] != "awaiting_input"


def test_derive_running_on_fresh_tool_call() -> None:
    out = _derive([
        _ev("llm_request", NOW - 8),
        _ev("tool_call_emitted", NOW - 3, call_id="c1", name="bash"),
    ], NOW)
    assert out["status"] == "running"


def test_derive_stale_tool_call_not_running() -> None:
    """daemon 重启/断流后，几小时前的 running 尾事件不能让任务永远转圈。"""
    out = _derive([
        _ev("tool_call_emitted", NOW - 7200, call_id="c1", name="bash"),
    ], NOW)
    assert out["status"] != "running"


def test_derive_mid_hop_llm_response_still_running() -> None:
    """多 hop 中段的 llm_response (tool_calls_count>0) 仍是 running。"""
    out = _derive([
        _ev("llm_response", NOW - 3, ok=True, tool_calls_count=2),
    ], NOW)
    assert out["status"] == "running"


def test_derive_plan_progress_and_done() -> None:
    out = _derive([
        _ev("plan_started", NOW - 100, step_ids=["a", "b", "c"], n_steps=3),
        _ev("plan_step_completed", NOW - 80, step_id="a"),
        _ev("plan_step_completed", NOW - 60, step_id="b"),
        _ev("plan_step_completed", NOW - 40, step_id="c"),
        _ev("plan_completed", NOW - 39, status="completed"),
        _ev("llm_response", NOW - 30, ok=True, tool_calls_count=0),
    ], NOW)
    assert out["status"] == "done"
    assert out["steps_total"] == 3
    assert out["steps_done"] == 3


def test_derive_plan_failed() -> None:
    out = _derive([
        _ev("plan_started", NOW - 100, step_ids=["a"], n_steps=1),
        _ev("plan_step_failed", NOW - 50, step_id="a", error="boom"),
        _ev("plan_failed", NOW - 49, status="failed", error="boom"),
        _ev("llm_response", NOW - 40, ok=True, tool_calls_count=0),
    ], NOW)
    assert out["status"] == "failed"


def test_derive_todo_steps_when_no_plan() -> None:
    out = _derive([
        _ev("todo_updated", NOW - 50, items=[
            {"content": "x", "status": "completed"},
            {"content": "y", "status": "in_progress"},
            {"content": "z", "status": "pending"},
        ]),
        _ev("llm_response", NOW - 1000, ok=True, tool_calls_count=0),
    ], NOW)
    assert out["steps_total"] == 3
    assert out["steps_done"] == 1


def test_derive_pure_chat() -> None:
    out = _derive([
        _ev("user_message", NOW - 600),
        _ev("llm_response", NOW - 590, ok=True, tool_calls_count=0),
    ], NOW)
    assert out["status"] == "chat"


def test_derive_enum_like_type_accepted() -> None:
    """bus.query 返回的 type 可能是 EventType enum——value 提取容错。"""
    enum_like = SimpleNamespace(value="agent_asked_question")
    out = _derive([SimpleNamespace(type=enum_like, ts=NOW - 5, payload={"question_id": "q"})], NOW)
    assert out["status"] == "awaiting_input"


# ── Layer 2: 端到端（前端真实 URL） ────────────────────────────────


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(bus=InProcessEventBus(), config={}))


def test_tasks_endpoint_end_to_end(client: TestClient) -> None:
    """前端任务栏水化打的真实 URL。InProcessEventBus 无 query →
    必须优雅退化为 chat 态列表而不是 500。"""
    r = client.get("/api/v2/tasks")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "tasks" in body
    assert isinstance(body["tasks"], list)
    for t in body["tasks"]:
        assert {"sid", "title", "status", "steps_total", "steps_done"} <= set(t)
        assert t["status"] in {"running", "awaiting_input", "done", "failed", "chat"}


def test_tasks_route_registered_once(client: TestClient) -> None:
    paths = [getattr(r, "path", "") for r in client.app.routes]
    assert paths.count("/api/v2/tasks") == 1


# ── /ui-next/ 挂载 ────────────────────────────────────────────────

_DIST = Path(__file__).resolve().parents[2] / "xmclaw" / "daemon" / "webui_dist"


def test_webui_dist_committed() -> None:
    """构建产物必须随仓库分发（最终用户零 Node）——设计规格 §3。"""
    assert (_DIST / "index.html").is_file(), (
        "xmclaw/daemon/webui_dist/index.html 缺失 — 在 webui/ 里跑 "
        "`npm run build` 并提交产物"
    )


def test_ui_next_serves_spa_shell(client: TestClient) -> None:
    r = client.get("/ui-next/")
    assert r.status_code == 200, r.text
    assert "<div id=\"root\">" in r.text
    # SPA fallback：未知路径也回壳，不 404。
    r2 = client.get("/ui-next/some/spa/route")
    assert r2.status_code == 200
    assert "<div id=\"root\">" in r2.text


def test_ui_next_serves_hashed_assets(client: TestClient) -> None:
    """index.html 引用的哈希 asset 必须真实可达（产物完整性）。"""
    import re

    html = client.get("/ui-next/").text
    refs = re.findall(r"(?:src|href)=\"\./(assets/[^\"]+)\"", html)
    assert refs, f"index.html 未引用任何 ./assets/* — 产物异常?\n{html[:500]}"
    for ref in refs:
        r = client.get(f"/ui-next/{ref}")
        assert r.status_code == 200, f"{ref} 不可达"
        assert "immutable" in r.headers.get("cache-control", ""), (
            f"{ref} 应带 immutable 缓存头（内容哈希命名）"
        )
