"""Lightweight i18n — dict-based, no external dependencies.

Set ``XMC_LANG=zh`` to force Chinese; otherwise auto-detected from the OS
locale (defaults to English).
"""
from __future__ import annotations

import locale
import os
from typing import Any


def _detect_lang() -> str:
    env = os.environ.get("XMC_LANG", "").lower()
    if env in ("zh", "zh-cn", "zh-tw", "zh-hk"):
        return "zh"
    if env:
        return "en"  # unknown env values fall back to English
    try:
        loc = locale.getdefaultlocale()[0] or ""
        if "Chinese" in loc or "zh" in loc.lower():
            return "zh"
    except Exception:
        pass
    return "en"


_MESSAGES: dict[str, dict[str, str]] = {
    "en": {
        "approvals.none_pending": "No pending approvals.",
        "approvals.header": "Pending approvals ({count}):",
        "approvals.approved": "Approved {request_id}.",
        "approvals.denied": "Denied {request_id}.",
        "approvals.error.not_found": "Request not found or already resolved",
        "guard.blocked.denied_list": "Tool '{tool_name}' is blocked by security policy (denied list).",
        "guard.blocked.severity": "Tool '{tool_name}' blocked: {severity} security finding(s).",
        "guard.scan_summary_header": "Security scan found {count} issue(s):",
        "guard.scan_summary_item": "  [{severity}] {rule_id}: {description}",
        "guard.scan_summary_remediation": "       Remediation: {remediation}",
        "agent.needs_approval_prompt": (
            "⚠️ Security check blocked tool `{tool_name}`.\n"
            "Run `xmclaw approvals approve {request_id}` "
            "to allow this call, then resend your message."
        ),
        "evolution.no_events": "No evolution events found.",
        "evolution.filtered_since": "  (filtered by --since {since})",
        "evolution.header_time": "Time",
        "evolution.header_skill": "Skill",
        "evolution.header_change": "Change",
        "evolution.score_label": " (score {score:.3f})",
        "evolution.reason_label": " — {reason}",
        "onboard.welcome": "Welcome to XMclaw! Let's get you set up.",
        "onboard.existing_config": "Config already exists at {path}. Overwrite?",
        "onboard.provider_select": "Choose your LLM provider:",
        "onboard.api_key_prompt": "Enter your API key (input hidden):",
        "onboard.workspace_prompt": "Workspace directory (default: {default}):",
        "onboard.tools_prompt": "Which tools would you like to enable?",
        "onboard.smoke_test": "Running smoke test...",
        "onboard.smoke_ok": "✅ Smoke test passed! XMclaw is ready.",
        "onboard.smoke_fail": "⚠️ Smoke test failed: {error}\nTry running `xmclaw doctor` for diagnostics.",
        "onboard.done": "Setup complete. Start the daemon with `xmclaw start` and chat with `xmclaw chat`.",
    },
    "zh": {
        "approvals.none_pending": "暂无待审批请求。",
        "approvals.header": "待审批请求 ({count}):",
        "approvals.approved": "已批准 {request_id}。",
        "approvals.denied": "已拒绝 {request_id}。",
        "approvals.error.not_found": "请求未找到或已处理",
        "guard.blocked.denied_list": "工具 '{tool_name}' 已被安全策略阻止（拒绝列表）。",
        "guard.blocked.severity": "工具 '{tool_name}' 被阻止：发现 {severity} 安全风险。",
        "guard.scan_summary_header": "安全扫描发现 {count} 个问题：",
        "guard.scan_summary_item": "  [{severity}] {rule_id}: {description}",
        "guard.scan_summary_remediation": "       修复建议: {remediation}",
        "agent.needs_approval_prompt": (
            "⚠️ 安全检测已阻止工具 `{tool_name}`。\n"
            "运行 `xmclaw approvals approve {request_id}` "
            "以允许此次调用，然后重新发送您的消息。"
        ),
        "evolution.no_events": "未找到进化事件。",
        "evolution.filtered_since": "  (按 --since {since} 过滤)",
        "evolution.header_time": "时间",
        "evolution.header_skill": "技能",
        "evolution.header_change": "变更",
        "evolution.score_label": " (分数 {score:.3f})",
        "evolution.reason_label": " — {reason}",
        "onboard.welcome": "欢迎使用 XMclaw！让我们开始配置。",
        "onboard.existing_config": "配置文件已存在于 {path}。是否覆盖？",
        "onboard.provider_select": "选择您的 LLM 提供商：",
        "onboard.api_key_prompt": "输入您的 API 密钥（输入已隐藏）：",
        "onboard.workspace_prompt": "工作目录（默认：{default}）：",
        "onboard.tools_prompt": "您想启用哪些工具？",
        "onboard.smoke_test": "正在运行冒烟测试...",
        "onboard.smoke_ok": "✅ 冒烟测试通过！XMclaw 已就绪。",
        "onboard.smoke_fail": "⚠️ 冒烟测试失败：{error}\n请运行 `xmclaw doctor` 进行诊断。",
        "onboard.done": "配置完成。使用 `xmclaw start` 启动守护进程，然后用 `xmclaw chat` 开始对话。",
    },
}


def _(key: str, **kwargs: Any) -> str:
    """Look up *key* in the active language dictionary.

    Missing keys are returned as-is so the call site never crashes.
    """
    lang = _detect_lang()
    text = _MESSAGES.get(lang, _MESSAGES["en"]).get(key, key)
    if kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, ValueError):
            pass
    return text
