"""Phase 9 M1 — Canvas 双向化（postMessage 桥 + 断链修复 + vendor 化）.

跨前后端测试（2026-05-09 规则）：所有断言走 TestClient 对真实
``create_app`` 发 HTTP，命中前端实际请求的 URL；不只 inspect 内部状态。

三个回归面：
  1. 断链修复 — 现役渲染器 MessageList.js 必须渲染 ``canvasArtifacts``
     （nebula 改版时漏迁，canvas_create 的产物从未在现役 UI 显示过）。
  2. postMessage 桥 — CanvasArtifact.js 注入 ``window.xmclaw`` 桥并以
     ``e.source`` 配对校验；工具描述教会 agent 桥的用法（后端层）。
  3. vendor 化 — mermaid / Chart.js 本地 UMD 构建经 /ui/vendor/ 可达，
     渲染器不再直接 import esm.sh（断网可用）。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from xmclaw.core.bus import InProcessEventBus
from xmclaw.daemon.app import create_app
from xmclaw.providers.tool.builtin import BuiltinTools


@pytest.fixture
def client() -> TestClient:
    bus = InProcessEventBus()
    app = create_app(bus=bus, config={})
    return TestClient(app)


# ── 1. 断链修复：MessageList 渲染 canvasArtifacts ──────────────────


def test_active_renderer_serves_canvas_artifacts(client: TestClient) -> None:
    """现役 nebula 渲染器必须 import CanvasArtifact 并渲染
    message.canvasArtifacts —— 否则 canvas_create 的产物在 UI 上不可见
    （正是 Phase 9 立项时发现的断链）。"""
    r = client.get("/ui/components/molecules/MessageList.js")
    assert r.status_code == 200
    body = r.text
    assert "CanvasArtifact" in body, "MessageList 没 import CanvasArtifact — 断链回归"
    assert "canvasArtifacts" in body, "MessageList 没渲染 message.canvasArtifacts — 断链回归"
    assert "onCanvasAction" in body, "MessageList 没穿 onCanvasAction props — 桥断"


def test_props_chain_reaches_chat_page(client: TestClient) -> None:
    """onCanvasAction 必须从 app.js 一路穿到 MessageList。"""
    for path in ("/ui/app.js", "/ui/pages/Chat.js"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert "onCanvasAction" in r.text, f"{path} 缺 onCanvasAction props 链"
    r = client.get("/ui/lib/composer_actions.js")
    assert r.status_code == 200
    assert "sendCanvasAction" in r.text, "composer_actions 缺 sendCanvasAction 出口"


# ── 2. postMessage 桥 ─────────────────────────────────────────────


def test_canvas_artifact_injects_bridge(client: TestClient) -> None:
    r = client.get("/ui/components/molecules/CanvasArtifact.js")
    assert r.status_code == 200
    body = r.text
    assert "window.xmclaw" in body, "iframe 桥脚本缺失"
    assert "sendPrompt" in body and "submit" in body
    # 安全模型：消息必须靠 e.source === iframe.contentWindow 配对，
    # 防止页面上其他 iframe / 窗口伪造桥消息。
    assert "e.source !== iframe.contentWindow" in body, "桥缺 source 配对校验 — 安全回归"
    # sandbox 不得放开 same-origin（opaque origin 是隔离的根）。
    # 只查 sandbox 属性值——注释里允许提到这个词。
    import re

    sandbox_attrs = re.findall(r'sandbox="([^"]*)"', body)
    assert sandbox_attrs, "html artifact iframe 缺 sandbox 属性"
    for attr in sandbox_attrs:
        assert "allow-same-origin" not in attr, "html artifact iframe 不得开 allow-same-origin"


def test_canvas_create_spec_teaches_bridge() -> None:
    """后端层：canvas_create 的工具描述必须教 agent 桥 API，否则模型
    不知道 html artifact 能做交互。"""
    tools = BuiltinTools()
    spec = next(s for s in tools.list_tools() if s.name == "canvas_create")
    assert "window.xmclaw.sendPrompt" in spec.description
    assert "window.xmclaw.submit" in spec.description


# ── 3. vendor 化（local-first 渲染） ──────────────────────────────


def test_vendor_bundles_served(client: TestClient) -> None:
    """mermaid / Chart.js 的本地 UMD 构建必须经前端实际请求的 URL 可达。"""
    r = client.get("/ui/vendor/mermaid.min.js")
    assert r.status_code == 200
    assert len(r.content) > 1_000_000, "mermaid.min.js 疑似截断/占位文件"
    assert b"mermaid" in r.content[:2000]

    r = client.get("/ui/vendor/chart.umd.min.js")
    assert r.status_code == 200
    assert len(r.content) > 100_000, "chart.umd.min.js 疑似截断/占位文件"

    r = client.get("/ui/lib/vendor_loaders.js")
    assert r.status_code == 200
    assert "./vendor/mermaid.min.js" in r.text
    assert "./vendor/chart.umd.min.js" in r.text


def test_renderers_use_shared_loader_not_cdn(client: TestClient) -> None:
    """两个渲染入口不得再直接 import esm.sh —— CDN 只能作为
    vendor_loaders 内部的 fallback。"""
    for path in (
        "/ui/components/molecules/CanvasArtifact.js",
        "/ui/pages/_panels/cognition_task_dag.js",
    ):
        r = client.get(path)
        assert r.status_code == 200, path
        assert "esm.sh" not in r.text, f"{path} 仍直连 esm.sh — 断网渲染回归"
        assert "vendor_loaders.js" in r.text, f"{path} 未走共享加载器"
