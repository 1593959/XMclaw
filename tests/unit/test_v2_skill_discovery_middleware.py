from __future__ import annotations

import json

from xmclaw.core.ir import ToolSpec
from xmclaw.skills.base import Skill, SkillInput, SkillOutput
from xmclaw.skills.discovery import SkillDiscoveryMiddleware, merge_skill_specs
from xmclaw.skills.manifest import SkillManifest
from xmclaw.skills.registry import SkillRegistry


class _NoopSkill(Skill):
    def __init__(self, sid: str) -> None:
        self.id = sid
        self.version = 1

    async def run(self, inp: SkillInput) -> SkillOutput:
        return SkillOutput(ok=True, result={}, side_effects=[])


def _registry() -> SkillRegistry:
    reg = SkillRegistry()
    reg.register(
        _NoopSkill("frontend.ui-review"),
        SkillManifest(
            id="frontend.ui-review",
            version=1,
            title="Frontend UI review",
            description="Review React UI, layout, accessibility, and visual polish.",
            triggers=("React", "UI", "frontend"),
            when_to_use="Use when the user asks to audit or improve frontend UI.",
        ),
    )
    reg.register(
        _NoopSkill("git.commit"),
        SkillManifest(
            id="git.commit",
            version=1,
            title="Git commit",
            description="Create a clean git commit message.",
        ),
    )
    return reg


def test_skill_discovery_outputs_structured_candidates_and_events() -> None:
    decision = SkillDiscoveryMiddleware(_registry(), {}).discover(
        "帮我 review React 前端 UI",
    )

    assert decision.matched is True
    assert decision.candidates[0].skill_id == "frontend.ui-review"
    assert decision.candidates[0].tool_name == "skill_frontend__ui-review"
    assert decision.events[0]["type"] == "skill_discovery_ran"
    assert decision.events[0]["candidate_count"] >= 1
    assert any(ev["type"] == "skill_considered" for ev in decision.events)
    assert "skip_reason" in decision.system_block
    assert "skill_browse" in decision.system_block
    assert "<skill-discovery-json>" in decision.system_block
    assert decision.skip_reasons
    payload = _json_payload(decision.system_block)
    assert payload["candidates"][0]["skill_id"] == "frontend.ui-review"
    assert payload["required_action"] == "call_skill_decision_then_use_or_skip"
    assert payload["decision_tool"] == "skill_decision"
    assert decision.required_action == "call_skill_decision_then_use_or_skip"


def test_skill_discovery_no_match_tells_agent_to_browse_catalog() -> None:
    decision = SkillDiscoveryMiddleware(_registry(), {}).discover("完全不相关的星际导航")

    assert decision.matched is False
    assert decision.tool_specs == ()
    assert decision.events[-1]["type"] == "skill_catalog_query_available"
    assert "candidates: []" in decision.system_block
    assert "skill_browse" in decision.system_block
    assert "skill_view" in decision.system_block
    assert "skill_run" in decision.system_block
    assert decision.recommended_browse_query
    payload = _json_payload(decision.system_block)
    assert payload["candidates"] == []
    assert payload["required_action"] == "call_skill_decision_browse_then_skill_browse"
    assert decision.required_action == "call_skill_decision_browse_then_skill_browse"
    assert decision.must_browse_catalog is True
    assert "no_direct_candidate_from_registry" in payload["skip_reasons"]


def test_skill_discovery_can_disable_auto_browse_on_no_match() -> None:
    decision = SkillDiscoveryMiddleware(
        _registry(),
        {"skills": {"autonomous_invocation": {"auto_browse_on_no_match": False}}},
    ).discover("完全不相关的星际导航")

    payload = _json_payload(decision.system_block)
    assert decision.must_browse_catalog is False
    assert payload["must_browse_catalog"] is False
    assert all(ev["type"] != "skill_catalog_query_available" for ev in decision.events)


def test_skill_discovery_failure_signal_requires_catalog_browse_even_with_match() -> None:
    decision = SkillDiscoveryMiddleware(_registry(), {}).discover(
        "React UI 一直报错失败，卡住了",
    )

    payload = _json_payload(decision.system_block)
    assert decision.matched is True
    assert decision.must_browse_catalog is True
    assert payload["must_browse_catalog"] is True


def test_skill_discovery_force_mode_changes_policy_text() -> None:
    decision = SkillDiscoveryMiddleware(
        _registry(),
        {"skills": {"autonomous_invocation": {"mode": "force"}}},
    ).discover("React UI")

    assert "mode: force" in decision.system_block
    assert "MUST load and follow" in decision.system_block


def test_skill_discovery_can_disable_required_decision() -> None:
    decision = SkillDiscoveryMiddleware(
        _registry(),
        {"skills": {"autonomous_invocation": {"require_decision": False}}},
    ).discover("React UI")

    payload = _json_payload(decision.system_block)
    assert decision.required_action == ""
    assert payload["required_action"] == ""


def test_merge_skill_specs_prepends_injected_skills_without_duplicates() -> None:
    current = [
        ToolSpec(name="bash", description="", parameters_schema={}),
        ToolSpec(name="skill_browse", description="", parameters_schema={}),
        ToolSpec(name="skill_git__commit", description="", parameters_schema={}),
    ]
    injected = (
        ToolSpec(name="skill_frontend__ui-review", description="", parameters_schema={}),
        ToolSpec(name="skill_git__commit", description="", parameters_schema={}),
    )

    merged = merge_skill_specs(current, injected)

    assert merged is not None
    assert [s.name for s in merged] == [
        "bash",
        "skill_frontend__ui-review",
        "skill_browse",
        "skill_git__commit",
    ]


def _json_payload(block: str) -> dict:
    start = block.index("<skill-discovery-json>") + len("<skill-discovery-json>")
    end = block.index("</skill-discovery-json>")
    return json.loads(block[start:end].strip())
