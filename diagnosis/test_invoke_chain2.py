"""深入测试：缓存失效、promote/rollback 后的调用"""
import asyncio
import sys

sys.path.insert(0, "C:/Users/15978/Desktop/XMclaw")

from xmclaw.skills.base import Skill, SkillInput, SkillOutput
from xmclaw.skills.registry import SkillRegistry
from xmclaw.skills.manifest import SkillManifest
from xmclaw.skills.tool_bridge import SkillToolProvider
from xmclaw.core.ir.toolcall import ToolCall


class DummySkill(Skill):
    id = "test.dummy"
    version = 1
    async def run(self, inp: SkillInput) -> SkillOutput:
        return SkillOutput(ok=True, result=f"v1: {inp.args}", side_effects=[])


class DummySkillV2(Skill):
    id = "test.dummy"
    version = 2
    async def run(self, inp: SkillInput) -> SkillOutput:
        return SkillOutput(ok=True, result=f"v2: {inp.args}", side_effects=[])


async def main():
    print("=" * 60)
    print("测试 A: promote 新版本后，旧缓存导致调用失败？")
    print("=" * 60)

    registry = SkillRegistry()
    manifest = SkillManifest(id="test.dummy", version=1, title="Dummy", description="V1")
    manifest2 = SkillManifest(id="test.dummy", version=2, title="Dummy", description="V2")

    registry.register(DummySkill(), manifest, set_head=True)
    provider = SkillToolProvider(registry)

    # 先调用一次，触发缓存构建
    call = ToolCall(name="skill_test__dummy", args={}, provenance="synthetic", id="c1")
    r1 = await provider.invoke(call)
    print(f"第一次调用: ok={r1.ok}, content={r1.content}")
    print(f"缓存状态: {provider._tool_name_cache}")

    # 注册 v2 并 promote
    registry.register(DummySkillV2(), manifest2, set_head=False)
    registry.promote("test.dummy", 2, evidence=["test"])

    print(f"promote 后 list_skill_ids: {registry.list_skill_ids()}")
    print(f"promote 后缓存状态: {provider._tool_name_cache}")

    # 再次调用
    call2 = ToolCall(name="skill_test__dummy", args={}, provenance="synthetic", id="c2")
    r2 = await provider.invoke(call2)
    print(f"promote 后调用: ok={r2.ok}, content={r2.content}")

    print()
    print("=" * 60)
    print("测试 B: rollback 后缓存是否失效？")
    print("=" * 60)

    registry.rollback("test.dummy", 1, reason="test rollback")
    print(f"rollback 后 list_skill_ids: {registry.list_skill_ids()}")
    print(f"rollback 后缓存状态: {provider._tool_name_cache}")

    call3 = ToolCall(name="skill_test__dummy", args={}, provenance="synthetic", id="c3")
    r3 = await provider.invoke(call3)
    print(f"rollback 后调用: ok={r3.ok}, content={r3.content}")

    print()
    print("=" * 60)
    print("测试 C: 新 skill 注册后，list_tools 能看到但 invoke 失败？")
    print("=" * 60)

    class NewSkill(Skill):
        id = "test.newskill"
        version = 1
        async def run(self, inp: SkillInput) -> SkillOutput:
            return SkillOutput(ok=True, result="newskill", side_effects=[])

    new_manifest = SkillManifest(id="test.newskill", version=1, title="New", description="New skill")
    registry.register(NewSkill(), new_manifest, set_head=True)

    tools = provider.list_tools()
    tool_names = [t.name for t in tools]
    print(f"list_tools 包含 test.newskill? {'skill_test__newskill' in tool_names}")
    print(f"缓存状态: {provider._tool_name_cache}")

    call4 = ToolCall(name="skill_test__newskill", args={}, provenance="synthetic", id="c4")
    r4 = await provider.invoke(call4)
    print(f"新 skill 调用: ok={r4.ok}, error={r4.error}")

    print()
    print("=" * 60)
    print("测试 D: skill_run 是否受缓存影响？")
    print("=" * 60)

    from xmclaw.skills.tool_bridge import META_RUN_TOOL_NAME
    call5 = ToolCall(
        name=META_RUN_TOOL_NAME,
        args={"skill_id": "test.newskill", "args": {}},
        provenance="synthetic",
        id="c5",
    )
    r5 = await provider.invoke(call5)
    print(f"skill_run 调用新 skill: ok={r5.ok}, content={r5.content}")


if __name__ == "__main__":
    asyncio.run(main())
