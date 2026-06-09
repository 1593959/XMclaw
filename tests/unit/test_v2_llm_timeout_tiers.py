"""AgentLoop._compute_llm_timeout — per-call wall-clock is now FLAT.

2026-06-08: the message-shape tiering (vision 120s / short 240s / full bound)
was removed. Root flaw (user-reported): it judged task complexity from the
FIRST user message, but the timeout applies to EVERY hop. A short "继续" can
launch an 18-hop complex task; by hop 2 it's clearly non-trivial, yet the
budget stayed locked to the opening message's "short" tier → "LLM call
exceeded 150s at hop 2" on a task that wasn't simple. The per-call wall-clock
is a stuck-provider safety net, not a budget to ration — so every call now
gets the full configured bound (config ``llm.timeout_s``, default 600s).

``_compute_llm_timeout`` only reads ``self._llm_timeout_s``, so we call it as
an unbound method against a tiny stub.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from xmclaw.daemon.agent_loop import AgentLoop


def _timeout(
    *,
    message: str,
    upper_bound: float = 600.0,
    has_image: bool = False,
    tool_count: int = 0,
) -> float:
    stub = SimpleNamespace(_llm_timeout_s=upper_bound)
    return AgentLoop._compute_llm_timeout(
        stub,  # type: ignore[arg-type]
        user_message=message,
        has_image=has_image,
        tool_count=tool_count,
    )


@pytest.mark.parametrize("kwargs", [
    {"message": "继续"},                       # 短消息(曾被掐)
    {"message": "你好"},                        # 纯问候
    {"message": "x" * 500},                     # 长消息
    {"message": "分析整个代码库并生成报告"},     # 工作型
    {"message": "look", "has_image": True},     # 带图(曾 120s 档)
    {"message": "hi", "tool_count": 99},        # 一堆工具可用
])
def test_every_input_gets_full_bound(kwargs) -> None:
    """不再按消息/图片/工具分档 —— 一律给满配置上限。"""
    assert _timeout(upper_bound=600.0, **kwargs) == 600.0


def test_configured_bound_is_the_only_knob() -> None:
    # 改 upper_bound,所有调用都跟着变;没有任何隐藏的低档上限。
    assert _timeout(message="继续", upper_bound=300.0) == 300.0
    assert _timeout(message="你好", upper_bound=900.0, has_image=True) == 900.0
    assert _timeout(message="分析", upper_bound=45.0, tool_count=50) == 45.0
