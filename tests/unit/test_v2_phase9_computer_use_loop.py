"""Phase 9 M2 — Computer-use 闭环（视觉接地 + 动作分级安全闸 + 动作后验证）.

三个回归面：

  1. **安全闸（M2.2）**：此前安全层对 computer-use 是空转的 ——
     ``_DEFAULT_GUARDED_TOOLS`` 不含任何 computer-use 工具，规则型
     guardian 对 ``mouse_click {x, y}`` 这类无可疑文本的参数也永远零
     finding。新增 ``ComputerUseActionGuardian`` 按动作性质分级
     （读取放行 / 操作按 mode 出 finding），engine guarded 集合补全。
  2. **视觉接地（M2.1）**：DPI 感知 + ``click_scale`` 坐标空间回报，
     治"截图物理像素 vs pyautogui 逻辑坐标"的点不准根因。
  3. **动作后验证（M2.3）**：``verify_text`` 把"点了但没生效"从静默
     继续变成显式 ``verified: false`` 信号。
"""
from __future__ import annotations

import json

import pytest

from xmclaw.core.ir import ToolCall
from xmclaw.providers.tool.computer_use import ComputerUseTools
from xmclaw.providers.tool.guarded import GuardedToolProvider
from xmclaw.security.approval_service import ApprovalService
from xmclaw.security.tool_guard.computer_use_guardian import (
    MUTATING_GUI_TOOLS,
    READONLY_GUI_TOOLS,
    ComputerUseActionGuardian,
)
from xmclaw.security.tool_guard.engine import ToolGuardEngine
from xmclaw.security.tool_guard.models import GuardSeverity


# ── 1. ComputerUseActionGuardian 分级 ─────────────────────────────


def test_allow_mode_is_default_and_silent() -> None:
    g = ComputerUseActionGuardian()
    assert g.mode == "allow"
    for tool in MUTATING_GUI_TOOLS | READONLY_GUI_TOOLS:
        assert g.guard(tool, {}) == []


def test_approve_mode_flags_every_mutating_tool_high() -> None:
    g = ComputerUseActionGuardian(mode="approve")
    for tool in MUTATING_GUI_TOOLS:
        findings = g.guard(tool, {"x": 1, "y": 2})
        assert len(findings) == 1, tool
        assert findings[0].severity == GuardSeverity.HIGH, tool
        assert findings[0].tool_name == tool


def test_approve_mode_never_flags_readonly_tools() -> None:
    g = ComputerUseActionGuardian(mode="approve")
    for tool in READONLY_GUI_TOOLS:
        assert g.guard(tool, {}) == [], tool
    # 非 computer-use 工具同样不归本 guardian 管
    assert g.guard("bash", {"command": "rm -rf /"}) == []


def test_deny_mode_flags_critical() -> None:
    g = ComputerUseActionGuardian(mode="deny")
    findings = g.guard("keyboard_type", {"text": "hi"})
    assert findings and findings[0].severity == GuardSeverity.CRITICAL


def test_invalid_mode_raises_at_construction() -> None:
    """坏配置启动期就炸（与 GuardianPolicy.from_config 同一原则）。"""
    with pytest.raises(ValueError):
        ComputerUseActionGuardian(mode="yolo")


def test_engine_default_guarded_set_covers_mutating_gui_tools() -> None:
    """guarded 集合漏了 = guardian 永远不被咨询（空转回归）。"""
    engine = ToolGuardEngine()
    for tool in MUTATING_GUI_TOOLS:
        assert engine.is_guarded(tool), (
            f"{tool!r} 不在 guarded 集合 — ComputerUseActionGuardian 空转"
        )


# ── 2. GuardedToolProvider 端到端（approve 模式 → NEEDS_APPROVAL） ─


class _EchoProvider:
    """Inner provider stub — 记录到达的调用并回 ok。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def list_tools(self):  # noqa: ANN201
        return []

    async def invoke(self, call: ToolCall):  # noqa: ANN201
        from xmclaw.core.ir import ToolResult
        self.calls.append(call.name)
        return ToolResult(call_id=call.id, ok=True, content="inner-ok")


def _call(name: str, args: dict | None = None) -> ToolCall:
    return ToolCall(
        id="c1", name=name, args=args or {},
        provenance="synthetic", session_id="sess-p9",
    )


@pytest.mark.asyncio
async def test_approve_mode_gates_mouse_click_behind_approval() -> None:
    inner = _EchoProvider()
    engine = ToolGuardEngine(
        guardians=[ComputerUseActionGuardian(mode="approve")],
    )
    approvals = ApprovalService()
    guarded = GuardedToolProvider(inner, engine, approval_service=approvals)

    out = await guarded.invoke(_call("mouse_click", {"x": 10, "y": 20}))
    assert out.ok is False
    assert out.error and out.error.startswith("NEEDS_APPROVAL:")
    assert inner.calls == []  # 动作没落地

    # 用户批准后,同一调用单次放行
    request_id = out.error.split(":", 1)[1]
    assert await approvals.approve(request_id) is True
    out2 = await guarded.invoke(_call("mouse_click", {"x": 10, "y": 20}))
    assert out2.ok is True
    assert inner.calls == ["mouse_click"]


@pytest.mark.asyncio
async def test_approve_mode_lets_screen_capture_through() -> None:
    inner = _EchoProvider()
    engine = ToolGuardEngine(
        guardians=[ComputerUseActionGuardian(mode="approve")],
    )
    guarded = GuardedToolProvider(inner, engine, approval_service=ApprovalService())
    out = await guarded.invoke(_call("screen_capture"))
    assert out.ok is True
    assert inner.calls == ["screen_capture"]


@pytest.mark.asyncio
async def test_allow_mode_passes_mutating_tools_through() -> None:
    inner = _EchoProvider()
    engine = ToolGuardEngine(
        guardians=[ComputerUseActionGuardian(mode="allow")],
    )
    guarded = GuardedToolProvider(inner, engine, approval_service=ApprovalService())
    out = await guarded.invoke(_call("keyboard_type", {"text": "hello"}))
    assert out.ok is True
    assert inner.calls == ["keyboard_type"]


# ── 3. M2.1 视觉接地：DPI + click_scale + spec 纪律 ───────────────


def test_ensure_dpi_aware_is_safe_everywhere() -> None:
    """任何平台调用都不得抛异常,且幂等。"""
    from xmclaw.providers.tool import computer_use as cu
    cu._ensure_dpi_aware()
    cu._ensure_dpi_aware()
    assert cu._dpi_aware_attempted is True


def test_screen_capture_spec_teaches_click_scale() -> None:
    tools = ComputerUseTools()
    spec = next(s for s in tools.list_tools() if s.name == "computer_use")
    assert "capture" in spec.description


def test_click_specs_teach_grounding_and_verify() -> None:
    tools = ComputerUseTools()
    by_name = {s.name: s for s in tools.list_tools()}
    spec = by_name["computer_use"]
    assert "verify_text" in spec.parameters_schema["properties"]
    assert "capture" in spec.description.lower()
    assert "click" in spec.description.lower()


# ── 4. M2.3 动作后验证 ────────────────────────────────────────────


class _FakePg:
    """pyautogui stub — 记录点击,position 返回固定点。"""

    FAILSAFE = True
    PAUSE = 0.05

    def __init__(self) -> None:
        self.clicks: list[dict] = []

    def click(self, **kw):  # noqa: ANN201
        self.clicks.append(kw)

    def position(self):  # noqa: ANN201
        return (5, 7)

    def size(self):  # noqa: ANN201
        return (1920, 1080)


@pytest.fixture
def patched_tools(monkeypatch: pytest.MonkeyPatch) -> tuple[ComputerUseTools, _FakePg]:
    tools = ComputerUseTools()
    fake = _FakePg()
    monkeypatch.setattr(tools, "_require_pyautogui", lambda: fake)
    return tools, fake


@pytest.mark.asyncio
async def test_mouse_click_verify_text_found(
    patched_tools: tuple[ComputerUseTools, _FakePg],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools, fake = patched_tools
    from xmclaw.providers.tool import computer_use as cu
    monkeypatch.setattr(
        cu, "_run_ocr_full_pipeline",
        lambda region, conf: [
            {"text": "保存成功", "confidence": 0.9,
             "center": [10, 10], "bbox": [0, 0, 20, 20]},
        ],
    )
    out = await tools.invoke(_call(
        "mouse_click",
        {"x": 3, "y": 4, "verify_text": "保存成功", "verify_timeout_s": 1},
    ))
    assert out.ok is True
    payload = json.loads(out.content)
    assert payload["verified"] is True
    assert fake.clicks  # 点击真的发生了


@pytest.mark.asyncio
async def test_mouse_click_verify_text_missing_is_explicit(
    patched_tools: tuple[ComputerUseTools, _FakePg],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """点了但预期文本没出现 → verified: false + 屏上实读样本,
    绝不静默当成功。"""
    tools, _ = patched_tools
    from xmclaw.providers.tool import computer_use as cu
    monkeypatch.setattr(
        cu, "_run_ocr_full_pipeline",
        lambda region, conf: [
            {"text": "毫不相干", "confidence": 0.8,
             "center": [1, 1], "bbox": [0, 0, 2, 2]},
        ],
    )
    out = await tools.invoke(_call(
        "mouse_click",
        {"x": 3, "y": 4, "verify_text": "保存成功", "verify_timeout_s": 0.5},
    ))
    assert out.ok is True  # 点击本身成功
    payload = json.loads(out.content)
    assert payload["verified"] is False
    assert payload["verify_attempts"] >= 1
    assert "sample_blocks_last_poll" in payload


@pytest.mark.asyncio
async def test_mouse_click_without_verify_unchanged(
    patched_tools: tuple[ComputerUseTools, _FakePg],
) -> None:
    """不带 verify_text 的老调用路径零变化。"""
    tools, fake = patched_tools
    out = await tools.invoke(_call("mouse_click", {"x": 3, "y": 4}))
    assert out.ok is True
    payload = json.loads(out.content)
    assert "verified" not in payload
    assert fake.clicks == [{"x": 3, "y": 4, "button": "left", "clicks": 1}]


@pytest.mark.asyncio
async def test_verify_degrades_when_no_ocr_engine(
    patched_tools: tuple[ComputerUseTools, _FakePg],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没装 OCR 引擎 → verify_skipped 说明而非报错;动作结果不受影响。"""
    tools, _ = patched_tools
    from xmclaw.providers.tool import computer_use as cu

    def _no_engine(region, conf):
        raise cu._NoOCREngineError("no OCR engine installed")

    monkeypatch.setattr(cu, "_run_ocr_full_pipeline", _no_engine)
    out = await tools.invoke(_call(
        "mouse_click", {"x": 1, "y": 2, "verify_text": "x"},
    ))
    assert out.ok is True
    payload = json.loads(out.content)
    assert payload["verified"] is None
    assert "verify_skipped" in payload
