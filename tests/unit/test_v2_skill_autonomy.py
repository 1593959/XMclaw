from __future__ import annotations

from xmclaw.skills.autonomy import (
    parse_skill_autonomy_policy,
    render_skill_autonomy_hint,
)


def test_skill_autonomy_defaults_to_prefer() -> None:
    policy = parse_skill_autonomy_policy({})

    assert policy.enabled is True
    assert policy.mode == "prefer"
    assert policy.max_loaded == 2
    assert "Prefer loading" in policy.policy_text


def test_skill_autonomy_force_renders_strong_system_block() -> None:
    hint = render_skill_autonomy_hint(
        config={
            "skills": {
                "autonomous_invocation": {
                    "enabled": True,
                    "mode": "force",
                    "max_loaded": 9,
                },
            },
        },
        matched=True,
    )

    assert "<skill-autonomy>" in hint
    assert "mode: force" in hint
    assert "MUST load and follow" in hint


def test_skill_autonomy_disabled_or_unmatched_is_silent() -> None:
    disabled = render_skill_autonomy_hint(
        config={"skills": {"autonomous_invocation": {"enabled": False}}},
        matched=True,
    )
    unmatched = render_skill_autonomy_hint(config={}, matched=False)

    assert disabled == ""
    assert unmatched == ""


def test_skill_autonomy_invalid_mode_falls_back_to_prefer() -> None:
    policy = parse_skill_autonomy_policy(
        {"skills": {"autonomous_invocation": {"mode": "banana", "max_loaded": -1}}},
    )

    assert policy.mode == "prefer"
    assert policy.max_loaded == 1
