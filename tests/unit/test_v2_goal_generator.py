"""Unit tests for Jarvis Phase 6.4: GoalGenerator + AutonomyPolicy.

Spec: docs/JARVIS_PHASE_6_DESIGN.md §3.5.
"""
from __future__ import annotations

import time
from typing import Any

import pytest

from xmclaw.cognition.goal_generator import (
    AutonomyPolicy,
    Goal,
    GoalGenerator,
)


# --------------------------------------------------------------- helpers


class FakeCognitiveState(dict):
    """Dict-backed fake — generator only reads ``current_goals``."""

    def __init__(self, current_goals: list[Any] | None = None) -> None:
        super().__init__()
        self["current_goals"] = current_goals or []


# --------------------------------------------------------------- AutonomyPolicy


class TestAutonomyPolicyFromLevel:
    def test_level_zero_all_off(self) -> None:
        p = AutonomyPolicy.from_level(0)
        assert p.level == 0
        assert p.autonomous_action_per_hour_cap == 0
        assert p.can_modify_files is False
        assert p.can_send_messages is False
        assert p.can_run_long_processes is False
        assert p.proactive_notification_enabled is False
        assert p.self_experiment_enabled is False
        assert p.weekly_summary_enabled is False

    def test_level_50_proactive_only(self) -> None:
        p = AutonomyPolicy.from_level(50)
        # proactive prompts on, weekly summary on
        assert p.proactive_notification_enabled is True
        assert p.weekly_summary_enabled is True
        # action flags still off, no cap
        assert p.can_modify_files is False
        assert p.can_send_messages is False
        assert p.can_run_long_processes is False
        assert p.autonomous_action_per_hour_cap == 0
        assert p.self_experiment_enabled is False

    def test_level_100_full_jarvis(self) -> None:
        p = AutonomyPolicy.from_level(100)
        assert p.autonomous_action_per_hour_cap == -1
        assert p.can_modify_files is True
        assert p.can_send_messages is True
        assert p.can_run_long_processes is True
        assert p.proactive_notification_enabled is True
        assert p.self_experiment_enabled is True
        assert p.weekly_summary_enabled is True

    def test_intermediate_level_75_guarded(self) -> None:
        p = AutonomyPolicy.from_level(75)
        # writes + long processes enabled, capped at 5/hr
        assert p.autonomous_action_per_hour_cap == 5
        assert p.can_modify_files is True
        assert p.can_run_long_processes is True
        # messaging still locked until level >= 90
        assert p.can_send_messages is False
        assert p.self_experiment_enabled is True

    def test_intermediate_level_30_summary_only(self) -> None:
        p = AutonomyPolicy.from_level(30)
        assert p.weekly_summary_enabled is True
        assert p.proactive_notification_enabled is False
        assert p.autonomous_action_per_hour_cap == 0

    def test_flags_ramp_monotonically(self) -> None:
        levels = [0, 30, 50, 75, 90, 100]
        bool_fields = (
            "can_modify_files",
            "can_send_messages",
            "can_run_long_processes",
            "proactive_notification_enabled",
            "self_experiment_enabled",
            "weekly_summary_enabled",
        )
        prev = AutonomyPolicy.from_level(levels[0])
        for lv in levels[1:]:
            cur = AutonomyPolicy.from_level(lv)
            for fld in bool_fields:
                # once a flag turns on at some level, it never turns off
                # again as level rises.
                if getattr(prev, fld):
                    assert getattr(cur, fld), f"{fld} regressed at level {lv}"
            prev = cur

    def test_level_clamped_below_zero(self) -> None:
        p = AutonomyPolicy.from_level(-25)
        assert p.level == 0
        assert p.autonomous_action_per_hour_cap == 0

    def test_level_clamped_above_hundred(self) -> None:
        p = AutonomyPolicy.from_level(250)
        assert p.level == 100
        assert p.autonomous_action_per_hour_cap == -1

    def test_policy_is_frozen(self) -> None:
        p = AutonomyPolicy.from_level(50)
        with pytest.raises(Exception):
            p.level = 99  # type: ignore[misc]


# --------------------------------------------------------------- can_act


class TestAutonomyPolicyCanAct:
    @staticmethod
    def _g(category: str, *, name: str = "x") -> Goal:
        return Goal(
            id="gid", name=name, description="d", priority=5, category=category,
        )

    def test_level_zero_blocks_everything(self) -> None:
        p = AutonomyPolicy.from_level(0)
        for cat in ("maintenance", "exploration", "social", "general"):
            assert p.can_act(self._g(cat)) is False

    def test_maintenance_requires_modify_files(self) -> None:
        p_mid = AutonomyPolicy.from_level(50)  # no can_modify_files
        assert p_mid.can_act(self._g("maintenance")) is False
        # but can_act gate fails with cap=0 too — bump to 75
        p_hi = AutonomyPolicy.from_level(75)
        assert p_hi.can_act(self._g("maintenance")) is True

    def test_exploration_requires_long_processes(self) -> None:
        p_lo = AutonomyPolicy.from_level(50)
        assert p_lo.can_act(self._g("exploration")) is False
        p_hi = AutonomyPolicy.from_level(75)
        assert p_hi.can_act(self._g("exploration")) is True

    def test_social_requires_messaging(self) -> None:
        p_75 = AutonomyPolicy.from_level(75)  # no messages yet
        assert p_75.can_act(self._g("social")) is False
        p_full = AutonomyPolicy.from_level(100)
        assert p_full.can_act(self._g("social")) is True

    def test_general_falls_through_to_any_action(self) -> None:
        p_zero = AutonomyPolicy.from_level(0)
        assert p_zero.can_act(self._g("general")) is False
        p_full = AutonomyPolicy.from_level(100)
        assert p_full.can_act(self._g("general")) is True


# --------------------------------------------------------------- GoalGenerator


@pytest.mark.asyncio
class TestGoalGenerator:
    async def test_maintenance_empty_at_level_zero(self) -> None:
        state = FakeCognitiveState()
        gen = GoalGenerator(state, AutonomyPolicy.from_level(0))
        assert await gen.maintenance() == []

    async def test_exploration_empty_when_long_processes_disallowed(self) -> None:
        state = FakeCognitiveState()
        # level 50 → proactive on but can_run_long_processes still False
        gen = GoalGenerator(state, AutonomyPolicy.from_level(50))
        assert await gen.exploration() == []

    async def test_social_empty_when_messages_disallowed(self) -> None:
        state = FakeCognitiveState()
        # level 75 → writes + long processes on, but can_send_messages False
        gen = GoalGenerator(state, AutonomyPolicy.from_level(75))
        assert await gen.social() == []

    async def test_maintenance_emits_at_level_75(self) -> None:
        state = FakeCognitiveState()
        gen = GoalGenerator(state, AutonomyPolicy.from_level(75))
        goals = await gen.maintenance()
        assert len(goals) == 3
        assert all(g.category == "maintenance" for g in goals)

    async def test_exploration_emits_at_level_75(self) -> None:
        state = FakeCognitiveState()
        gen = GoalGenerator(state, AutonomyPolicy.from_level(75))
        goals = await gen.exploration()
        assert len(goals) == 3
        assert all(g.category == "exploration" for g in goals)

    async def test_social_emits_at_level_100(self) -> None:
        state = FakeCognitiveState()
        gen = GoalGenerator(state, AutonomyPolicy.from_level(100))
        goals = await gen.social()
        assert len(goals) == 2
        assert all(g.category == "social" for g in goals)

    async def test_generate_all_dedupes_against_current_goals(self) -> None:
        existing = [
            {"name": "maintenance.clean_stale_temp", "description": "old"},
        ]
        state = FakeCognitiveState(current_goals=existing)
        gen = GoalGenerator(state, AutonomyPolicy.from_level(100))
        goals = await gen.generate_all()
        names = {g.name for g in goals}
        assert "maintenance.clean_stale_temp" not in names
        # the rest should still pass
        assert "maintenance.compact_journal" in names

    async def test_generate_all_dedupes_by_description(self) -> None:
        existing = [
            {
                "name": "user-typed",
                "description": (
                    "Compact memory journal segments older than 14 days."
                ),
            },
        ]
        state = FakeCognitiveState(current_goals=existing)
        gen = GoalGenerator(state, AutonomyPolicy.from_level(100))
        goals = await gen.generate_all()
        names = {g.name for g in goals}
        assert "maintenance.compact_journal" not in names

    async def test_generate_all_returns_empty_at_level_zero(self) -> None:
        state = FakeCognitiveState()
        gen = GoalGenerator(state, AutonomyPolicy.from_level(0))
        assert await gen.generate_all() == []

    async def test_generate_all_respects_rate_cap(self) -> None:
        state = FakeCognitiveState()
        # level 75 → cap=5
        gen = GoalGenerator(state, AutonomyPolicy.from_level(75))
        goals = await gen.generate_all()
        # maintenance(3) + exploration(3) = 6 candidates, cap=5
        assert len(goals) == 5

    async def test_rate_cap_enforced_across_calls_in_window(self) -> None:
        state = FakeCognitiveState()
        gen = GoalGenerator(state, AutonomyPolicy.from_level(75))
        first = await gen.generate_all()
        assert len(first) == 5
        # subsequent call within the same hour should yield nothing —
        # the ledger already holds 5 minted-timestamps.
        second = await gen.generate_all()
        assert second == []

    async def test_rate_cap_resets_after_window(self) -> None:
        state = FakeCognitiveState()
        gen = GoalGenerator(
            state, AutonomyPolicy.from_level(75), recent_goal_window_s=0.01,
        )
        first = await gen.generate_all()
        assert len(first) == 5
        # wait past the 10ms window
        time.sleep(0.05)
        # still dedup against existing current_goals (none here), and
        # rate window is now empty → fresh batch should mint again.
        second = await gen.generate_all()
        assert len(second) == 5

    async def test_unlimited_cap_at_level_100(self) -> None:
        state = FakeCognitiveState()
        gen = GoalGenerator(state, AutonomyPolicy.from_level(100))
        goals = await gen.generate_all()
        # 3 maintenance + 3 exploration + 2 social = 8 candidates,
        # cap=-1 (unlimited) so all should pass.
        assert len(goals) == 8

    async def test_categories_set_correctly(self) -> None:
        state = FakeCognitiveState()
        gen = GoalGenerator(state, AutonomyPolicy.from_level(100))
        goals = await gen.generate_all()
        cats = {g.category for g in goals}
        assert cats == {"maintenance", "exploration", "social"}

    async def test_goals_have_unique_ids(self) -> None:
        state = FakeCognitiveState()
        gen = GoalGenerator(state, AutonomyPolicy.from_level(100))
        goals = await gen.generate_all()
        ids = [g.id for g in goals]
        assert len(ids) == len(set(ids))

    async def test_generate_all_priority_order_keeps_most_important(self) -> None:
        state = FakeCognitiveState()
        # cap=5 trims to top 5 by priority
        gen = GoalGenerator(state, AutonomyPolicy.from_level(75))
        goals = await gen.generate_all()
        priorities = [g.priority for g in goals]
        assert priorities == sorted(priorities, reverse=True)

    async def test_state_attribute_access_supported(self) -> None:
        # not just dict — generator should also handle objects with
        # ``current_goals`` attribute (real CognitiveState shape).
        class StateObj:
            current_goals: list[Any] = []

        s = StateObj()
        s.current_goals = [{"name": "maintenance.clean_stale_temp"}]
        gen = GoalGenerator(s, AutonomyPolicy.from_level(100))
        goals = await gen.generate_all()
        names = {g.name for g in goals}
        assert "maintenance.clean_stale_temp" not in names

    async def test_goal_dataclass_is_frozen(self) -> None:
        g = Goal(id="x", name="n", description="d", priority=1)
        with pytest.raises(Exception):
            g.priority = 9  # type: ignore[misc]
