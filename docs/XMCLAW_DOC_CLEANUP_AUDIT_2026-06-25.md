# XMclaw 文档清理审计（2026-06-25）

## 当前准绳

以下文档是当前记忆、技能、Agent 内核方向的准绳：

- `docs/XMCLAW_MEMORY_EVENT_REDESIGN_2026-06-24.md`
- `docs/XMCLAW_MEMORY_SKILL_CONTRACT_2026-06-25.md`
- `docs/XMCLAW_CONTROL_CENTER_AGENT_REDESIGN_2026-06-24.md`

其中，记忆与技能的具体边界以 `XMCLAW_MEMORY_SKILL_CONTRACT_2026-06-25.md` 为准；Agent 内核、任务状态、Artifact Ledger、GraphState、技能选择的迁移顺序以 `XMCLAW_CONTROL_CENTER_AGENT_REDESIGN_2026-06-24.md` 为准。

## 已清理问题

| 文件 | 处理 | 原问题 |
| --- | --- | --- |
| `docs/XMCLAW_MEMORY_EVENT_REDESIGN_2026-06-24.md` | 已重写 | 乱码、旧环境记忆方向、新旧方案混杂、MD 自动投影描述错误 |
| `docs/XMCLAW_MEMORY_SKILL_CONTRACT_2026-06-25.md` | 已重写 | 乱码、关键契约不可读、残留旧环境审计表述 |

## 保留为路线图或历史审计

| 文件 | 状态 | 使用方式 |
| --- | --- | --- |
| `docs/XMCLAW_REMEDIATION_DEVELOPMENT_PLAN_2026-06-24.md` | 保留 | 作为整体修复路线图和批次记录，不覆盖最新记忆/技能契约 |
| `docs/audit/XMCLAW_FRONTEND_AND_AGENT_GAP_AUDIT_2026-06-24.md` | 保留 | 作为差距审计和外部对标历史依据 |
| `docs/research/tool_use_architecture_report.md` | 保留待复查 | 作为工具使用研究材料，不直接作为实现契约 |

## 废弃方向

后续开发不得再按以下方向实现：

- 独立环境记忆产品分支；
- 环境审计页面；
- 环境审计 API；
- `memory(action="environment")`；
- 自动事实默认写回 MD；
- MD 文件作为事实库镜像；
- 向量库作为事实源；
- 未验证失败轨迹直接固化为长期经验。

## 后续清理清单

- 扫描源码注释和 docstring 中的乱码，优先处理 `xmclaw/memory/v2/` 与 `xmclaw/providers/tool/`。
- 把路线图中的“已完成/剩余”状态和当前代码再对齐一次。
- 给前端增加“文档/契约入口”，让用户能看到当前记忆、技能、运行时开关的真实规则。
- 为 `memory_decision`、技能决策、PromptMemoryPack、Artifact Ledger、GraphState 增加端到端事件时间线。

