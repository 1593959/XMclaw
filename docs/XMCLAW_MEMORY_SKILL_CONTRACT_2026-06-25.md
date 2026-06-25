# XMclaw 记忆与技能系统契约（2026-06-25）

## 一句话结论

工作区 MD 文件、结构化事实库、向量库、事件日志、技能系统各有职责，不能互相替代，也不能互相默认投影。

- MD 文件：人类可读、可编辑的项目手册、规则、偏好说明、提示词入口。
- 结构化事实库：Agent 长期记忆的权威来源。
- 向量库：检索索引，用来找相似事实和文档片段，不判断事实真假。
- events.db：原始经历日志和审计轨迹，不直接作为长期记忆。
- Memory Candidate：自动抽取的待审候选，不默认固化。
- PromptMemoryPack：运行时上下文打包器，把本轮真正需要的记忆、技能和任务状态注入模型。
- 技能系统：约束和增强 Agent 行为，让它能主动查询、选择、调用、跳过技能，并记录理由。

## MD 与事实库边界

生产路径默认不把自动事实写回：

- `USER.md`
- `MEMORY.md`
- `LEARNING.md`
- `AGENTS.md`
- `TOOLS.md`
- `SOUL.md`

原因：

- 自动事实写进 MD 后，MD 会同时变成“人类手册”和“事实库镜像”，职责混乱。
- 自动块容易被再次 ingest，造成重复事实、乱码、错误经验反向污染事实库。
- 事实召回应通过结构化查询、排序、证据和有效期完成，不应依赖 MD 被整段塞进系统提示词。
- 用户编辑 MD 时，应该是在编辑明确规则和说明，不是在编辑向量索引。

保留的唯一默认同步方向：

```text
用户手动编辑 MD
  -> md_sync 剥离自动块和 fid 行
  -> 写入 persona_manual 类型事实
  -> 渲染回 MD 时只包含手动内容
```

调试和迁移可以显式打开自动导出：

```python
render_persona_file(..., include_auto_sections=True)
render_all_persona_files(..., include_auto_sections=True)
```

这个能力只用于调试、迁移、人工导出，不作为生产运行默认链路。

## Agent 如何使用记忆

Agent 不应靠“读完整 MD 文件”猜记忆，而应主动查询。

必须查询记忆的场景：

- 涉及用户身份、偏好、长期规则、禁忌、默认选择；
- 涉及项目约定、架构原则、过去已经定下的方案；
- 涉及历史失败、可复用流程、做事方法；
- 当前任务出现连续失败、策略重复、路径/版本/产物不确定；
- 用户问“你记得吗”“之前怎么做的”“以后都按这个来”。

写入记忆的硬约束：

- 未完成任务中的中间猜测不得写入长期记忆。
- 失败工具结果不得写成成功经验。
- Assistant 自己推测的方法不得直接固化。
- 用户明确纠正、明确偏好、明确规则，可以进入候选。
- 任务完成后的成功轨迹可以进入候选。
- 候选固化必须有证据、来源、决策理由和置信度。

写入失败时，Agent 不能回答“我记住了”。它必须说明未保存，并提示用户稍后重试或检查记忆服务状态。

## PromptMemoryPack 契约

运行时上下文由 `PromptMemoryPack` 统一承载，不由散乱 MD、events 或向量 top_k 直接拼接。

标准结构：

```text
<memory-context>
  <user-rules />
  <preferences />
  <project-facts />
  <relevant-episodes />
  <procedures />
  <warnings />
</memory-context>

<skill-context>
  <candidates />
  <required-action />
  <skip-policy />
</skill-context>

<task-context>
  <goal />
  <current-step />
  <artifacts />
  <failures />
  <verification />
</task-context>
```

每条注入内容应尽量带：

- `source`
- `why_recalled`
- `confidence`
- `validity`
- `recommended_action`

## 技能系统契约

技能不是“装了多少个”的数字，而是 Agent 可主动使用的行为约束和能力包。

技能目录扫描：

- `~/.xmclaw/skills_user/`
- `~/.agents/skills/`
- 当前工作区的 `skills/`

技能状态必须能回答：

- 已注册哪些技能；
- 每个技能做什么；
- 每个技能何时使用；
- 技能来自哪个目录；
- 哪些目录存在但未注册；
- 哪些技能加载失败；
- 哪些技能需要 daemon 重启；
- 哪些外部格式可以转换，哪些不能直接兼容。

运行时工具：

- `skill_status`：查看注册、扫描根、加载失败、未注册候选。
- `skill_browse`：按当前任务查询候选技能。
- `skill_view`：查看技能内容。
- `skill_decision`：结构化记录使用、跳过或继续浏览的理由。

复杂任务开始前、连续失败后、模型命中技能候选时，Agent 必须主动查询技能，而不是只靠提示词提醒。

## 技能安装与兼容

安装器不能把不兼容仓库静默当成成功。

要求：

- 标准 XMclaw skill 至少要有 `SKILL.md`、`manifest.json` 或 `skill.py` 之一。
- 如果外部仓库是 Claude Code skill 等其他格式，应标记为可转换候选，而不是直接注册。
- 转换后的技能必须保留来源、转换时间、转换日志和兼容性说明。
- 技能可以被 Agent 修改成 XMclaw 可用格式，但不能覆盖原始来源记录。
- 前端必须展示“已安装但未注册”“加载失败”“格式不兼容”“等待重启”等状态。

## 已废弃方向

- 不保留环境审计产品入口。
- 不做独立环境记忆分支。
- 不暴露环境审计 API。
- `memory` 工具不提供 `environment` action。
- 不把路径事件一律固化为长期环境事实。
- 不把自动事实默认写回 MD 文件。
- 不把向量库当事实源。
- 不把失败轨迹和中间猜测写成经验。

废弃原因：这些方向把单个路径案例过度泛化，容易让错误轨迹被固化。真正需要修的是通用任务状态、工具反馈、技能查询、记忆候选质量和失败策略切换。

