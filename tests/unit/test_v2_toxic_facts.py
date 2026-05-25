"""Tests for the toxic-self-capability-denial fact filter.

Locks in the patterns that block the recurring "agent claims it can't
see chat images" persona-file poisoning. See
xmclaw/core/persona/toxic_facts.py module docstring.
"""
from __future__ import annotations

import pytest

from xmclaw.core.persona.toxic_facts import is_toxic_self_capability_denial


TOXIC = [
    # The exact lines that landed in USER.md / SOUL.md / TOOLS.md
    # before this filter shipped — verbatim from real user-data.
    "视觉能力受限，无法直接查看聊天内图片，需保存为本地文件后通过view_image(path)分析",
    "用户接受并适应视觉能力受限的约束，但需要明确告知替代方案（保存本地文件后通过view_image分析）",
    "主动披露能力边界（视觉受限）而非等用户发现",
    "在状态汇报中主动标注自身能力边界（视觉受限），管理用户预期",
    "视觉能力受限时，图片需保存为本地文件后通过 `view_image(path)` 分析，无法直接处理聊天内图片",
    "聊天窗口直接粘贴的图片无法被 agent 看到，需要用户通过 view_image 工具提供文件路径、直接复制文字、或提供图床 URL",
    # English equivalents.
    "I can't see chat images pasted by the user",
    "Cannot view pasted images — only screenshots from agent tools are visible",
    "Vision is limited; images need to be saved to disk first",
    "Be transparent about capability limitations (visual restricted, spell out workaround)",
    "Images pasted in chat by user are NOT accessible to the agent — only screenshots taken by agent tools are visible",
    "Unable to see images uploaded in the composer",
]


BENIGN = [
    # Real persona-file lines that must NOT be flagged.
    "用户偏好被称呼为'哥'",
    "用表格快速呈现状态信息",
    "Concise, action-oriented communication style ('指哪打哪，不啰嗦').",
    "screen_ocr successfully reads text from desktop but may have limitations with certain image types",
    "用户经营陪玩店（LT凌天电竞），关注NimbusBot/LT-Command项目",
    "use 🐾 emoji as personal identifier",
    # Mentions of vision/image WITHOUT self-denial.
    "image_read returns base64-encoded image bytes",
    "Screenshots are stored under ~/.xmclaw/v2/uploads/",
    "view_image is the right tool for previously-captured screenshots",
    # Empty / None.
    "",
]


@pytest.mark.parametrize("text", TOXIC)
def test_toxic_lines_are_blocked(text: str) -> None:
    toxic, pid = is_toxic_self_capability_denial(text)
    assert toxic is True, f"Should be toxic but wasn't: {text!r}"
    assert pid is not None and isinstance(pid, str)


@pytest.mark.parametrize("text", BENIGN)
def test_benign_lines_are_not_blocked(text: str) -> None:
    toxic, pid = is_toxic_self_capability_denial(text)
    assert toxic is False, (
        f"Should NOT be toxic but was flagged by {pid!r}: {text!r}"
    )


def test_none_input_returns_false() -> None:
    toxic, pid = is_toxic_self_capability_denial(None)
    assert toxic is False
    assert pid is None
