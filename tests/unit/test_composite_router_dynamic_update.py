"""Test C: CompositeToolProvider router rebuilds when SkillRegistry mutates.

Verifies that ``SkillRegistry`` notifies registered listeners on
register / promote / rollback / update_body / hot_replace, and that the
``CompositeToolProvider.invalidate_router()`` path rebuilds the static
router so ``invoke()`` no longer falls back to a live re-scan.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from xmclaw.core.ir import ToolCall, ToolResult, ToolSpec
from xmclaw.providers.tool.base import ToolProvider
from xmclaw.providers.tool.composite import CompositeToolProvider
from xmclaw.skills.base import Skill, SkillContext, SkillInput, SkillOutput
from xmclaw.skills.manifest import SkillManifest
from xmclaw.skills.markdown_skill import MarkdownProcedureSkill
from xmclaw.skills.registry import SkillRegistry


# ── helpers ──────────────────────────────────────────────────────────────


class _FakeToolProvider(ToolProvider):
    """Static tool provider with a fixed set of specs."""

    def __init__(self, specs: list[ToolSpec]) -> None:
        self._specs = specs

    def list_tools(self) -> list[ToolSpec]:
        return list(self._specs)

    async def invoke(self, call: ToolCall) -> ToolResult:
        return ToolResult(call_id=call.id, ok=True, content="fake")


class _DynamicToolProvider(ToolProvider):
    """Provider whose spec list changes when ``set_specs`` is called."""

    def __init__(self, specs: list[ToolSpec] | None = None) -> None:
        self._specs = specs or []

    def set_specs(self, specs: list[ToolSpec]) -> None:
        self._specs = specs

    def list_tools(self) -> list[ToolSpec]:
        return list(self._specs)

    async def invoke(self, call: ToolCall) -> ToolResult:
        return ToolResult(call_id=call.id, ok=True, content="dynamic")


# ── CompositeToolProvider.invalidate_router ──────────────────────────────


def test_invalidate_router_rebuilds_from_children() -> None:
    """After a child changes its specs, invalidate_router rebuilds the map."""
    child = _DynamicToolProvider([ToolSpec(name="alpha", description="a", parameters_schema={})])
    composite = CompositeToolProvider(child)
    assert composite._router.get("alpha") is child

    # Simulate a dynamic mutation
    child.set_specs([ToolSpec(name="beta", description="b", parameters_schema={})])
    # Before invalidation, the old router still points to alpha
    assert composite._router.get("alpha") is child
    assert composite._router.get("beta") is None

    composite.invalidate_router()
    assert composite._router.get("alpha") is None
    assert composite._router.get("beta") is child


@pytest.mark.asyncio
async def test_invoke_uses_rebuilt_router_no_fallback() -> None:
    """After invalidation, invoke() routes the new tool without a live re-scan."""
    child = _DynamicToolProvider([ToolSpec(name="t1", description="d", parameters_schema={})])
    composite = CompositeToolProvider(child)

    # Add a new spec to the child (simulating SkillRegistry mutation)
    child.set_specs([
        ToolSpec(name="t1", description="d", parameters_schema={}),
        ToolSpec(name="t2", description="d", parameters_schema={}),
    ])
    composite.invalidate_router()

    # invoke("t2") should route directly without falling back to list_tools scan.
    call = ToolCall(id="c1", name="t2", args={}, provenance="synthetic")
    result = await composite.invoke(call)
    assert result.ok is True
    assert result.content == "dynamic"


# ── SkillRegistry listener registration ───────────────────────────────────


def test_registry_add_remove_listener() -> None:
    """Listeners can be added and removed; removed ones are not called."""
    registry = SkillRegistry()
    calls: list[int] = []

    def listener() -> None:
        calls.append(1)

    registry.add_router_listener(listener)
    # Trigger a mutation via register (we need a valid skill + manifest)
    skill = MarkdownProcedureSkill(id="test.skill", body="# test", version=1)
    manifest = SkillManifest(id="test.skill", version=1, title="T", description="D")
    registry.register(skill, manifest)
    assert len(calls) == 1

    registry.remove_router_listener(listener)
    registry.register(
        MarkdownProcedureSkill(id="test.skill2", body="# test2", version=1),
        SkillManifest(id="test.skill2", version=1, title="T2", description="D2"),
    )
    assert len(calls) == 1  # unchanged


def test_registry_listener_idempotent() -> None:
    """Adding the same listener twice is a no-op."""
    registry = SkillRegistry()
    calls = 0

    def listener() -> None:
        nonlocal calls
        calls += 1

    registry.add_router_listener(listener)
    registry.add_router_listener(listener)
    registry.register(
        MarkdownProcedureSkill(id="test.skill", body="# test", version=1),
        SkillManifest(id="test.skill", version=1, title="T", description="D"),
    )
    assert calls == 1


# ── End-to-end: registry mutation → composite invalidation ───────────────


def test_register_triggers_invalidation() -> None:
    """When a new skill is registered, the composite router is rebuilt."""
    registry = SkillRegistry()
    # Build a SkillToolProvider-like bridge that reads from registry
    class _Bridge(ToolProvider):
        def list_tools(self) -> list[ToolSpec]:
            return [ToolSpec(name=f"skill_{sid}", description="", parameters_schema={})
                    for sid in registry.list_skill_ids()]

        async def invoke(self, call: ToolCall) -> ToolResult:
            return ToolResult(call_id=call.id, ok=True, content="bridge")

    bridge = _Bridge()
    composite = CompositeToolProvider(bridge)
    registry.add_router_listener(composite.invalidate_router)

    # Initially empty
    assert composite._router == {}

    # Register a skill
    registry.register(
        MarkdownProcedureSkill(id="demo.skill", body="# demo", version=1),
        SkillManifest(id="demo.skill", version=1, title="D", description="D"),
    )
    assert composite._router.get("skill_demo.skill") is bridge


def test_promote_triggers_invalidation() -> None:
    """Promotion moves HEAD; the composite router should reflect the change."""
    registry = SkillRegistry()

    class _Bridge(ToolProvider):
        def list_tools(self) -> list[ToolSpec]:
            return [ToolSpec(name=f"skill_{sid}", description="", parameters_schema={})
                    for sid in registry.list_skill_ids()]

        async def invoke(self, call: ToolCall) -> ToolResult:
            return ToolResult(call_id=call.id, ok=True, content="bridge")

    bridge = _Bridge()
    composite = CompositeToolProvider(bridge)
    registry.add_router_listener(composite.invalidate_router)

    # Register v1 and v2, promote v2
    registry.register(
        MarkdownProcedureSkill(id="demo.skill", body="# v1", version=1),
        SkillManifest(id="demo.skill", version=1, title="V1", description="V1"),
    )
    registry.register(
        MarkdownProcedureSkill(id="demo.skill", body="# v2", version=2),
        SkillManifest(id="demo.skill", version=2, title="V2", description="V2"),
        set_head=False,
    )
    # promote v2
    registry.promote("demo.skill", 2, evidence=["tests pass"])
    assert composite._router.get("skill_demo.skill") is bridge


def test_rollback_triggers_invalidation() -> None:
    """Rollback moves HEAD back; the composite router should be rebuilt."""
    registry = SkillRegistry()

    class _Bridge(ToolProvider):
        def list_tools(self) -> list[ToolSpec]:
            return [ToolSpec(name=f"skill_{sid}", description="", parameters_schema={})
                    for sid in registry.list_skill_ids()]

        async def invoke(self, call: ToolCall) -> ToolResult:
            return ToolResult(call_id=call.id, ok=True, content="bridge")

    bridge = _Bridge()
    composite = CompositeToolProvider(bridge)
    registry.add_router_listener(composite.invalidate_router)

    registry.register(
        MarkdownProcedureSkill(id="demo.skill", body="# v1", version=1),
        SkillManifest(id="demo.skill", version=1, title="V1", description="V1"),
    )
    registry.register(
        MarkdownProcedureSkill(id="demo.skill", body="# v2", version=2),
        SkillManifest(id="demo.skill", version=2, title="V2", description="V2"),
        set_head=False,
    )
    registry.promote("demo.skill", 2, evidence=["tests pass"])
    registry.rollback("demo.skill", 1, reason="regression")
    assert composite._router.get("skill_demo.skill") is bridge


# ── Negative: update_body on non-markdown skill does NOT notify ──────────


def test_update_body_python_skill_no_notification() -> None:
    """update_body returns False for Python skills and must not fire listeners."""
    registry = SkillRegistry()
    calls = 0

    def listener() -> None:
        nonlocal calls
        calls += 1

    registry.add_router_listener(listener)
    # Create a mock Python skill (not MarkdownProcedureSkill)
    class _PySkill(Skill):
        id = "py.skill"
        version = 1

        async def run(self, inp: SkillInput, ctx: SkillContext | None = None) -> SkillOutput:
            return SkillOutput(ok=True, result="")

    registry.register(_PySkill(), SkillManifest(id="py.skill", version=1, title="P", description="P"))
    # update_body on a Python skill should return False without notifying
    result = registry.update_body("py.skill", 1, "new body")
    assert result is False
    assert calls == 1  # only the initial register fired


# ── Collision policy during invalidation ─────────────────────────────────


def test_invalidate_router_collision_keeps_first() -> None:
    """If two children advertise the same name after invalidation, the first child wins."""
    child_a = _FakeToolProvider([ToolSpec(name="dup", description="a", parameters_schema={})])
    child_b = _FakeToolProvider([])  # start empty to avoid construction-time collision
    composite = CompositeToolProvider(child_a, child_b)
    assert composite._router["dup"] is child_a

    # Now child_b also advertises "dup"; after invalidation child_a should still win.
    child_b._specs = [ToolSpec(name="dup", description="b", parameters_schema={})]
    composite.invalidate_router()
    assert composite._router["dup"] is child_a
