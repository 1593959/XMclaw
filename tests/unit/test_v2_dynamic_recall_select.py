"""动态召回筛选 select_recall_indices —— 替代"强制 5 条"，相关性驱动动态条数。

用户痛点：强制召回 5 条 → 有时不够、有时全是噪音，污染聊天/任务。
"""
from __future__ import annotations

from xmclaw.daemon.agent_loop import (
    _UNIFIED_RECALL_MAX_DIST,
    _UNIFIED_RECALL_MAX_ITEMS,
    _UNIFIED_RECALL_REL_BAND,
    select_recall_indices,
)


def test_all_irrelevant_returns_empty() -> None:
    # 全部距离 > 阈值（"需要"这种模糊词的边缘命中）→ 一条都不注入
    dists = [0.55, 0.6, 0.7, 0.8, 0.9]
    assert select_recall_indices(dists) == []


def test_keeps_only_close_to_best_hit() -> None:
    # 最佳命中很相关(0.10)，相对带 0.10 → 只留 ≤0.20 的；0.33 虽过绝对阈值也被带挡掉
    dists = [0.10, 0.15, 0.20, 0.33]
    kept = select_recall_indices(dists, max_dist=0.40, rel_band=0.10, max_items=8)
    assert kept == [0, 1, 2]


def test_dynamic_count_can_exceed_old_fixed_5() -> None:
    # 一堆都强相关 → 给到动态上限(8)，而非硬卡 5（解决"5 条不够"）
    dists = [0.05 + i * 0.005 for i in range(15)]  # 全在最佳+band 内
    kept = select_recall_indices(dists, max_dist=0.40, rel_band=0.10, max_items=8)
    assert len(kept) == 8


def test_absolute_threshold_trims_tail() -> None:
    dists = [0.10, 0.20, 0.50, 0.60]  # 后两条超绝对阈值 0.34
    kept = select_recall_indices(dists, max_dist=0.34, rel_band=0.30, max_items=8)
    assert kept == [0, 1]


def test_weak_best_hit_keeps_few() -> None:
    # 最佳命中本身偏弱(0.30)，但仍 < max_dist；band 内只它自己
    dists = [0.30, 0.42, 0.50]
    kept = select_recall_indices(dists, max_dist=0.34, rel_band=0.10, max_items=8)
    assert kept == [0]


def test_defaults_are_sane() -> None:
    assert 0.0 < _UNIFIED_RECALL_MAX_DIST < 1.0
    assert _UNIFIED_RECALL_MAX_ITEMS > 5  # 比旧的固定 5 大
    assert _UNIFIED_RECALL_REL_BAND > 0
