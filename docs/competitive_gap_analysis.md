# XMclaw v1.0.0 竞品差距分析

> **分析日期**: 2026-05-20  
> **基准版本**: XMclaw `eb13c08` (JARVIS J2/J3 完整交付)  
> **对标竞品**: Codex CLI (`free-code-main` 本地镜像), OpenClaw (`openclaw/openclaw`), Hermes Agent (`NousResearch/hermes-agent`)  
> **审计维度**: (1) 基础模块完整度  (2) 信息流 / 输出质量  (3) 详细流程与提示词工程

---

## 1. 执行摘要

XMclaw 在**本地语义代码库索引**（tree-sitter + LanceDB/sqlite-vec）和**分层意图预测**（Rule→Statistical→LLM）上拥有差异化优势，但在**用户记忆闭环**、**上下文压缩**、**技能自进化**、**多平台连接器**、**Prompt 缓存与分层架构**等关键模块上，与三款主流竞品存在显著差距。Codex CLI 在编码场景的系统提示词工程与上下文管理上最为成熟；Hermes 在自进化技能与记忆冻结策略上技术深度最高；OpenClaw 在多通道交付与终端用户体验覆盖面上领先。

---

## 2. 维度一：基础模块完整度

### 2.1 核心 Agent 循环与运行时

| 能力 | XMclaw | Codex CLI | OpenClaw | Hermes | 差距评级 |
|------|--------|-----------|----------|--------|----------|
| **对话循环** | `AgentLoop.run_turn()` 基础多轮 | `QueryEngine` 全生命周期管理（token budget、task budget、interrupt、recovery、grace call） | Gateway 多 session 路由 + 平台适配 | `run_conversation()` 同步循环 + 中断检查 + budget 跟踪 | **高** |
| **上下文压缩** | ❌ 无自动压缩；对话无限增长 | ✅ `autoCompact` / `reactiveCompact` / `contextCollapse` / `snipCompact` 四级压缩；`buildPostCompactMessages` | 自动 summarization | ✅ `context_compressor.py` 自动摘要 + 7 天 curator 归档 | **高** |
| **Token Budget 管理** | ❌ 无逐 turn / 逐 session budget | ✅ `createBudgetTracker` / `checkTokenBudget` / `incrementBudgetContinuationCount` + 500k auto-continue | 无公开细节 | ✅ `iteration_budget.remaining` + 工具结果存储 budget (`tool_result_storage.py`) | **高** |
| **中断与恢复** | WS 连接断线即终止 | ✅ `AbortController` + `stopHooks` + `orphanedPermission` 恢复 + SDK 断线续传 | Gateway 持久化 | ✅ Session SQLite 持久化 + 任意退出路径自动保存 | **中** |
| **最大输出恢复** | ❌ 无 | ✅ `MAX_OUTPUT_TOKENS_RECOVERY_LIMIT = 3` 的恢复循环 | 无公开细节 | 通过 LLM 客户端重试 | **中** |

**关键发现**：XMclaw 的 `JarvisOrchestrator` 仅做了 trivial/complex 路由，对话循环本身仍停留在基础 `AgentLoop`。三款竞品均实现了生产级的对话生命周期管理，尤其是 Codex 的**四级上下文压缩**和 Hermes 的**工具结果预算限制**，是支撑长会话的核心基建。

### 2.2 记忆与用户画像

| 能力 | XMclaw | Codex CLI | OpenClaw | Hermes | 差距评级 |
|------|--------|-----------|----------|--------|----------|
| **跨会话持久记忆** | `ProjectMemoryProvider` 仅注入代码库约定；无用户画像 | `memdir` 自动记忆 + `CLAUDE.md` 注入；`loadMemoryPrompt` | `MEMORY.md` / `USER.md` 注入 | ✅ **双存储**：`MEMORY.md`（环境/约定）+ `USER.md`（用户偏好/习惯），带字符上限（2200/1375） | **高** |
| **记忆冻结策略** | 每次查询重新索引（无冻结） | 缓存 `getUserContext` / `getSystemContext`，但 mid-session 可更新 | 无公开细节 | ✅ **Frozen Snapshot**：session 启动时捕获，mid-session 写盘但不改 system prompt，保护 prefix cache | **高** |
| **记忆安全扫描** | ❌ 无 | 无公开扫描 | 无公开细节 | ✅ `_scan_memory_content()`：检测 prompt injection、invisible unicode、数据外泄 | **高** |
| **记忆闭环** | ❌ 被动查询 | 被动加载 | 被动加载 | ✅ **Background Review**：≥10 轮工具调用后触发后台 review agent，自动更新记忆和技能 | **极高** |
| **FTS5 会话搜索** | `events.db` 有 WAL+FTS5，但未用于跨会话 recall | 无 | 无公开细节 | ✅ 带 LLM 摘要的 FTS5 session search | **中** |

**关键发现**：XMclaw 的 `ProjectMemoryProvider` 止步于“代码库约定注入”，竞品已全部进入**用户级长期记忆 + 自动维护**阶段。Hermes 的 **frozen snapshot + background review** 是目前开源领域最先进的记忆架构。

### 2.3 技能系统

| 能力 | XMclaw | Codex CLI | OpenClaw | Hermes | 差距评级 |
|------|--------|-----------|----------|--------|----------|
| **技能发现** | `SkillRegistry` append-only，全量加载 | `EXPERIMENTAL_SKILL_SEARCH` + `DiscoverSkillsTool` + prefetch | ClawHub 技能市场 | ✅ **三级披露**：compact index (~3k tokens) → `skill_view(name)` → `skill_view(name, path)` | **高** |
| **技能自创建** | ❌ 无 | ✅ `generateAgent.ts` 内置 agent 创建向导（6 步结构化 prompt） | 模板化 `SOUL.md` | ✅ Background review 可自动 CREATE/UPDATE 技能；命名强制为 class-level umbrella | **高** |
| **技能自进化** | ❌ 无 | ❌ 无（静态 agent） | ❌ 无 | ✅ **GEPA** (Genetic-Pareto Prompt Evolution)：离线 DSPy 进化 pipeline，LLM-as-judge 评分，自动提 PR | **极高** |
| **技能格式** | Python 模块 + JSON manifest | Agent JSON（identifier/whenToUse/systemPrompt） | `SKILL.md` (YAML frontmatter + markdown) | `SKILL.md` (YAML frontmatter，兼容 agentskills.io) | **中** |
| **技能安全** | append-only registry | 无 | 无 | ✅ 安全扫描 + bundled/hub/pinned 技能受保护，review agent 不可修改 | **中** |

### 2.4 多平台连接器

| 能力 | XMclaw | Codex CLI | OpenClaw | Hermes | 差距评级 |
|------|--------|-----------|----------|--------|----------|
| **CLI/TUI** | ✅ `textual` TUI（基础 session tree + message stream） | ✅ `ink` React TUI（多行编辑、slash 命令、历史、流式输出） | ✅ CLI + Control UI | ✅ `rich` TUI + Ink JSON-RPC 后端 | **中** |
| **消息平台** | ❌ 仅本地 WebSocket | ❌ 仅本地/SDK | ✅ **20+ 通道**：WhatsApp/Telegram/Slack/Discord/iMessage/Signal/Teams/微信/QQ 等 | ✅ Telegram/Discord/Slack/WhatsApp/Signal | **极高** |
| **Voice I/O** | ❌ 无 | ❌ 无 | ✅ Voice Wake (macOS/iOS) + Talk Mode (Android) + TTS | ✅ 语音备忘录转录 + TTS | **高** |
| **IDE 集成** | ✅ ACP (JSON-RPC stdio) | ❌ 无（Codex 本身是 IDE） | 无公开细节 | ✅ ACP Adapter (VS Code/Zed/JetBrains) | **低** |
| **移动端** | ❌ 无 | ❌ 无 | ✅ iOS/Android Node (WebSocket 配对) | ❌ 无 | **高** |

### 2.5 工具与执行环境

| 能力 | XMclaw | Codex CLI | OpenClaw | Hermes | 差距评级 |
|------|--------|-----------|----------|--------|----------|
| **代码搜索** | ✅ 语义索引 (`codebase_index`) + LanceDB | LSP/Glob/Grep/FileRead/FileEdit 等大量编码工具 | Browser/Canvas/Nodes | Web search + browser + vision + shell | **低** |
| **沙箱执行** | ❌ 本地进程直接执行 | ❌ 本地执行（权限模式控制） | ✅ Docker/SSH/OpenShell 多后端沙箱 | ✅ **7 后端**：local/Docker/SSH/Singularity/Modal/Daytona/Vercel Sandbox | **高** |
| **MCP 支持** | ❌ 无（仅自有 ACP） | ✅ MCP Server 连接 + 动态指令注入 | 无公开细节 | ✅ MCP 工具发现 + 动态 schema | **高** |
| **浏览器工具** | ❌ 无 | ❌ 无 | ✅ Browser tool | ✅ Browser navigate + 截图 | **高** |
| **任务调度** | `TaskScheduler` + HTNPlanner | `TaskCreateTool` / `TodoWriteTool` | Cron jobs | ✅ 内置 cron scheduler + 自然语言定时任务 | **中** |

---

## 3. 维度二：信息流与输出质量

### 3.1 System Prompt 工程与缓存策略

| 能力 | XMclaw | Codex CLI | OpenClaw | Hermes | 差距评级 |
|------|--------|-----------|----------|--------|----------|
| **Prompt 分层架构** | 单一 system prompt + conventions 注入 | `systemPromptSections.ts`：section 级别缓存控制（`cacheBreak: boolean`） | `SOUL.md` + `AGENTS.md` + `TOOLS.md` 分层注入 | ✅ **10 层架构**：Identity → Guidance → Memory Snapshot → User Profile → Skills Index → Context Files → Timestamp → Platform Hint | **极高** |
| **缓存稳定性** | 每次请求重新组装 | `SYSTEM_PROMPT_DYNAMIC_BOUNDARY` 区分 global cacheable vs user-specific | 无公开细节 | ✅ **Frozen Snapshot**：session 期间 system prompt 不变，保护 prefix cache | **高** |
| **模型特定 Guidance** | ❌ 所有模型共用同一 prompt | 针对不同模型族有 thinking/budget/output 差异配置 | 无公开细节 | ✅ 按模型注入：`TOOL_USE_ENFORCEMENT` / `OPENAI_EXECUTION` / `GOOGLE_OPERATIONAL` / `COMPUTER_USE` | **高** |
| **输出风格控制** | ❌ 无 | ✅ `OutputStyleConfig` 可配置 name + prompt | 无 | 通过 `SOUL.md` 控制 | **中** |
| **安全扫描（Prompt 侧）** | Guardian 4-path 运行时安全 | `CYBER_RISK_INSTRUCTION` 内嵌 | DM pairing 安全 | ✅ `_scan_context_content()`：prompt injection / invisible unicode / exfiltration / hidden HTML divs | **高** |

**Codex CLI 的 `systemPromptSections.ts` 细节**：
- 每个 section 是一个 `{ name, compute, cacheBreak }` 结构
- `systemPromptSection()`：一次计算，缓存到 `/clear` 或 `/compact`
- `DANGEROUS_uncachedSystemPromptSection()`：每轮重新计算，**必须提供 `_reason`** 说明为何打破缓存
- 这种显式缓存控制是 XMclaw 完全缺失的基建

**Hermes 的 10 层 Prompt 细节**（从 `prompt_builder.py`）：
1. Agent Identity (`~/.hermes/SOUL.md`)
2. Tool-aware behavior guidance (`MEMORY_GUIDANCE`, `SESSION_SEARCH_GUIDANCE`, `SKILLS_GUIDANCE`)
3. Honcho static block (可选)
4. Optional system message (config/API)
5. **Frozen MEMORY snapshot** (session 启动捕获)
6. **Frozen USER profile snapshot** (session 启动捕获)
7. **Skills index** (compact category listing, ~3k tokens)
8. **Context files** (`.hermes.md` → `AGENTS.md` → `CLAUDE.md` → `.cursorrules`，向上走到 git root)
9. Timestamp + session
10. Platform hint (CLI, Telegram, Discord...)

### 3.2 上下文注入与代码库感知

| 能力 | XMclaw | Codex CLI | OpenClaw | Hermes | 差距评级 |
|------|--------|-----------|----------|--------|----------|
| **代码库上下文** | ✅ `codebase_index`：tree-sitter chunking + sliding-window fallback + Ollama embedding + LanceDB | `CLAUDE.md` 文件注入 + git status + LSP 符号上下文 | `AGENTS.md` / `CLAUDE.md` 注入 | Context files  walking to git root | **低** |
| **自动索引** | ✅ 首次查询自动索引 pwd | 需手动维护 `CLAUDE.md` | 需手动维护 | 需手动维护 | **优势** |
| **Git 状态注入** | ❌ 无 | ✅ `getGitStatus()`：branch / main branch / status (截断 2k) / recent commits | 无 | 无 | **中** |
| **文件历史快照** | ❌ 无 | ✅ `fileHistoryMakeSnapshot` + `fileStateCache` | 无 | 无 | **中** |
| **工具 schema 动态重建** | ❌ 静态 schema | ❌ 静态 | 无 | ✅ 按实际可用工具动态重建 schema（如 `execute_code` 只列已配置沙箱工具） | **中** |

### 3.3 结果合成与信息呈现

| 能力 | XMclaw | Codex CLI | OpenClaw | Hermes | 差距评级 |
|------|--------|-----------|----------|--------|----------|
| **Worker/Swarm 合成** | `_synthesize()`：简单 system+user prompt，300字限制，失败简述 | Coordinator mode 多 agent 路由；`SyntheticOutputTool` | 多 agent routing | `delegate_task`：spawn subagents，max 3 并发；父 agent 继承 cache | **高** |
| **流式工具输出** | ❌ 无（等待全部完成） | ✅ `StreamingToolExecutor` | 无公开细节 | 流式 chunk 处理 + `StreamingContextScrubber` | **高** |
| **工具结果截断** | `[truncated]` 标记 | `applyToolResultBudget` + 存储预算 | 无 | ✅ 按工具/按 turn aggregate budget | **中** |
| **输出格式化** | 纯文本 / markdown | `OutputStyleConfig` + 多种 render mode | Live Canvas / A2UI | Rich TUI + markdown | **中** |

---

## 4. 维度三：详细流程与提示词工程

### 4.1 Plan → Execute → Review 流程

| 环节 | XMclaw | Codex CLI | OpenClaw | Hermes | 差距评级 |
|------|--------|-----------|----------|--------|----------|
| **Planning** | `PlanEngine.create_plan()`：HTNPlanner → `ExecutionPlan`；`_validate_plan` 仅检查文件存在性 | 无显式 plan 阶段（隐式在对话中通过 tool use 推进） | 隐式推进 | 隐式推进（工具调用即 plan） | **中** |
| **验证与评分** | `HonestGrader`（hard≥0.8, LLM≤0.2）+ codebase context files | `VerificationAgent`（`VERIFICATION_AGENT_TYPE`） | 无 | 无显式 plan 验证，靠工具调用自我纠正 | **低** |
| **执行监控** | `WorkerSwarm.execute_plan()`：topo-sort + `asyncio.gather`，max_workers=4 | 单 agent 多轮工具调用；Coordinator mode 多 agent | Gateway session 路由 | 同步循环 + 后台 review | **中** |
| **事后复盘** | ❌ 无 | ❌ 无 | ❌ 无 | ✅ **Background Review**： qualifying turns 后 fork review agent，记忆+技能双维度更新 | **极高** |

**Hermes Background Review 的 Prompt 结构**（来自 `agent/background_review.py`）：
- 三个 review prompt 模板：`_MEMORY_REVIEW_PROMPT`、`_SKILL_REVIEW_PROMPT`、`_COMBINED_REVIEW_PROMPT`
- Review agent 的 action space 被严格限制为 `memory` + `skill_manage` 两个 toolset
- Skill review 的优先级顺序：
  1. UPDATE 当前加载的 skill
  2. UPDATE 现有 umbrella skill
  3. ADD support file (`references/`, `templates/`, `scripts/`)
  4. CREATE 新的 class-level umbrella skill
- 明确禁止创建 session artifact（如 `fix-PR-123` 这种一次性 skill）

### 4.2 Agent / Worker 创建流程

| 环节 | XMclaw | Codex CLI | OpenClaw | Hermes | 差距评级 |
|------|--------|-----------|----------|--------|----------|
| **动态 Agent 创建** | ❌ 无（WorkerAgent 是代码硬编码） | ✅ `generateAgent.ts`：6 步结构化 prompt（Core Intent → Persona → Instructions → Performance → Identifier → Examples） | `SOUL.md` + 模板 | `delegate_task` spawn subagent | **高** |
| **Agent 记忆继承** | ❌ 无 | Agent 可携带 memory instructions | 无 | Review agent 继承 parent 的 `_cached_system_prompt` + credentials | **中** |
| **Agent 使用示例** | ❌ 无 | `generateAgent.ts` 强制要求输出 `<example>` 块，说明何时调用 Agent tool | 无 | 无 | **中** |

**Codex CLI 的 Agent 创建 Prompt 节选**（来自 `generateAgent.ts`）：
- System prompt 本身就是一个完整的 agent architect：
  - "Extract Core Intent"（显式+隐式需求）
  - "Design Expert Persona"
  - "Architect Comprehensive Instructions"（行为边界、方法论、edge cases、输出格式）
  - "Optimize for Performance"（决策框架、质量控制、fallback 策略）
  - "Create Identifier"（2-4 词，小写+连字符）
  - 强制要求包含 `<example>` 块，说明何时应调用 `${AGENT_TOOL_NAME}`
- 若用户提到 memory/remember/learn，自动追加 `AGENT_MEMORY_INSTRUCTIONS` 段落

### 4.3 Proactive / Intent 流程

| 环节 | XMclaw | Codex CLI | OpenClaw | Hermes | 差距评级 |
|------|--------|-----------|----------|--------|----------|
| **Proactive 触发** | `IntentEngine`：Rule (O(1)) → Statistical (EWMA+bigram) → LLM (stubbed) | `proactiveModule` / `KAIROS` / `BriefTool` | Voice Wake + Cron | Background review（被动复盘型，非主动预测） | **中** |
| **事件订阅** | ✅ EventBus 订阅，emit `PROACTIVE_INTENT_DETECTED` | 系统级 hook / event | Gateway event 路由 | 同步循环内检查 | **低** |
| **意图标签映射** | `_INTENT_LABELS`：slug → 中文标签 | 无公开细节 | 无 | 无 | **优势** |
| **24h 事件摘要** | ❌ Layer 3 stubbed | 无 | 无 | 无 | **中** |

### 4.4 提示词安全与治理

| 能力 | XMclaw | Codex CLI | OpenClaw | Hermes | 差距评级 |
|------|--------|-----------|----------|--------|----------|
| **运行时安全** | ✅ Guardian 4-path 安全模型 | 权限模式（auto/ask/none）+ 文件系统权限 | DM pairing + sandbox | 权限模式 + sandbox | **低** |
| **Prompt Injection 防御** | ❌ 无（依赖模型自身） | `CYBER_RISK_INSTRUCTION` + 工具结果注入检测 | DM 配对码 | ✅ `_scan_context_content()`：多模式扫描 + 拦截 | **高** |
| **Invisible Unicode 防御** | ❌ 无 | ❌ 无 | ❌ 无 | ✅ 扫描 zero-width joiners、directionality marks | **高** |
| **错误信息脱敏** | ✅ ACP：`Internal server error` / `Tool execution failed` | 客户端侧脱敏 | 无 | Tool error 中剥离 XML role tags / CDATA / markdown fences | **低** |
| **工具参数强制转换** | ❌ 无 | ❌ 无 | ❌ 无 | ✅ `coerce_tool_args()`：string→number/boolean/array 自动转换 | **中** |

---

## 5. 差距优先级矩阵

| 优先级 | 差距项 | 影响 | 预估工作量 | 建议阶段 |
|--------|--------|------|------------|----------|
| **P0** | **上下文压缩 / Token Budget** | 长会话不可用，context window 耗尽即崩溃 | 2-3 周 | J3 热修复 |
| **P0** | **用户级跨会话记忆（MEMORY.md + USER.md）+ Frozen Snapshot** | 用户体验从“每次从零开始”跃迁到“持续积累” | 2 周 | J3 热修复 |
| **P0** | **System Prompt 分层架构 + 缓存控制** | 每轮浪费大量 token，API 成本高，延迟大 | 1-2 周 | J3 热修复 |
| **P1** | **Background Review（事后复盘 → 记忆/技能更新）** | 自进化闭环的核心 | 3-4 周 | J4 |
| **P1** | **技能三级披露（Progressive Disclosure）** | 长 skill list 直接撑爆 context | 1 周 | J4 |
| **P1** | **模型特定 Guidance 注入** | 不同模型（GPT/Claude/本地）行为差异大，单一 prompt 无法最优 | 1 周 | J4 |
| **P1** | **多平台连接器（Telegram/Discord/Slack 等）** | 从“开发工具”扩展到“个人助手”的关键 | 3-4 周 | J4 |
| **P2** | **Prompt 安全扫描（Injection + Invisible Unicode）** | 生产环境必要防护 | 1 周 | J4 |
| **P2** | **MCP 支持** | 生态兼容性；XMclaw 已有 ACP，MCP 是行业标准 | 2 周 | J4 |
| **P2** | **Worker/Swarm 合成质量提升** | 当前合成 prompt 过于简陋，输出质量差 | 3 天 | J3 热修复 |
| **P2** | **Agent 创建向导（类似 Codex `generateAgent.ts`）** | 降低用户创建自定义 agent 的门槛 | 1-2 周 | J4 |
| **P3** | **Voice I/O** | 移动端/无键盘场景 | 2-3 周 | J5 |
| **P3** | **Live Canvas / 可视化输出** | 高端体验差异化 | 3-4 周 | J5 |
| **P3** | **GEPA 离线进化** | 长期技术壁垒 | 4-6 周 | J5+ |
| **P3** | **浏览器工具 / 沙箱多后端** | 通用 agent 能力扩展 | 2-4 周 | J5 |

---

## 6. 技术细节附录

### 6.1 Codex CLI `QueryEngine` 关键状态机

```
submitMessage()
  → buildSystemPrompt (resolve sections, cache hit/miss)
  → prependUserContext / appendSystemContext
  → processUserInput (skill prefetch, memory load)
  → query() loop:
      while budget.remaining > 0 || grace_call:
        → API call (streaming)
        → if tool_calls: runTools() via StreamingToolExecutor
        → if max_output_tokens: recovery loop (up to 3 retries)
        → autoCompact / reactiveCompact if threshold exceeded
        → yield messages
  → stopHooks (save state, flush session storage)
```

### 6.2 Hermes Prompt Builder 10 层堆叠顺序

```
[Layer 1]  DEFAULT_AGENT_IDENTITY  ← 可被 ~/.hermes/SOUL.md 覆盖
[Layer 2]  TOOL_USE_ENFORCEMENT_GUIDANCE / MEMORY_GUIDANCE / SESSION_SEARCH_GUIDANCE / SKILLS_GUIDANCE
[Layer 3]  Honcho static block (optional)
[Layer 4]  Optional system message (config / API)
[Layer 5]  Frozen MEMORY.md snapshot (captured at load)
[Layer 6]  Frozen USER.md snapshot (captured at load)
[Layer 7]  Skills index (compact listing)
[Layer 8]  Context files (.hermes.md → AGENTS.md → CLAUDE.md → .cursorrules)
[Layer 9]  Timestamp + Session ID
[Layer 10] Platform hint (CLI / Telegram / Discord ...)
```

**关键不变量**：Layer 5-6 在 session 期间绝不修改；Layer 7 使用 mtime/size 校验的磁盘快照缓存。

### 6.3 XMclaw 当前 Orchestrator 路由逻辑

```
handle_message(session_id, user_message)
  → _is_trivial(user_message) ? threshold=0.7
    → TRUE  → AgentLoop.run_turn() [direct]
    → FALSE → PlanEngine.create_plan()
              → if None: AgentLoop.run_turn() [direct]
              → else:  WorkerSwarm.execute_plan()
                        → topo_sort tasks
                        → asyncio.gather(max_workers=4)
                        → _synthesize()  [简单 LLM 合成]
```

**差距**：没有 context compression、没有 token budget、没有 recovery loop、没有 background review。

---

## 7. 结论与建议

### 立即行动（J3 热修复窗口）
1. **引入 Context Compression**：在 `AgentLoop` 中集成基于 token 计数的自动摘要，参考 Codex 的 `buildPostCompactMessages` 模式。
2. **实现 Frozen Snapshot Memory**：创建 `~/.xmclaw/MEMORY.md` 和 `~/.xmclaw/USER.md`，在 session 启动时捕获并注入 system prompt，mid-session 只写盘不改 prompt。
3. **重构 System Prompt 组装**：引入 `SystemPromptSection` 概念，区分 `cacheBreak=false`（每 session 计算一次）和 `cacheBreak=true`（每 turn 计算），为 prefix cache 优化做准备。
4. **提升 WorkerSwarm 合成质量**：将当前简陋的 synthesis prompt 替换为结构化多步推理 prompt，要求 worker 输出包含置信度、引用来源、失败原因分析。

### 中期重点（J4）
1. **Background Review Loop**：在 `IntentEngine` 或 `PlanEngine` 中引入 qualifying turns 检测（如 ≥10 轮工具调用），fork 一个受限 review agent 更新记忆和技能。
2. **Progressive Skill Disclosure**：重构 `SkillRegistry` 为三级加载（index → full → file），避免一次性加载所有 skill 描述。
3. **多平台 Gateway**：基于现有 WebSocket 架构，扩展 Telegram/Discord/Slack 适配器，复用 OpenClaw 的 connector 设计模式。
4. **MCP 适配器**：在现有 ACP 旁边增加 MCP server/client 能力，接入外部工具生态。

### 长期差异化（J5+）
1. **语义代码库索引的深化**：XMclaw 的 `codebase_index` 已经领先，下一步应将其与 Hermes 的 context file walking 结合，实现“自动发现 + 深度语义检索”的混合注入。
2. **GEPA 自进化**：在技能数量达到 20+ 后，引入 DSPy-based 进化 pipeline，以 LLM-as-judge 优化 SKILL.md 和 tool description。
3. **Voice + Canvas**：在有资源后跟进 OpenClaw 的 Voice Wake 和 Live Canvas 体验。
