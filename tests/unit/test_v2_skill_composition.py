"""Tests for skill composition (Wave-33):
- SkillContext injection
- skill_compose meta-tool
- StructuredProcedureSkill (JSON steps extraction)
"""
from __future__ import annotations

import pytest

from xmclaw.core.ir import ToolCall, ToolResult
from xmclaw.skills.base import Skill, SkillContext, SkillInput, SkillOutput
from xmclaw.skills.manifest import SkillManifest
from xmclaw.skills.markdown_skill import MarkdownProcedureSkill, _extract_structured_steps
from xmclaw.skills.registry import SkillRef, SkillRegistry
from xmclaw.skills.tool_bridge import (
    META_COMPOSE_TOOL_NAME,
    SkillToolProvider,
)


# ── Fake registry fixtures ───────────────────────────────────────

class FakeManifest:
    def __init__(self, skill_id, version=1, title="", description="", trust_level=""):
        self.id = skill_id
        self.version = version
        self.title = title
        self.description = description
        self.trust_level = trust_level
        self.permissions_fs = ()
        self.permissions_net = ()


class FakeSkill(Skill):
    def __init__(self, skill_id, version=1):
        self.id = skill_id
        self.version = version
        self.ran_with_ctx = False

    async def run(self, inp: SkillInput, ctx=None) -> SkillOutput:
        if ctx is not None:
            self.ran_with_ctx = True
        return SkillOutput(
            ok=True,
            result={"echo": inp.args, "skill_id": self.id},
            side_effects=[],
        )


class FailingSkill(Skill):
    def __init__(self, skill_id, version=1):
        self.id = skill_id
        self.version = version

    async def run(self, inp: SkillInput, ctx=None) -> SkillOutput:
        return SkillOutput(
            ok=False,
            result={"error": "intentional failure"},
            side_effects=[],
        )


class OldStyleSkill(Skill):
    """Skill that does NOT accept ctx — tests backward compatibility."""
    def __init__(self, skill_id, version=1):
        self.id = skill_id
        self.version = version

    async def run(self, inp: SkillInput) -> SkillOutput:
        return SkillOutput(
            ok=True,
            result={"legacy": True},
            side_effects=[],
        )


class PipeSkill(Skill):
    """Reads _prev_result from args."""
    def __init__(self, skill_id, version=1):
        self.id = skill_id
        self.version = version

    async def run(self, inp: SkillInput, ctx=None) -> SkillOutput:
        prev = inp.args.get("_prev_result")
        return SkillOutput(
            ok=True,
            result={"received_prev": prev},
            side_effects=[],
        )


@pytest.fixture
def registry():
    reg = SkillRegistry()
    reg.register(
        FakeSkill("echo", version=1),
        manifest=SkillManifest(id="echo", version=1, title="Echo", description="echoes args"),
    )
    reg.register(
        OldStyleSkill("legacy", version=1),
        manifest=SkillManifest(id="legacy", version=1, title="Legacy", description="no ctx"),
    )
    reg.register(
        PipeSkill("pipe", version=1),
        manifest=SkillManifest(id="pipe", version=1, title="Pipe", description="pipes prev"),
    )
    reg.register(
        FailingSkill("fail", version=1),
        manifest=SkillManifest(id="fail", version=1, title="Fail", description="always fails"),
    )
    return reg


@pytest.fixture
def provider(registry):
    return SkillToolProvider(registry, disclosure_mode="inline")


# ── SkillContext tests ───────────────────────────────────────────

def test_skill_context_list_skills(registry):
    ctx = SkillContext(_registry=registry)
    skills = ctx.list_skills()
    assert len(skills) == 4
    ids = {s["id"] for s in skills}
    assert ids == {"echo", "legacy", "pipe", "fail"}


def test_skill_context_list_skills_filtered(registry):
    ctx = SkillContext(_registry=registry)
    skills = ctx.list_skills(query="echo", top_k=2)
    assert len(skills) == 1
    assert skills[0]["id"] == "echo"


def test_skill_context_get_skill_info(registry):
    ctx = SkillContext(_registry=registry)
    info = ctx.get_skill_info("echo")
    assert info is not None
    assert info["id"] == "echo"
    assert info["title"] == "Echo"


def test_skill_context_get_skill_info_missing(registry):
    ctx = SkillContext(_registry=registry)
    assert ctx.get_skill_info("nonexistent") is None


def test_skill_context_active_version(registry):
    ctx = SkillContext(_registry=registry)
    assert ctx.active_version("echo") == 1


# ── skill_compose tests ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_compose_two_skills(provider):
    call = ToolCall(
        id="c1",
        name=META_COMPOSE_TOOL_NAME,
        args={
            "workflow": [
                {"skill_id": "echo", "args": {"msg": "hello"}},
                {"skill_id": "pipe", "args": {}},
            ],
        },
        provenance="test",
    )
    result = await provider.invoke(call)
    assert result.ok is True
    trace = result.content["trace"]
    assert len(trace) == 2
    assert trace[0]["skill_id"] == "echo"
    assert trace[1]["skill_id"] == "pipe"
    # Pipe received prev result
    assert trace[1]["result"]["received_prev"]["echo"]["msg"] == "hello"


@pytest.mark.asyncio
async def test_compose_empty_workflow(provider):
    call = ToolCall(
        id="c2", name=META_COMPOSE_TOOL_NAME, args={"workflow": []},
        provenance="test",
    )
    result = await provider.invoke(call)
    assert result.ok is False
    assert "non-empty" in result.error.lower()


@pytest.mark.asyncio
async def test_compose_missing_skill_id(provider):
    call = ToolCall(
        id="c3",
        name=META_COMPOSE_TOOL_NAME,
        args={"workflow": [{"args": {}}]},
        provenance="test",
    )
    result = await provider.invoke(call)
    assert result.ok is False
    assert "skill_id" in result.error.lower()


@pytest.mark.asyncio
async def test_compose_fails_midway(provider):
    call = ToolCall(
        id="c4",
        name=META_COMPOSE_TOOL_NAME,
        args={
            "workflow": [
                {"skill_id": "echo", "args": {}},
                {"skill_id": "fail", "args": {}},
                {"skill_id": "pipe", "args": {}},
            ],
        },
        provenance="test",
    )
    result = await provider.invoke(call)
    assert result.ok is False
    trace = result.content["trace"]
    assert len(trace) == 2  # echo + fail
    assert trace[1]["skill_id"] == "fail"


@pytest.mark.asyncio
async def test_compose_with_legacy_skill(provider):
    call = ToolCall(
        id="c5",
        name=META_COMPOSE_TOOL_NAME,
        args={
            "workflow": [
                {"skill_id": "legacy", "args": {}},
            ],
        },
        provenance="test",
    )
    result = await provider.invoke(call)
    assert result.ok is True
    assert result.content["trace"][0]["result"]["legacy"] is True


@pytest.mark.asyncio
async def test_skill_runs_with_ctx_when_accepted(registry, provider):
    skill = registry.get("echo")
    call = ToolCall(id="t1", name="skill_echo", args={"x": 1}, provenance="test")
    await provider.invoke(call)
    assert skill.ran_with_ctx is True


@pytest.mark.asyncio
async def test_skill_runs_without_ctx_when_not_accepted(registry, provider):
    skill = registry.get("legacy")
    call = ToolCall(id="t2", name="skill_legacy", args={}, provenance="test")
    result = await provider.invoke(call)
    assert result.ok is True


# ── Structured Markdown Skill tests ──────────────────────────────

def test_extract_structured_steps_found():
    body = "Some text\n```json\n{\"steps\": [{\"action\": \"bash\", \"args\": {\"cmd\": \"echo hi\"}}]}\n```\nMore text"
    steps = _extract_structured_steps(body)
    assert steps is not None
    assert len(steps) == 1
    assert steps[0]["action"] == "bash"


def test_extract_structured_steps_no_block():
    body = "# Title\n\nJust plain markdown."
    assert _extract_structured_steps(body) is None


def test_extract_structured_steps_invalid_json():
    body = "```json\nnot json\n```"
    assert _extract_structured_steps(body) is None


def test_extract_structured_steps_missing_action():
    body = '```json\n{"steps": [{"note": "no action"}]}\n```'
    assert _extract_structured_steps(body) is None


@pytest.mark.asyncio
async def test_markdown_skill_returns_structured():
    body = (
        "---\nname: test\ndescription: test\n---\n"
        "```json\n"
        '{"steps": [{"action": "file_read", "args": {"path": "/etc/hosts"}}]}'
        "\n```"
    )
    skill = MarkdownProcedureSkill(id="test", body=body)
    out = await skill.run(SkillInput(args={}))
    assert out.ok is True
    assert out.result["kind"] == "structured_procedure"
    assert len(out.result["steps"]) == 1
    assert out.result["steps"][0]["action"] == "file_read"


@pytest.mark.asyncio
async def test_markdown_skill_returns_plain_when_no_structured():
    body = "---\nname: test\n---\n# Title\n\nDo something."
    skill = MarkdownProcedureSkill(id="test", body=body)
    out = await skill.run(SkillInput(args={}))
    assert out.ok is True
    assert out.result["kind"] == "markdown_procedure"
    assert "instructions" in out.result
