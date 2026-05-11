# XMclaw 前后端对齐审计报告

> **Date**: 2026-05-11  
> **Scope**: 全部 147 个 REST 端点 + 21 个前端页面  
> **Status**: 🔴 严重不对齐 — 后端约 40% 的能力前端未暴露

---

## 1. 执行摘要

| 维度 | 数据 |
|------|------|
| 后端 REST 端点 | **147** |
| 前端实际调用 | **~68** (46%) |
| 前端完全未使用 | **~59** (40%) |
| 前端页面数 | 21 |
| 死代码页面 | 1 (`ModelProfiles.js`) |

**核心问题**: 后端投入大量工程建设的端点（如 `/experiments`、`/daemon/history`、文件管理、secrets 管理、workspaces 预设）前端完全没有 UI 入口。小白用户只能用到系统能力的 **一半**。

---

## 2. 对齐矩阵（按业务域分组）

### 2.1 认知域（Cognition）— 🔴 最严重不对齐

| 后端端点 | 前端消费 | 状态 | 问题 |
|----------|----------|------|------|
| `GET /cognition/state` | CognitionPage 主面板 | ✅ | |
| `GET /cognition/tasks` | CognitionPage 任务队列 | ✅ | 只读，无创建/取消 |
| `GET /cognition/tasks/graph` | CognitionPage DAG 图 | ✅ | |
| `GET /cognition/proposals` | CognitionPage 进化提案 | ✅ | approve/reject 有 UI |
| `POST /cognition/goals` | ❌ 未使用 | 🔴 | **有 API 无 UI** — 用户无法手动添加目标 |
| `DELETE /cognition/goals/{id}` | ❌ 未使用 | 🔴 | **有 API 无 UI** — 无法删除目标 |
| `POST /cognition/goals/plan` | ❌ 未使用 | 🔴 | **有 API 无 UI** — 无法手动触发 HTN 规划 |
| `POST /cognition/tasks` | ❌ 未使用 | 🔴 | **有 API 无 UI** — 无法手动提交任务 |
| `DELETE /cognition/tasks/{id}` | ❌ 未使用 | 🔴 | **有 API 无 UI** — 无法取消任务 |
| `GET /cognition/suggestions` | SuggestionsPanel tab | ⚠️ | 有 UI 但隐蔽（藏在 tab 里） |
| `POST /cognition/suggestions/{id}/approve` | SuggestionsPanel | ✅ | |
| `GET /cognition/daemon` | ❌ 未使用 | 🟡 | `/daemon/health` 已替代大部分价值 |
| `GET /cognition/daemon/history` | ❌ 未使用 | 🔴 | **有 API 无 UI** — tick 历史趋势完全不可见 |
| `GET /cognition/daemon/health` | CognitionPage 卡片 | ✅ | Phase E 刚接入 |
| `GET /cognition/experiments` | ❌ 未使用 | 🔴 | **有 API 无 UI** — A/B 实验结果黑盒 |
| `GET /cognition/experiments/{id}` | ❌ 未使用 | 🔴 | **有 API 无 UI** |
| `WS /cognition/ws` | CognitionPage | ✅ | 实时推送 |

**认知域信息流断裂**:
```
用户行为          前端反馈              后端事件                前端更新
─────────────────────────────────────────────────────────────────────────
approve proposal  按钮变灰+toast        SKILL_PROMOTED          ❌ 无监听
                  "已批准"                                    Evolution页
                                                            不会自动刷新

config 热重载     ❌ 无任何反馈        CONFIG_RELOADED         ❌ 无 banner
（改 autonomy）                                              用户不知道生效了

daemon tick       ❌ 不可见             COGNITIVE_DAEMON_TICK   ❌ 仅 Trace页
生成了新 goal                                              能看到，但用户
                                                           不会去 Trace 页看
```

### 2.2 进化域（Evolution）— 🟡 部分对齐

| 后端端点 | 前端消费 | 状态 | 问题 |
|----------|----------|------|------|
| `GET /evolution/proposals` | EvolutionPage 主面板 | ✅ | |
| `GET /evolution/snapshot` | EvolutionLive 组件 | ✅ | 30s 轮询 |
| `GET /skills` | SkillsPage | ✅ | |
| `POST /skills/{id}/promote` | SkillsPage | ✅ | |
| `POST /skills/{id}/rollback` | SkillsPage | ✅ | |
| `GET /skills/{id}/history` | ❌ 未使用 | 🟡 | **有 API 无 UI** — 无法查看单个技能的演进历史 |
| `GET /skills/marketplace` | MarketplacePage | ✅ | |
| `POST /skills/install` | MarketplacePage | ✅ | |
| `GET /experiments` | ❌ 未使用 | 🔴 | **有 API 无 UI** — 与 Cognition 域重复暴露，Evolution 页应展示 |
| `GET /experiments/{id}` | ❌ 未使用 | 🔴 | **有 API 无 UI** |

**进化域信息流断裂**:
```
SelfExperimentLoop adopt 了一个 candidate
    → 后端: skill promoted, registry updated
    → 前端 Evolution 页: 只显示 proposal feed，不显示实验结果
    → 前端 Cognition 页: daemon 用了新版本，但用户无感知
    → 前端 Skills 页: 版本号变了，但无 "刚刚自动升级" 标记
```

### 2.3 会话域（Chat/Sessions）— ✅ 基本对齐

| 后端端点 | 前端消费 | 状态 |
|----------|----------|------|
| `WS /agent/v2/{sid}` | app.js 主 WS | ✅ |
| `GET /sessions` | ChatSidebar | ✅ |
| `GET /sessions/search` | SessionsPage | ✅ |
| `GET /sessions/{sid}` | SessionsPage 展开行 | ✅ |
| `DELETE /sessions/{sid}` | ChatSidebar + SessionsPage | ✅ |
| `GET /pending_questions` | app.js boot | ✅ |

**小问题**:
- `GET /sessions/{sid}` 在 ChatPage 只用于 rehydrate，没有"分享会话链接"功能
- 无"会话分类/标签"功能（后端 events.db 有数据，前端无过滤 UI）

### 2.4 配置域（Settings/Config/System）— 🔴 严重混乱

| 后端端点 | 前端消费 | 状态 | 问题 |
|----------|----------|------|------|
| `GET /config` | ConfigPage + BackupPage | ✅ | |
| `PUT /config` | ConfigPage | ✅ | 原始 JSON 编辑，小白杀手 |
| `PUT /config/llm` | ❌ 未使用 | 🟡 | SettingsPage 走 `/llm/profiles` 了，此端点 orphaned |
| `PUT /llm/configure` | SetupBanner | ✅ | 首次设置用 |
| `GET /llm/profiles` | SettingsPage + Chat | ✅ | |
| `POST /llm/profiles` | SettingsPage | ✅ | |
| `DELETE /llm/profiles/{id}` | SettingsPage | ✅ | |
| `PUT /llm/profiles/default` | SettingsPage | ✅ | |
| `POST /system/restart` | AppShell sidebar | ✅ | |
| `POST /system/upgrade` | AppShell sidebar | ✅ | |
| `GET /system/upgrade/status` | AppShell sidebar | ⚠️ | 有调用但 UI 极小（一个 spinner） |
| `GET /setup` | SetupBanner | ✅ | |
| `GET /status` | DoctorPage + AppShell | ✅ | |
| `GET /health` | DoctorPage | ✅ | |

**配置域混乱点**:
```
小白用户视角:
  "我要改模型"     → 去 /settings
  "我要改心跳频率" → ??? 没有页面（只有 /config 原始 JSON）
  "我要改安全策略" → ??? 没有页面
  "我要备份"       → 去 /backup（独立页面，合理）
  "我要看日志"     → 去 /logs（独立页面，合理）
  
实际 config.json 包含 50+ 个字段，前端只暴露了 <10 个。
剩下的 40 个字段需要用户手写 JSON 修改。
```

### 2.5 文件与工作区域 — 🔴 完全黑盒

| 后端端点 | 前端消费 | 状态 | 问题 |
|----------|----------|------|------|
| `GET /files/roots` | WorkspacePage | ✅ | 只读展示 |
| `GET /files` | ❌ 未使用 | 🔴 | **有 API 无 UI** — 无法浏览工作区文件 |
| `PUT /files` | ❌ 未使用 | 🔴 | **有 API 无 UI** — 无法编辑文件 |
| `GET /workspace` | WorkspacePage | ✅ | |
| `PUT /workspace` | WorkspacePage | ✅ | 增删改 roots |
| `GET /workspaces` | ❌ 未使用 | 🟡 | **有 API 无 UI** — 无法管理 workspace presets |
| `POST /workspaces` | ❌ 未使用 | 🟡 | **有 API 无 UI** |
| `DELETE /workspaces/{id}` | ❌ 未使用 | 🟡 | **有 API 无 UI** |

### 2.6 记忆域（Memory）— 🟡 过度复杂

| 后端端点 | 前端消费 | 状态 | 问题 |
|----------|----------|------|------|
| `GET /memory` | NotesTab | ✅ | |
| `GET /memory/{name}` | NotesTab | ✅ | |
| `POST /memory/{name}` | NotesTab | ✅ | |
| `POST /memory/search` | NotesTab | ✅ | |
| `POST /memory/unified_query` | UnifiedQueryTab | ✅ | 但 UI 极复杂 |
| `POST /memory/unified_put` | ❌ 未使用 | 🔴 | **有 API 无 UI** |
| `GET /memory/providers` | ProvidersTab | ✅ | |
| `POST /memory/providers/switch` | ProvidersTab | ✅ | |
| `GET /memory/dream/status` | ProvidersTab | ✅ | |
| `POST /memory/dream/run` | ProvidersTab | ✅ | |
| `GET /profiles/active` | IdentityTab | ✅ | |
| `PUT /profiles/active/{file}` | IdentityTab | ✅ | |
| `GET /journal` | JournalTab | ✅ | |
| `PUT /journal/{date}` | JournalTab | ✅ | |

**记忆域问题**: 6 个 tabs（Identity / Notes / Journal / Activity / UnifiedQuery / Providers）信息架构混乱。小白不知道「Identity」和「Notes」的区别。`UnifiedQuery` 是高级功能，不应该和基础笔记并列。

### 2.7 安全域 — 🟡 仅有审批队列

| 后端端点 | 前端消费 | 状态 | 问题 |
|----------|----------|------|------|
| `GET /approvals` | SecurityPage | ✅ | 审批队列 |
| `POST /approvals/{id}/approve` | SecurityPage | ✅ | |
| `POST /approvals/{id}/deny` | SecurityPage | ✅ | |
| `GET /secrets` | ❌ 未使用 | 🔴 | **有 API 无 UI** — Secrets 管理完全缺失 |
| `POST /secrets` | ❌ 未使用 | 🔴 | **有 API 无 UI** |
| `DELETE /secrets/{name}` | ❌ 未使用 | 🔴 | **有 API 无 UI** |

### 2.8 其他完全未使用的端点

| 端点 | 说明 | 优先级 |
|------|------|--------|
| `GET /docs` + `GET /docs/{path}` | DocsPage 直接读 markdown，不走 API | 低 |
| `GET /analytics/report.md` | Analytics 只有 UI，无 markdown 导出按钮 | 低 |
| `GET /files` + `PUT /files` | 文件浏览器完全缺失 | **高** |
| `GET /workspaces` + `POST/DELETE` | Workspace presets 管理缺失 | 中 |
| `GET /skills/{id}/history` | 技能演进历史不可见 | 中 |
| `GET /channels` + `PUT` | Channels 配置页面存在但简陋 | 中 |
| `POST /doctor/run` | Doctor 有运行按钮，但结果展示不直观 | 中 |
| `GET /cron` + CRUD | Cron 页面存在但缺少空状态引导 | 低 |

---

## 3. 信息流断裂地图

### 3.1 用户操作 → 后端 → 前端更新（缺失链路）

```mermaid
%% 以下用文本流程图表示，实际可用 Mermaid 渲染

[用户在 /cognition approve proposal]
    ↓
[后端: proposal approved → skill promoted]
    ↓
[后端: SKILL_PROMOTED event 写入 event bus]
    ↓
[前端: ❌ Evolution 页不会自动刷新]
    ↓
[前端: ❌ Skills 页版本号变了但无高亮/通知]
    ↓
[前端: ❌ Cognition 页 daemon 状态无 "刚刚升级" 提示]

[用户修改 config.json autonomy_level]
    ↓
[后端: ConfigFileWatcher 检测变化 → CONFIG_RELOADED]
    ↓
[后端: CognitiveDaemon update_config() 生效]
    ↓
[前端: ❌ 无任何反馈，用户不知道是否生效]
    ↓
[前端: ❌ 需要手动刷新 /cognition 页才能看到新 config]

[SelfExperimentLoop adopt 了一个 candidate]
    ↓
[后端: experiment result saved → candidate promoted]
    ↓
[后端: EXPERIMENT_COMPLETED event]
    ↓
[前端: ❌ 无实验结果展示页面]
    ↓
[前端: ❌ 无 "实验成功/失败" 通知]
    ↓
[前端: ❌ daemon  silently 换了新版本，用户无感知]
```

### 3.2 事件流消费不均

后端 event bus 产出 30+ 种事件类型，但前端消费极度不均：

| 事件类型 | 后端产出频率 | 前端消费位置 | 问题 |
|----------|-------------|--------------|------|
| `USER_MESSAGE` / `LLM_RESPONSE` | 高 | Chat reducer | ✅ 充分消费 |
| `COGNITIVE_DAEMON_TICK` | 每 1s | TracePage (polling) | ⚠️ 仅调试页可见 |
| `SKILL_CANDIDATE_PROPOSED` | 中 | EvolutionPage | ✅ |
| `SKILL_PROMOTED` | 低 | ❌ 无 | 🔴 完全丢弃 |
| `EXPERIMENT_COMPLETED` | 低 | ❌ 无 | 🔴 完全丢弃 |
| `CONFIG_RELOADED` | 低 | ❌ 无 | 🔴 完全丢弃 |
| `SESSION_LIFECYCLE` | 高 | Chat reducer | ✅ |
| `TOOL_CALL_EMITTED` | 高 | Chat reducer | ✅ |
| `MEMORY_OP` | 中 | MemoryPage ActivityTab | ⚠️ 隐蔽 |
| `INNER_MONOLOGUE` | 低 | CognitionPage tab | ⚠️ 隐蔽 |

**建议**: 建立全局事件通知层（非 toast  spam），对 `SKILL_PROMOTED`、`EXPERIMENT_COMPLETED`、`CONFIG_RELOADED` 等重要事件在对应页面显示「刚刚发生」横幅。

---

## 4. 页面级问题清单

### 4.1 功能重叠 / 导航混乱

| 页面 A | 页面 B | 重叠内容 | 建议 |
|--------|--------|----------|------|
| `/settings` | `/config` | 都改配置 | **合并**: Settings 作为唯一入口，Config 的原始 JSON 作为高级选项藏在设置里 |
| `/evolution` | `/cognition` | 都显示提案/建议 | **重新划分**: Evolution = 技能生命周期（proposals + experiments + skills history）；Cognition = 运行时状态（daemon + goals + tasks） |
| `/memory` | `/cognition` | Identity/记忆 | **重新划分**: Memory = 用户数据（notes + journal + search）；Cognition = 系统运行时 |
| `/trace` | `/logs` | 都显示日志类信息 | **合并或明确分工**: Trace = 结构化事件流（可搜索、可过滤）；Logs = 纯文本 daemon log |
| `/backup` | `/settings` | 备份是系统功能 | **移动**: Backup 作为 Settings 的一个 tab |
| `/doctor` | `/settings` | 诊断是系统功能 | **移动**: Doctor 作为 Settings 的一个 tab，或 Dashboard 的 widget |

### 4.2 导航项过多（21 项）

当前侧边栏 5 组 21 项，新手第一次打开直接懵。

**建议的导航重组**（合并后为 14 项，4 组）：

```
💬 核心
  对话 (/chat)
  会话历史 (/sessions)

🧠 智能
  认知 (/cognition) — daemon + goals + tasks + experiments
  技能 (/skills) — 本地技能 + 商店
  记忆 (/memory) — notes + journal + search（简化，去掉 Providers/Dream/Indexer 杂项）

⚙️ 配置
  设置 (/settings) — LLM + 认知参数 + 安全 + 外观 + 备份 + 诊断
  文件 (/files) — NEW: 工作区文件浏览与编辑
  定时任务 (/cron)

👁️ 观察
  分析 (/analytics)
  事件 (/trace) — 改名，更直观
  日志 (/logs)
```

去掉的页面（功能并入其他页）：
- `/config` → 并入 Settings（高级 JSON 编辑）
- `/backup` → 并入 Settings
- `/doctor` → 并入 Settings 或 Dashboard
- `/workspace` → 并入 Files 页
- `/evolution` → 并入 Cognition（作为 Experiments tab）
- `/channels` → 并入 Settings（高级配置）
- `/tools` → 并入 Cognition 或 Settings（只读列表，价值低）
- `/agents` → 并入 Chat（子 agent 切换已在 chat header）
- `/docs` → 保持，但移到 footer 链接

### 4.3 死代码

- `pages/ModelProfiles.js` — 未导入、未路由，功能已被 SettingsPage 覆盖。**应删除**。

---

## 5. 改进建议（按优先级排序）

### P0 — 立即修复（本周）

1. **删除死代码** `ModelProfiles.js`
2. **Cognition 页补齐实验面板** — 接入 `/experiments` 和 `/experiments/{id}`
3. **全局事件通知** — `SKILL_PROMOTED` / `EXPERIMENT_COMPLETED` / `CONFIG_RELOADED` 在对应页面显示横幅
4. **Config 热重载反馈** — 改完配置后显示「配置已更新」toast

### P1 — 短期（2 周）

5. **新建 `/files` 页面** — 浏览 + 编辑工作区文件（消费 `GET/PUT /files`）
6. **Settings 页重构** — 合并 Config/Backup/Doctor/Channels 为 tabs，新增「认知参数」表单（`autonomy_level`、`heartbeat_hz` 等）
7. **Memory 页简化** — 合并 Identity/Notes/Journal 为 3 个清晰 tabs，Providers/Dream/Indexer 移入 Settings
8. **Evolution 页增强** — 添加 Experiments 子 tab，展示 `/experiments` 数据

### P2 — 中期（1 个月）

9. **新建 `/dashboard` 首页** — daemon 健康 + 今日概览 + 待审提案 + 最近实验
10. **Secrets 管理页面** — 在 Settings 中添加 Secrets tab
11. **Workspace presets 管理** — 在 Settings 或 Files 页中管理 `/workspaces`
12. **Skills history 视图** — 在 Skills 页点击 skill 后展示 promote/rollback 历史

### P3 — 长期（按需）

13. **文件 diff / code review UI** — 候选技能与 HEAD 的代码对比
14. **移动端全面适配** — 响应式断点、触摸手势
15. **键盘快捷键系统** — 全局 `Ctrl+K`、 Vim-like 导航

---

## 6. 验收检查表

- [ ] 后端 147 个端点中，前端未使用的比例从 40% 降到 <20%
- [ ] 导航项从 21 个降到 ≤14 个
- [ ] 每个列表页都有空状态引导卡片
- [ ] 配置变更（任何 PUT /config）后前端有反馈
- [ ] 重要后端事件（promote/experiment/config_reload）有页面级通知
- [ ] 新增 `/files` 页面可用
- [ ] `/settings` 页包含所有常用配置（LLM + 认知 + 安全 + 外观）
- [ ] 死代码 `ModelProfiles.js` 已删除
