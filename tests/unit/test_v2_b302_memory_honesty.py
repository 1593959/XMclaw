# B-302 memory honesty guard — regression tests.
#
# 2026-06-11 真实事故 (用户截图): 模型调了 ``memory(action='add')`` 并说
# "已记录", 守卫仍然注入纠正提示, 模型回嘴, 整段对话暴露在 UI 里。
# 两个根因:
#   1. ``_MEMORY_TOOLS`` 只认 remember/learn_about_user, 不认 V2 的
#      多动作 ``memory`` 工具;
#   2. ``tool_calls_made`` 的元素是 dict, 旧代码用 getattr 取 .name
#      永远得到 "" — 即使调了 remember 也会误报。
from __future__ import annotations

from pathlib import Path

from xmclaw.daemon.hop_loop import _check_memory_honesty

_REPO = Path(__file__).resolve().parents[2]

# 三处共享的开头短语: hop_loop 产出、history_compression 落盘剥离、
# MessageBubbleParts.js 前端渲染剥离。改任何一处必须同步另两处。
_NUDGE_PREFIX = "你刚才说记住了/记下了，但我没有检测到"


def test_no_claim_returns_none() -> None:
    assert _check_memory_honesty("好的，我来看看这个文件。", []) is None
    assert _check_memory_honesty("", []) is None
    assert _check_memory_honesty(None, []) is None


def test_claim_without_tool_returns_nudge() -> None:
    nudge = _check_memory_honesty("这个信息我记下了！", [])
    assert nudge is not None
    assert nudge.startswith(_NUDGE_PREFIX)


def test_memory_v2_tool_call_is_honest() -> None:
    # 真实事故的精确形状: tool_calls_made 是 dict 列表, 工具名是 "memory"。
    calls = [{"name": "memory", "args": {"action": "add"}, "ok": True}]
    assert _check_memory_honesty("✅ 已记录到 Failure Modes 下面", calls) is None


def test_legacy_remember_dict_call_is_honest() -> None:
    # 旧 bug: dict 上 getattr(.name) 返回 "" → 即使调了 remember 也误报。
    calls = [{"name": "remember", "args": {}, "ok": True}]
    assert _check_memory_honesty("记下了", calls) is None


def test_unrelated_tool_call_still_nudges() -> None:
    calls = [{"name": "read_file", "args": {}, "ok": True}]
    nudge = _check_memory_honesty("我记住了这个约定", calls)
    assert nudge is not None


def test_nudge_prefix_dropped_from_persisted_history() -> None:
    # history_compression._persist_history 必须按这个前缀整条丢弃
    # nudge user 消息 (与 GOAL-ANCHOR 同等待遇), 否则刷新后泄漏到 UI。
    src = (_REPO / "xmclaw" / "daemon" / "history_compression.py").read_text(
        encoding="utf-8",
    )
    assert _NUDGE_PREFIX in src, (
        "history_compression.py no longer drops the B-302 nudge — "
        "it will leak into the chat UI after a history reload"
    )


def test_nudge_prefix_stripped_by_frontend() -> None:
    # 前端兜底: MessageBubbleParts.js 按同一前缀把 nudge 渲染为空。
    js = (
        _REPO / "xmclaw" / "daemon" / "static" / "components"
        / "molecules" / "MessageBubbleParts.js"
    ).read_text(encoding="utf-8")
    assert _NUDGE_PREFIX in js, (
        "MessageBubbleParts.js no longer strips the B-302 nudge prefix"
    )


def test_nudge_text_still_starts_with_shared_prefix() -> None:
    # 守卫真实产出的文本必须以共享前缀开头, 否则两处剥离全部失效。
    nudge = _check_memory_honesty("我记住了", [])
    assert nudge is not None
    assert nudge.startswith(_NUDGE_PREFIX)
