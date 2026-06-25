from __future__ import annotations

from xmclaw.memory.v2.gateway_recall import (
    build_recall_plan,
    classify_buckets_heuristic,
    should_recall_heuristic,
)


def test_clean_chinese_gate_skips_greetings_and_confirmations() -> None:
    assert should_recall_heuristic("你好") is False
    assert should_recall_heuristic("好的") is False
    assert should_recall_heuristic("谢谢") is False


def test_clean_chinese_gate_keeps_memory_signals() -> None:
    assert should_recall_heuristic("以后都用中文回答我") is True
    assert should_recall_heuristic("上次那个微信在 E 盘的事情继续处理") is True


def test_clean_chinese_bucket_classifier_covers_user_project_rules() -> None:
    assert "user_preference" in classify_buckets_heuristic("我喜欢简洁的回复")
    assert "project_fact" in classify_buckets_heuristic("我们项目的服务器地址是什么")
    assert "rules" in classify_buckets_heuristic("永远别删我的配置文件")


def test_recall_plan_for_path_task_uses_general_memory_not_environment_branch() -> None:
    plan = build_recall_plan("删除桌面的微信，别只在 C 盘找，先检查历史失败经验")

    assert plan.need_recall is True
    assert "environment" not in plan.relevant_buckets
    assert "failure_modes" in plan.relevant_buckets
    assert "project_fact" in plan.relevant_buckets


def test_recall_plan_for_similar_task_promotes_procedural_recall() -> None:
    plan = build_recall_plan("以后遇到类似任务要先总结规律")

    assert plan.need_recall is True
    assert "procedural" in plan.relevant_buckets
    assert "可复用流程" in plan.query_expansion


def test_recall_plan_skips_smalltalk() -> None:
    plan = build_recall_plan("在吗")

    assert plan.need_recall is False
    assert plan.relevant_buckets == []
