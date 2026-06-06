"""端到端测试：Skill → Registry → SkillToolProvider → invoke"""
import asyncio
import sys

# 使用项目虚拟环境
sys.path.insert(0, "C:/Users/15978/Desktop/XMclaw")

from xmclaw.skills.base import Skill, SkillInput, SkillOutput
from xmclaw.skills.registry import SkillRegistry
from xmclaw.skills.manifest import SkillManifest
from xmclaw.skills.tool_bridge import SkillToolProvider, META_RUN_TOOL_NAME, META_BROWSE_TOOL_NAME, META_VIEW_TOOL_NAME
from xmclaw.core.ir.toolcall import ToolCall, ToolSpec, ToolResult


class DummySkill(Skill):
    id = "test.dummy"
    version = 1

    async def run(self, inp: SkillInput) -> SkillOutput:
        return SkillOutput(
            ok=True,
            result={"echo": inp.args},
            side_effects=[],
        )


async def main():
    print("=" * 60)
    print("测试 1: 基础注册 + 直接调用 per-skill 工具")
    print("=" * 60)

    registry = SkillRegistry()
    skill = DummySkill()
    manifest = SkillManifest(
        id="test.dummy",
        version=1,
        title="Dummy",
        description="A dummy skill for testing.",
    )
    ref = registry.register(skill, manifest, set_head=True)
    print(f"注册结果: {ref}")
    print(f"list_skill_ids: {registry.list_skill_ids()}")

    provider = SkillToolProvider(registry)
    tools = provider.list_tools()
    print(f"暴露的工具数: {len(tools)}")
    for t in tools:
        print(f"  - {t.name}")

    # 直接调用 skill_test__dummy
    call = ToolCall(
        name="skill_test__dummy",
        args={"foo": "bar"},
        provenance="synthetic",
        id="call-1",
    )
    result = await provider.invoke(call)
    print(f"invoke 结果: ok={result.ok}, content={result.content}")

    print()
    print("=" * 60)
    print("测试 2: skill_run meta-tool 调用路径")
    print("=" * 60)

    call_run = ToolCall(
        name=META_RUN_TOOL_NAME,
        args={"skill_id": "test.dummy", "args": {"key": "value"}},
        provenance="synthetic",
        id="call-2",
    )
    result_run = await provider.invoke(call_run)
    print(f"skill_run 结果: ok={result_run.ok}, content={result_run.content}")

    print()
    print("=" * 60)
    print("测试 3: skill_browse → skill_view 渐进披露")
    print("=" * 60)

    call_browse = ToolCall(
        name=META_BROWSE_TOOL_NAME,
        args={"query": "dummy"},
        provenance="synthetic",
        id="call-3",
    )
    result_browse = await provider.invoke(call_browse)
    print(f"skill_browse 结果: ok={result_browse.ok}, content={result_browse.content}")

    # skill_view 需要实际磁盘上的 skill 目录，这里测试构造签名
    call_view = ToolCall(
        name=META_VIEW_TOOL_NAME,
        args={"skill_id": "test.dummy"},
        provenance="synthetic",
        id="call-4",
    )
    result_view = await provider.invoke(call_view)
    print(f"skill_view 结果: ok={result_view.ok}, error={result_view.error}")

    print()
    print("=" * 60)
    print("测试 4: ToolCall / ToolSpec 构造函数签名匹配检查")
    print("=" * 60)

    # 检查 ToolCall 构造
    try:
        tc = ToolCall(name="x", args={}, provenance="synthetic")
        print(f"ToolCall 构造成功: {tc}")
    except Exception as e:
        print(f"ToolCall 构造失败: {e}")

    # 检查 ToolSpec 构造
    try:
        ts = ToolSpec(name="x", description="d", parameters_schema={"type": "object"})
        print(f"ToolSpec 构造成功: {ts}")
    except Exception as e:
        print(f"ToolSpec 构造失败: {e}")

    # 检查 ToolResult 构造（invoke 返回的）
    try:
        tr = ToolResult(call_id="c1", ok=True, content="hello")
        print(f"ToolResult 构造成功: {tr}")
    except Exception as e:
        print(f"ToolResult 构造失败: {e}")

    print()
    print("=" * 60)
    print("测试 5: 缓存失效问题 — 注册后创建 provider，再注册新 skill")
    print("=" * 60)

    registry2 = SkillRegistry()
    provider2 = SkillToolProvider(registry2)

    # 先创建 provider，再注册 skill
    skill2 = DummySkill()
    registry2.register(skill2, manifest, set_head=True)

    tools2 = provider2.list_tools()
    per_skill_tools = [t for t in tools2 if t.name.startswith("skill_test")]
    print(f"provider 创建后注册的 skill 是否暴露: {len(per_skill_tools)} 个 per-skill 工具")

    # 尝试调用
    call2 = ToolCall(
        name="skill_test__dummy",
        args={},
        provenance="synthetic",
        id="call-5",
    )
    result2 = await provider2.invoke(call2)
    print(f"invoke 结果: ok={result2.ok}, error={result2.error}")

    print()
    print("=" * 60)
    print("测试 6: 检查 _tool_name_cache 缓存失效")
    print("=" * 60)

    # provider2 的缓存可能已经在 list_tools 时构建？不，list_tools 不调用 _tool_name_to_skill_id
    # 但 invoke 会调用
    print(f"provider2._tool_name_cache = {provider2._tool_name_cache}")

    # 现在再注册一个新 skill，看缓存是否更新
    class DummySkill2(Skill):
        id = "test.dummy2"
        version = 1
        async def run(self, inp: SkillInput) -> SkillOutput:
            return SkillOutput(ok=True, result="dummy2", side_effects=[])

    manifest2 = SkillManifest(
        id="test.dummy2",
        version=1,
        title="Dummy2",
        description="Another dummy skill.",
    )
    registry2.register(DummySkill2(), manifest2, set_head=True)

    call3 = ToolCall(
        name="skill_test__dummy2",
        args={},
        provenance="synthetic",
        id="call-6",
    )
    result3 = await provider2.invoke(call3)
    print(f"新 skill 调用结果: ok={result3.ok}, error={result3.error}")
    print(f"调用后 provider2._tool_name_cache = {provider2._tool_name_cache}")


if __name__ == "__main__":
    asyncio.run(main())
