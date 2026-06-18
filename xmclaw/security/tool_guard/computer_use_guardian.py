"""ComputerUseActionGuardian — 按动作分级闸控 computer-use 工具（Phase 9 M2.2）.

背景：computer-use 是 XMclaw 暴露的最危险工具面（agent 直接驱动用户
GUI），但在本 guardian 之前，安全层对它是**空转**的：

  1. ``utils/security.py`` 的 ``TOOL_CATEGORIES`` 给 mouse/keyboard 标了
     DANGEROUS，但那张表不在工具调用链上（没有任何 invoke 路径查它）。
  2. ``ToolGuardEngine._DEFAULT_GUARDED_TOOLS`` 不含任何 computer-use
     工具 → 只跑 always-run 的 file_path guardian，等于没扫。
  3. 规则型 guardian 靠 regex 匹配参数，而 ``mouse_click {x, y}`` 的
     参数毫无可疑文本 —— 即使被扫也永远零 finding。

本 guardian 按**动作性质**而非参数内容分级：

  * 读取类（截图 / OCR / 找字 / 控件树检查 / 窗口列表 / 等待文本）——
    无副作用，零 finding，直接放行。
  * 操作类（鼠标 / 键盘 / 拖拽 / 滚动 / 窗口置前 / 点击文本或图像 /
    UIA 点击 / gui_send_chat）—— 按 ``mode`` 出 finding：

      - ``allow``（默认）: 零 finding 放行。computer-use provider 本身
        默认关闭（``tools.computer_use.enabled``），开它已是显式授权；
        且 channel 自动化流程（如 gui_send_chat 发消息）无人守在
        Web UI 旁点批准，默认 approve 会让这些流程卡死在 pending。
        每次调用仍有 TOOL_CALL/TOOL_RESULT 事件流留痕可审计。
      - ``approve``: HIGH finding → GuardianPolicy 默认映射 APPROVE →
        ApprovalService 单次确认放行（NEEDS_APPROVAL 流）。
      - ``deny``: CRITICAL finding → 直接拒绝。

配置: ``security.guardians.computer_use_mode``（factory 接线）。
"""
from __future__ import annotations

from typing import Any

from .base import BaseToolGuardian
from .models import GuardFinding, GuardSeverity

# GUI 操作类工具 —— 会改变屏幕上的世界。新增 computer-use 操作工具时
# 必须同步加进来（engine 的 guarded_tools 用同一份名单,漏加 = 不扫）。
MUTATING_GUI_TOOLS: frozenset[str] = frozenset({
    # Desktop computer-use tools
    "mouse_move",
    "mouse_click",
    "mouse_drag",
    "mouse_scroll",
    "keyboard_type",
    "keyboard_press",
    "window_focus",
    "click_on_text",
    "click_on_image",
    "scroll_to_text",
    "ui_click",
    "gui_send_chat",
    # Android companion tools (Phase 12 / Android Companion M1)
    "phone_open_app",
    "phone_click",
    "phone_tap",
    "phone_input",
    "phone_swipe",
    "phone_key",
    "phone_notification",
    "phone_clipboard_set",
})

# 读取类 —— 这里列出只为文档完整性 + 测试断言用,guard() 对不在
# MUTATING_GUI_TOOLS 里的名字一律零 finding。
READONLY_GUI_TOOLS: frozenset[str] = frozenset({
    # Desktop computer-use tools
    "screen_capture",
    "screen_size",
    "cursor_position",
    "window_list",
    "screen_ocr",
    "find_on_screen",
    "wait_for_text",
    "screen_region_capture",
    "find_image_on_screen",
    "ui_inspect",
    # Android companion tools
    "phone_screenshot",
    "phone_ui_tree",
    "phone_wait",
    "phone_clipboard_get",
})

_VALID_MODES = ("allow", "approve", "deny")


class ComputerUseActionGuardian(BaseToolGuardian):
    """Tier-based gate for GUI-mutating computer-use tools."""

    # 2026-06-18 refactor: unified computer_use 的 mutating action 子命令
    _MUTATING_ACTIONS: frozenset[str] = frozenset({
        "move", "click", "double_click", "right_click", "drag", "scroll",
        "type", "key", "click_text", "click_image", "ui_click",
        "gui_send_chat", "focus_window", "wait_for_text",
    })

    def __init__(self, mode: str = "allow") -> None:
        mode = (mode or "allow").lower()
        if mode not in _VALID_MODES:
            raise ValueError(
                f"computer_use_mode must be one of {_VALID_MODES}, got {mode!r}"
            )
        self._mode = mode

    @property
    def name(self) -> str:
        return "computer_use_action"

    @property
    def mode(self) -> str:
        return self._mode

    def guard(self, tool_name: str, params: dict[str, Any]) -> list[GuardFinding]:
        if self._mode == "allow":
            return []
        # 2026-06-18 refactor: unified computer_use tool
        if tool_name == "computer_use":
            action = str(params.get("action", "")).strip().lower()
            if action not in self._MUTATING_ACTIONS and action not in MUTATING_GUI_TOOLS:
                return []
            severity = (
                GuardSeverity.CRITICAL if self._mode == "deny" else GuardSeverity.HIGH
            )
            return [
                GuardFinding(
                    rule_id="computer_use_mutating_action",
                    category="unauthorized_tool_use",
                    severity=severity,
                    title="GUI 控制动作",
                    description=(
                        f"computer_use(action={action!r}) 会直接操作用户的"
                        f"鼠标/键盘/窗口（security.guardians.computer_use_mode="
                        f"{self._mode!r}）"
                    ),
                    tool_name=tool_name,
                    remediation=(
                        "确认该动作符合预期后批准；如需免确认，把 "
                        "security.guardians.computer_use_mode 设为 'allow'"
                    ),
                    guardian=self.name,
                )
            ]
        # Legacy tool names (backward compatible)
        if tool_name not in MUTATING_GUI_TOOLS:
            return []
        severity = (
            GuardSeverity.CRITICAL if self._mode == "deny" else GuardSeverity.HIGH
        )
        return [
            GuardFinding(
                rule_id="computer_use_mutating_action",
                category="unauthorized_tool_use",
                severity=severity,
                title="GUI 控制动作",
                description=(
                    f"工具 {tool_name!r} 会直接操作用户的鼠标/键盘/窗口"
                    f"（security.guardians.computer_use_mode={self._mode!r}）"
                ),
                tool_name=tool_name,
                remediation=(
                    "确认该动作符合预期后批准；如需免确认，把 "
                    "security.guardians.computer_use_mode 设为 'allow'"
                ),
                guardian=self.name,
            )
        ]
