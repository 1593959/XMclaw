# XMclaw 前端重构与对齐实施计划

> **Version**: 2026-05-11  
> **Status**: 实施中  
> **Scope**: `xmclaw/daemon/static/` + 后端 API 对齐  
> **约束**: 保持 "无构建步骤"，不引入 Node.js 工具链

---

## 1. 现状快照

### 1.1 前后端对齐度

| 指标 | 数值 |
|------|------|
| 后端 REST 端点 | **147** |
| 前端实际调用 | **~68** (46%) |
| 前端完全未使用 | **~59** (40%) |
| 前端页面数 | 21 |
| 死代码页面 | 1 (`ModelProfiles.js`) |

**核心矛盾**: 后端建设了丰富的 API 能力（实验系统、文件管理、Secrets、目标/任务 CRUD），但前端只暴露了不到一半。小白用户只能用到系统能力的 46%。

### 1.2 最严重的 5 个缺口

| # | 缺口 | 后端端点 | 前端状态 | 影响 |
|---|------|----------|----------|------|
| 1 | **实验结果不可见** | `/experiments`, `/experiments/{id}` | ❌ 完全缺失 | A/B 闭环黑盒，用户不知道进化是否成功 |
| 2 | **目标/任务只读** | `POST/DELETE /goals`, `POST/DELETE /tasks` | ❌ 完全缺失 | 用户无法干预认知系统的目标和任务 |
| 3 | **文件管理缺失** | `GET/PUT /files` | ❌ 完全缺失 | 工作区文件不可浏览/编辑 |
| 4 | **Secrets 管理缺失** | `/secrets` CRUD | ❌ 完全缺失 | 安全凭证只能手写 JSON |
| 5 | **重要事件丢弃** | `SKILL_PROMOTED`, `EXPERIMENT_COMPLETED`, `CONFIG_RELOADED` | ❌ 无监听 | 系统变更用户完全无感知 |

### 1.3 导航混乱

当前侧边栏 **5 组 21 项**，重叠严重：
- `/settings` 和 `/config` 都改配置，小白不知道去哪个
- `/evolution` 和 `/cognition` 都显示提案/建议，概念混淆
- `/backup` `/doctor` `/config` 是低频操作，不该占独立导航项

---

## 2. 目标："对齐 + 简洁 + 感知"

不追求一次性完美，追求**每改一页就对齐一页、每合并一个导航项就减少一分认知负担**。

### 2.1 对齐原则
- 后端有 API → 前端必须有入口（哪怕是只读列表）
- 后端发事件 → 前端必须有反馈（toast / banner / 徽章）
- 用户操作 → 必须有结果确认（成功/失败/进行中）

### 2.2 简洁原则
- 导航项从 21 个合并到 **≤14 个**
- 配置入口只有一个（`/settings`）
- 每个页面只负责一个主语（Cognition = 运行时，Skills = 技能，Files = 文件）

### 2.3 感知原则
- 系统做了什么事 → 用户必须能在 3 秒内感知到
- 不要让用户去 Trace 页翻事件才能知道发生了什么

---

## 3. 实施路线（6 个迭代，每个 1–3 天）

### 迭代 1: 清理与骨架（1 天）
**目标**: 去掉死代码，搭好新页面骨架，不对齐能力但铺好路。

- [ ] **删除死代码** `pages/ModelProfiles.js`
- [ ] **导航重组** — 侧边栏改为 4 组 14 项：
  - 💬 核心: 对话、会话历史
  - 🧠 智能: 认知、技能、记忆
  - ⚙️ 配置: 设置（合并 config/backup/doctor）、文件、定时任务
  - 👁️ 观察: 分析、事件、日志
- [ ] **新建页面空壳**（只渲染标题和"开发中"占位）：
  - `/files` — 文件浏览（预留）
  - `/dashboard` — 首页（预留）
- [ ] **重定向** `/config` → `/settings`，`/backup` → `/settings`，`/doctor` → `/settings`

**验收**: 侧边栏 14 项，点击无 404，被合并的页面自动跳转到新位置。

---

### 迭代 2: 全局事件感知层（1–2 天）
**目标**: 让系统变更对用户可见。

- [ ] **新增 `lib/event_banner.js`** — 全局事件监听层
  - 订阅 `SKILL_PROMOTED` → 在 Skills 页显示「🎉 技能 X 已自动升级」横幅
  - 订阅 `EXPERIMENT_COMPLETED` → 在 Cognition 页显示「实验完成：adopt/reject/extend」横幅
  - 订阅 `CONFIG_RELOADED` → 全局 toast「配置已热更新」
  - 订阅 `COGNITIVE_DAEMON_TICK`（带 `errors`）→ 如果 `status=degraded`，显示警告横幅
- [ ] **Cognition 页 config 热重载反馈** — 改完 autonomy/heartbeat 后，页面顶部显示「配置已生效」

**验收**: approve 一个提案后，切换到 Skills 页能看到「刚刚升级」横幅。

---

### 迭代 3: Cognition 页补齐（2–3 天）
**目标**: 把认知域的缺口全部填上。

- [ ] **接入 `/experiments`** — 新增「实验记录」tab（与「内心独白」「建议盒子」并列）
  - 表格列：时间、假设、baseline、 treatment、delta、p-value、decision
  - 点击行弹窗展示 `/experiments/{id}` 详情
- [ ] **接入 `/daemon/history`** — 在健康卡片下方添加 latency 趋势图（纯 CSS 柱状图，不引入图表库）
- [ ] **Goals 管理** — 在「当前目标」卡片顶部添加「+ 添加目标」按钮，调用 `POST /goals`
  - 每个 goal 行右侧添加「完成」按钮，调用 `DELETE /goals/{id}`
- [ ] **Tasks 管理** — 在「任务队列」卡片顶部添加「+ 提交任务」按钮，调用 `POST /tasks`
  - 每个 task 行右侧添加「取消」按钮，调用 `DELETE /tasks/{id}`

**验收**: `/cognition` 页可以完整管理 goals/tasks，能看到实验历史和 tick 趋势。

---

### 迭代 4: Settings 页重构（2–3 天）
**目标**: 合并分散的配置入口，补齐缺失的配置 UI。

- [ ] **Settings 改为左侧 tab 导航**:
  - **模型** — 现有的 LLM profiles 卡片（从原 SettingsPage 迁移）
  - **认知** — 新增：autonomy_level slider、heartbeat_hz input、slow_subsystem_threshold input，调用 `PUT /config`
  - **安全** — 新增：guardians policy textarea、secrets 列表（调用 `/secrets` CRUD）
  - **外观** — theme / density / language（已有，迁移）
  - **系统** — 备份列表（从 BackupPage 迁移）+ 一键备份按钮、诊断运行（从 DoctorPage 迁移）、重启/升级按钮（从 AppShell 迁移）
- [ ] **Config 页标记 deprecated** — 添加 banner「此页面已合并到设置，3 秒后跳转」

**验收**: 所有常用配置都能在 `/settings` 完成，不需要手写 JSON。

---

### 迭代 5: Files 页（2 天）
**目标**: 补齐文件管理能力。

- [ ] **新建 `/files` 页面**:
  - 左侧：文件树（调用 `GET /files/roots` + `GET /files?path=`）
  - 右侧：代码编辑器（`textarea` 即可，不需要 Monaco）
  - 顶部面包屑 + 保存按钮（调用 `PUT /files`）
  - 只显示文本文件（≤1MB），二进制文件显示「无法预览」
- [ ] **Workspace roots 管理** — 在 Files 页顶部显示当前 roots，可添加/删除（复用 WorkspacePage 逻辑）

**验收**: 能在浏览器里浏览工作区文件、编辑 `.py`/`.md`/`.json`、保存后后端文件确实变了。

---

### 迭代 6: 体验打磨（2–3 天）
**目标**: 小白友好 + 响应式 + 空状态。

- [ ] **空状态引导卡片** — 以下页面为空时显示引导：
  - Skills: 「还没有技能？去商店看看 或 创建第一个」
  - Sessions: 「还没有会话？去对话页开始聊天」
  - Tasks: 「任务队列为空。当 daemon 接到目标时会自动填充」
  - Experiments: 「还没有实验。daemon 会在后台自动运行 A/B 测试」
- [ ] **响应式断点** — 所有数据页网格改为 `minmax(min(100%,320px),1fr)`
- [ ] **移动端 sidebar** — 点击汉堡菜单滑出，点击遮罩关闭，支持左滑手势关闭
- [ ] **加载态统一** — 新增 `Skeleton` atom 组件，替换所有「加载中…」文字
- [ ] **删除旧页面文件** — Backup.js、Doctor.js、Config.js（保留路由重定向）

**验收**: iPhone SE 模拟器下所有页面无横向溢出，空状态有 CTA。

---

## 4. 架构变更

### 4.1 目录结构调整

```
xmclaw/daemon/static/
├── index.html
├── bootstrap.js
├── app.js
├── router.js
├── store.js
├── styles/
│   ├── tokens.css
│   ├── reset.css
│   ├── utilities.css          # NEW: 原子工具类（迭代 6 引入）
│   ├── components.css         # 合并 hermes-*.css
│   └── pages.css              # 合并 chat.css / workspace.css
├── lib/
│   ├── api.js
│   ├── ws.js
│   ├── store.js               # 增强 selector 订阅（迭代 2 后）
│   ├── event_banner.js        # NEW: 全局事件感知层
│   ├── queries.js             # NEW: useQuery hook（可选，迭代 3+）
│   └── icons.js               # 图标 sprite（迭代 6）
├── components/
│   ├── atoms/
│   │   ├── button.js
│   │   ├── badge.js
│   │   ├── spinner.js
│   │   ├── skeleton.js        # NEW
│   │   └── card.js            # NEW: 统一卡片壳
│   ├── molecules/
│   │   ├── ChatSidebar.js
│   │   ├── MessageList.js
│   │   ├── Composer.js
│   │   ├── EmptyState.js      # NEW: 空状态引导卡片
│   │   └── EventBanner.js     # NEW: 全局事件横幅
│   └── organisms/
│       ├── AppShell.js        # 导航重组
│       └── Backdrop.js
└── pages/
    ├── Chat.js
    ├── Sessions.js
    ├── Cognition.js           # 大幅增强
    ├── Skills.js
    ├── Memory.js              # 简化 tabs
    ├── Files.js               # NEW
    ├── Settings.js            # 重构为 tab 导航
    ├── Dashboard.js           # NEW（可选，迭代 6 后）
    ├── Analytics.js
    ├── Trace.js
    ├── Logs.js
    ├── Cron.js
    ├── Marketplace.js
    └── Agents.js              # 可选保留或并入 Chat
```

### 4.2 信息流转图（重构后）

```
用户操作
    │
    ├─→ 前端页面 → apiGet/apiPost → 后端 REST API
    │                              ↓
    │                         数据库 / 状态变更
    │                              ↓
    └─← EventBanner 监听 ←── 事件总线 (event_bus)
           │
           ├─→ toast / banner（全局反馈）
           └─→ 页面级徽章刷新（如 Skills 页版本号）
```

---

## 5. 关键技术决策

| 决策 | 选择 | 理由 |
|------|------|------|
| 构建工具 | ❌ 不引入 | 保持 ADR-001，无 Node.js 依赖 |
| 类型系统 | JSDoc `@typedef` + `tsc --noEmit` | 零运行时成本，IDE 有补全 |
| 样式 | 手写 utility class + 内联 style 做动态值 | 不引入 Tailwind，控制体积 |
| 图表 | 纯 CSS 柱状图 / 表格 | 不引入 Chart.js，减少依赖 |
| 状态管理 | 保持现有 store，增加 selector 订阅 | 够用，不引入 Signals/Zustand |

---

## 6. 验收标准（迭代完成检查表）

### 全局
- [ ] 导航项 ≤ 14 个，无重复/重叠概念
- [ ] 被合并的页面（/config /backup /doctor）访问时自动跳转
- [ ] 移动端 375px 下无横向溢出

### 对齐
- [ ] `/cognition` 页展示 experiments 列表
- [ ] `/cognition` 页可以添加/删除 goals
- [ ] `/cognition` 页可以提交/取消 tasks
- [ ] `/settings` 页可以改认知参数（autonomy/heartbeat/threshold）
- [ ] `/settings` 页可以管理 secrets
- [ ] `/files` 页可以浏览和编辑文本文件
- [ ] approve proposal 后，Evolution/Skills/Cognition 页有反馈

### 体验
- [ ] 所有列表页有空状态引导卡片
- [ ] 加载态有 skeleton，不是纯文字
- [ ] config 热重载后有 toast 反馈

---

## 7. 风险与回滚

| 风险 | 缓解 |
|------|------|
| 重构期间引入回归 | 每迭代完成后跑 `pytest tests/unit/test_v2_ui_scaffold.py` + 手动 smoke |
| 导航重组用户不适应 | 被合并的页面保留 30 天重定向，不直接 404 |
| Settings 页过于庞大 | 如果 settings tabs 超过 7 个，拆出 `/system` 页面 |

---

## 8. 立即开始

**当前状态**: 已完成 Phase A–E（后端能力齐全，前端骨架完整）。

**下一步**: 从 **迭代 1（清理与骨架）** 开始实施。

预估总工期: **10–14 天**（6 个迭代，含测试和打磨）。
