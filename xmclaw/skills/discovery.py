"""Structured skill discovery middleware.

The agent should not rely on "maybe the model noticed a skill in the tool
list". This middleware turns skill use into a structured routing decision:
candidates, events, skip reasons, and a machine-readable policy block.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from xmclaw.core.ir import ToolSpec
from xmclaw.skills.autonomy import parse_skill_autonomy_policy


@dataclass(frozen=True)
class SkillCandidate:
    skill_id: str
    tool_name: str
    title: str
    description: str
    triggers: tuple[str, ...] = ()
    trust_level: str = ""
    reason: str = "registry_intent_match"
    score: float = 1.0

    def to_event_payload(self) -> dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "tool_name": self.tool_name,
            "title": self.title,
            "description": self.description[:500],
            "triggers": list(self.triggers),
            "trust_level": self.trust_level,
            "reason": self.reason,
            "score": self.score,
        }


@dataclass(frozen=True)
class SkillDiscoveryDecision:
    candidates: tuple[SkillCandidate, ...]
    tool_specs: tuple[ToolSpec, ...]
    events: tuple[dict[str, Any], ...]
    system_block: str
    skip_reasons: tuple[str, ...] = ()
    recommended_browse_query: str = ""
    required_action: str = ""
    must_browse_catalog: bool = False

    @property
    def matched(self) -> bool:
        return bool(self.candidates)


class SkillDiscoveryMiddleware:
    """Discover relevant skills and render a deterministic turn policy."""

    def __init__(self, registry: Any, config: dict[str, Any] | None = None) -> None:
        self._registry = registry
        self._config = config if isinstance(config, dict) else {}

    def discover(self, user_message: str, *, top_k: int | None = None) -> SkillDiscoveryDecision:
        policy = parse_skill_autonomy_policy(self._config)
        max_loaded = top_k or policy.max_loaded
        candidates: list[SkillCandidate] = []
        specs: list[ToolSpec] = []

        if self._registry is not None and (user_message or "").strip():
            try:
                for rank, skill in enumerate(
                    self._registry.find_multi(user_message, top_k=max_loaded),
                    start=1,
                ):
                    cand = self._candidate_from_skill(skill, rank=rank)
                    if cand.score < policy.min_score:
                        continue
                    candidates.append(cand)
                    specs.append(self._tool_spec_for_candidate(cand))
            except Exception:  # noqa: BLE001
                candidates = []
                specs = []

        skip_reasons = self._skip_reasons(candidates, user_message)
        browse_query = self._browse_query(user_message)
        required_action = self._required_action(policy, candidates)
        must_browse = self._must_browse_catalog(user_message, candidates)
        if not policy.auto_browse_on_no_match and not candidates:
            must_browse = False
        events = self._events(
            candidates,
            must_browse_catalog=must_browse,
            required_action=required_action,
        )
        block = self._render_system_block(
            policy,
            candidates,
            events=events,
            skip_reasons=skip_reasons,
            browse_query=browse_query,
            required_action=required_action,
            must_browse_catalog=must_browse,
        )
        return SkillDiscoveryDecision(
            candidates=tuple(candidates),
            tool_specs=tuple(specs),
            events=tuple(events),
            system_block=block,
            skip_reasons=tuple(skip_reasons),
            recommended_browse_query=browse_query,
            required_action=required_action,
            must_browse_catalog=must_browse,
        )

    def _candidate_from_skill(self, skill: Any, *, rank: int) -> SkillCandidate:
        skill_id = str(getattr(skill, "id", "") or "")
        manifest = None
        try:
            manifest = self._registry.ref(skill_id).manifest
        except Exception:  # noqa: BLE001
            manifest = None
        title = str(getattr(manifest, "title", "") or skill_id)
        description = str(getattr(manifest, "description", "") or title or skill_id)
        triggers = tuple(str(t) for t in (getattr(manifest, "triggers", ()) or ()))
        when_to_use = str(getattr(manifest, "when_to_use", "") or "")
        if when_to_use and when_to_use not in description:
            description = f"{description}\nWhen to use: {when_to_use}"
        trust = str(getattr(manifest, "trust_level", "") or "")
        if "." in trust:
            trust = trust.rsplit(".", 1)[-1]
        safe_id = skill_id.replace(".", "__")
        return SkillCandidate(
            skill_id=skill_id,
            tool_name=f"skill_{safe_id}"[:64],
            title=title,
            description=description,
            triggers=triggers,
            trust_level=trust,
            score=max(0.0, 1.0 - ((rank - 1) * 0.1)),
        )

    def _tool_spec_for_candidate(self, cand: SkillCandidate) -> ToolSpec:
        desc = cand.description
        if cand.triggers:
            desc += "\nUse when: " + ", ".join(cand.triggers[:6])
        return ToolSpec(
            name=cand.tool_name,
            description=f"Skill: {desc}",
            parameters_schema={"type": "object", "additionalProperties": True},
        )

    def _required_action(self, policy: Any, candidates: list[SkillCandidate]) -> str:
        if not policy.require_decision:
            return ""
        return (
            "call_skill_decision_then_use_or_skip"
            if candidates else
            "call_skill_decision_browse_then_skill_browse"
        )

    def _events(
        self,
        candidates: list[SkillCandidate],
        *,
        must_browse_catalog: bool,
        required_action: str,
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = [
            {
                "type": "skill_discovery_ran",
                "candidate_count": len(candidates),
                "candidates": [c.to_event_payload() for c in candidates],
                "required_action": required_action,
                "must_browse_catalog": must_browse_catalog,
            },
        ]
        for cand in candidates:
            events.append({
                "type": "skill_considered",
                **cand.to_event_payload(),
                "skip_reason_required": bool(required_action),
            })
        if must_browse_catalog:
            events.append({
                "type": "skill_catalog_query_available",
                "tool_name": "skill_browse",
                "reason": "no_direct_candidate" if not candidates else "failure_signal_or_forced_browse",
            })
        return events

    def _skip_reasons(
        self,
        candidates: list[SkillCandidate],
        user_message: str,
    ) -> list[str]:
        if candidates:
            return [
                "candidate_not_applicable_to_task",
                "candidate_untrusted_or_requires_review",
                "candidate_missing_required_capability",
            ]
        if not (user_message or "").strip():
            return ["empty_user_message"]
        return ["no_direct_candidate_from_registry"]

    def _browse_query(self, user_message: str) -> str:
        text = " ".join((user_message or "").split())
        return text[:120] if text else "current task intent"

    def _must_browse_catalog(
        self,
        user_message: str,
        candidates: list[SkillCandidate],
    ) -> bool:
        text = (user_message or "").lower()
        failure_signals = (
            "失败", "报错", "卡住", "超时", "找不到", "重复", "死磕",
            "failed", "error", "timeout", "stuck",
        )
        return not candidates or any(sig in text for sig in failure_signals)

    def _render_system_block(
        self,
        policy: Any,
        candidates: list[SkillCandidate],
        *,
        events: list[dict[str, Any]],
        skip_reasons: list[str],
        browse_query: str,
        required_action: str,
        must_browse_catalog: bool,
    ) -> str:
        browse_line = self._browse_instruction(policy)
        structured = self._render_structured_block(
            candidates,
            events=events,
            skip_reasons=skip_reasons,
            browse_query=browse_query,
            required_action=required_action,
            must_browse_catalog=must_browse_catalog,
        )
        if not policy.enabled:
            return (
                "\n\n<skill-discovery>\n"
                "autonomy: disabled\n"
                f"{browse_line}\n"
                f"{structured}\n"
                "</skill-discovery>"
            )
        if not candidates:
            run_line = (
                "When skill_browse returns a promising result, call "
                "skill_decision(action='use', skill_id=...) then use "
                "skill_view to inspect it and skill_run to execute it.\n"
                if policy.auto_browse_on_no_match else ""
            )
            return (
                "\n\n<skill-discovery>\n"
                "candidates: []\n"
                f"{browse_line}\n"
                f"{run_line}"
                f"recommended_browse_query: {browse_query}\n"
                f"{structured}\n"
                "</skill-discovery>"
            )
        rows = "\n".join(
            f"- {c.tool_name} | id={c.skill_id} | score={c.score:.2f} | reason={c.reason}"
            for c in candidates
        )
        decision_rule = (
            "Decision rule: call skill_decision(action='use', skill_id=...) "
            "before using the best applicable skill; call skill_view or "
            "skill_run when you need its procedure; or call "
            "skill_decision(action='skip', skill_id=..., skip_reason=...) "
            "before using generic tools.\n"
            if policy.require_decision else
            "Decision rule: prefer the best applicable skill, but decision "
            "logging is optional for this configuration.\n"
        )
        return (
            "\n\n<skill-discovery>\n"
            f"mode: {policy.mode}\n"
            f"policy: {policy.policy_text}\n"
            "candidates:\n"
            f"{rows}\n"
            f"{decision_rule}"
            f"{browse_line}\n"
            "Allowed skip_reasons: " + ", ".join(skip_reasons) + "\n"
            f"recommended_browse_query: {browse_query}\n"
            f"{structured}\n"
            "</skill-discovery>"
        )

    def _browse_instruction(self, policy: Any) -> str:
        if policy.auto_browse_on_no_match:
            return (
                "If no listed candidate fits, call skill_decision(action='browse', "
                "browse_query=...) and then skill_browse with a concise query for "
                "the user's intent before falling back to generic tools."
            )
        return (
            "If no listed candidate fits, generic tools are allowed directly; "
            "record a skip reason when decision logging is enabled."
        )

    def _render_structured_block(
        self,
        candidates: list[SkillCandidate],
        *,
        events: list[dict[str, Any]],
        skip_reasons: list[str],
        browse_query: str,
        required_action: str,
        must_browse_catalog: bool,
    ) -> str:
        payload = {
            "candidates": [c.to_event_payload() for c in candidates],
            "events": events,
            "skip_reasons": skip_reasons,
            "recommended_browse_query": browse_query,
            "required_action": required_action,
            "must_browse_catalog": must_browse_catalog,
            "decision_tool": "skill_decision",
        }
        return (
            "<skill-discovery-json>\n"
            + json.dumps(payload, ensure_ascii=False, sort_keys=True)
            + "\n</skill-discovery-json>"
        )


def merge_skill_specs(
    current: list[ToolSpec] | None,
    injected: tuple[ToolSpec, ...],
) -> list[ToolSpec] | None:
    if current is None or not injected:
        return current
    existing = {getattr(spec, "name", "") for spec in current}
    new_specs = [spec for spec in injected if spec.name not in existing]
    if not new_specs:
        return current
    non_skills = [
        spec for spec in current
        if not (getattr(spec, "name", "") or "").startswith("skill_")
    ]
    skills = [
        spec for spec in current
        if (getattr(spec, "name", "") or "").startswith("skill_")
    ]
    return non_skills + new_specs + skills


__all__ = [
    "SkillCandidate",
    "SkillDiscoveryDecision",
    "SkillDiscoveryMiddleware",
    "merge_skill_specs",
]
