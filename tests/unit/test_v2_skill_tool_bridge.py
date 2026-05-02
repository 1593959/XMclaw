"""B-124 — SkillToolProvider unit tests.

Pins:
  * registered Skill HEAD entries surface as tools named
    ``skill_<id>`` (with ``.`` → ``__`` namespace mapping)
  * tool invocation routes back to ``skill.run(SkillInput(args=...))``
  * ok=False from a skill becomes ToolResult(ok=False, error=<msg>)
  * skill exceptions become ToolResult(ok=False, error=<repr>) — never
    propagate out of invoke()
  * skill list is dynamic — promote/rollback after construction is
    reflected on the next list_tools() / invoke() call
  * CompositeToolProvider routes correctly through the bridge, including
    the new dynamic-discovery fallback on a registered-after-wrap skill
"""
from __future__ import annotations

import pytest

from xmclaw.core.ir import ToolCall
from xmclaw.providers.tool.composite import CompositeToolProvider
from xmclaw.skills.base import Skill, SkillInput, SkillOutput
from xmclaw.skills.manifest import SkillManifest
from xmclaw.skills.registry import SkillRegistry
from xmclaw.skills.tool_bridge import SkillToolProvider, _to_tool_name


# ── fixtures ────────────────────────────────────────────────────────


class _EchoSkill(Skill):
    """Skill that returns whatever args it received — easy to assert."""

    def __init__(self, skill_id: str, version: int = 1, *,
                 fail: bool = False) -> None:
        self.id = skill_id
        self.version = version
        self._fail = fail

    async def run(self, inp: SkillInput) -> SkillOutput:
        if self._fail:
            return SkillOutput(
                ok=False,
                result={"error": "intentional failure", "args": inp.args},
                side_effects=[],
            )
        return SkillOutput(
            ok=True,
            result={"echoed": inp.args, "version": self.version},
            side_effects=["/tmp/test"],
        )


class _BoomSkill(Skill):
    id = "demo.boom"
    version = 1

    async def run(self, inp: SkillInput) -> SkillOutput:  # noqa: ARG002
        raise RuntimeError("kaboom")


def _manifest(id_: str, v: int) -> SkillManifest:
    return SkillManifest(id=id_, version=v)


def _registry_with(*skills_and_manifests) -> SkillRegistry:
    reg = SkillRegistry()
    for skill, manifest in skills_and_manifests:
        reg.register(skill, manifest, set_head=True)
    return reg


# ── name encoding ───────────────────────────────────────────────────


def test_tool_name_replaces_dots() -> None:
    assert _to_tool_name("demo.read_and_summarize") == \
        "skill_demo__read_and_summarize"


def test_tool_name_strips_invalid_chars() -> None:
    # `+` and `@` are not in [a-zA-Z0-9_-]; they get squashed to `_`.
    assert _to_tool_name("foo+bar@baz") == "skill_foo_bar_baz"


# ── list_tools ──────────────────────────────────────────────────────


def test_list_tools_exposes_registered_skills() -> None:
    reg = _registry_with(
        (_EchoSkill("demo.echo"), _manifest("demo.echo", 1)),
        (_EchoSkill("simple", 2), _manifest("simple", 2)),
    )
    bridge = SkillToolProvider(reg)
    names = sorted(s.name for s in bridge.list_tools())
    assert names == ["skill_demo__echo", "skill_simple"]


def test_list_tools_description_carries_provenance() -> None:
    reg = SkillRegistry()
    reg.register(
        _EchoSkill("x"),
        SkillManifest(id="x", version=1, created_by="evolved",
                      evidence=("bench:1.12x",)),
    )
    bridge = SkillToolProvider(reg)
    spec = bridge.list_tools()[0]
    assert "x v1" in spec.description
    assert "created_by=evolved" in spec.description
    assert "bench:1.12x" in spec.description


def test_list_tools_is_dynamic_after_promotion() -> None:
    """A skill registered AFTER the bridge construction must show up
    on the next list_tools() call — no restart required."""
    reg = SkillRegistry()
    bridge = SkillToolProvider(reg)
    assert bridge.list_tools() == []

    reg.register(_EchoSkill("late"), _manifest("late", 1))
    names = [s.name for s in bridge.list_tools()]
    assert names == ["skill_late"]


# ── invoke ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_invoke_runs_skill_and_returns_result() -> None:
    reg = _registry_with(
        (_EchoSkill("demo.echo"), _manifest("demo.echo", 1)),
    )
    bridge = SkillToolProvider(reg)
    call = ToolCall(
        name="skill_demo__echo",
        args={"hello": "world"},
        provenance="synthetic",
    )
    result = await bridge.invoke(call)
    assert result.ok is True
    assert result.content == {"echoed": {"hello": "world"}, "version": 1}
    assert result.side_effects == ("/tmp/test",)
    assert result.call_id == call.id


@pytest.mark.asyncio
async def test_invoke_unknown_tool_returns_structured_error() -> None:
    bridge = SkillToolProvider(SkillRegistry())
    result = await bridge.invoke(ToolCall(
        name="skill_does_not_exist",
        args={}, provenance="synthetic",
    ))
    assert result.ok is False
    assert "unknown skill tool" in (result.error or "")


@pytest.mark.asyncio
async def test_invoke_skill_returning_ok_false_surfaces_error() -> None:
    reg = _registry_with(
        (_EchoSkill("flaky", fail=True), _manifest("flaky", 1)),
    )
    bridge = SkillToolProvider(reg)
    result = await bridge.invoke(ToolCall(
        name="skill_flaky", args={"x": 1}, provenance="synthetic",
    ))
    assert result.ok is False
    assert result.error == "intentional failure"


@pytest.mark.asyncio
async def test_invoke_skill_exception_does_not_propagate() -> None:
    """A skill that raises must NOT crash the agent loop — it surfaces
    as ok=False with a diagnostic error string."""
    reg = _registry_with(
        (_BoomSkill(), _manifest("demo.boom", 1)),
    )
    bridge = SkillToolProvider(reg)
    result = await bridge.invoke(ToolCall(
        name="skill_demo__boom", args={}, provenance="synthetic",
    ))
    assert result.ok is False
    assert "RuntimeError" in (result.error or "")
    assert "kaboom" in (result.error or "")


# ── composite + dynamic discovery ──────────────────────────────────


class _StubBuiltin:
    """Minimal stub that gives CompositeToolProvider one stable child."""

    def list_tools(self) -> list:
        from xmclaw.core.ir import ToolSpec
        return [ToolSpec(
            name="builtin_noop", description="noop",
            parameters_schema={"type": "object"},
        )]

    async def invoke(self, call: ToolCall):
        from xmclaw.core.ir import ToolResult
        return ToolResult(
            call_id=call.id, ok=True, content="builtin",
        )


@pytest.mark.asyncio
async def test_composite_routes_to_skill_tool_provider() -> None:
    reg = _registry_with(
        (_EchoSkill("a"), _manifest("a", 1)),
    )
    composite = CompositeToolProvider(_StubBuiltin(), SkillToolProvider(reg))
    names = {s.name for s in composite.list_tools()}
    assert "builtin_noop" in names
    assert "skill_a" in names

    # Invoke the skill via the composite — proves end-to-end routing.
    result = await composite.invoke(ToolCall(
        name="skill_a", args={"k": "v"}, provenance="synthetic",
    ))
    assert result.ok is True
    assert result.content == {"echoed": {"k": "v"}, "version": 1}


@pytest.mark.asyncio
async def test_composite_dynamic_discovery_for_late_registered_skill() -> None:
    """B-124 router fallback: a skill registered AFTER CompositeToolProvider
    construction must still be invokable. Without the fallback,
    composite._router would be stale and invoke() would 'unknown tool'."""
    reg = SkillRegistry()
    composite = CompositeToolProvider(SkillToolProvider(reg))

    # Register AFTER the composite is built.
    reg.register(_EchoSkill("late"), _manifest("late", 1))

    # list_tools sees it (no caching there).
    assert any(
        s.name == "skill_late" for s in composite.list_tools()
    )
    # invoke must also work — the static router missed but the
    # B-124 fallback rescans children.
    result = await composite.invoke(ToolCall(
        name="skill_late", args={"x": 9}, provenance="synthetic",
    ))
    assert result.ok is True
    assert result.content == {"echoed": {"x": 9}, "version": 1}


# ── B-176: tool description carries manifest body ─────────────────


def test_list_tools_description_includes_manifest_body() -> None:
    """B-176: pre-fix the LLM saw only "Skill: id v1 (created_by=user)".
    Post-fix the rich frontmatter description + triggers come through
    so the model can actually decide whether to pick the skill."""
    reg = SkillRegistry()
    reg.register(
        _EchoSkill("git-commit"),
        SkillManifest(
            id="git-commit", version=1, created_by="user",
            title="Git Commit Workflow",
            description=(
                "Execute git commit with conventional commit message "
                "analysis. Use when the user asks to commit changes."
            ),
            triggers=("/commit", "git commit", "提交"),
        ),
    )
    bridge = SkillToolProvider(reg)
    specs = bridge.list_tools()
    assert len(specs) == 1
    desc = specs[0].description
    # Body description shows up.
    assert "conventional commit message analysis" in desc
    # Triggers shown so the LLM can keyword-match.
    assert "/commit" in desc and "提交" in desc
    # Title surfaces too.
    assert "Git Commit Workflow" in desc
    # Provenance still present (audit trail) but not the headline.
    assert "created_by=user" in desc


def test_list_tools_description_minimal_when_no_frontmatter() -> None:
    """A skill with empty manifest description still gets a valid
    (terse) tool spec — just the id + version + provenance, same
    pre-B-176 behaviour as the floor."""
    reg = SkillRegistry()
    reg.register(
        _EchoSkill("bare"),
        SkillManifest(id="bare", version=1, created_by="evolved"),
    )
    bridge = SkillToolProvider(reg)
    specs = bridge.list_tools()
    assert len(specs) == 1
    desc = specs[0].description
    assert "bare" in desc and "v1" in desc
    assert "created_by=evolved" in desc
