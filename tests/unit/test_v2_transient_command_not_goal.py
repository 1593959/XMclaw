"""根因修复：一次性命令不该被 _QUAL_GOAL_RE 误存成长期"目标/愿景"。

Bug 现场（用户截图）：聊天里说"希望删除所有无法正常使用的技能"，被结晶成
工作记忆"目标: 删除所有无法正常使用的技能" —— 把当下指令当成了人生目标。
"""
from __future__ import annotations

from xmclaw.memory.v2.key_info_extractor import (
    _is_transient_command,
    extract_keys,
)


def _goal_texts(message: str) -> list[str]:
    return [k.text for k in extract_keys(message)
            if getattr(k, "pattern_name", "") == "qual_goal"]


def test_imperative_command_not_stored_as_goal() -> None:
    # 截图原案 + 几个变体：都不该产出 qual_goal
    for msg in (
        "希望删除所有无法正常使用的技能",
        "想要你帮我改一下配置",
        "打算把这些坏掉的技能清理掉",
        "希望修复这个 bug",
    ):
        assert _goal_texts(msg) == [], f"误把命令当目标: {msg!r}"


def test_real_aspirational_goal_still_extracted() -> None:
    # 真·终态目标仍要抓住（不能误杀）
    assert _goal_texts("希望提升客户满意度")
    assert _goal_texts("目标是做到行业第一")


def test_is_transient_command_predicate() -> None:
    assert _is_transient_command("删除所有无法正常使用的技能")
    assert _is_transient_command("帮我改一下配置")
    assert _is_transient_command("跑一下测试")
    # 终态/愿景类不算命令
    assert not _is_transient_command("提升客户满意度")
    assert not _is_transient_command("成为行业第一")
