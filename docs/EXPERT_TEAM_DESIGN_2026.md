# 专家团（Expert Team）设计与开发文档 — 2026-06-17

> 借鉴 WorkBuddy「专家团」，在 XMclaw 现有多智能体引擎之上补一层**可视、可配、像真团队**的产品/UX。
> 本文是实现级开发文档，覆盖 P0/P1/P2 全部设计、事件/数据 schema、文件改动清单与测试计划。
> 状态：📐 设计已定，P0 待开工。

---

## 0. 背景与动机

WorkBuddy 的「专家团」= 一个**有组长的多智能体团队**：组长自动**拆解**任务 → 按专长**分配**给成员 →
成员**并行**执行 → 组长**整合**结果（来源：腾讯云 WorkBuddy 文档/实测）。本质是 orchestrator-worker
多智能体 + 命名领域专家 + 看团队干活的 UI。

用户希望借鉴。调研后的关键结论：**XMclaw 的引擎已经齐全，不需要重造**——差距在产品/UX 层。

---

## 1. 现状盘点（已有，复用，不重写）

| 能力 | XMclaw 现状 | 文件 |
|---|---|---|
| 任务拆解 | LLM 驱动 DAG 分解 | `xmclaw/cognition/htn_planner.py` |
| 分配+并行+整合 | decompose→load-balance→schedule(依赖)→aggregate(concat/vote/map_reduce) | `xmclaw/daemon/swarm_orchestrator.py` |
| 临时专家并行 | `parallel_subagents` 工具：2-8 子任务并发、各自隔离上下文、合成 | `xmclaw/providers/tool/builtin_subagent.py` |
| 角色特化 | 5 个角色 general/code/research/ops/comm，各带专属 system prompt | `builtin_subagent._ROLE_HINTS` |
| 长驻 worker | 注册式 worker 管理 | `xmclaw/daemon/multi_agent_manager.py`、`xmclaw/core/multi_agent/manager.py` |
| 模式触发 | swarm 模式（"并行/对比 A B C"等线索触发，opt-in） | `xmclaw/cognition/mode_router.py` |
| 生命周期事件 | `SUBAGENT_STARTED` / `SUBAGENT_COMPLETED`（每 leaf 一条） | `xmclaw/core/bus/events.py:471` |
| 前端内联渲染 | 子代理在 Timeline 内联成可折叠"Agent {role} #{idx}"组 | `webui/src/components/ToolCards.tsx:361`、`webui/src/lib/reducer.ts:786` |

### 已有事件 payload（关键）
- `subagent_started`：`{index, subtask, role, specialist}`
- `subagent_completed`：`{index, subtask, role, ok, output, error, hops, elapsed_s}`
- 发布机制：`builtin_subagent._publish_subagent_event(kind, **payload)`，用
  `getattr(EventType, kind.upper())` 解析事件类型，best-effort（无 bus/session 时 no-op）。

---

## 2. 差距分析（WorkBuddy 强在哪 = 要借鉴的）

1. **没有显式「组长」框架**：现在只有 per-subagent 的 started/completed，没有一条携带"组长目标 +
   完整拆解计划"的事件，用户看不到"组长把任务拆成了哪几块、分给了谁"。
2. **没有专门的「专家团」看板**：子代理只在聊天 Timeline 内联，淹没在消息流里；不是一个能"盯着团队
   干活"的结构化面板。
3. **专家只有 5 个泛角色**，不是 WorkBuddy 那种命名领域专家（PPT 专家/数据分析师/文案…，各带工具子集
   + 人设），用户不能配置自己的专家团。
4. **不是一等入口**：swarm 靠 config + 启发式触发，没有明确的"派专家团"affordance。

→ 借鉴的是"**显式组长 + 命名专家 + 看团队的 UI + 一等入口**"这层产品化，引擎不动。

---

## 3. 设计目标 / 非目标

**目标**
- P0：一个独立的 Mission Control「专家团」视图，把现有 fanout 事件重组成结构化团队看板（组长目标 +
  拆解计划 + 专家卡片墙 + 实时状态 + 整合结果）。
- P1：命名领域专家 roster（人设 + 工具子集 + 模型），用户可增删，组长按 roster 分派。
- P2：一等「派专家团」入口（按钮/指令显式触发 swarm）。

**非目标**
- 不重写拆解/调度/合成引擎（`htn_planner` / `swarm_orchestrator` / `parallel_subagents` 全部复用）。
- 不引入新的长驻进程或外部依赖。
- P0 不做专家配置（沿用 5 个内置角色）。

---

## 4. 架构总览

```
用户任务
  │
  ▼
mode_router → swarm 模式（P2 加显式入口）
  │
  ▼
组长（主 AgentLoop）── 调 parallel_subagents 工具 ──┐
  │  发 FANOUT_STARTED(goal, plan)  ← P0 新增          │
  │                                                    ▼
  │                                   _fanout：并行跑 N 个子代理（角色特化）
  │                                     每个发 SUBAGENT_STARTED / _COMPLETED（已有）
  ▼
组长合成结果（concat / llm）
  │
  ▼  事件流（WS）
webui reducer → store(chat.entries: kind=fanout|subagent)
  │
  ▼
「专家团」视图(TeamView)：组长卡 + 专家卡片墙（实时）   ← P0 新增
专家 roster 配置                                        ← P1
「派专家团」入口                                        ← P2
```

**核心思路**：引擎产出的事件已经够用，P0 主要是"把事件重组成看板"的前端工作 + 一个小的组长事件；
P1/P2 才动到后端的角色解析与触发。

---

## 5. P0：专家团可视化看板（实现级）

### 5.1 新事件 `FANOUT_STARTED`

补一条携带"组长目标 + 完整拆解计划"的事件，作为看板的"组长头"。

**EventType**（`xmclaw/core/bus/events.py`，紧邻 `SUBAGENT_STARTED`）：
```python
FANOUT_STARTED = "fanout_started"
```

**payload schema**：
```jsonc
{
  "goal": "string",            // call.args["goal"]，可能为空字符串
  "total": 3,                  // 子任务数
  "synthesis": "concat|llm",   // 合成策略
  "plan": [                    // 拆解计划，组长视角
    { "index": 0, "role": "research", "subtask": "...", "specialist": "" },
    { "index": 1, "role": "code",     "subtask": "...", "specialist": "" }
  ]
}
```

**发布点**（`builtin_subagent.py::invoke`，解析完 roles/specialists、进入 `_fanout` 之前，约 line 372）：
```python
await self._publish_subagent_event(
    "fanout_started",
    goal=goal, total=len(subtasks), synthesis=synthesis,
    plan=[
        {"index": i, "role": roles[i], "subtask": subtasks[i],
         "specialist": specialists[i]}
        for i in range(len(subtasks))
    ],
)
```
复用现有 `_publish_subagent_event`（`getattr(EventType,"FANOUT_STARTED")` 自动命中）。best-effort，
无 bus/session 时 no-op。`SUBAGENT_*` 不变。

> 注：嵌套 fanout（subagent 内部再 fanout，`_run_nested_fanout`）P0 不单独处理——它们仍发各自的
> subagent 事件；组长头只对顶层 fanout 发。后续可加 `depth` 字段区分。

### 5.2 前端数据模型（已有 + 新增）

store 里 `chat.entries`（`webui/src/lib/types.ts` 的 `Entry`）已有 `kind:"subagent"` 条目，字段：
`subagentIndex / roleHint / promptPreview / status(running|ok|error) / outputPreview / errorPreview /
hops / elapsedSeconds`。

**新增**：
- `Entry.kind` 联合加 `"fanout"`。
- `Entry` 加可选字段：`goal?: string`、`plan?: Array<{index, role, subtask, specialist}>`、
  `total?: number`、`synthesis?: string`。

### 5.3 reducer 改动（`webui/src/lib/reducer.ts`）

加一个 case（`subagent_started/completed` 现有 case 不动）：
```ts
case "fanout_started": {
  const id = `fanout_${ts}`;
  if (chat.entries.some((e) => e.id === id)) return chat;
  return {
    ...chat,
    entries: chat.entries.concat({
      id, role: "system", kind: "fanout", content: "",
      ts, status: "running",
      goal: str(payload.goal),
      total: (payload.total as number) || 0,
      synthesis: str(payload.synthesis),
      plan: (payload.plan as Entry["plan"]) || [],
    }),
  };
}
```

### 5.4 TeamView 组件（`webui/src/views/TeamView.tsx`，主体）

镜像 `webui/src/views/FilesView.tsx` 的接入与样式（mc-* 主题类、`useApp` 选择器）。

**数据**：从 store 选 `chat.entries`，过滤 `kind === "fanout" || kind === "subagent"`，按时间分组成
**批次（round）**——每个 `fanout` 条目开一个批次，其后到下一个 `fanout` 之间的 `subagent` 条目归属该
批次；没有 `fanout` 头的散落 subagent 归到一个"未分组"批次（向后兼容旧事件）。

**布局**：
- 顶栏：标题「👥 专家团」+ 副标题「组长拆解 · 并行执行 · 整合」。
- 批次列表（最新置顶，历史折叠）：
  - **组长卡**（每批次顶部）：目标 `goal`、专家数 `total`、计数徽章「✓ 完成 N · ⏳ 进行 M · ✗ 失败 K」、
    总耗时（max(elapsedSeconds)）、合成策略。
  - **专家卡片墙**（grid，响应式 2~3 列）：每个 subagent 一张卡：
    - 角色图标+中文名（`ROLE_META`），`#index`。
    - 子任务 `promptPreview`（截断，hover 全文）。
    - 状态徽章：running→旋转点、ok→✓ 绿、error→✗ 红。
    - `hops` / `elapsedSeconds`。
    - 可展开 `outputPreview`（成功）/ `errorPreview`（失败）。
  - 批次底部：整合结果占位（P0 用最后的合成文本——见 5.8 备注；或先留"组长整合中/已整合"标记）。
- 空态：大图标 + 「还没有专家团在跑。发个『并行对比 A/B/C』之类的任务，组长会拆给专家们并行做」。

**角色元信息**（仿 FilesView `ROOT_ICON`）：
```ts
const ROLE_META: Record<string, { icon: string; label: string; tone: string }> = {
  general:  { icon: "🧩", label: "通用",   tone: "text-mc-muted" },
  code:     { icon: "💻", label: "代码",   tone: "text-blue-400" },
  research: { icon: "🔬", label: "研究",   tone: "text-emerald-400" },
  ops:      { icon: "🛠", label: "运维",   tone: "text-amber-400" },
  comm:     { icon: "✍️", label: "文案",   tone: "text-pink-400" },
};
```

### 5.5 导航接入（同 FilesView 那批）
- `webui/src/store/app.ts`：`view` 联合类型加 `"team"`。
- `webui/src/components/TaskRail.tsx`：`DOMAINS` 加 `{ key:"team", label:"专家团", icon:"👥" }`；
  grid `grid-cols-5` → `grid-cols-6`。
- `webui/src/App.tsx`：`const TeamView = lazy(...)` + `{view === "team" && <TeamView/>}`。
- `webui/src/components/SlashMenu.tsx`：加 `{ cmd:"/team", desc:"打开专家团", run:(a)=>a.setView("team") }`。

### 5.6 文件改动清单（P0）
后端：
- `xmclaw/core/bus/events.py` — 加 `FANOUT_STARTED`。
- `xmclaw/providers/tool/builtin_subagent.py` — `invoke()` 发 `fanout_started`。
- `tests/unit/test_v2_subagent_fanout.py`（或并入既有 subagent 测试）— 断言事件发布。
前端：
- 新增 `webui/src/views/TeamView.tsx`。
- `webui/src/lib/reducer.ts` — 加 `fanout_started` case。
- `webui/src/lib/types.ts` — `Entry.kind` 加 `"fanout"` + 新字段。
- `webui/src/store/app.ts`、`components/TaskRail.tsx`、`App.tsx`、`components/SlashMenu.tsx` — 导航接入。
- 构建产物 `xmclaw/daemon/webui_dist/`（`npm run build`）。

### 5.7 测试（P0）
- 后端单测：构造 `ParallelSubagentsToolProvider`（mock LLM + in-proc bus 订阅），调用
  `parallel_subagents`，断言收到 1 条 `fanout_started`（goal/total/plan 正确）+ N 条 subagent 事件。
  跑 `tools` lane。
- 前端：`npm run build`（tsc + vite）通过、产出 `TeamView` chunk、无 tsc 报错。
- 端到端（硬刷新后）：发"并行对比 X/Y/Z" → 打开「专家团」→ 组长卡 + 专家卡实时刷新 → 完成显示整合。

### 5.8 备注 / 已知取舍
- **整合结果**：合成文本目前在工具 `ToolResult` 里（`summary.result`），不在事件流。P0 可先不在看板
  显示整合全文（或显示"已整合，见聊天")；P1 可加 `FANOUT_COMPLETED{result}` 事件补全闭环。
- **历史持久化**：看板数据来自 store 的 `chat.entries`（内存/会话级）。刷新后历史批次依赖 WS 重放/水化；
  P0 不做独立持久化。

---

## 6. P1：命名专家 roster（实现级）

把 5 个泛角色升级为**用户可配置的命名领域专家**，组长按 roster 分派。

### 6.1 专家定义（数据模型）
一个「专家」= 人设 + 工具子集 + 模型 + 触发关键词。落盘在 `config.json` 的新块 `experts`（或
`personas/experts/*.md` frontmatter，复用文件域可编辑）：
```jsonc
// config.json
"experts": {
  "enabled": true,
  "roster": [
    {
      "id": "ppt",
      "name": "PPT 专家",
      "icon": "📊",
      "system_prompt": "你是演示文稿专家：结构清晰、要点凝练、善用 pptx 工具…",
      "tools": ["generate_image", "bash", "file_write"],   // 工具子集（空=继承父）
      "model_capability": null,                              // 可选 specialist 能力
      "keywords": ["ppt", "幻灯片", "演示", "slides"]          // 组长分派提示
    },
    { "id": "data", "name": "数据分析师", "icon": "📈", "system_prompt": "…",
      "tools": ["bash", "code_python"], "keywords": ["数据", "分析", "csv", "图表"] }
  ]
}
```

### 6.2 后端改动
- `builtin_subagent.py`：
  - `_ROLE_HINTS` 从硬编码 5 个 → 动态合并 roster（保留 5 个内置作为兜底）。`roles` 参数的 enum 扩成
    `内置 + roster id`。`_do_run_one` 按专家定义注入 `system_prompt` + 限定 `tools` 子集 + 选 `model`。
  - 工具子集：构造子代理用的 ToolProvider 时按 `tools` 过滤（复用现有 `effective_tools` 路径）。
- 组长侧（prompt）：在 `parallel_subagents` 工具描述 / 系统提示里**注入可用专家清单**（id+name+keywords），
  让组长按专长选 `roles`。落点：`builtin_subagent._PARALLEL_SUBAGENTS_SPEC.description` 动态拼接，
  或 `prompt_builder` 注入。
- factory：从 `config.experts.roster` 解析专家定义，注入 `ParallelSubagentsToolProvider`（构造参数）。
- `model_capabilities` / specialist：复用 Phase 11 的 `specialist_models` 能力路由。

### 6.3 前端改动
- 「专家团」视图加一个「**专家管理**」子标签（或复用文件域编辑 `personas/experts/*.md`）：列出 roster、
  增删改专家（name/icon/prompt/tools/keywords）。读写走已有 `/api/v2/files`（personas 根可写）或新增
  `/api/v2/experts` CRUD。
- 卡片墙的角色图标/名改为 roster 驱动（fallback 到内置 ROLE_META）。

### 6.4 文件改动清单（P1）
- `daemon/config.example.json` — `experts` 块样例。
- `xmclaw/daemon/factory.py` — 解析 roster → 注入 subagent provider。
- `xmclaw/providers/tool/builtin_subagent.py` — 动态角色/工具子集/模型。
- `xmclaw/daemon/prompt_builder.py` 或工具描述 — 注入可用专家清单。
- 前端：专家管理 UI + roster 驱动的卡片元信息。
- 测试：roster 解析、动态角色注入、工具子集限定。

---

## 7. P2：一等「派专家团」入口

让"派专家团"成为显式可触发，而非靠启发式猜。

- **Composer 入口**：发送框旁加「👥 派专家团」开关/按钮（类似现有 plan/think 开关），打开后本回合强制
  swarm 模式。落点：`webui` Composer + `store/app.ts`（加 `teamMode` 标志，随帧发 `mode:"swarm"`）。
- **指令**：`/team-dispatch <任务>` 或在 SlashMenu 增项，显式触发。
- **后端**：`mode_router` 接受显式 `forced_mode="swarm"`（已有 `forced` 字段，确认透传）；
  `agent_loop` 在 swarm 模式下主动提示组长用 `parallel_subagents`。
- 文件：`webui` Composer/store；`xmclaw/cognition/mode_router.py`；`xmclaw/daemon/agent_loop.py`（确认
  swarm 模式的工具 prominence）。

---

## 8. 事件 / 数据模型汇总

| 事件 | 时机 | payload | 状态 |
|---|---|---|---|
| `fanout_started` | 组长进入 fanout 前 | `{goal,total,synthesis,plan:[{index,role,subtask,specialist}]}` | P0 新增 |
| `subagent_started` | 每个子代理启动 | `{index,subtask,role,specialist}` | 已有 |
| `subagent_completed` | 每个子代理结束 | `{index,subtask,role,ok,output,error,hops,elapsed_s}` | 已有 |
| `fanout_completed` | 组长合成后（可选） | `{goal,result,completed,failed,total,elapsed_s}` | P1 可选，补整合闭环 |

前端 `Entry`（store）：`kind: "fanout" | "subagent"`，批次分组规则见 §5.4。

---

## 9. 测试与验收

**P0 验收**
- [ ] `parallel_subagents` 触发时发 1 条 `fanout_started`（goal/total/plan 正确）。
- [ ] `npm run build` 通过，产出 `TeamView` chunk，无 tsc 报错。
- [ ] 端到端：fanout 任务 → 专家团视图组长卡 + 专家卡实时刷新 → 完成显示状态。
- [ ] ruff + import 方向通过；现有 subagent/reducer 测试不回归。

**P1 验收**
- [ ] config.experts.roster 解析正确，子代理按专家定义注入 prompt/工具子集/模型。
- [ ] 组长能看到可用专家清单并据此分派。
- [ ] 专家管理 UI 增删改生效（落盘 + 重启后保留）。

**P2 验收**
- [ ] Composer「派专家团」开关强制 swarm，端到端触发 fanout。

---

## 10. 风险与开放问题
- **整合结果闭环**：P0 看板暂不显示整合全文（在 ToolResult 里）。是否 P0 就加 `fanout_completed`
  事件？倾向 P1 补，P0 先把"团队在干活"做出来。
- **嵌套 fanout 的看板归属**：P0 只对顶层发组长头；深层 subagent 仍显示但不单独成批次。需要的话加 `depth`。
- **历史持久化**：看板依赖 store 内存数据，刷新后靠 WS 重放。是否需要独立持久化（events.db 已有，可后补
  一个 `/api/v2/team/history` 查询）。
- **专家工具子集的安全**：P1 给专家限定工具子集时，必须经现有 tool_guard / 沙箱（不绕过）。

---

## 11. 里程碑拆分（建议提交粒度）
1. **P0-a 后端**：`FANOUT_STARTED` 事件 + 发布 + 单测（1 commit）。
2. **P0-b 前端**：TeamView + reducer/types + 导航 + build（1 commit）。
3. **P1-a 后端**：roster 解析 + 动态角色/工具子集 + 组长清单注入（1 commit）。
4. **P1-b 前端**：专家管理 UI（1 commit）。
5. **P2**：派专家团入口（1 commit）。

每个里程碑独立可验证、独立提交（Phase 10 UI 改动按开发纪律引用 Phase + 更新 JARVIS_PLAN 进度日志）。
