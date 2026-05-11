"""AutonomyPolicy + SuggestionInbox unit tests — R5 (2026-05-10).

Covers:
  * AutonomyPolicy.evaluate at all 3 tiers (observe / suggest /
    execute) × 3 risk levels (low / medium / high) × confidence
    floors × user-presence × rate limiter.
  * SuggestionInbox SQLite round-trip: add / list_pending /
    decide (approved/rejected/expired) / mark_applied / count.
"""
from __future__ import annotations

from pathlib import Path


from xmclaw.cognition.autonomy import (
    AutonomyPolicy,
)
from xmclaw.cognition.suggestion_inbox import (
    Suggestion,
    SuggestionInbox,
)


# ── AutonomyPolicy: tier resolution ──────────────────────────────


def test_observe_tier_drops_everything() -> None:
    p = AutonomyPolicy(autonomy_level=0)
    d = p.evaluate(action_kind="curriculum_edit", confidence=0.9)
    assert d.verdict == "drop"
    assert d.tier == "observe"


def test_suggest_tier_surfaces_low_risk() -> None:
    p = AutonomyPolicy(autonomy_level=50)
    d = p.evaluate(action_kind="curriculum_edit", confidence=0.5)
    assert d.verdict == "surface"
    assert d.tier == "suggest"
    assert d.risk == "low"


def test_suggest_tier_surfaces_high_risk() -> None:
    """At suggest tier high-risk also surfaces — never auto-applies."""
    p = AutonomyPolicy(autonomy_level=50)
    d = p.evaluate(action_kind="skill_propose", confidence=0.5)
    assert d.verdict == "surface"
    assert d.risk == "high"


def test_suggest_tier_drops_low_confidence() -> None:
    p = AutonomyPolicy(autonomy_level=50)
    d = p.evaluate(action_kind="curriculum_edit", confidence=0.1)
    assert d.verdict == "drop"


def test_execute_tier_high_risk_needs_confirmation() -> None:
    """Even at execute tier, high-risk → double-confirm."""
    p = AutonomyPolicy(autonomy_level=100)
    d = p.evaluate(action_kind="skill_propose", confidence=0.9)
    assert d.verdict == "needs_confirmation"
    assert d.risk == "high"


def test_execute_tier_low_risk_auto_applies() -> None:
    p = AutonomyPolicy(autonomy_level=100)
    d = p.evaluate(action_kind="curriculum_edit", confidence=0.9)
    assert d.verdict == "auto_apply"


def test_execute_tier_medium_risk_low_conf_needs_confirm() -> None:
    p = AutonomyPolicy(autonomy_level=100)
    d = p.evaluate(action_kind="goal_add", confidence=0.3)
    assert d.verdict == "needs_confirmation"


def test_execute_tier_medium_risk_user_away_surfaces() -> None:
    p = AutonomyPolicy(autonomy_level=100)
    d = p.evaluate(
        action_kind="goal_add", confidence=0.7,
        is_user_present=False,
    )
    assert d.verdict == "surface"
    assert "user away" in d.reason


def test_execute_tier_medium_risk_user_present_auto_applies() -> None:
    p = AutonomyPolicy(autonomy_level=100)
    d = p.evaluate(
        action_kind="goal_add", confidence=0.7, is_user_present=True,
    )
    assert d.verdict == "auto_apply"


def test_unknown_kind_defaults_to_high_risk() -> None:
    """Fail-closed: unknown kinds get treated as high → forces
    operator confirmation rather than silent auto-apply."""
    p = AutonomyPolicy(autonomy_level=100)
    d = p.evaluate(action_kind="some_new_thing", confidence=0.9)
    assert d.risk == "high"
    assert d.verdict == "needs_confirmation"


def test_risk_overrides_take_precedence() -> None:
    p = AutonomyPolicy(
        autonomy_level=100,
        risk_overrides={"shell_command": "low"},  # operator de-classified
    )
    assert p.risk_of("shell_command") == "low"
    d = p.evaluate(action_kind="shell_command", confidence=0.9)
    assert d.verdict == "auto_apply"


# ── Rate limiter ─────────────────────────────────────────────────


def test_rate_limiter_kicks_in_after_cap() -> None:
    p = AutonomyPolicy(
        autonomy_level=100,
        max_auto_applies_per_hour=3,
    )
    # Burn 3 auto-applies of the same kind.
    for _ in range(3):
        d = p.evaluate(action_kind="curriculum_edit", confidence=0.9)
        assert d.verdict == "auto_apply"
        p.record_applied("curriculum_edit")
    # 4th should be rate-limited → surface.
    d = p.evaluate(action_kind="curriculum_edit", confidence=0.9)
    assert d.verdict == "surface"
    assert "rate-limited" in d.reason
    assert d.rate_limit_remaining == 0


def test_rate_limiter_isolated_per_kind() -> None:
    """Different kinds have separate rate buckets."""
    p = AutonomyPolicy(
        autonomy_level=100, max_auto_applies_per_hour=2,
    )
    p.record_applied("curriculum_edit")
    p.record_applied("curriculum_edit")
    # curriculum_edit cap reached.
    assert (
        p.evaluate(action_kind="curriculum_edit", confidence=0.9).verdict
        == "surface"
    )
    # preference_update untouched.
    assert (
        p.evaluate(action_kind="preference_update", confidence=0.9).verdict
        == "auto_apply"
    )


def test_low_risk_low_confidence_surfaces_at_execute() -> None:
    """Even low-risk needs confidence > 0.3 to auto-apply."""
    p = AutonomyPolicy(autonomy_level=100)
    d = p.evaluate(action_kind="curriculum_edit", confidence=0.2)
    assert d.verdict == "surface"


# ── SuggestionInbox ──────────────────────────────────────────────


def test_inbox_add_and_list_pending(tmp_path: Path) -> None:
    inbox = SuggestionInbox(db_path=tmp_path / "s.db")
    sg = Suggestion(
        kind="curriculum_edit", source="metacognition",
        summary="don't decline so eagerly",
        payload={"addendum": "try first, then explain"},
        risk="low", confidence=0.5, verdict="surface",
    )
    sid = inbox.add(sg)
    assert sid == sg.id

    pending = inbox.list_pending()
    assert len(pending) == 1
    assert pending[0].id == sid
    assert pending[0].kind == "curriculum_edit"
    assert pending[0].payload == {"addendum": "try first, then explain"}
    assert inbox.count_pending() == 1
    inbox.close()


def test_inbox_decide_approved(tmp_path: Path) -> None:
    inbox = SuggestionInbox(db_path=tmp_path / "s.db")
    sid = inbox.add(Suggestion(kind="x", summary="x"))
    assert inbox.decide(sid, status="approved")
    sg = inbox.get(sid)
    assert sg is not None
    assert sg.status == "approved"
    assert sg.decided_at is not None
    assert sg.decided_by == "user"
    # Re-deciding a non-pending row is a no-op.
    assert not inbox.decide(sid, status="rejected")
    inbox.close()


def test_inbox_decide_rejected(tmp_path: Path) -> None:
    inbox = SuggestionInbox(db_path=tmp_path / "s.db")
    sid = inbox.add(Suggestion(kind="x", summary="x"))
    assert inbox.decide(sid, status="rejected")
    sg = inbox.get(sid)
    assert sg.status == "rejected"
    inbox.close()


def test_inbox_decide_expired_works_then_blocks_re_decide(
    tmp_path: Path,
) -> None:
    inbox = SuggestionInbox(db_path=tmp_path / "s.db")
    sid = inbox.add(Suggestion(kind="x", summary="x"))
    assert inbox.decide(sid, status="expired", decided_by="auto_expire")
    # Can't re-approve expired ones.
    assert not inbox.decide(sid, status="approved")
    sg = inbox.get(sid)
    assert sg.status == "expired"
    assert sg.decided_by == "auto_expire"
    inbox.close()


def test_inbox_mark_applied_only_after_approval(tmp_path: Path) -> None:
    inbox = SuggestionInbox(db_path=tmp_path / "s.db")
    sid = inbox.add(Suggestion(kind="x", summary="x"))
    # Can't apply pending — must be approved first.
    assert not inbox.mark_applied(sid)
    inbox.decide(sid, status="approved")
    assert inbox.mark_applied(sid, outcome="curriculum patched")
    sg = inbox.get(sid)
    assert sg.status == "applied"
    assert sg.applied_outcome == "curriculum patched"
    assert sg.applied_at is not None
    inbox.close()


def test_inbox_decide_invalid_status_returns_false(tmp_path: Path) -> None:
    inbox = SuggestionInbox(db_path=tmp_path / "s.db")
    sid = inbox.add(Suggestion(kind="x", summary="x"))
    assert not inbox.decide(sid, status="bogus")  # type: ignore[arg-type]
    inbox.close()


def test_inbox_list_recent_filters_by_status(tmp_path: Path) -> None:
    inbox = SuggestionInbox(db_path=tmp_path / "s.db")
    s1 = inbox.add(Suggestion(kind="a", summary="a"))
    s2 = inbox.add(Suggestion(kind="b", summary="b"))
    inbox.decide(s1, status="approved")
    approved = inbox.list_recent(status="approved")
    assert {sg.id for sg in approved} == {s1}
    pending = inbox.list_recent(status="pending")
    assert {sg.id for sg in pending} == {s2}
    inbox.close()


def test_inbox_get_unknown_returns_none(tmp_path: Path) -> None:
    inbox = SuggestionInbox(db_path=tmp_path / "s.db")
    assert inbox.get("ghost") is None
    inbox.close()
