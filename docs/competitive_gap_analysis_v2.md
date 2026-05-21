# XMclaw v1.0.0 竞品差距分析（校正版 v2）

> **分析日期**: 2026-05-20  
> **基准版本**: XMclaw `eb13c08` (JARVIS J2/J3 完整交付)  
> **对标竞品**: Codex CLI (`free-code-main` 本地镜像), OpenClaw (`openclaw/openclaw`), Hermes Agent (`NousResearch/hermes-agent`)  
> **审计维度**: (1) 基础模块完整度  (2) 信息流 / 输出质量  (3) 详细流程与提示词工程  
> **校正说明**: v1 版本严重低估了 XMclaw 已实现的子系统。本版基于对 `xmclaw/` 全目录的逐文件审阅重新校准。

---

## 1. 执行摘要

XMclaw 的代码成熟度远超 v1 分析的假设。**上下文压缩、统一记忆系统、Prompt 分层组装、多平台通道、进化闭环、Swarm 编排**等核心模块均已实现，且部分实现（如 CJK-aware 的 5 阶段 `ContextCompressor`、slot-based `PersonaAssembler`、vector+graph+temporal 三索引 `UnifiedMemorySystem`）已达到与 Hermes 直接对标甚至局部超越的水平。

**真正的差距集中在**：
1. **外围生态覆盖**（OpenClaw 的 20+ 消息平台、Voice/Canvas、MCP 生态）；
2. **Prompt 缓存的严格不变性**（Hermes 的 "frozen snapshot" 故意 mid-session 不改 system prompt，XMclaw 的 mtime 缓存会在记忆更新时失效）；
3. **模型特定 guidance 的细粒度**（Hermes 按 GPT/Claude/Google 分别注入 operational guidance）；
4. **离线进化技术栈**（Hermes 的 GEPA/DSPy 离线 pipeline vs XMclaw 的在线 bandit+controller 进化）。

---

## 2. 维度一：基础模块完整度

### 2.1 核心 Agent 循环与运行时

| 能力 | XMclaw | Codex CLI | OpenClaw | Hermes | 差距评级 |
|------|--------|-----------|----------|--------|----------|
| **对话循环** | `AgentLoop.run_turn()` — 2058 LOC，显式状态机，max_hops 限制，每 hop 发布 BehavioralEvent | `QueryEngine` 全生命周期（token/task budget、interrupt、recovery） | Gateway 多 session 路由 | `run_conversation()` 同步循环 + 中断 | **低** |
| **上下文压缩** | ✅ **`ContextCompressor`** (1200 LOC，Hermes 5 阶段 port)：CJK-aware token 估算、工具结果修剪（去重/摘要/JSON-aware 截断）、head+tail 保护、用户消息保护、结构化摘要（Active Task/Goal/Blocked/Key Decisions）、反抖动、动态 ctx_len 提升 | ✅ 四级压缩（autoCompact/reactiveCompact/snip/collapse） | 自动 summarization | ✅ `context_compressor.py` | **平齐** |
| **Token Budget 管理** | ✅ `ContextCompressor` 85% 阈值、tail_token_budget（按比例）、max_summary_tokens、工具结果 budget（prune_old_tool_results） | ✅ `createBudgetTracker` / `checkTokenBudget` / 500k auto-continue | 无公开细节 | ✅ `iteration_budget` + `tool_result_storage.py` | **平齐** |
| **中断与恢复** | ✅ `AgentLoop` 检查 `_cancel_session` flag；`SessionStore` 持久化历史；`_persist_history` 清理 scaffolding | ✅ `AbortController` + `stopHooks` + SDK 断线续传 | Gateway 持久化 | ✅ Session SQLite 持久化 | **平齐** |
| **最大输出恢复** | ❌ 未实现 | ✅ `MAX_OUTPUT_TOKENS_RECOVERY_LIMIT = 3` | 无 | 通过 LLM 客户端重试 | **中** |

**关键发现（v1 错误纠正）**: XMclaw 的 `AgentLoop` 不是"基础循环"。它实现了完整的生命周期：`run_turn` → `build_messages` → `llm.complete` → 工具调用循环 → `_persist_history`（清理 scaffolding、prune tool results）→ `HonestGrader` 评分 → EventBus 发布。`HistoryCompressionMixin` (781 LOC) 和 `HopLoopMixin` 提供了子目标分组压缩、LLM 升级摘要、continuation anchor 等高级功能。

### 2.2 记忆与用户画像

| 能力 | XMclaw | Codex CLI | OpenClaw | Hermes | 差距评级 |
|------|--------|-----------|----------|--------|----------|
| **跨会话持久记忆** | ✅ **`UnifiedMemorySystem`**：vector + graph + temporal 三索引；working/short/long/procedural 四存储层。`AutobiographicalMemory` 结构化表（People/Projects/Facts/Routines）。7 persona 文件（AGENTS/SOUL/IDENTITY/LEARNING/USER/TOOLS/BOOTSTRAP/MEMORY） | `memdir` + `CLAUDE.md` | `MEMORY.md` / `USER.md` | ✅ **双存储** MEMORY.md + USER.md，frozen snapshot | **平齐** |
| **记忆冻结策略** | ✅ **部分实现**：`AgentLoop._frozen_prompts` 缓存静态 system prompt（B-25 Hermes parity）。但缓存 key 基于 persona 文件 mtime，**mid-session 记忆更新会触发缓存失效**，破坏 prefix cache。 | 缓存 `getUserContext` / `getSystemContext`，mid-session 可更新 | 无公开细节 | ✅ **严格 Frozen Snapshot**：session 期间 system prompt 绝对不变，mid-session 写盘但不注入 | **中** |
| **记忆安全扫描** | ✅ **`sanitize_for_prompt`**（`persona/loader.py`）：检测 prompt injection markers；`prompt_injection_policy`（`AgentLoop`）；`redact_string`（摘要时脱敏） | `CYBER_RISK_INSTRUCTION` | DM pairing | ✅ `_scan_memory_content()`：injection / invisible unicode / exfiltration | **低** |
| **记忆闭环** | ✅ **`SkillDreamCycle`**（30min 周期 + 实时触发）+ **`DreamCompactor`**（每日 MEMORY.md 压缩）+ **`MemoryIndexer`** + `ExtractLessonsHook`（每 turn 后自动提取）。`AutobiographicalMemory` 双路径提取（Rule-based + LLM-based） | 被动加载 | 被动加载 | ✅ **Background Review**：≥10 轮工具调用后 fork review agent | **平齐** |
| **FTS5 会话搜索** | ✅ `events.db` WAL+FTS5；`memory_search` 工具搜索 vector+graph+temporal | 无 | 无 | ✅ FTS5 + LLM 摘要 | **平齐** |

**关键发现（v1 错误纠正）**: XMclaw 不是"仅有 ProjectMemoryProvider"。它有完整的 `UnifiedMemorySystem`（`memory/unified.py` 655 LOC）、`memory/v2/` 目录（LanceDB 后端 22KB、embedding、entity、LLM extractor/topic、service 72KB），以及 `AutobiographicalMemory`（637 LOC）结构化用户画像。`remember`/`learn_about_user`/`update_persona`/`memory_pin`/`memory_compact` 等工具均已实现。

### 2.3 技能系统

| 能力 | XMclaw | Codex CLI | OpenClaw | Hermes | 差距评级 |
|------|--------|-----------|----------|--------|----------|
| **技能发现** | ✅ **`SkillRegistry`** + `SkillsWatcher`（~10s 重扫）；`skill_browse` 工具全注册表扫描；B-238 token-overlap 预过滤到 ~12 个技能 | `EXPERIMENTAL_SKILL_SEARCH` + `DiscoverSkillsTool` | ClawHub 技能市场 | ✅ **三级披露**：compact index → `skill_view(name)` → `skill_view(name, path)` | **中** |
| **技能自创建** | ✅ **`SkillProposer`** + `SkillDreamCycle`：从 journal 历史提取模式，生成 `ProposedSkill`，发布 `SKILL_CANDIDATE_PROPOSED` 事件 | ✅ `generateAgent.ts` 向导 | 模板化 `SOUL.md` | ✅ Background review 自动 CREATE/UPDATE | **平齐** |
| **技能自进化** | ✅ **在线进化**：`EvolutionController`（4 门控晋升：min_plays/min_mean/gap_over_head/gap_over_second）+ `EvolutionAgent` 无头观察器 + `ReflectiveMutator` + Pareto frontier + Promotion policy + Iron Rule #1/#2 | ❌ 无 | ❌ 无 | ✅ **GEPA** (Genetic-Pareto Prompt Evolution)：离线 DSPy pipeline，LLM-as-judge 评分 | **中** |
| **技能格式** | ✅ Python 模块 (`skill.py`) + `SKILL.md` (markdown 步骤) + `manifest.json` | Agent JSON | `SKILL.md` (YAML frontmatter) | `SKILL.md` (YAML frontmatter) | **平齐** |
| **技能安全** | ✅ 通过 `SkillRegistry` 晋升门控 + `EvidenceGatedPromotion` + `GateBundle`（结构 4 门控） | 无 | 无 | ✅ 安全扫描 + bundled/hub/pinned 受保护 | **平齐** |

**关键发现（v1 错误纠正）**: XMclaw 有完整的进化闭环：`SkillProposer` 提出候选 → `EvolutionController` 门控评估 → `EvolutionAgent` 观察器汇总 grader verdicts → `registry.promote` 晋升。`ReflectiveMutator`（13924 bytes）实现了反射式变异。与 Hermes 的主要差异在于：XMclaw 是在线 bandit+controller 进化，Hermes 是离线 DSPy+GEPA 进化。

### 2.4 多平台连接器

| 能力 | XMclaw | Codex CLI | OpenClaw | Hermes | 差距评级 |
|------|--------|-----------|----------|--------|----------|
| **CLI/TUI** | ✅ `textual` TUI（`xmclaw chat`）；WebSocket 客户端 | ✅ `ink` React TUI | ✅ CLI + Control UI | ✅ `rich` TUI + Ink JSON-RPC | **平齐** |
| **消息平台** | ✅ **`ChannelDispatcher`**：支持多 channel adapter（飞书/钉钉等），slash 命令路由，延迟 ack，per-user session 隔离 | ❌ 仅本地/SDK | ✅ **20+ 通道**：WhatsApp/Telegram/Slack/Discord/iMessage/Signal/Teams/微信/QQ | ✅ Telegram/Discord/Slack/WhatsApp/Signal | **中** |
| **Voice I/O** | ❌ 无（有 vision：截图/摄像头/screen_capture） | ❌ 无 | ✅ Voice Wake + Talk Mode + TTS | ✅ 语音转录 + TTS | **高** |
| **IDE 集成** | ✅ ACP (JSON-RPC stdio) | ❌ 无 | 无 | ✅ ACP Adapter | **平齐** |
| **移动端** | ❌ 无 | ❌ 无 | ✅ iOS/Android Node | ❌ 无 | **高** |

### 2.5 工具与执行环境

| 能力 | XMclaw | Codex CLI | OpenClaw | Hermes | 差距评级 |
|------|--------|-----------|----------|--------|----------|
| **代码搜索** | ✅ 语义索引 (`codebase_index`)：tree-sitter chunking + LanceDB/sqlite-vec + Ollama embedding | LSP/Glob/Grep/FileRead/FileEdit | Browser/Canvas/Nodes | Web search + browser + vision + shell | **平齐** |
| **沙箱执行** | ❌ 本地进程直接执行（但有权限模式 `PolicyMode`） | ❌ 本地执行（权限模式控制） | ✅ Docker/SSH/OpenShell | ✅ **7 后端**：local/Docker/SSH/Singularity/Modal/Daytona/Vercel | **高** |
| **MCP 支持** | ❌ 无（仅自有 ACP） | ✅ MCP Server 连接 + 动态指令注入 | 无公开细节 | ✅ MCP 工具发现 + 动态 schema | **高** |
| **浏览器工具** | ❌ 无（有 `web_fetch` / `web_search`） | ❌ 无 | ✅ Browser tool | ✅ Browser navigate + 截图 | **高** |
| **任务调度** | ✅ `TaskScheduler` + HTNPlanner + `cron_create`（`core/scheduler/cron.py`） | `TaskCreateTool` / `TodoWriteTool` | Cron jobs | ✅ 内置 cron + 自然语言定时任务 | **平齐** |

---

## 3. 维度二：信息流与输出质量

### 3.1 System Prompt 工程与缓存策略

| 能力 | XMclaw | Codex CLI | OpenClaw | Hermes | 差距评级 |
|------|--------|-----------|----------|--------|----------|
| **Prompt 分层架构** | ✅ **`PersonaAssembler`**（`core/persona/assembler.py`）：slot-based 5 层组装（DEFAULT_IDENTITY → Bootstrap → Persona files → Platform hint → Tools digest），直接 port 自 Hermes slot ordering + OpenClaw `buildProjectContextSection` | `systemPromptSections.ts`：section 级缓存控制（`cacheBreak: boolean`） | `SOUL.md` + `AGENTS.md` + `TOOLS.md` | ✅ **10 层架构**：Identity → Guidance → Memory Snapshot → Skills Index → Context Files → Platform Hint | **低** |
| **缓存稳定性** | ✅ **部分实现**：`_frozen_prompts` 缓存静态部分；time slot 每 turn 重新附加（不 bust cache）。**但 persona 文件 mtime 变化会触发缓存重建**，mid-session 记忆写入导致 cache miss。 | `SYSTEM_PROMPT_DYNAMIC_BOUNDARY` 区分 global vs user-specific | 无公开细节 | ✅ **严格 Frozen Snapshot**：session 期间 system prompt 绝对不变 | **中** |
| **模型特定 Guidance** | ⚠️ **部分实现**：`backend_label` 注入当前 provider/model；但没有按模型族（GPT/Claude/Google）的差异化 operational guidance | 针对不同模型族有 thinking/budget/output 差异 | 无公开细节 | ✅ 按模型注入：`TOOL_USE_ENFORCEMENT` / `OPENAI_EXECUTION` / `GOOGLE_OPERATIONAL` / `COMPUTER_USE` | **中** |
| **输出风格控制** | ❌ 无（但 system prompt 中有详细的 narration/plan/synthesis 风格规则） | ✅ `OutputStyleConfig` | 无 | 通过 `SOUL.md` | **低** |
| **Prompt 安全扫描** | ✅ `sanitize_for_prompt`（检测 injection markers）；`redact_string`（摘要脱敏）；`prompt_injection_policy`（`PolicyMode.DETECT_ONLY`） | `CYBER_RISK_INSTRUCTION` + 工具结果注入检测 | DM pairing | ✅ `_scan_context_content()`：injection / invisible unicode / exfiltration / hidden HTML | **平齐** |

**关键发现（v1 错误纠正）**: XMclaw 的 prompt builder (`daemon/prompt_builder.py`, 587 LOC) 不是"单一 prompt"。它构建了极其详细的 system prompt，包含：
- 7 persona 文件注入规则
- Vision capability 指导
- Honesty Rule (B-302)：禁止"记下了" hallucination
- Plan-first 规则 (B-217)：Phase 1 PLAN → Phase 2 PROGRESS → Phase 3 SYNTHESIS
- Approval Gate (B-239)：高风险的自主计划需 `ask_user_question`
- Skill-first dispatch (B-177/B-178)
- Narration discipline (B-206)：工具调用间必须说话
- Self-evolution 指令：维护全部 7 persona 文件

### 3.2 上下文注入与代码库感知

| 能力 | XMclaw | Codex CLI | OpenClaw | Hermes | 差距评级 |
|------|--------|-----------|----------|--------|----------|
| **代码库上下文** | ✅ `codebase_index`：tree-sitter chunking + sliding-window fallback + Ollama embedding + LanceDB + 自动索引 pwd | `CLAUDE.md` + git status + LSP | `AGENTS.md` / `CLAUDE.md` | Context files walking to git root | **平齐** |
| **自动索引** | ✅ 首次查询自动索引 pwd | 需手动维护 `CLAUDE.md` | 需手动维护 | 需手动维护 | **优势** |
| **Git 状态注入** | ❌ 无 | ✅ `getGitStatus()`：branch / status / commits | 无 | 无 | **中** |
| **文件历史快照** | ❌ 无 | ✅ `fileHistoryMakeSnapshot` + `fileStateCache` | 无 | 无 | **中** |
| **工具 schema 动态重建** | ❌ 静态 schema | ❌ 静态 | 无 | ✅ 按实际可用工具动态重建 | **中** |

### 3.3 结果合成与信息呈现

| 能力 | XMclaw | Codex CLI | OpenClaw | Hermes | 差距评级 |
|------|--------|-----------|----------|--------|----------|
| **Worker/Swarm 合成** | ✅ **`SwarmOrchestrator`**：`concat` / `vote` / `map_reduce` 三种聚合策略；`TaskAggregator` 带超时轮询；`LoadBalancer` 简单能力启发式分配 | Coordinator mode 多 agent 路由 | 多 agent routing | `delegate_task`：spawn subagents，max 3 并发 | **平齐** |
| **流式工具输出** | ❌ 无（等待全部完成） | ✅ `StreamingToolExecutor` | 无公开细节 | 流式 chunk + `StreamingContextScrubber` | **中** |
| **工具结果截断** | ✅ `tool_result_prune.py`：按 token 预算保护尾部，旧结果摘要化；大参数 JSON-aware shrink | `applyToolResultBudget` | 无 | ✅ 按工具/按 turn aggregate budget | **平齐** |
| **输出格式化** | 纯文本 / markdown /  vision 图片附件 | `OutputStyleConfig` + 多种 render | Live Canvas / A2UI | Rich TUI + markdown | **中** |

---

## 4. 维度三：详细流程与提示词工程

### 4.1 Plan → Execute → Review 流程

| 环节 | XMclaw | Codex CLI | OpenClaw | Hermes | 差距评级 |
|------|--------|-----------|----------|--------|----------|
| **Planning** | ✅ `PlanEngine.create_plan()`：HTNPlanner → `ExecutionPlan`；`_validate_plan` 检查文件存在性；system prompt 中 **Plan-first 规则 (B-217)** 强制 agent 在首 hop 输出编号计划；`todo_write` 持久化计划 | 无显式 plan 阶段（隐式 tool use） | 隐式推进 | 隐式推进 | **平齐** |
| **验证与评分** | ✅ `HonestGrader`（hard≥0.8, LLM≤0.2）+ `EvolutionController` 4 门控 + Iron Rule #1/#2 | `VerificationAgent` | 无 | 无显式 plan 验证 | **平齐** |
| **执行监控** | ✅ `WorkerSwarm.execute_plan()`：topo-sort + `asyncio.gather`；`SwarmOrchestrator.dispatch()`：DAG 分解 + 负载均衡 + 聚合 | 单 agent 多轮工具调用；Coordinator mode | Gateway session 路由 | 同步循环 + 后台 review | **平齐** |
| **事后复盘** | ✅ **`SkillDreamCycle`**（30min 周期 + 实时触发）+ **`DreamCompactor`**（每日压缩）+ `ExtractLessonsHook`（每 turn 自动提取）+ `EvolutionAgent` 晋升观察器 | ❌ 无 | ❌ 无 | ✅ **Background Review**：≥10 轮后 fork review agent | **平齐** |

**关键发现（v1 错误纠正）**: XMclaw 有多个复盘/进化闭环：
1. **每 turn**：`ExtractLessonsHook` 自动提取工作流/工具怪癖/失败模式 → AGENTS.md/TOOLS.md/MEMORY.md
2. **实时**：`RealtimeEvolutionTrigger`（debounced 15s）触发 `SkillProposer`
3. **周期**：`SkillDreamCycle`（30min）+ `SleepWorker` 空闲感知调度
4. **每日**：`DreamCompactor` 压缩 MEMORY.md
5. **晋升**：`EvolutionAgent` 观察器汇总 grader verdicts，触发 `EvolutionController` 晋升决策

### 4.2 Agent / Worker 创建流程

| 环节 | XMclaw | Codex CLI | OpenClaw | Hermes | 差距评级 |
|------|--------|-----------|----------|--------|----------|
| **动态 Agent 创建** | ❌ `WorkerAgent` 代码硬编码；`SkillProposer` 提出 skill 候选但非完整 agent | ✅ `generateAgent.ts`：6 步结构化 prompt | `SOUL.md` + 模板 | `delegate_task` spawn subagent | **中** |
| **Agent 记忆继承** | ❌ 无 | Agent 可携带 memory instructions | 无 | Review agent 继承 parent cache + credentials | **中** |
| **Agent 使用示例** | ❌ 无 | `generateAgent.ts` 强制输出 `<example>` 块 | 无 | 无 | **低** |

### 4.3 Proactive / Intent 流程

| 环节 | XMclaw | Codex CLI | OpenClaw | Hermes | 差距评级 |
|------|--------|-----------|----------|--------|----------|
| **Proactive 触发** | ✅ `IntentEngine`：Rule (O(1)) → Statistical (EWMA+bigram) → LLM (stubbed)；`ProactiveAgent` + `PerceptionBus`（calendar/clipboard/screen/window watcher）；`CognitiveState`（goals/attention/fatigue） | `proactiveModule` / `KAIROS` / `BriefTool` | Voice Wake + Cron | Background review（被动复盘型） | **平齐** |
| **事件订阅** | ✅ EventBus 订阅，emit `PROACTIVE_INTENT_DETECTED` | 系统级 hook / event | Gateway event 路由 | 同步循环内检查 | **平齐** |
| **意图标签映射** | ✅ `_INTENT_LABELS`：slug → 中文标签 | 无公开细节 | 无 | 无 | **优势** |
| **24h 事件摘要** | ❌ Layer 3 stubbed | 无 | 无 | 无 | **中** |

### 4.4 提示词安全与治理

| 能力 | XMclaw | Codex CLI | OpenClaw | Hermes | 差距评级 |
|------|--------|-----------|----------|--------|----------|
| **运行时安全** | ✅ Guardian 4-path 安全模型 + `PolicyMode` | 权限模式（auto/ask/none） | DM pairing + sandbox | 权限模式 + sandbox | **平齐** |
| **Prompt Injection 防御** | ✅ `sanitize_for_prompt` + `prompt_injection_policy` + `redact_string` | `CYBER_RISK_INSTRUCTION` + 工具结果注入检测 | DM 配对码 | ✅ `_scan_context_content()`：多模式扫描 | **平齐** |
| **Invisible Unicode 防御** | ❌ 未明确实现 | ❌ 无 | ❌ 无 | ✅ 扫描 zero-width joiners、directionality marks | **中** |
| **错误信息脱敏** | ✅ ACP：`Internal server error` / `Tool execution failed`；摘要时 `redact_string` | 客户端侧脱敏 | 无 | Tool error 剥离 XML role tags / CDATA / markdown fences | **平齐** |
| **工具参数强制转换** | ❌ 无 | ❌ 无 | ❌ 无 | ✅ `coerce_tool_args()`：string→number/boolean/array | **中** |

---

## 5. 差距优先级矩阵（校正后）

| 优先级 | 差距项 | 影响 | 竞品参考 | 预估工作量 |
|--------|--------|------|----------|------------|
| ~~P0~~ | ~~**模型特定 Guidance 注入**~~ | ✅ **已实现** (`provider_guidance.py`：GPT/Claude/Google 三家族操作提示注入) | Hermes `prompt_builder.py` | 3-5 天 |
| ~~P0~~ | ~~**Prompt 缓存严格不变性（Frozen Snapshot）**~~ | ✅ **已实现** (`AgentLoop.strict_freeze`：session 级不可变性 + `thaw_session()` 显式刷新) | Hermes frozen snapshot 模式 | 1 周 |
| ~~P1~~ | ~~**MCP 适配器**~~ | ✅ **已实现** (`MCPBridge` + `MCPHub`：多 server 管理、stdio transport、tool name mangling) | Codex MCP 连接 | 2 周 |
| ~~P1~~ | ~~**浏览器工具**~~ | ✅ **已实现** (`browser.py` 2589 行：open/click/fill/screenshot/snapshot/eval/tabs/download/persistent profile/cookie import) | OpenClaw Browser / Hermes browser_navigate | 2-3 周 |
| ~~P1~~ | ~~**Voice I/O**~~ | ✅ **已实现** (`voice/` 包：`WhisperSTT` + `EdgeTTS`；`voice_transcribe` / `voice_synthesize` 工具) | OpenClaw Voice Wake / Hermes TTS | 2-3 周 |
| **P1** | **沙箱多后端** | 安全隔离 + 远程执行（Docker/SSH/Modal 等） | Hermes 7 后端 | 3-4 周 |
| **P2** | **Invisible Unicode 扫描** | 生产环境 prompt 注入的纵深防御 | Hermes `_scan_context_content()` | 2-3 天 |
| **P2** | **最大输出恢复（max_output_tokens recovery）** | 长输出场景下用户体验 | Codex `MAX_OUTPUT_TOKENS_RECOVERY_LIMIT` | 3-5 天 |
| **P2** | **Live Canvas / 可视化输出** | 高端体验差异化 | OpenClaw Canvas / A2UI | 3-4 周 |
| **P2** | **多平台覆盖扩展** | 从飞书/钉钉扩展到 20+ 平台（Telegram/Discord/Slack/WhatsApp 等） | OpenClaw connectors | 3-4 周 |
| **P3** | **GEPA 离线进化** | 长期技术壁垒；当前在线 bandit 进化可与之互补 | Hermes `hermes-agent-self-evolution` | 4-6 周 |
| **P3** | **技能三级披露** | 404 技能时长上下文爆炸；当前 B-238 预过滤到 ~12 个是临时方案 | Hermes compact index → full → file | 1-2 周 |
| **P3** | **工具参数强制转换** | 减少模型幻觉导致的参数类型错误 | Hermes `coerce_tool_args()` | 3-5 天 |
| **P3** | **Git 状态自动注入** | 编码场景上下文丰富度 | Codex `getGitStatus()` | 2-3 天 |

---

## 6. 技术细节附录

### 6.1 XMclaw ContextCompressor 5 阶段管道（`xmclaw/context/compressor.py`）

```
Phase 1: prune_old_tool_results
  → 去重（相同内容只保留最新）
  → 旧工具结果摘要化为 1 行
  → 大参数 JSON-aware shrink

Phase 2: protect head messages
  → 前 protect_first_n 条永不压缩（system prompt + 开场交流）

Phase 3: find tail boundary by token budget
  → 从尾部反向累积 token，直到 tail_token_budget
  → 软上限 1.5× budget，硬下限 3 条消息
  → 锚定到最后一条 user message（#10896 fix）
  → 按模型上下文窗口比例保护（Wave-27 fix-6）

Phase 4: summarize middle turns
  → 仅压缩 assistant + tool turns，user turns 保持原样（Wave-27 fix-8）
  → 结构化模板：Active Task / Goal / Constraints / Completed Actions / Active State / Blocked / Key Decisions / Pending User Asks / Relevant Files / Critical Context
  → 迭代更新：前一轮 summary 作为输入

Phase 5: assemble
  → head + preserved_users + summary + tail
  → 清理孤儿 tool_call / tool_result 对
```

### 6.2 XMclaw Prompt 组装 5-Slot 架构（`xmclaw/core/persona/assembler.py`）

```
Slot 0: DEFAULT_IDENTITY_LINE — "You are XMclaw…"
Slot 0.5: backend_label — 当前 provider/model 标注
Slot 1: Bootstrap prefix — BOOTSTRAP.md 存在时注入
Slot 2: Persona files — AGENTS/SOUL/IDENTITY/LEARNING/USER/TOOLS/MEMORY（OpenClaw 优先级顺序）
         → 每个文件经过 sanitize_for_prompt（注入检测）
Slot 3: Platform/shell hint — OS-aware
Slot 4: Tools digest — 当前可用工具列表
+ 非缓存：_with_fresh_time() 每 turn 附加当前时刻
```

缓存 key: `(profile_dir, workspace_dir, mtime_fingerprint, tools_tuple, backend_label)`

### 6.3 XMclaw 进化闭环架构

```
Per-turn: AgentLoop.run_turn()
  → ExtractLessonsHook → AGENTS.md / TOOLS.md / MEMORY.md
  → HonestGrader.verdict() → GRADER_VERDICT event

Realtime (~15s debounce): RealtimeEvolutionTrigger
  → SkillProposer → SKILL_CANDIDATE_PROPOSED event

Periodic (30min): SkillDreamCycle
  → SkillProposer → audit log + bus event

Daily: DreamCompactor
  → MEMORY.md 压缩

Observer: EvolutionAgent (headless workspace)
  → 订阅 GRADER_VERDICT
  → 聚合 per-(skill_id, version) EWMA + 简单均值
  → EvolutionController.consider_promotion()
  → 若 PROMOTE → SKILL_CANDIDATE_PROPOSED event
  → registry.promote(evidence=...)（Anti-req #12：无证据不晋升）
```

---

## 7. 结论与建议（校正后）

### 7.1 立即行动（P0）

1. **模型特定 Guidance 注入**：在 `PersonaAssembler` 中增加 `ModelSpecificGuidanceSlot`，根据 `backend_label` 识别的 provider（openai/anthropic/google/local）注入对应的 tool-use enforcement / execution discipline / operational guidance。参考 Hermes `prompt_builder.py` 的 `TOOL_USE_ENFORCEMENT_MODELS` / `OPENAI_MODEL_EXECUTION_GUIDANCE` / `GOOGLE_MODEL_OPERATIONAL_GUIDANCE`。

2. **Frozen Snapshot 严格化**：将 `_frozen_prompts` 的缓存失效条件从 mtime 检查改为**仅在下个 session 启动时重建**。mid-session 的 `remember`/`learn_about_user` 调用应写盘但不触发 system prompt 重建，保护 prefix cache。作为补偿，在 turn 开始时通过 `memory_search` 等工具让 agent 主动查询最新记忆，而非依赖 prompt 注入。

### 7.2 中期重点（P1）

3. **MCP 适配器**：在 ACP 旁边增加 MCP server/client 能力。XMclaw 的 `ChannelDispatcher` + adapter 模式天然适合接入 MCP 生态。

4. **浏览器工具**：基于现有 `web_fetch`/`web_search`，增加 `browser_navigate` + `browser_screenshot` + `browser_click`，填补通用 web 交互能力。

5. **Voice I/O**：利用已有 vision 基础设施（screen_capture、camera_capture），扩展 voice wake + TTS 支持。

### 7.3 长期差异化（P3+）

6. **GEPA 离线进化**：当前在线 bandit+controller 进化适合快速迭代，但长期应引入 DSPy-based 离线优化 pipeline，对 `SKILL.md` 和 tool description 进行遗传-帕累托进化，与在线进化形成互补。

7. **技能三级披露**：当注册表技能数量 >50 时，将当前 B-238 token-overlap 预过滤升级为 Hermes 式 compact index → `skill_view(name)` → `skill_view(name, path)` 的渐进加载，避免上下文膨胀。
