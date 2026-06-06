"""XMclaw 技能系统端到端诊断测试脚本。

运行方式:
    .venv/Scripts/python.exe diagnosis/e2e_test_runner.py

输出:
    控制台打印每个测试的完整结果和错误堆栈。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 确保项目根目录在路径中
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# ---------------------------------------------------------------------------
# 导入 XMclaw 技能系统核心组件
# ---------------------------------------------------------------------------
from xmclaw.skills.base import Skill, SkillInput, SkillOutput
from xmclaw.skills.registry import SkillRegistry
from xmclaw.skills.user_loader import UserSkillsLoader
from xmclaw.skills.tool_bridge import SkillToolProvider, META_RUN_TOOL_NAME, META_BROWSE_TOOL_NAME, META_VIEW_TOOL_NAME
from xmclaw.skills.marketplace import install_from_source, remove, _read_installed_registry
from xmclaw.skills.manifest import SkillManifest
from xmclaw.core.ir.toolcall import ToolCall, ToolResult

# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def banner(title: str) -> str:
    return f"\n{'='*60}\n{title}\n{'='*60}"


def section(title: str) -> str:
    return f"\n{'-'*60}\n{title}\n{'-'*60}"


# 全局结果收集器
results: list[dict[str, Any]] = []


def record(test_name: str, passed: bool, code: str, output: str, error: str = "") -> None:
    results.append({
        "test_name": test_name,
        "passed": passed,
        "code": code,
        "output": output,
        "error": error,
    })


# ---------------------------------------------------------------------------
# 测试 1：Python 技能完整生命周期
# ---------------------------------------------------------------------------

TEST1_CODE = '''
# 1. 写一个临时 skill.py（Skill 子类，零参 __init__）
# 2. 用 UserSkillsLoader 加载到 SkillRegistry
# 3. 用 SkillToolProvider 暴露为工具
# 4. 调用 list_tools() 确认技能出现
# 5. 构造 ToolCall 调用 invoke()
# 6. 检查返回结果
'''


def run_test1() -> tuple[bool, str, str]:
    out_lines: list[str] = []
    err_lines: list[str] = []
    tmpdir = Path(tempfile.mkdtemp(prefix="xmclaw_e2e_test1_"))
    try:
        out_lines.append(f"临时技能根目录: {tmpdir}")

        # 1. 写 skill.py —— 必须放在 <skills_root>/<skill_id>/skill.py
        skill_dir = tmpdir / "test.hello"
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_py = skill_dir / "skill.py"
        skill_py.write_text(
            '''
from xmclaw.skills.base import Skill, SkillInput, SkillOutput

class HelloSkill(Skill):
    id = "test.hello"
    version = 1

    async def run(self, inp: SkillInput) -> SkillOutput:
        name = inp.args.get("name", "world")
        return SkillOutput(
            ok=True,
            result=f"Hello, {name}!",
            side_effects=[],
        )
''',
            encoding="utf-8",
        )

        # 2. 用 UserSkillsLoader 加载
        registry = SkillRegistry()
        loader = UserSkillsLoader(registry, skills_root=tmpdir)
        load_results = loader.load_all()
        out_lines.append(f"load_all 结果: {load_results}")
        if not load_results or not load_results[0].ok:
            err_lines.append("UserSkillsLoader 加载失败")
            return False, "\n".join(out_lines), "\n".join(err_lines)

        # 3. 用 SkillToolProvider 暴露
        provider = SkillToolProvider(registry, disclosure_mode="inline")

        # 4. 调用 list_tools() 确认技能出现
        tools = provider.list_tools()
        tool_names = [t.name for t in tools]
        out_lines.append(f"list_tools 返回 {len(tools)} 个工具")
        out_lines.append(f"工具名列表: {tool_names}")
        expected_name = "skill_test__hello"
        if expected_name not in tool_names:
            err_lines.append(f"期望工具 {expected_name} 未在 list_tools 中出现")
            return False, "\n".join(out_lines), "\n".join(err_lines)

        # 5. 构造 ToolCall 调用 invoke()
        call = ToolCall(
            name=expected_name,
            args={"name": "XMclaw"},
            provenance="synthetic",
        )
        result = asyncio.run(provider.invoke(call))

        # 6. 检查返回结果
        out_lines.append(f"invoke 返回: ok={result.ok}, content={result.content}")
        if not result.ok:
            err_lines.append(f"invoke 返回 ok=False, error={result.error}")
            return False, "\n".join(out_lines), "\n".join(err_lines)
        if "Hello, XMclaw!" not in str(result.content):
            err_lines.append(f"返回内容不包含预期字符串: {result.content}")
            return False, "\n".join(out_lines), "\n".join(err_lines)

        return True, "\n".join(out_lines), ""
    except Exception as exc:
        err_lines.append(traceback.format_exc())
        return False, "\n".join(out_lines), "\n".join(err_lines)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 测试 2：SKILL.md 技能完整生命周期
# ---------------------------------------------------------------------------

TEST2_CODE = '''
# 1. 写一个临时 SKILL.md（带 YAML frontmatter）
# 2. 用 UserSkillsLoader 加载
# 3. 用 SkillToolProvider 暴露为工具
# 4. 调用 invoke() 执行
# 5. 检查返回结果
'''


def run_test2() -> tuple[bool, str, str]:
    out_lines: list[str] = []
    err_lines: list[str] = []
    tmpdir = Path(tempfile.mkdtemp(prefix="xmclaw_e2e_test2_"))
    try:
        out_lines.append(f"临时技能根目录: {tmpdir}")

        # 1. 写 SKILL.md —— 必须放在 <skills_root>/<skill_id>/SKILL.md
        skill_dir = tmpdir / "test.markdown"
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text(
            '''---
name: Test Markdown Skill
description: A simple markdown procedure skill for e2e testing.
---

# Test Markdown Skill

This skill simply returns its instructions for the agent to follow.

## Steps
1. Acknowledge the user.
2. Confirm the skill loaded correctly.
''',
            encoding="utf-8",
        )

        # 2. 用 UserSkillsLoader 加载
        registry = SkillRegistry()
        loader = UserSkillsLoader(registry, skills_root=tmpdir)
        load_results = loader.load_all()
        out_lines.append(f"load_all 结果: {load_results}")
        if not load_results or not load_results[0].ok:
            err_lines.append("UserSkillsLoader 加载失败")
            return False, "\n".join(out_lines), "\n".join(err_lines)

        # 3. 用 SkillToolProvider 暴露
        provider = SkillToolProvider(registry, disclosure_mode="inline")

        # 4. 调用 list_tools() 确认技能出现
        tools = provider.list_tools()
        tool_names = [t.name for t in tools]
        out_lines.append(f"list_tools 返回 {len(tools)} 个工具")
        out_lines.append(f"工具名列表: {tool_names}")
        expected_name = "skill_test__markdown"
        if expected_name not in tool_names:
            err_lines.append(f"期望工具 {expected_name} 未在 list_tools 中出现")
            return False, "\n".join(out_lines), "\n".join(err_lines)

        # 5. 构造 ToolCall 调用 invoke()
        call = ToolCall(
            name=expected_name,
            args={},
            provenance="synthetic",
        )
        result = asyncio.run(provider.invoke(call))

        # 6. 检查返回结果
        out_lines.append(f"invoke 返回: ok={result.ok}, content={result.content}")
        if not result.ok:
            err_lines.append(f"invoke 返回 ok=False, error={result.error}")
            return False, "\n".join(out_lines), "\n".join(err_lines)
        content_str = str(result.content)
        if "markdown_procedure" not in content_str:
            err_lines.append(f"返回内容不包含 'markdown_procedure': {content_str[:500]}")
            return False, "\n".join(out_lines), "\n".join(err_lines)

        return True, "\n".join(out_lines), ""
    except Exception as exc:
        err_lines.append(traceback.format_exc())
        return False, "\n".join(out_lines), "\n".join(err_lines)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 测试 3：技能安装流程
# ---------------------------------------------------------------------------

TEST3_CODE = '''
# 1. 构造一个临时技能目录（模拟 git clone 结果）
# 2. 调用 marketplace.install_from_source（本地路径）
# 3. 检查是否安装到 ~/.xmclaw/skills_user/
# 4. 检查安装后是否能被 UserSkillsLoader 加载
'''


def run_test3() -> tuple[bool, str, str]:
    out_lines: list[str] = []
    err_lines: list[str] = []
    source_dir = Path(tempfile.mkdtemp(prefix="xmclaw_e2e_test3_src_"))
    # 使用独立的 install_root，避免污染真实用户目录
    install_root = Path(tempfile.mkdtemp(prefix="xmclaw_e2e_test3_install_"))
    try:
        out_lines.append(f"模拟源目录: {source_dir}")
        out_lines.append(f"安装根目录: {install_root}")

        # 1. 构造临时技能目录
        (source_dir / "skill.py").write_text(
            '''
from xmclaw.skills.base import Skill, SkillInput, SkillOutput

class InstallTestSkill(Skill):
    id = "install_test"
    version = 1

    async def run(self, inp: SkillInput) -> SkillOutput:
        return SkillOutput(ok=True, result="installed skill works", side_effects=[])
''',
            encoding="utf-8",
        )
        (source_dir / "manifest.json").write_text(
            json.dumps({
                "id": "install_test",
                "version": 1,
                "title": "Install Test",
                "description": "Testing install_from_source",
            }),
            encoding="utf-8",
        )

        # 2. 调用 install_from_source
        from xmclaw.skills.marketplace import install_from_source
        result = install_from_source(
            source=str(source_dir),
            skill_id="install_test",
            install_root=install_root,
        )
        out_lines.append(f"install_from_source 返回: {result}")

        # 3. 检查是否安装到目标目录
        installed_path = install_root / "install_test"
        out_lines.append(f"检查安装路径: {installed_path}")
        if not installed_path.is_dir():
            err_lines.append(f"安装后目录不存在: {installed_path}")
            return False, "\n".join(out_lines), "\n".join(err_lines)
        if not (installed_path / "skill.py").is_file():
            err_lines.append("安装后 skill.py 不存在")
            return False, "\n".join(out_lines), "\n".join(err_lines)

        # 4. 检查安装后是否能被 UserSkillsLoader 加载
        registry = SkillRegistry()
        loader = UserSkillsLoader(registry, skills_root=install_root)
        load_results = loader.load_all()
        out_lines.append(f"UserSkillsLoader 加载结果: {load_results}")
        ok_results = [r for r in load_results if r.ok]
        if not ok_results:
            err_lines.append("UserSkillsLoader 未能成功加载已安装技能")
            return False, "\n".join(out_lines), "\n".join(err_lines)
        skill_ids = [r.skill_id for r in ok_results]
        if "install_test" not in skill_ids:
            err_lines.append(f"加载的技能 ID 列表中缺少 install_test: {skill_ids}")
            return False, "\n".join(out_lines), "\n".join(err_lines)

        return True, "\n".join(out_lines), ""
    except Exception as exc:
        err_lines.append(traceback.format_exc())
        return False, "\n".join(out_lines), "\n".join(err_lines)
    finally:
        shutil.rmtree(source_dir, ignore_errors=True)
        shutil.rmtree(install_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# 测试 4：版本控制流程
# ---------------------------------------------------------------------------

TEST4_CODE = '''
# 1. 注册技能 v1
# 2. 注册技能 v2
# 3. promote v2
# 4. 检查 HEAD 是否切换
# 5. rollback 到 v1
# 6. 检查 HEAD 是否恢复
'''


def run_test4() -> tuple[bool, str, str]:
    out_lines: list[str] = []
    err_lines: list[str] = []
    try:
        registry = SkillRegistry()

        # 1. 注册技能 v1
        class SkillV1(Skill):
            id = "versioned_skill"
            version = 1

            async def run(self, inp: SkillInput) -> SkillOutput:
                return SkillOutput(ok=True, result="v1", side_effects=[])

        manifest_v1 = SkillManifest(id="versioned_skill", version=1)
        registry.register(SkillV1(), manifest_v1)
        out_lines.append("注册 v1 完成")
        out_lines.append(f"HEAD = {registry.active_version('versioned_skill')}")

        # 2. 注册技能 v2
        class SkillV2(Skill):
            id = "versioned_skill"
            version = 2

            async def run(self, inp: SkillInput) -> SkillOutput:
                return SkillOutput(ok=True, result="v2", side_effects=[])

        manifest_v2 = SkillManifest(id="versioned_skill", version=2)
        registry.register(SkillV2(), manifest_v2, set_head=False)
        out_lines.append("注册 v2 完成 (set_head=False)")
        out_lines.append(f"HEAD = {registry.active_version('versioned_skill')}")

        # 3. promote v2
        registry.promote("versioned_skill", 2, evidence=["grader approved v2"])
        out_lines.append("promote v2 完成")

        # 4. 检查 HEAD 是否切换
        head = registry.active_version("versioned_skill")
        out_lines.append(f"promote 后 HEAD = {head}")
        if head != 2:
            err_lines.append(f"promote 后 HEAD 应为 2，实际为 {head}")
            return False, "\n".join(out_lines), "\n".join(err_lines)

        # 5. rollback 到 v1
        registry.rollback("versioned_skill", 1, reason="v2 caused regression in staging")
        out_lines.append("rollback 到 v1 完成")

        # 6. 检查 HEAD 是否恢复
        head = registry.active_version("versioned_skill")
        out_lines.append(f"rollback 后 HEAD = {head}")
        if head != 1:
            err_lines.append(f"rollback 后 HEAD 应为 1，实际为 {head}")
            return False, "\n".join(out_lines), "\n".join(err_lines)

        # 额外：检查历史记录
        history = registry.history("versioned_skill")
        out_lines.append(f"历史记录条目数: {len(history)}")
        for h in history:
            out_lines.append(f"  {h.kind}: v{h.from_version} -> v{h.to_version}")

        return True, "\n".join(out_lines), ""
    except Exception as exc:
        err_lines.append(traceback.format_exc())
        return False, "\n".join(out_lines), "\n".join(err_lines)


# ---------------------------------------------------------------------------
# 测试 5：meta-tool 调用
# ---------------------------------------------------------------------------

TEST5_CODE = '''
# 1. 注册一个技能
# 2. 调用 skill_browse
# 3. 调用 skill_view
# 4. 调用 skill_run
# 5. 检查每一步是否成功
'''


def run_test5() -> tuple[bool, str, str]:
    out_lines: list[str] = []
    err_lines: list[str] = []
    tmpdir = Path(tempfile.mkdtemp(prefix="xmclaw_e2e_test5_"))
    try:
        # 1. 注册一个技能
        skill_dir = tmpdir / "meta.test"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "skill.py").write_text(
            '''
from xmclaw.skills.base import Skill, SkillInput, SkillOutput

class MetaTestSkill(Skill):
    id = "meta.test"
    version = 1

    async def run(self, inp: SkillInput) -> SkillOutput:
        action = inp.args.get("action", "default")
        return SkillOutput(ok=True, result=f"meta-test action={action}", side_effects=[])
''',
            encoding="utf-8",
        )

        registry = SkillRegistry()
        loader = UserSkillsLoader(registry, skills_root=tmpdir)
        loader.load_all()
        out_lines.append("技能注册完成")

        provider = SkillToolProvider(registry, disclosure_mode="inline")

        # 2. 调用 skill_browse
        browse_call = ToolCall(
            name=META_BROWSE_TOOL_NAME,
            args={"query": "meta test"},
            provenance="synthetic",
        )
        browse_result = asyncio.run(provider.invoke(browse_call))
        out_lines.append(f"skill_browse: ok={browse_result.ok}, content={browse_result.content}")
        if not browse_result.ok:
            err_lines.append(f"skill_browse 失败: {browse_result.error}")
            return False, "\n".join(out_lines), "\n".join(err_lines)

        # 3. 调用 skill_view —— 需要 patch resolve_skill_roots 指向临时目录
        from unittest.mock import patch
        from xmclaw.skills import user_loader as _user_loader_mod

        def fake_resolve():
            return (tmpdir, [])

        with patch.object(_user_loader_mod, "resolve_skill_roots", fake_resolve):
            view_call = ToolCall(
                name=META_VIEW_TOOL_NAME,
                args={"skill_id": "meta.test"},
                provenance="synthetic",
            )
            view_result = asyncio.run(provider.invoke(view_call))
            out_lines.append(f"skill_view: ok={view_result.ok}, content={view_result.content}")
            if not view_result.ok:
                err_lines.append(f"skill_view 失败: {view_result.error}")
                return False, "\n".join(out_lines), "\n".join(err_lines)

        # 4. 调用 skill_run
        run_call = ToolCall(
            name=META_RUN_TOOL_NAME,
            args={"skill_id": "meta.test", "args": {"action": "ping"}},
            provenance="synthetic",
        )
        run_result = asyncio.run(provider.invoke(run_call))
        out_lines.append(f"skill_run: ok={run_result.ok}, content={run_result.content}")
        if not run_result.ok:
            err_lines.append(f"skill_run 失败: {run_result.error}")
            return False, "\n".join(out_lines), "\n".join(err_lines)
        if "meta-test action=ping" not in str(run_result.content):
            err_lines.append(f"skill_run 返回内容不符合预期: {run_result.content}")
            return False, "\n".join(out_lines), "\n".join(err_lines)

        return True, "\n".join(out_lines), ""
    except Exception as exc:
        err_lines.append(traceback.format_exc())
        return False, "\n".join(out_lines), "\n".join(err_lines)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 主执行逻辑
# ---------------------------------------------------------------------------

def main() -> None:
    print(banner("XMclaw 技能系统端到端诊断测试"))
    print(f"Python: {sys.version}")
    print(f"项目根目录: {PROJECT_ROOT}")

    tests = [
        ("测试 1：Python 技能完整生命周期", TEST1_CODE, run_test1),
        ("测试 2：SKILL.md 技能完整生命周期", TEST2_CODE, run_test2),
        ("测试 3：技能安装流程", TEST3_CODE, run_test3),
        ("测试 4：版本控制流程", TEST4_CODE, run_test4),
        ("测试 5：meta-tool 调用", TEST5_CODE, run_test5),
    ]

    for name, code, runner in tests:
        print(section(name))
        passed, output, error = runner()
        record(name, passed, code, output, error)
        status = "[PASS]" if passed else "[FAIL]"
        print(f"\n结果: {status}")
        print(f"\n输出:\n{output}")
        if error:
            print(f"\n错误:\n{error}")

    # 汇总
    print(banner("测试汇总"))
    total = len(results)
    passed = sum(1 for r in results if r["passed"])
    print(f"总计: {total} | 通过: {passed} | 失败: {total - passed}")
    for r in results:
        status = "[PASS]" if r["passed"] else "[FAIL]"
        print(f"  {status} {r['test_name']}")

    # 生成 Markdown 报告
    generate_report()
    print(f"\n报告已保存到: {PROJECT_ROOT / 'diagnosis' / 'e2e_test.md'}")


def generate_report() -> None:
    lines: list[str] = []
    lines.append("# XMclaw 技能系统端到端诊断报告")
    lines.append("")
    lines.append(f"**生成时间**: 2026-06-06")
    lines.append(f"**Python 版本**: {sys.version}")
    lines.append("")

    total = len(results)
    passed = sum(1 for r in results if r["passed"])
    lines.append(f"## 汇总")
    lines.append("")
    lines.append(f"- 总计: {total}")
    lines.append(f"- 通过: {passed}")
    lines.append(f"- 失败: {total - passed}")
    lines.append("")

    for idx, r in enumerate(results, 1):
        status = "[PASS]" if r["passed"] else "[FAIL]"
        lines.append(f"## 测试 {idx}：{r['test_name']} — {status}")
        lines.append("")
        lines.append("### 测试代码")
        lines.append("")
        lines.append("```python")
        lines.append(r["code"].strip())
        lines.append("```")
        lines.append("")
        lines.append("### 执行输出")
        lines.append("")
        lines.append("```text")
        lines.append(r["output"].strip())
        lines.append("```")
        lines.append("")
        if r["error"]:
            lines.append("### 错误堆栈")
            lines.append("")
            lines.append("```text")
            lines.append(r["error"].strip())
            lines.append("```")
            lines.append("")

    report_path = PROJECT_ROOT / "diagnosis" / "e2e_test.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
