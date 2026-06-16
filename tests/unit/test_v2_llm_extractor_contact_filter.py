"""LLM fact-extractor must NOT store contact-number facts (2026-06-16).

The extractor runs over the user message AND the assistant response, so it
kept mining placeholder phone numbers from agent-generated content (e.g. a
promo poster's "联系电话 178…") and storing them as "用户电话号码为 X" —
the user reported these reappearing after deletion. Real user phones go
through the deterministic regex KeyInfoExtractor instead.
"""
from __future__ import annotations

import pytest

from xmclaw.memory.v2.llm_extractor import _is_contact_number_fact


@pytest.mark.parametrize("text", [
    "用户电话号码为 1781594433",
    "用户联系电话为 17815-90376",
    "用户的手机是 138 0013 8000",
    "客户传真 010-12345678",
    "user phone is 13800138000",
])
def test_contact_number_facts_are_flagged_for_drop(text: str) -> None:
    assert _is_contact_number_fact(text) is True


@pytest.mark.parametrize("text", [
    "用户想做电商，月流水目标 5 万",         # 电商≠电话, 5万 not a digit run
    "用户偏好简洁的回复风格",
    "项目用 Python + FastAPI",
    "客户QQ群有 1500 人",                    # qq kw but only 4 digits
    "用户叫张伟",
])
def test_normal_facts_are_kept(text: str) -> None:
    assert _is_contact_number_fact(text) is False
