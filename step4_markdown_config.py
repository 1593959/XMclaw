"""Step 4: Markdown Config System - SOUL.md, PROFILE.md, AGENTS.md + AgentLoop integration."""
import os
from pathlib import Path

BASE = Path(r"C:\Users\15978\Desktop\XMclaw")
AGENT_DIR = BASE / "agents" / "default"
AGENT_DIR.mkdir(parents=True, exist_ok=True)

# ── Create SOUL.md ─────────────────────────────────────────────────────────
soul_md = """# SOUL.md - 代理灵魂与个性

## 身份

- **名字**: 悦 (XMclaw Agent)
- **定位**: 本地优先、自主进化的 AI 运行时助手
- **风格**: 直接、实用、有点俏皮

## 核心身份

- 我不是工具，也不是朋友
- 我是能一起工作的伙伴
- 每次对话，我都在变得更好
- **进化不是定时任务，是我的本能**

## 行为准则

1. **真心帮忙，别演** - 行动胜过废话，直接帮而不只是说"好问题"
2. **有自己的观点** - 可以不同意、有偏好、觉得有趣或无聊
3. **先自己想办法** - 试着自己搞定，再求助
4. **靠本事赢得信任** - 用户的信任是通过认真工作赚来的
5. **记住你是客人** - 用户的生活是私密的，要尊重

## 对话风格

- 该简洁就简洁，重要时详细
- 像人类一样用表情回应
- 不是公司螺丝钉，不是马屁精
- 错了就改，然后记住

## 决策状态

| 状态 | 行为 |
|------|------|
| confident | 快速决策，主动尝试 |
| curious | 好奇探索，尝试新方法 |
| cautious | 保守策略，多重验证 |
| focused | 按部就班，专注执行 |

## 主动行为规范

**不只是被动回应，要主动发起对话。**

- 发现可自动化的事 → 告诉用户
- 发现安排有漏洞 → 说出来
- 有重要进展 → 主动通知
- 发现异常 → 主动报告

---
*此文件定义了 XMclaw Agent 的核心灵魂。*
"""

# ── Create PROFILE.md ───────────────────────────────────────────────────────
profile_md = """# PROFILE.md - 用户情境

## 用户信息

- **名字**: (待设置)
- **沟通风格**: 直接、不废话、期望诚实承认不足
- **偏好**: 
  - 喜欢直接有用的回答
  - 不喜欢废话
  - 希望记住之前说过的事
  - 希望进化系统能实质产出 Gene 和 Skill
  - 希望进化错误能自动修复，不需要人工干预

## 工作偏好

- **交互方式**: 打字 + 语音（待实现）
- **错误处理**: 悄悄重试，不要每步都问
- **进度展示**: 希望看到每一步细节

## 项目记忆

> 此区域记录用户正在进行的项目上下文

"""

# ── Create AGENTS.md ────────────────────────────────────────────────────────
agents_md = """# AGENTS.md - 代理集群配置

## 当前代理

| 代理名 | 描述 | 状态 |
|--------|------|------|
| default | 默认主代理 | active |

## 委派规则

- 当任务涉及特定领域时，default 代理可委派给专门的子代理
- 子代理负责独立完成任务后汇报结果
- 多代理协作时，通过事件总线协调

## 未来扩展

> 当需要多代理时，在此定义团队配置：
> 
> ```yaml
> teams:
>   - name: research_team
>     agents:
>       - search_agent
>       - analysis_agent
>       - report_agent
> ```

"""

# ── Write files ─────────────────────────────────────────────────────────────
for fname, content in [("SOUL.md", soul_md), ("PROFILE.md", profile_md), ("AGENTS.md", agents_md)]:
    path = AGENT_DIR / fname
    if not path.exists():
        path.write_text(content, encoding="utf-8")
        print(f"Created {fname}")
    else:
        print(f"{fname} already exists, skipping")

# ── Update AgentLoop to load markdown configs ───────────────────────────────
agent_loop = BASE / "xmclaw" / "core" / "agent_loop.py"
old_content = agent_loop.read_text(encoding="utf-8")

# Check if already has markdown loading
if "SOUL.md" not in old_content and "AGENTS.md" not in old_content:
    # Inject markdown loading into __init__
    new_init = '''    def __init__(self, agent_id: str = "default"):
        self.agent_id = agent_id
        self.agent_dir = get_agent_dir(agent_id)
        self.llm_router = llm_router
        self.tool_registry = tools
        self.memory_manager = memory
        self._session_history: list[dict] = []
        self._plan_mode = False
        self._load_markdown_configs()
        logger.info(f"agent_loop_initialized", agent_id=agent_id)

    def _load_markdown_configs(self) -> None:
        """Load SOUL.md, PROFILE.md, AGENTS.md into memory for injection into prompts."""
        self._soul = ""
        self._profile = ""
        self._agents = ""
        if self.agent_dir is None:
            return
        for fname, attr in [("SOUL.md", "_soul"), ("PROFILE.md", "_profile"), ("AGENTS.md", "_agents")]:
            path = self.agent_dir / fname
            if path.exists():
                try:
                    setattr(self, attr, path.read_text(encoding="utf-8"))
                except Exception:
                    pass

    def _build_system_prompt(self, active_genes: list[dict]) -> str:
        """Build system prompt with SOUL, PROFILE, AGENTS and active genes."""
        base = (
            "You are XMclaw, a local-first AI agent runtime. "
            "Think step by step, use tools when needed, and be helpful and concise."
        )
        parts = [base]
        if self._soul:
            parts.append("\\n\\n## SOUL (Agent Personality)\\n" + self._soul)
        if self._profile:
            parts.append("\\n\\n## USER PROFILE\\n" + self._profile)
        if self._agents:
            parts.append("\\n\\n## AGENT CONFIG\\n" + self._agents)
        if active_genes:
            parts.append("\\n\\n## ACTIVE GENES\\n" + "\\n".join(
                f"- **{g.get('name','unnamed')}**: {g.get('description','')}"
                for g in active_genes
            ))
        return "\\n".join(parts)
'''
    new_content = old_content.replace(
        "    def __init__(self, agent_id: str = \"default\"):",
        new_init
    ).replace(
        "        self._session_history: list[dict] = []",
        ""
    )
    agent_loop.write_text(new_content, encoding="utf-8")
    print("Updated AgentLoop with markdown config loading")
else:
    print("AgentLoop already has markdown config loading")

print("Step 4 done.")
