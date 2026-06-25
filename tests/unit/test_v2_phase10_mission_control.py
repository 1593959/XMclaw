"""Phase 10.M1 — Mission Control 任务聚合 + /ui-next/ 挂载测试。

按 2026-05-09 测试规则双层覆盖：
  1. 状态推导纯函数单测（_derive 启发式）
  2. TestClient 端到端 — 打前端真实会请求的 URL：
     GET /api/v2/tasks（任务栏水化）+ GET /ui-next/（新 UI 壳可达）
"""
from __future__ import annotations

import time
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from xmclaw.core.bus import InProcessEventBus
from xmclaw.daemon.app import create_app
from xmclaw.daemon.routers.tasks import _clean_title, _derive


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


def test_derive_abandoned_question_not_awaiting() -> None:
    """用户实测反馈（2026-06-12）：历史会话里被弃置的提问（之后又有别的
    事件）不能让任务永远顶着"等你回答"。"""
    out = _derive([
        _ev("agent_asked_question", NOW - 7200, question_id="q1"),
        _ev("user_message", NOW - 7000),
        _ev("llm_response", NOW - 6990, ok=True, tool_calls_count=0),
    ], NOW)
    assert out["status"] != "awaiting_input"


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


def test_derive_title_from_first_user_message() -> None:
    out = _derive([
        _ev("user_message", NOW - 100, content="帮我重构登录模块"),
        _ev("user_message", NOW - 50, content="继续"),
        _ev("llm_response", NOW - 40, ok=True, tool_calls_count=0),
    ], NOW)
    assert out["_first_user_text"] == "帮我重构登录模块"


# ── 标题清洗（10.M2 实测发现：preview 被注入块污染） ───────────────


def test_clean_title_strips_injected_blocks() -> None:
    raw = "<session-workspace>\nScratch dir: /x\n</session-workspace>\n\n帮我写周报"
    assert _clean_title(raw, "sid1") == "帮我写周报"


def test_clean_title_strips_memory_blocks() -> None:
    raw = "<memory-v2-facts>\nfact1\n</memory-v2-facts>查天气"
    assert _clean_title(raw, "sid1") == "查天气"


def test_clean_title_truncated_block_falls_back_to_sid() -> None:
    """preview 截断导致闭合标签丢失 → 宁可退回 sid 也不显示半截注入块。"""
    raw = "<memory-v2-facts>\nfact1 fact2 fact3（这里被 preview 截断了"
    assert _clean_title(raw, "sid1") == "sid1"


def test_clean_title_plain_text_passthrough() -> None:
    assert _clean_title("正常的任务标题", "sid1") == "正常的任务标题"


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


def test_tasks_routes_are_reachable(client: TestClient) -> None:
    r = client.get("/api/v2/tasks")
    assert r.status_code == 200
    artifacts = client.get("/api/v2/tasks/synthetic-session/artifacts")
    assert artifacts.status_code in {200, 503}


def test_system_health_not_422(client: TestClient) -> None:
    """回归（10.M3 实测发现）：health_check(request: \"Request\") 字符串注解
    且未 import Request — FastAPI 把 request 当必填 query 参数，端点对
    所有调用方 422。系统域健康卡因此全挂。"""
    r = client.get("/api/v2/system/health")
    assert r.status_code in (200, 503), f"422 = Request 注解又坏了: {r.text}"
    body = r.json()
    assert "status" in body and "checks" in body


# ── /ui-next/ 挂载 ────────────────────────────────────────────────

_DIST = Path(__file__).resolve().parents[2] / "xmclaw" / "daemon" / "webui_dist"
_WEBUI_SRC = Path(__file__).resolve().parents[2] / "webui" / "src"


def test_webui_dist_committed() -> None:
    """构建产物必须随仓库分发（最终用户零 Node）——设计规格 §3。"""
    assert (_DIST / "index.html").is_file(), (
        "xmclaw/daemon/webui_dist/index.html 缺失 — 在 webui/ 里跑 "
        "`npm run build` 并提交产物"
    )


def test_webui_initial_bundle_budget() -> None:
    """Heavy renderers must stay lazy-loaded instead of entering boot JS."""
    html = (_DIST / "index.html").read_text(encoding="utf-8")
    refs = re.findall(r'src="\./(assets/index-[^"]+\.js)"', html)
    assert refs, "index.html must reference one boot JS asset"
    boot_js = _DIST / refs[0]
    assert boot_js.is_file(), f"boot JS missing: {boot_js}"
    assert boot_js.stat().st_size < 380_000, (
        f"boot JS grew to {boot_js.stat().st_size} bytes; keep Mermaid, "
        "Cytoscape, KaTeX, and Markdown renderers behind lazy imports"
    )


def test_react_rest_fetches_use_header_token_not_query_param() -> None:
    """REST fetches should keep pairing tokens out of URLs."""
    api_src = (_WEBUI_SRC / "lib" / "api.ts").read_text(encoding="utf-8")
    store_src = (_WEBUI_SRC / "store" / "app.ts").read_text(encoding="utf-8")
    voice_src = (_WEBUI_SRC / "lib" / "voice.ts").read_text(encoding="utf-8")

    assert "X-XMC-Token" in api_src
    assert "fetch(withToken" not in api_src
    assert "?token=${encodeURIComponent(token)}" not in store_src
    assert "?token=${encodeURIComponent(token)}" not in voice_src


def test_high_churn_views_use_abortable_fetches() -> None:
    """Session/view scoped panels should not let stale GET responses win."""
    for rel in [
        "views/SystemView.tsx",
        "views/CognitionView.tsx",
        "views/MemoryView.tsx",
        "views/SkillsView.tsx",
        "views/ModelConfig.tsx",
        "views/CronView.tsx",
        "views/FilesView.tsx",
    ]:
        src = (_WEBUI_SRC / rel).read_text(encoding="utf-8")
        assert "apiGetFresh" in src, f"{rel} should use abortable GETs"
        assert "new AbortController()" in src, f"{rel} should abort stale requests"
        assert "return () => ctl.abort()" in src or "ctl.abort();" in src
    files_src = (_WEBUI_SRC / "views/FilesView.tsx").read_text(encoding="utf-8")
    assert "openSeq" in files_src
    assert "openSeq.current !== seq" in files_src


def test_frontend_accessible_control_primitives() -> None:
    """Interactive controls need stable names and state semantics."""
    ui_button_src = (_WEBUI_SRC / "components" / "UiButton.tsx").read_text(encoding="utf-8")
    seg_tabs_src = (_WEBUI_SRC / "components" / "SegTabs.tsx").read_text(encoding="utf-8")
    thinking_src = (_WEBUI_SRC / "components" / "ThinkingBlock.tsx").read_text(encoding="utf-8")
    tool_cards_src = (_WEBUI_SRC / "components" / "ToolCards.tsx").read_text(encoding="utf-8")
    skills_src = (_WEBUI_SRC / "views" / "SkillsView.tsx").read_text(encoding="utf-8")
    channel_src = (_WEBUI_SRC / "views" / "ChannelEditor.tsx").read_text(encoding="utf-8")

    assert "export function IconButton" in ui_button_src
    assert "export function ToggleButton" in ui_button_src
    assert "aria-label={label}" in ui_button_src
    assert "aria-pressed={pressed}" in ui_button_src
    assert 'role="tablist"' in seg_tabs_src
    assert "aria-selected={active}" in seg_tabs_src
    assert "aria-pressed={active}" in seg_tabs_src
    assert "aria-expanded={open}" in thinking_src
    assert "aria-expanded={open}" in tool_cards_src
    assert "aria-expanded={open}" in skills_src
    assert "<ToggleButton" in channel_src


def test_team_view_surfaces_graph_state_topology() -> None:
    """The team/planning page should show the canonical task GraphState."""
    team_src = (_WEBUI_SRC / "views" / "TeamView.tsx").read_text(encoding="utf-8")

    assert "/api/v2/cognition/tasks/graph-state" in team_src
    assert "apiGetFresh<GraphStateSnapshot>" in team_src
    assert "new AbortController()" in team_src
    assert "GraphStatePanel" in team_src
    assert "GraphNodeRow" in team_src
    assert "graphState.metadata?.inspection" in team_src
    assert "inspection.runnable_ids" in team_src
    assert "inspection.blocked_ids" in team_src
    assert "inspection.failed_ids" in team_src
    assert "aria-expanded={expanded}" in team_src


def test_ui_switchover_primary_and_legacy(client: TestClient) -> None:
    """10.M3.2 切换：/ui/ = 新 Mission Control，/ui-legacy/ = 旧 Preact UI，
    /ui-next/ 别名仍可用，/ 重定向到 /ui/。"""
    r = client.get("/ui/", follow_redirects=False)
    assert r.status_code == 200
    assert '<div id="root">' in r.text, "/ui/ 应是新 Mission Control 壳"

    r = client.get("/ui-next/", follow_redirects=False)
    assert r.status_code == 200
    assert '<div id="root">' in r.text, "/ui-next/ 别名应仍服务新 UI"

    r = client.get("/ui-legacy/", follow_redirects=False)
    assert r.status_code == 200
    assert "bootstrap" in r.text.lower(), "/ui-legacy/ 应是旧 Preact UI"

    r = client.get("/", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert r.headers.get("location") == "/ui/"


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
def test_react_mobile_domain_nav_is_available() -> None:
    """Regression guard: TaskRail is hidden on mobile, so App must render
    DomainNav outside the rail for small screens."""
    root = Path(__file__).resolve().parents[2]
    app_src = (root / "webui" / "src" / "App.tsx").read_text(encoding="utf-8")
    rail_src = (root / "webui" / "src" / "components" / "TaskRail.tsx").read_text(encoding="utf-8")
    assert "import TaskRail, { DomainNav }" in app_src
    assert '<DomainNav className="md:hidden bg-mc-panel" />' in app_src
    assert "export function DomainNav" in rail_src
    assert 'aria-current={view === d.key ? "page" : undefined}' in rail_src


def test_generated_artifact_iframes_do_not_allow_scripts() -> None:
    """Generated HTML/SVG artifacts are tool/LLM-originated content and
    must not get script permission by default."""
    root = Path(__file__).resolve().parents[2]
    src = (root / "webui" / "src" / "components" / "WorkspacePanel.tsx").read_text(encoding="utf-8")
    assert 'sandbox="allow-scripts"' not in src
    assert 'sandbox=""' in src


def test_generated_artifacts_use_central_sanitizer_and_csp() -> None:
    """Artifact previews must be sanitized before srcDoc/innerHTML use."""
    root = Path(__file__).resolve().parents[2]
    workspace_src = (root / "webui" / "src" / "components" / "WorkspacePanel.tsx").read_text(encoding="utf-8")
    mermaid_src = (root / "webui" / "src" / "components" / "MermaidView.tsx").read_text(encoding="utf-8")
    security_src = (root / "webui" / "src" / "lib" / "artifactSecurity.ts").read_text(encoding="utf-8")
    markdown_src = (root / "webui" / "src" / "lib" / "Markdown.tsx").read_text(encoding="utf-8")

    assert "artifactSrcDoc" in workspace_src
    assert "srcDoc={doc}" not in workspace_src
    assert "sanitizeArtifactMarkup(svg)" in mermaid_src
    assert "DOMPurify.sanitize" in security_src
    assert "Content-Security-Policy" in security_src
    assert "script-src 'none'" in security_src
    assert "isSafeMarkdownHref" in markdown_src
    assert "isSafeMarkdownImageUrl" in markdown_src
