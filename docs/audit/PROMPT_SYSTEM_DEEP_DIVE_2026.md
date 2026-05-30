# XMclaw 提示词系统深度调研报告

> **调研范围**: System Prompt 组装、Memory 注入、Context 管理、Tool 暴露、Caching 策略
> **调研方法**: 源码审计 + 学术论文综述 + 行业顶尖产品对标 (Claude Code, Cursor, Letta/MemGPT, OpenAI, Kimi)
> **约束**: 不动手改代码，仅提供证据-backed 的优化方向
> **日期**: 2026-05-29

---

## 1. 执行摘要

XMclaw 的提示词系统已经具备了**生产级的基础骨架**（5-slot assembler、11-bucket Memory V2、B-398 递归防护、Wave-30 cache-aware time_block 位移）。但对比 2025-2026 年学术界和工业界的最新进展，在 **Context Compaction、Progressive Tool Disclosure、Episodic Memory Tiering、Instruction Hierarchy** 四个维度存在显著的架构级优化空间。

**核心结论**: XMclaw 的优化重点应从"如何塞更多内容进上下文"转向"如何让更少、更精准的内容在正确的时间出现在正确的位置"。

---

## 2. XMclaw 当前架构审计

### 2.1 全链路回顾

```
7-file SOUL Pack (IDENTITY.md + BEHAVIOR.md + ...)
         ↓
5-slot assembler (build_system_prompt)
    Slot 0:  DEFAULT_IDENTITY_LINE (硬编码)
    Slot 0.5: backend_label (动态)
    Slot 2:  persona files (三层 overlay)
    Slot 3:  OS/Shell 环境提示
    Slot 4:  工具摘要
         ↓
AgentLoop.run_turn()
    Phase 6: Memory V2 render_for_prompt() 注入用户消息尾部
    Phase 6: Percept push (B-398 guard 已补全)
         ↓
Message Builder (OpenAI-compatible)
    system: assembled prompt
    user:   user_message + memory_block
         ↓
LLM API (Kimi K2.6 via api.kimi.com)
```

### 2.2 当前优势

| 维度 | 现状 | 评级 |
|------|------|------|
| Cache-aware structure | Wave-30 将 time_block 移到 system prompt 最尾，用 marker 分割 | ⭐⭐⭐⭐ |
| Memory injection | 通过 user message 注入，避免 bust system prompt cache | ⭐⭐⭐⭐⭐ |
| Sanitization | 三层防御（零宽字符剥离 → 18 条正则 → prompt_scanner redact） | ⭐⭐⭐⭐⭐ |
| Recursive guard | B-398 已阻断 autonomous→percept→goal 无限循环 | ⭐⭐⭐⭐⭐ |
| Persona overlay | 三层优先级（project > profile > builtin） | ⭐⭐⭐⭐ |

### 2.3 当前短板

| 维度 | 现状 | 风险 |
|------|------|------|
| Conversation compaction | 无显式机制，历史消息线性增长 | Context rot, 成本爆炸 |
| Tool disclosure | 全部工具摘要一次性注入 system prompt | Token 膨胀，cache 失效 |
| Memory retrieval | Static cap-based render（2.0s 超时，4 并发） | Query-agnostic，可能注入无关记忆 |
| Instruction hierarchy | 无显式优先级分层 | Prompt injection 风险 |
| Episodic memory | events.db 181MB 但未结构化压缩 | 历史轨迹无法有效利用 |
| Context rot monitoring | 无 token 使用监控或预警 | 性能静默退化 |

---

## 3. 七大优化维度深度分析

### 3.1 Prompt Caching 策略优化

#### 行业证据

**Anthropic** (Prefix Caching, 2025): 显式 `cache_control` 标记，cached tokens 成本降低 90%（$0.30/M vs $3.00/M），break-even 为 2+ cache hits per prefix。TTL 5 分钟，可延长至 1 小时。要求最小 1,024 tokens per checkpoint，最多 4 个 checkpoints [^1][^2]。

**OpenAI** (Automatic Caching, 2024): 自动为 >1024 tokens 的 prompt 启用缓存，50% 成本折扣，无需代码变更。Cache hits 以 128-token 为增量单位，TTL 5-10 分钟 [^3][^4]。

**Kimi** (Built-in LCP Caching, 2026): K2.6 支持 Longest Common Prefix similarity cache，cached input $0.15/M vs $0.60/M standard（75% reduction）。**关键要求: system digest 必须 byte-identical**（空格、时间戳变化都会破坏缓存）[^5][^6]。

#### 论文证据

**"An Evaluation of Prompt Caching for Long-Horizon Agentic Tasks"** (arXiv 2601.06007, 2026): 
- 首个跨三大 provider（OpenAI/Anthropic/Google）的 prompt caching 系统性评估
- **核心发现**: "System Prompt Only Caching" 策略在成本和延迟两个维度上最一致有效
- 50,000 token system prompt 时，GPT-5.2 成本降低 89%，Claude Sonnet 4.5 降低 88%
- **关键警告**: 动态内容（时间戳、session ID、用户特定信息）嵌入 system prompt 会彻底破坏缓存；应将动态内容放在 system prompt 末尾或移到 user message [^7]

#### XMclaw 差距

| 问题 | 证据 | 影响 |
|------|------|------|
| time_block 仍在 system prompt 内 | Wave-30 将其移到最尾，但仍属 system | 每次请求时间变化 → byte-identical 要求不满足 → Kimi LCP cache miss |
| backend_label 动态变化 | Slot 0.5 包含当前后端信息 | 后端切换时 cache 失效 |
| 无 cache hit rate 监控 | 未追踪 cached_tokens / cache_read_tokens | 无法验证 Wave-30 优化效果 |

#### 优化方向

1. **将 time_block 完全移出 system prompt**，作为第一条 user message 或独立的 assistant message 注入
2. **对 Kimi 启用 `prompt_cache_key`**（如 OpenAI 支持），确保相同 persona 的跨 session 请求能共享 cache
3. **添加 cache metrics 采集**，追踪 `cached_tokens` / `prompt_tokens` 比例，目标 >80%

---

### 3.2 System Prompt 层次结构设计

#### 行业证据

**Claude Code** (Anthropic, 2026): 
- `CLAUDE.md` 要求 <200 lines, <2,000 tokens
- 使用 XML tags (`<instructions>`, `<context>`, `<example>`) 而非 Markdown 或编号列表
- 明确警告: aggressive 语言（"CRITICAL!", "YOU MUST", "NEVER EVER"）会 overtrigger 并降低输出质量 [^8]

**Cursor** (2026):
- `.cursor/rules/*.mdc` 支持 globbing 模式，按文件类型作用域
- 系统提示词分三层: identity → capabilities → constraints
- Agent prompt v1.2 开篇定边界: "你是谁、你能做什么、你不能做什么" [^9]

#### 论文证据

**"HIPO: Instruction Hierarchy via Constrained Reinforcement Learning"** (arXiv 2603.16152, 2026):
- 当前 LLM 将 system prompt 和 user prompt 同等对待，导致 jailbreak 和 prompt injection
- 提出 CMDP-style 优化框架：将 system compliance 作为约束，在可行区域内最大化 user utility
- **关键洞察**: 需要显式的 instruction hierarchy — system > user > tool output [^10]

**"How Architectural Scaffolding Enables Hypothesis-Space Restructuring in LLM Agents"** (arXiv 2604.20039, 2026):
- 三层 agent 架构对比: Base (仅 task description) → CG (+ context graph) → CG+DB (+ dynamic behaviors)
- 系统提示词的结构化程度直接影响 agent 在复杂任务中的探索效率 [^11]

#### XMclaw 差距

| 问题 | 现状 | 对比 |
|------|------|------|
| 无显式 instruction hierarchy | 5-slot 是物理组装顺序，非语义优先级 | HIPO 要求显式层次 |
| Persona 文件可能包含 aggressive 语言 | sanitize_for_prompt 仅做安全过滤，不做 tone 优化 | Claude 明确反对 aggressive 语言 |
| 无 per-channel / per-task 的 system prompt 变体 | 同一套 system prompt 应对所有模式 (instant/thinking/agent/swarm) | Cursor rules 支持 globbing 作用域 |

#### 优化方向

1. **引入语义分层标签**: `<identity>` → `<capabilities>` → `<constraints>` → `<tools>` → `<context>`，帮助模型建立注意力优先级
2. **Tone audit**: 扫描 persona 文件中的 aggressive 词汇（"MUST", "NEVER", "CRITICAL"），替换为 calm, direct instructions
3. **Mode-specific system prompt**: instant/thinking/agent/swarm 四档可加载不同深度的 persona 内容

---

### 3.3 Memory Architecture Tiering

#### 行业证据

**MemGPT / Letta** (2023→2026):
- OS-inspired 三层记忆: **Main Context** (prompt tokens, 类比 RAM) → **Working Context** (固定大小 R/W 块，存关键事实) → **FIFO Queue** (滚动历史 + recursive summary)
- 当 core memory 满时，agent 自主决定写入 archival；需要旧信息时从 recall/archival 检索
- 使用 LLM 自身作为 memory manager，通过 function call 自主编辑记忆 [^12][^13]

**Claude Code** (2026):
- `/compact`: 将历史对话压缩为保留关键决策、文件路径、错误信息的形式，释放 60-80% tokens
- `/clear`: 完全重置
- Auto-compact 在 83.5% 容量触发，但社区共识建议在 70-75% 时手动触发 [^14]

#### 论文证据

**"Tiered Memory Architecture and Retrieval Bottleneck Analysis"** (arXiv 2605.03675, 2026):
- MemTier: 异步 daemon-driven consolidation + RL-based retrieval policy adaptation
- 关键发现: LoCoMo benchmark 对记忆架构不敏感（因为对话在上下文内），但 **LongMemEval-S**（跨 53 sessions）能有效区分记忆系统质量
- **五信号检索评分**: recency + importance + relevance + access frequency + cognitive weight (来自工具结果) [^15]

**"D-Mem: A Dual-Process Memory System for LLM Agents"** (arXiv 2603.18631, 2026):
- 借鉴认知科学 System 1 / System 2:
  - **System 1**: 快速语义回忆（类似当前 Memory V2 的 embedding-based retrieval）
  - **System 2**: 深度情景重建（按需重播完整轨迹）
- 关键洞察: 大多数日常查询可用快速回忆解决，仅在复杂推理时才需深度重建 [^16]

**"AriadneMem: Threading the Maze of Lifelong Memory"** (arXiv 2603.03290, 2026):
- 批评现有系统的 flat top-k retrieval → 提出 **connected evidence subgraph**
- 冲突感知粗化（conflict-aware coarsening）处理记忆更新
- 近似 Steiner completion + bridge-node discovery 构建连通证据子图 [^17]

**"SimpleMem"** (Liu et al., 2026):
- 当前 Memory V2 的对标基准: write-time semantic lossless compression
- 将记忆组织为层级结构，query-aware retrieval
- LoCoMo F1=0.432, tokens=555（state-of-the-art token efficiency）[^18]

#### XMclaw 差距

| 维度 | XMclaw 现状 | 行业前沿 | 差距 |
|------|------------|---------|------|
| Semantic memory | 11-bucket 静态渲染 | SimpleMem 层级压缩 + query-aware | 无 query-aware 检索，静态 cap |
| Episodic memory | events.db 181MB 原始事件 | MemGPT FIFO + recursive summary | 无轨迹压缩，无情景记忆 |
| Working memory | 无显式 working context | Letta core memory (固定大小 R/W) | 无即时工作记忆区 |
| Retrieval signal | 4 并发 recall，2.0s 超时 | 五信号评分 (MemTier) | 仅 relevance + recency |
| Consolidation | 无 | 异步 daemon-driven (MemTier) | 无记忆整合/遗忘机制 |

#### 优化方向

1. **Query-aware memory retrieval**: 当前 `render_for_prompt` 是静态 cap-based，应改为根据 user_message 语义动态选择相关 buckets 和 items
2. **引入 Episodic Memory tier**: 将 events.db 中的历史轨迹定期 summary 为 episodic memory，而非全部丢弃
3. **Working Context 区**: 在 user message 中开辟固定大小的 working context 块，存放当前 session 的关键状态（类似 MemGPT 的 core memory）
4. **Memory consolidation daemon**: 借鉴 CognitiveDaemon 的 1Hz tick，增加 memory consolidation 任务（事实去重、冲突解决、过期清理）

---

### 3.4 Tool Use Progressive Disclosure

#### 行业证据

**Claude Skills** (Anthropic, 2025):
- 启动时仅加载 skill 的 name + description (~200 tokens)
- 完整 SKILL.md (~4-5k tokens) 仅在 skill 被显式调用时加载
- Playwright skill 对比: MCP (22 tools, ~14.3k tokens) vs Skill (~200 tokens 常驻)，节省 ~10k tokens/次 [^19]

**MCP Meta-Tool Pattern** (2026):
- 两个 meta-tool: `discover` + `execute`
- 不加载 29 个 tool schemas，只加载 2 个 meta-tool schemas (~600 tokens)
- 需要时请求特定 schema (~150 tokens/tool)
- **Token 节省 85-95%** [^20][^21]

**LLM-effective-tool-calling** (GitHub, 2025):
- Two-Phase Progressive Disclosure: Phase 1 看 name+category (~15 tokens/tool)，Phase 2 看完整定义
- 200 个工具时，token 节省 ~76%，准确率 95%+ [^22]

#### 论文证据

**"CodeMem: Architecting Reproducible Agents via Dynamic MCP"** (arXiv 2512.15813, 2025):
- 标准 tool use 需要将全部 tool definitions 注入 system prompt
- 随着工具库增长，导致 context window bloat 和 attention degradation
- **Dynamic ReAct**: decouple tool existence from tool definition，agent 通过 registry 按需查询 [^23]

**"Agent Skills for Large Language Models"** (arXiv 2602.12430, 2026):
- Progressive disclosure 被正式定义为 agent skill 的核心设计原则
- 当 tool 数量爆炸（100+）时，tool selection 本身成为一个推理任务
- 建议: 分阶段暴露 → 先选能力类别 → 再选具体 tool [^24]

#### XMclaw 差距

| 问题 | 现状 | 影响 |
|------|------|------|
| 全部工具常驻 system prompt | Slot 4 `_tools_digest` 一次性注入所有可用工具 | 工具增多时 system prompt 膨胀，cache 失效 |
| 无 tool search / discovery | 无 meta-tool 或 skill registry | 无法支持大规模工具集 |
| 工具变化破坏 cache | MCP 动态连接/断开会改变 tool set | 每次工具变化 → system prompt 变化 → cache miss |

#### 优化方向

1. **Tool category registry**: 将工具按 category 分组，system prompt 中仅暴露 category 列表 + 高频工具
2. **Lazy tool loading**: 当 agent 表达需要某类工具时，再注入完整 schema（类似 Claude Skills）
3. **Tool digest versioning**: 缓存 tool digest 的 hash，仅当工具集变化时重新组装 system prompt

---

### 3.5 Conversation History Compaction

#### 行业证据

**Claude Code**:
- `/compact` 保留: key decisions, file paths, function names, error messages
- 丢弃: 完整的工具输出、探索性搜索过程、已修复的中间错误
- 社区共识: 在逻辑断点手动 compact > 等待 83.5% auto-compact [^14]

**OpenAI Codex / Responses API**:
- Server-side compaction: 当输入超过阈值时自动生成 compaction block
- 支持 custom compaction instructions [^25]

**MS-Agent v1.6.0** (ModelScope, 2026):
- Context Compression mechanism: token usage monitoring + overflow detection + auto compaction
- 策略: pruning historical tool outputs + LLM-based summarization [^26]

#### 论文证据

**"Do LLMs Benefit from Their Own Words?"** (arXiv 2602.24287, 2026):
- **核心发现**: 在真实多轮对话中，assistant 回复在后续轮次中很少被复用
- 仅保留 user-side turns（省略 assistant responses）往往能保持下游响应质量
- 这是首个在真实多轮对话数据上评估该假设的研究 [^27]

**"Parallel Context Compaction for Long-Horizon LLM Agent Serving"** (arXiv 2605.23296, 2026):
- 同步 compaction 的问题: summary 比原文短 90-99%，但**刚积累的上下文也会被压缩**
- 提出 parallel compaction: 在独立上下文中异步生成 summary，不影响当前推理
- 关键指标: compaction 不应在 agent 刚获得关键洞察时触发 [^28]

**"Context Rot: Why LLMs Degrade as Context Grows"** (Chroma Research, 2025):
- 18 个前沿模型全部随上下文增长而退化 — **这是架构属性，非训练可解**
- 在 100K tokens 时，agent 成功率显著下降； doubling task duration quadruples failure rate
- 编码 agent 最坏情况: 60%+ 的首轮时间花在搜索上，35 分钟后每个 agent 成功率下降 [^29]

**"Lost in the Middle"** (Liu et al., TACL 2024):
- U-shaped attention: 开头/结尾准确率 ~75%，中间 (positions 5-15) 降至 ~45-55%
- **30%+ accuracy drop** 当相关信息位于长上下文中间
- 根因: RoPE 长程衰减 + Softmax 归一化 [^30]

#### XMclaw 差距

| 问题 | 现状 | 风险 |
|------|------|------|
| 无 conversation compaction | 历史消息线性增长至 context limit | Context rot，成本二次增长 |
| 无 token 使用监控 | 不知道何时接近 context limit | 静默性能退化 |
| 助手回复全部保留 | 未评估 assistant responses 的复用价值 | 大量无用 token |
| 工具输出未压缩 | grep/ls/read_file 等输出原样保留 | 最快膨胀源 |

#### 优化方向

1. **Assistant response omission**: 根据 "Do LLMs Benefit from Their Own Words?" 的证据，试验省略早期 assistant responses，仅保留最近 N 轮
2. **Tool output pruning**: 历史工具输出是最应该被压缩的 — 保留结果摘要，丢弃完整输出
3. **Token budget monitor**: 每轮估算当前 token 使用，在达到阈值前主动触发 compaction
4. **Session handoff pattern**: 跨 session 时保留 compacted state（如 PROGRESS.md），而非完整历史

---

### 3.6 Context Engineering: Position & Structure

#### 行业证据

**Anthropic Engineering Blog** (2025):
- "Stuffing 100K tokens of history degrades the model's ability to reason about what actually matters"
- 倡导 **Context Engineering**（而非 Prompt Engineering）：管理整个上下文状态，而非单条提示
- 目标: 找到最小的高信号 token 集合 [^29]

**Cursor** (2026):
- Agent mode 默认只读文件前 250 行，需要时再加 250 行
- 搜索时最多返回 100 行
- 建议文件 <500 行，前 100 行包含函数文档 [^9]

#### 论文证据

**"Elevating RAG via Performance-Driven Context Compression"** (arXiv 2508.19282, 2026):
- 当前压缩方法多为启发式，导致下游任务性能下降
- 提出 RL-based performance-driven compression: 奖励函数基于下游任务表现而非启发式规则
- **关键洞察**: 压缩应 preserve verbatim source evidence，而非改写（abstractive summarization 会丢失精确信息）[^31]

**"Squeez: Task-Conditioned Tool-Output Pruning for Coding Agents"** (arXiv 2604.04979, 2026):
- 针对 coding agent 的混合观察（code + logs + shell traces + metadata）进行 task-conditioned 剪枝
- 与通用压缩不同: 保留 verbatim evidence，只移除无关部分
- 定位: 填补 LLMLingua（token-level）和 SWE-Pruner（repository-level）之间的空白 [^32]

#### XMclaw 差距

| 问题 | 现状 | 优化空间 |
|------|------|---------|
| Memory 注入位置 | 通过 user message 尾部注入 | 正确（避免 bust cache），但可探索 user message 开头 vs 结尾的效果 |
| 文件读取策略 | 未限制读取范围，可能一次性读入大文件 | Cursor 的 partial read（250行）策略值得借鉴 |
| 工具输出格式 | 原始输出直接放入 context | 可引入 Squeez 式的 task-conditioned 剪枝 |

---

### 3.7 Safety: Instruction Hierarchy & Prompt Injection

#### 论文证据

**"Targeting the Core: Attacking RAG-based Agents via Direct LLM Manipulation"** (arXiv 2412.04415):
- 多轮 jailbreak 攻击利用指令层次缺失
- 建议: 借鉴 HRL 原则构建指令处理层，维护 secure foundational layer [^33]

**HIPO** (arXiv 2603.16152, 2026):
- System prompt compliance 应作为 hard constraint
- User utility 在 feasible region 内最大化
- 训练-free 方法: adaptive dual ascent 维持优先级不对称 [^10]

#### XMclaw 差距

| 问题 | 现状 | 差距 |
|------|------|------|
| 防御性 sanitization | sanitize_for_prompt + prompt_scanner redact | 仅事后过滤，无事前层次设计 |
| 无 explicit hierarchy | system/user/tool 平级处理 | 违反 HIPO 原则 |
| Percept push 风险 | B-398 已阻断递归，但 percept 内容仍直接进入 context | 需验证 percept 内容的信任等级 |

#### 优化方向

1. **显式分层标记**: 在 prompt 中使用 XML tags 区分 `<system_priority>`、`<user_input>`、`<tool_output>`、`<retrieved_memory>`
2. **Percept trust scoring**: 为 PerceptionBus 的 percept 添加信任等级，低信任内容放入隔离区
3. **Memory provenance tracking**: 追溯每条记忆的事实来源，防止污染记忆被反复注入

---

## 4. 优先级矩阵

### 4.1 影响 ×  effort 矩阵

```
            低 Effort          中 Effort          高 Effort
         ┌─────────────────┬─────────────────┬─────────────────┐
高影响   │ ① time_block 移出 │ ② Query-aware   │ ⑤ Episodic Memory│
         │    system prompt │    memory render │    tier +        │
         │ ③ Token budget   ├─────────────────┤    consolidation │
         │    monitor       │ ④ Tool category │                  │
         │ ⑥ Assistant resp │    registry      │                  │
         │    omission试验  │                  │                  │
         ├─────────────────┼─────────────────┼─────────────────┤
中影响   │ ⑦ Cache metrics  │ ⑧ Tone audit    │ ⑨ Progressive   │
         │    采集          │    (persona)     │    tool loading │
         │ ⑩ Tool digest    │                  │                  │
         │    versioning    │                  │                  │
         ├─────────────────┼─────────────────┼─────────────────┤
低影响   │ ⑪ XML tag 分层   │ ⑫ Instruction   │ ⑬ RL-based      │
         │    标记          │    hierarchy     │    memory policy│
         │                  │    enforcement   │    (MemTier)     │
         └─────────────────┴─────────────────┴─────────────────┘
```

### 4.2 推荐实施顺序

| 优先级 | 优化项 | 预期收益 | 证据强度 |
|--------|--------|---------|---------|
| P0 | time_block 完全移出 system prompt | Kimi cache hit rate ↑, latency ↓ | 强（论文+厂商文档） |
| P0 | Token budget monitor + 预警 | 防止 context rot | 强（Chroma 研究） |
| P1 | Assistant response 省略试验 | Token ↓ 20-40%，质量不变 | 强（arXiv 2602.24287） |
| P1 | Tool output 历史剪枝 | Token ↓ 30-50% | 强（MS-Agent, Squeez） |
| P2 | Query-aware memory retrieval | 记忆相关性 ↑ | 中（SimpleMem, AriadneMem） |
| P2 | Tool category registry | System prompt ↓, cache 稳定 | 强（Meta-Tool Pattern） |
| P3 | Episodic memory tier | 长程一致性 ↑ | 中（MemGPT, MemTier） |
| P3 | Instruction hierarchy | 安全性 ↑ | 中（HIPO） |

---

## 5. 风险与权衡

### 5.1 每项优化的潜在风险

| 优化 | 风险 | 缓解 |
|------|------|------|
| time_block 移出 system | 模型可能忽略时间信息（位置变到 user message） | A/B test: 开头 vs 结尾；加显式标记 `<current_time>` |
| Assistant response 省略 | 某些任务确实需要引用 assistant 的早期推理 | 保留最近 3 轮，省略更早；或保留包含 tool call 的轮次 |
| Tool output 剪枝 | 丢失调试所需的完整输出 | 剪枝策略 configurable，保留 error 输出 |
| Query-aware retrieval | 检索延迟增加，可能超时 | 保持 2.0s 超时 fallback 到 static render |
| Progressive tool loading | 额外 tool discovery 延迟 | 缓存常见 tool 组合；预加载高频工具 |

### 5.2 与 Kimi 平台特性的适配

Kimi K2.6 的特定约束:
- **LCP cache 要求 byte-identical prefix** — 任何空格、换行、时间变化都会破坏
- **Long context prefill 速度随长度下降** — 16K prefill 44 分钟（CPU 推理数据），但 API 层面也有类似趋势
- **Cached token $0.15/M** — 对于 agentic 循环（大量重复 system prompt），缓存收益显著

这意味着 XMclaw 的优化应**极度保守地对待 system prompt 的稳定性**，任何动态内容都应远离 system prompt。

---

## 6. 参考文献

[^1]: Introl Blog, "Prompt Caching Infrastructure", Mar 2026. https://introl.com/blog/prompt-caching-infrastructure-llm-cost-latency-reduction-guide-2025
[^2]: Anthropic API Documentation, "Prompt Caching", 2026.
[^3]: OpenAI API Documentation, "Prompt Caching", 2026. https://developers.openai.com/api/docs/guides/prompt-caching
[^4]: Prompt Engineering Best Practices 2026, Thomas Wiegold Blog, Feb 2026.
[^5]: DeepInfra, "Kimi K2.6 API Benchmarks", Apr 2026.
[^6]: loftllc.dev, "Kimi-K2.5 CPU Inference: Prompt Cache Design Principles", Mar 2026.
[^7]: Ji et al., "An Evaluation of Prompt Caching for Long-Horizon Agentic Tasks", arXiv:2601.06007, Jan 2026.
[^8]: Generative.inc, "The Complete Claude Code Guide 2026", Mar 2026.
[^9]: Multiple Cursor prompt analysis sources, 2025-2026.
[^10]: HIPO: "Instruction Hierarchy via Constrained Reinforcement Learning", arXiv:2603.16152, Mar 2026.
[^11]: "How Architectural Scaffolding Enables Hypothesis-Space Restructuring", arXiv:2604.20039, Apr 2026.
[^12]: Packer et al., "MemGPT: Towards LLMs as Operating Systems", arXiv:2310.08560, 2023.
[^13]: Letta (formerly MemGPT) documentation and reviews, 2026.
[^14]: Claude Code Ultimate Guide, GitHub, 2026.
[^15]: "Tiered Memory Architecture and Retrieval Bottleneck Analysis", arXiv:2605.03675, May 2026.
[^16]: "D-Mem: A Dual-Process Memory System for LLM Agents", arXiv:2603.18631, Mar 2026.
[^17]: "AriadneMem: Threading the Maze of Lifelong Memory", arXiv:2603.03290, Nov 2025.
[^18]: Liu et al., "SimpleMem", 2026 (cited in MemTier).
[^19]: Damiangalarza.com, "Understanding Claude Code's Context Window", Dec 2025.
[^20]: Synaptic Labs, "The Meta-Tool Pattern: Progressive Disclosure for MCP", Jan 2026.
[^21]: Matthew Kruczek, "Progressive Disclosure for MCP Servers", Jan 2026.
[^22]: GitHub: omersuve/LLM-effective-tool-calling, Nov 2025.
[^23]: "CodeMem: Architecting Reproducible Agents via Dynamic MCP", arXiv:2512.15813, 2025.
[^24]: "Agent Skills for Large Language Models", arXiv:2602.12430, Feb 2026.
[^25]: OpenClaw GitHub Issue #54041, "Anthropic Context Management Beta Passthrough", Mar 2026.
[^26]: ModelScope MS-Agent v1.6.0 Release Notes, Mar 2026.
[^27]: "Do LLMs Benefit from Their Own Words?", arXiv:2602.24287, Feb 2026.
[^28]: "Parallel Context Compaction for Long-Horizon LLM Agent Serving", arXiv:2605.23296, May 2026.
[^29]: MorphLLM, "Context Rot: Why LLMs Degrade as Context Grows", Mar 2026.
[^30]: Liu et al., "Lost in the Middle: How Language Models Use Long Contexts", TACL 2024.
[^31]: "Elevating RAG via Performance-Driven Context Compression", arXiv:2508.19282, May 2026.
[^32]: "Squeez: Task-Conditioned Tool-Output Pruning", arXiv:2604.04979, Mar 2026.
[^33]: "Targeting the Core: Attacking RAG-based Agents", arXiv:2412.04415, 2024.

---

## 7. 附录: XMclaw 特定问题核查清单

- [ ] `time_block` 是否可完全移出 system prompt？测试 Kimi cache hit rate 变化
- [ ] `backend_label` 是否可通过 API 参数而非 system prompt 传递？
- [ ] 当前 tool_names 的平均数量和 token 消耗？是否已超过 1,024 tokens？
- [ ] events.db 中工具输出占总事件的比例？是否可引入自动剪枝？
- [ ] Memory V2 的 11 buckets 中，哪些在大多数 queries 中从未被命中？
- [ ] 是否有数据支持 "assistant responses 在多轮中很少被复用" 在 XMclaw 场景下成立？
- [ ] Kimi API response 中是否返回 `cached_tokens` 字段？当前是否采集？
