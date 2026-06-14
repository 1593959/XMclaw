"""回归：无 ``tools`` section 的默认 config 必须装配全部工具族（2026-06-14）。

事故：build_tools_from_config 在 ``cfg["tools"]`` 缺失时早退，只返回
``BuiltinTools(session_store, persona_dir_provider)`` —— 丢掉 browser /
lsp / computer_use / media 整族，且 builtin 本身也少接 canvas_listener /
workspace / undo。注释写着 "all tool families on" 但代码相反。

用户态症状：默认 config(无 tools 键)下 agent 只有内置工具、没有
browser_*；而多个内置工具的错误提示语会建议 LLM 用 ``browser_open`` →
LLM 照做 → 运行时 "unknown tool: browser_open"。

锁：默认 config 装出的工具集应包含浏览器族(若 playwright 可用)，且至少
不能比显式 ``tools: {}`` 的装配少。
"""
from __future__ import annotations

import importlib.util

from xmclaw.daemon.factory import build_tools_from_config

_HAS_PLAYWRIGHT = importlib.util.find_spec("playwright") is not None


def _names(cfg: dict) -> list[str]:
    tools = build_tools_from_config(cfg)
    return [s.name for s in (tools.list_tools() or [])]


def test_missing_tools_section_matches_empty_section() -> None:
    """无 ``tools`` 键 == 显式 ``tools: {}`` —— 早退不得偷工。"""
    no_section = set(_names({"llm": {}}))
    empty_section = set(_names({"llm": {}, "tools": {}}))
    assert no_section == empty_section, (
        "默认(无 tools)装配与 tools:{} 不一致 → 早退分支又在偷工: "
        f"缺 {empty_section - no_section}"
    )


def test_default_config_includes_core_builtin_wiring() -> None:
    """默认 config 必须含 canvas / undo 等(早退曾漏接)。"""
    names = set(_names({"llm": {}}))
    for must in ("canvas_create", "undo_recent", "bash", "file_write"):
        assert must in names, f"默认工具集缺 {must}（builtin 装配不全）"


def test_default_config_includes_browser_when_playwright_present() -> None:
    """playwright 可用时默认 config 应注册浏览器族(browser_open 等)。"""
    if not _HAS_PLAYWRIGHT:
        import pytest

        pytest.skip("playwright 未安装 — 浏览器族按设计跳过，非回归")
    names = set(_names({"llm": {}}))
    assert "browser_open" in names, (
        "playwright 已装但默认 config 无 browser_open —— 早退分支回归了"
    )
    assert len([n for n in names if n.startswith("browser_")]) >= 10
