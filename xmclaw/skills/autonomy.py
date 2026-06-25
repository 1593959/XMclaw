"""Skill autonomy policy helpers.

The skill runtime has two separate responsibilities:

* discovery/routing: decide which installed skills are relevant;
* autonomy policy: decide how strongly the current turn should prefer or
  require those skills once they have been discovered.

This module owns the second responsibility so AgentLoop does not keep growing
ad-hoc prompt fragments.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


VALID_SKILL_AUTONOMY_MODES = {"suggest", "prefer", "force"}


@dataclass(frozen=True)
class SkillAutonomyPolicy:
    enabled: bool = True
    mode: str = "prefer"
    min_score: float = 0.0
    max_loaded: int = 2
    require_decision: bool = True
    auto_browse_on_no_match: bool = True

    @property
    def policy_text(self) -> str:
        if self.mode == "force":
            return (
                "MUST load and follow the best matching skill first unless it "
                "is clearly unsafe or irrelevant."
            )
        if self.mode == "prefer":
            return (
                "Prefer loading the best matching skill before using generic "
                "tools."
            )
        return "Consider relevant installed skills before choosing tools."


def parse_skill_autonomy_policy(config: dict[str, Any] | None) -> SkillAutonomyPolicy:
    skills_cfg = (config or {}).get("skills", {}) if isinstance(config, dict) else {}
    raw = skills_cfg.get("autonomous_invocation", {}) if isinstance(skills_cfg, dict) else {}
    raw = raw if isinstance(raw, dict) else {}
    mode = str(raw.get("mode", "prefer")).lower()
    if mode not in VALID_SKILL_AUTONOMY_MODES:
        mode = "prefer"
    try:
        max_loaded = int(raw.get("max_loaded", 2) or 2)
    except (TypeError, ValueError):
        max_loaded = 2
    try:
        min_score = float(raw.get("min_score", 0.0) or 0.0)
    except (TypeError, ValueError):
        min_score = 0.0
    return SkillAutonomyPolicy(
        enabled=bool(raw.get("enabled", True)),
        mode=mode,
        min_score=max(0.0, min_score),
        max_loaded=max(1, min(5, max_loaded)),
        require_decision=bool(raw.get("require_decision", True)),
        auto_browse_on_no_match=bool(raw.get("auto_browse_on_no_match", True)),
    )


def render_skill_autonomy_hint(
    *,
    config: dict[str, Any] | None,
    matched: bool,
) -> str:
    """Render a system context block for the current skill routing result.

    ``matched`` is deliberately boolean for the first extraction pass: the
    current AgentLoop already renders the concrete matched skill names in its
    router hint. A later SkillDiscoveryMiddleware should pass structured
    candidate metadata here and emit telemetry events.
    """
    policy = parse_skill_autonomy_policy(config)
    if not policy.enabled or not matched:
        return ""
    return (
        "\n\n<skill-autonomy>\n"
        f"mode: {policy.mode}\n"
        f"policy: {policy.policy_text}\n"
        "If a matched skill is applicable, call that skill tool or "
        "skill_browse/skill_run to load its procedure, then execute the "
        "procedure with normal tools. Record why you skipped it when you "
        "choose not to use it.\n"
        "</skill-autonomy>"
    )


__all__ = [
    "SkillAutonomyPolicy",
    "VALID_SKILL_AUTONOMY_MODES",
    "parse_skill_autonomy_policy",
    "render_skill_autonomy_hint",
]
