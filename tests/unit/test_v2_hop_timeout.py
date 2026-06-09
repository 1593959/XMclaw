"""_hop_timeout — per-call wall-clock scales with hop DEPTH, not opening message.

2026-06-08: replaces the message-shape tiering. Root flaw the user hit: a short
"继续" launched a deep task, but the budget stayed locked to the opening
message's "short" tier → "LLM call exceeded 150s at hop 2" on a non-trivial task.
Now deeper hop = proven-more-complex + bigger context → more time (monotonic),
capped at the configured llm.timeout_s.
"""
from __future__ import annotations

from xmclaw.daemon.hop_loop import (
    _HOP_BASE_TIMEOUT_S,
    _HOP_STEP_TIMEOUT_S,
    _hop_timeout,
)


def test_grows_with_hop_depth_capped_at_bound():
    assert _hop_timeout(0, 600.0) == 240.0
    assert _hop_timeout(1, 600.0) == 360.0
    assert _hop_timeout(2, 600.0) == 480.0   # ← the bug case: was starved, now 480
    assert _hop_timeout(3, 600.0) == 600.0
    assert _hop_timeout(9, 600.0) == 600.0   # capped


def test_monotonic_non_decreasing():
    prev = 0.0
    for hop in range(12):
        cur = _hop_timeout(hop, 600.0)
        assert cur >= prev   # 只增不减
        prev = cur


def test_configured_bound_is_hard_cap():
    # 一个紧的 bound 钳制所有 hop —— 用户设 300 则封顶 300。
    assert _hop_timeout(2, 300.0) == 300.0
    assert _hop_timeout(0, 100.0) == 100.0   # bound < base → bound wins


def test_constants_are_sane():
    assert _HOP_BASE_TIMEOUT_S >= 120.0      # hop0 给推理模型留足首 token
    assert _HOP_STEP_TIMEOUT_S > 0           # 必须递增


def test_negative_or_weird_hop_is_safe():
    assert _hop_timeout(-1, 600.0) == _HOP_BASE_TIMEOUT_S  # clamp hop<0 → base
