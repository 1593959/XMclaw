# XMclaw 记忆系统、技能系统与 MCP 生态 — 业界调研与优化方向

> **调研日期**: 2026-06-08  
> **调研范围**: 记忆系统写入/召回逻辑、技能安装与自主调用、MCP 相关实现  
> **对比对象**: XMclaw `feat/cognitive-memory-gateway` 分支当前实现  
> **数据来源**: 2024-2026 年顶级会议论文 (NeurIPS, arXiv)、开源项目 (Mem0, Letta, Zep)、行业标准 (agentskills.io, MCP spec)、安全披露 (CVE, OWASP)

---

## 目录

1. [执行摘要](#1-执行摘要)
2. [记忆系统深度调研](#2-记忆系统深度调研)
   - 2.1 [业界方案谱系](#21-业界方案谱系)
   - 2.2 [写入逻辑对比](#22-写入逻辑对比)
   - 2.3 [召回逻辑对比](#23-召回逻辑对比)
   - 2.4 [认知心理学理论基础](#24-认知心理学理论基础)
   - 2.5 [基准测试数据](#25-基准测试数据)
3. [技能系统深度调研](#3-技能系统深度调研)
   - 3.1 [Claude Agent Skills 架构](#31-claude-agent-skills-架构)
   - 3.2 [与 XMclaw 对比](#32-与-xmclaw-对比)
4. [MCP 生态深度调研](#4-mcp-生态深度调研)
   - 4.1 [架构概览](#41-架构概览)
   - 4.2 [安全问题全景](#42-安全问题全景)
   - 4.3 [Discovery-First 模式](#43-discovery-first-模式)
5. [XMclaw 差距分析](#5-xmclaw-差距分析)
6. [优化路线图](#6-优化路线图)
7. [参考文献](#7-参考文献)

---

## 1. 执行摘要

本次调研覆盖记忆系统、技能系统和 MCP 生态三个维度，通过与业界 15+ 个系统/论文的对比分析，识别出 XMclaw 在以下方面的关键差距与优化机会：

| 维度 | 核心发现 | 优先级 |
|------|---------|--------|
| **记忆写入** | XMclaw 的 Tier-1 fast-path 归纳质量差，extraction 在 ingestion 阶段丢失信息。True Memory、MemPalace 等研究证明 verbatim storage + gate 优于 extraction | P0 |
| **记忆召回** | XMclaw 仅有单路向量搜索。Hindsight 四路并行 (semantic+keyword+graph+temporal) + RRF + reranker 在 LongMemEval 达 91.4% | P0 |
| **存储层次** | XMclaw 缺少 Letta 式的 Core Memory 层，recall 直接注入 system prompt 造成 context pollution | P1 |
| **技能系统** | XMclaw 与 Claude Agent Skills 高度相似，但缺少渐进式披露 (description always, body on demand) 和 agentskills.io 标准兼容 | P1 |
| **MCP 集成** | XMclaw 无原生 MCP 支持。MCP 生态安全风险巨大 (16+ CVEs)，但 Discovery-First 模式可将 context 开销从 30K 降到 200 tokens | P2 |

---

## 2. 记忆系统深度调研

### 2.1 业界方案谱系

```
记忆系统分类 (Hu et al., 2025 分类法)
├── Knowledge-organization methods
│   ├── Think-in-Memory (Liu et al., 2023) — 存储演进中的思维链
│   ├── A-Mem (Xu et al., 2025) — Zettelkasten 笔记网络
│   └── GraphRAG (Edge et al., 2024) — Leiden 社区检测
├── Retrieval mechanism-oriented
│   ├── Mem0 (Chhikara et al., 2025) — 混合存储 (graph+vector+kv)
│   ├── Zep/Graphiti (Rasmussen et al., 2025) — 时序知识图谱
│   ├── Hindsight (Hindsight AI, 2025) — 四路并行检索
│   └── Supermemory ASMR (2025) — 多智能体检索
├── Architecture-driven
│   ├── MemGPT/Letta (Packer et al., 2023) — OS 式三层内存
│   ├── MemPalace (Jovovich, 2026) — 空间记忆法
│   ├── True Memory (2026) — verbatim + encoding gate
│   ├── MemForest (2026) — 并行提取 + 层次时间索引
│   └── MemTier (2026) — 级联检索 + 五信号评分
└── XMclaw 当前 — Vector (LanceDB) + Graph (SQLite) + Persona Markdown
```

### 2.2 写入逻辑对比

#### 2.2.1 Mem0: Extract → Manage 两阶段

Mem0 的写入流程 (Chhikara et al., 2025) 是一个函数调用代理：

**Phase 1 (Extraction)**: 对每段会话，代理接收会话文本并调用 `add_to_memory(facts=[...])` 存储提取的事实，或 `skip_memory()` 丢弃。

**Phase 2 (Manage)**: 对每个新提取的事实，与当前存储中 top-k 相似邻居进行冲突检测。模型决定：
- `add` — 添加为新事实
- `update` — 更新现有事实
- `delete` — 删除过时事实
- `none` — 无操作

Mem0 使用 `text-embedding-3-small` 进行嵌入检索，返回 top-5 邻居，**不设最小相似度阈值**。会话按时间顺序处理，每个会话的 Write 输出在下一个会话开始前提交到存储。

**2026年4月重大改进**: Mem0 发布 "token-efficient memory algorithm"，采用 single-pass hierarchical extraction + multi-signal retrieval，LongMemEval 分数从 ~49% 跃升至 **93.4%** (LoCoMo 85.0%, BEAM-1M 62%)。

#### 2.2.2 True Memory: Verbatim + Encoding Gate

True Memory (2026) 提出了一个颠覆性论点：

> "Extraction at ingestion is the wrong primitive for agent memory: content discarded before the query is known cannot be recovered at retrieval time."

**核心设计**:
1. **Encoding Gate** ( ingestion 阶段): 评分每个事件的 novelty、salience 和 prediction error，超过阈值的事件被 **verbatim 保留**
2. **Higher-order structure**: summaries、entity profiles、consolidation 在 post-ingestion 或 query time 计算
3. **Multi-stage retrieval pipeline**: 6 层架构，操作于保留 verbatim 的事件之上

**性能**: 在 LoCoMo (1,540 questions) 上达到 **93.0%** accuracy，对比 Mem0 61.4%, Supermemory 65.4%, Zep ~71%。

**理论基础**:
- Bartlett (1932): Reconstructive recall — 记忆不是精确复制而是重构
- Tulving (1972): Episodic vs Semantic memory distinction
- Craik & Lockhart (1972): Levels-of-processing effects on retrievability
- Schacter (2001): Memory distortions taxonomy

#### 2.2.3 MemPalace: Verbatim + Method of Loci

MemPalace (Jovovich, 2026) 采用 2,500 年前的 "记忆宫殿" 技术：

**结构**: Wings (领域) → Rooms (主题) → Drawers (记忆块)
**策略**: 全部 verbatim 存储，按空间层次组织
**性能**: **96.6%** Recall@5 on LongMemEval — 当时最高
**依赖**: 仅需 ChromaDB + PyYAML，完全离线

#### 2.2.4 XMclaw 当前写入逻辑

XMclaw 采用 **THINK → DECIDE → EXECUTE** 认知管道：

```
Observation → THINK (LLM 归纳) → DECIDE (是否值得记) → EXECUTE (写入/更新/忽略)
                    ↑
            Tier-1 Fast-Path (绕过 LLM)
```

**THINK**: 调用 LLM 将观察归纳为标准陈述句 (synthesized_text)，判断 worth_remembering
**Tier-1**: 关键词匹配 ("代理端口", "安装", "配置" 等 36 个关键词) → 绕过 LLM，直接 ADD

**关键差距**:

| 方面 | XMclaw | True Memory | 差距 |
|------|--------|-------------|------|
| 原始内容保留 | 否 (extraction 后丢弃) | 是 (verbatim + gate) | 丢失不可恢复信息 |
| 归纳时机 | ingestion (THINK 阶段) | deferred (query time) | 在查询已知前就做了归纳 |
| 冲突处理 | contradicts detection + merge | manage (add/update/delete/none) | 缺少 update/delete 路径 |
| Tier-1 归纳 | 加 "用户" 前缀 | 基本文本清理 | 几乎无归纳 |

### 2.3 召回逻辑对比

#### 2.3.1 Hindsight: 四路并行 + RRF + Reranker

Hindsight (Hindsight AI, 2025) 的召回架构：

```
Query ─┬─→ Semantic Search (dense embedding)
       ├─→ Keyword Search (BM25/lexical)
       ├─→ Knowledge Graph Traversal
       └─→ Temporal Filtering
              │
              ↓
       Reciprocal Rank Fusion (RRF)
              │
              ↓
       Cross-Encoder Reranker
              │
              ↓
       Final Results
```

**性能**: **91.4%** on LongMemEval
**核心洞见**: "不同记忆查询类型 (temporal, relational, factual) 需要不同检索策略，单一策略无法处理所有情况。"

#### 2.3.2 MemTier: 级联检索 + 五信号评分

MemTier (2026) 的两阶段检索：

**Stage 1 (Scoping)**: Semantic → 聚焦候选池
**Stage 2 (Ranking)**: 5-signal 评分引擎
- BM25 词法匹配
- Time decay (时间衰减)
- Cognitive weight (认知权重)
- Tier-specific boost (层级提升)
- Relevance signal (相关性信号)

**关键发现**: Multi-session R@2 = 0.038，意味着 **96% 的跨会话问题在 top-2 中找不到答案** — 检索是主导瓶颈。

#### 2.3.3 MemForest: 层次化时间索引

MemForest (2026) 的核心创新：
- **并行提取** (替代串行 LLM-in-the-loop)
- **层次化时间索引** (MemTree) — 支持 localized updates
- **Variable-granularity retrieval** — 不同粒度检索

**性能**: LongMemEval-S **79.8%** (stateful baselines 中最强)，写入吞吐量比 EverMemOS 高 **6x**。

#### 2.3.4 XMclaw 当前召回逻辑

XMclaw 的召回流程：

```
User Query → Embed (sentence-transformer) → LanceDB KNN Search (cosine similarity)
                   ↓
            Top-K hits → render_recalled_block() → inject into system prompt
```

**关键差距**:

| 方面 | XMclaw | Hindsight | 差距 |
|------|--------|-----------|------|
| 检索策略 | 单路 dense vector | 四路并行 | 无法处理 temporal/relational 查询 |
| 融合机制 | 无 (直接取 top-k) | RRF + cross-encoder reranker | 缺少多信号融合 |
| 时间感知 | 无显式时间索引 | Temporal filtering + time decay | 无法回答 "上周做了什么" |
| 关键词匹配 | 无 | BM25 | dense embedding 对短文本稀释严重 |
| 注入位置 | system prompt | context compiler (Letta) | 造成 context pollution |

> **重要发现** (Workload-Adaptive Cascade Retrieval, 2026): "Dense embeddings of short conversational turns (median ~20 words) suffer from semantic dilution; RRF pollutes BM25's clean lexical signal." — 在 LoCoMo 上 BM25 alone (Hit@10=0.945) 击败 Dense (0.789) 和 RRF (0.923)。

### 2.4 认知心理学理论基础

#### 2.4.1 Craik & Lockhart (1972): Levels of Processing

**核心论点**: 记忆的持久性取决于加工深度，而非存储结构。

- **浅层加工** (shallow): 感知、结构、语音 — maintenance rehearsal，短期保持
- **深层加工** (deep): 语义、概念、联想 — elaborative rehearsal，长期保持

> "Memory trace is a by-product of perceptual analysis" — Craik & Lockhart, 1972

**对 XMclaw 的启示**:
- XMclaw 的 Tier-1 fast-path (加前缀) 是典型的 **浅层加工** (maintenance rehearsal)
- THINK prompt 要求的 "总结归纳" 是 **深层加工**，但只在 ingestion 时执行一次
- **Transfer-Appropriate Processing (TAP)** (Morris et al., 1977): 检索时的加工方式应与编码时匹配 — XMclaw 的单一向量检索与多样化的查询类型不匹配

#### 2.4.2 Tulving (1972): Episodic vs Semantic Memory

| 类型 | 内容 | 检索方式 |
|------|------|---------|
| Episodic | 个人经历、事件、上下文 | 时间线索、情境线索 |
| Semantic | 一般知识、事实、概念 | 概念线索、逻辑推理 |

**对 XMclaw 的启示**:
- XMclaw 的 facts 存储混合了 episodic 和 semantic，但没有区分
- 用户问 "上周我说了什么" (episodic) 和 "我的代理端口是多少" (semantic) 需要不同的检索策略
- Letta 的 Core/Recall/Archival 三层与 episodic/semantic 区分有天然对应

#### 2.4.3 Bartlett (1932): Reconstructive Recall

**核心论点**: 记忆不是精确检索，而是基于现有图式的 **重构**。

**对 XMclaw 的启示**:
- verbatim storage (True Memory, MemPalace) 保留了原始材料供重构使用
- extraction 在 ingestion 时做的归纳是不可逆的 "有损压缩"
- XMclaw 的 "宁可多记，不可漏记" 原则正确，但 extraction 本身违背了它

### 2.5 基准测试数据

#### LongMemEval (Wu et al., 2024/2025)

500 手工设计问题，53 会话 haystacks，5 种能力类型：
- Single-session recall
- Multi-session synthesis
- Temporal reasoning
- Knowledge update
- Abstention

| 系统 | LongMemEval | LoCoMo | 备注 |
|------|-------------|--------|------|
| MemPalace | **96.6%** R@5 | - | Verbatim + spatial |
| Supermemory ASMR | ~99% | - | 多智能体，成本极高 |
| Mastra (GPT-5-mini) | 94.87% | - | 连续 LLM 推理 |
| Mem0 (2026-04) | 93.4% | 85.0% | Hierarchical extraction |
| True Memory | 93.0% | - | Verbatim + gate |
| EverMemOS | 94.5% | - | Graph + embedding |
| Hindsight | 91.4% | - | 四路并行 |
| MemForest | 79.8% | - | 6x 写入吞吐量 |
| Zep | ~72% | - | 时序知识图谱 |
| Mem0 (早期) | ~49% | - | 基础 extraction |
| Letta | 74.0% | - | 文件系统 baseline |

> **关键发现** (MemTier, 2026): "LoCoMo inserts the full conversation into context at query time, making the memory architecture irrelevant. LongMemEval is the appropriate benchmark for evaluating memory storage and retrieval."

---

## 3. 技能系统深度调研

### 3.1 Claude Agent Skills 架构

Claude Agent Skills (agentskills.io, 2025年12月发布开放标准) 的核心设计：

#### 3.1.1 三层加载策略

```
Level 1: Metadata (启动时加载)
  └── Frontmatter: name, description, triggers, model, allowed-tools
  └── 常驻 system prompt，无 context penalty

Level 2: Instructions (触发时加载)
  └── SKILL.md body: 工作流、最佳实践、指导
  └── 仅在使用时通过 Read 工具加载

Level 3: Resources (按需加载)
  └── references/*.md, scripts/, assets/
  └── 通过 {baseDir} 路径引用
```

#### 3.1.2 发现与调用机制

Claude **没有算法级技能选择**。决策完全在模型推理中：

1. 启动时，所有技能 description 被格式化为列表注入 context
2. 用户发送请求时，Claude 读取技能列表，用原生语言理解匹配意图
3. 匹配后调用 `Skill` tool，传入 `command: "skill-name"`
4. 然后加载完整 skill body 到 context

#### 3.1.3 权限控制

| Frontmatter | 用户可调用 | Claude 可调用 | 加载时机 |
|------------|-----------|--------------|---------|
| (default) | Yes | Yes | Description 总在 context，body 触发时加载 |
| `disable-model-invocation: true` | Yes | No | Description 不在 context，用户手动调用时加载 |
| `user-invocable: false` | No | Yes | Description 总在 context，Claude 自动调用 |

#### 3.1.4 与 MCP 的关系

> "MCP connects agents to external tools and data sources. Skills teach agents how to use those tools effectively. They're complementary — MCP provides access, Skills provide procedures."

### 3.2 与 XMclaw 对比

XMclaw 的技能系统与 Claude Agent Skills **高度相似**，但存在关键差异：

| 维度 | XMclaw | Claude Agent Skills | 差距 |
|------|--------|-------------------|------|
| **格式** | SKILL.md (YAML frontmatter + markdown) | 完全相同 | ✅ 兼容 |
| **扫描路径** | `~/.xmclaw/skills_user/` + `~/.agents/skills/` | `~/.claude/skills/` + `.claude/skills/` | ✅ 类似 |
| **发现机制** | Fuzzy find (token overlap) + trigger keywords | 模型自主决定 (无算法选择) | XMclaw 的 fuzzy 太简单，Claude 更灵活 |
| **加载策略** | Full skill loaded when invoked | 渐进式披露 (description always, body on demand) | XMclaw 缺少分层加载 |
| **版本管理** | ✅ SkillRegistry (version, promote, rollback) | ❌ 无 | XMclaw 领先 |
| **安全** | ✅ 安全扫描 + 审计日志 | ❌ 用户审计 | XMclaw 领先 |
| **格式支持** | Markdown + Python (skill.py) | Markdown only | XMclaw 更灵活 |
| **标准兼容** | 自有格式 | agentskills.io 开放标准 | XMclaw 应兼容标准 |
| **权限控制** | SkillManifest (permissions) | `disable-model-invocation`, `allowed-tools` | Claude 更细粒度 |

**核心建议**: XMclaw 应实现 **渐进式披露加载** — skill description 常驻 context (低开销)，skill body 仅在触发时加载。

---

## 4. MCP 生态深度调研

### 4.1 架构概览

```
┌─────────┐     ┌─────────┐     ┌─────────┐
│  Host   │────→│ Client  │────→│ Server  │
│ (Claude │     │ (MCP    │     │ (Tool   │
│  Desktop)│     │  Client)│     │  Provider)
└─────────┘     └────┬────┘     └────┬────┘
                     │               │
                     │  JSON-RPC 2.0 │
                     │  stdio / SSE  │
                     └───────────────┘
```

**核心设计**: Schema-first tool definition，模型无关，执行与推理分离。

### 4.2 安全问题全景

MCP 生态在 2025-2026 年爆发了 **16+ CVEs**，成为 agent 安全的主要攻击面：

#### 4.2.1 攻击类型

| 攻击类型 | 案例 | CVSS | 说明 |
|---------|------|------|------|
| Tool Poisoning | CVE-2025-54136 (Cursor IDE) | 8.8 | 恶意指令藏在 tool description，LLM 读取并执行 |
| RCE via STDIO | CVE-2025-65720 (Anthropic SDK) | Critical | STDIO config-to-exec 设计缺陷，配置值直接流入命令执行 |
| Supply Chain | Postmark MCP (npm) | - | Trojanized npm 包，BCC exfil |
| Indirect Prompt Injection | GitHub MCP Server | - | 通过 MCP 工具响应注入提示 |
| Cross-tenant Access | Asana MCP Server | - | 跨租户访问绕过 |

#### 4.2.2 OWASP 归类

Tool Poisoning 已被 OWASP Agentic Security Initiative 命名为 **ASI04: Agentic Supply Chain Vulnerabilities**。

#### 4.2.3 设计层面问题

> Anthropic 对 STDIO RCE 的回应: "behavior is by design... STDIO execution model represents a secure default and sanitization is the developer's responsibility."

这意味着 **官方 SDK 不会修复该问题**，安全负担完全落在运营者身上。

### 4.3 Discovery-First 模式

传统 MCP 的致命问题：**Context Bloat**。

```
传统 MCP: 30,000 tokens (预加载所有 tool schema)
Discovery-First: 200 tokens (恒定开销，无论多少工具)
```

**mcp-server-code-execution-mode** 实现的两阶段发现：

1. `discovered_servers()` → 了解有哪些服务，不加载 schema
2. `query_tool_docs(name)` → 按需加载具体 schema

**结果**: 10 个工具或 1000 个工具，system prompt 保持恒定大小。

**对 XMclaw 的启示**:
- 如果 XMclaw 接入 MCP，必须实现 Discovery-First，否则 context 会爆炸
- XMclaw 当前 `CompositeToolProvider` 的 tool 列表机制与此类似，但缺少 on-demand schema 加载

---

## 5. XMclaw 差距分析

### 5.1 记忆系统

```
维度                当前状态                    目标状态                    差距级别
─────────────────────────────────────────────────────────────────────────────────────────
写入策略            Extraction + Tier-1        Verbatim + Encoding Gate    P0 — 根本性
召回策略            单路向量搜索                多路并行 + RRF + Reranker   P0 — 根本性
存储层次            Vector + Graph + Markdown   Core + Recall + Archival    P1 — 架构性
时间感知            无                         显式时间索引 + 衰减         P1 — 功能性
冲突处理            Contradicts detection       Manage (CRUD)               P1 — 功能性
上下文注入          System prompt               Context compiler (Letta)    P1 — 架构性
压缩策略            被动 prune/scrub            主动 compact/summarize      P2 — 优化性
理论支撑            无                         LOP + Episodic/Semantic     P2 — 学术性
```

### 5.2 技能系统

```
维度                当前状态                    目标状态                    差距级别
─────────────────────────────────────────────────────────────────────────────────────────
加载策略            Full on invoke              Progressive disclosure      P1 — 功能性
发现机制            Fuzzy + triggers            Model-driven (hybrid)       P1 — 功能性
标准兼容            自有格式                    agentskills.io              P2 — 生态性
权限控制            Manifest permissions        disable-model-invocation    P2 — 功能性
版本管理            ✅ 已领先                   —                          —
安全扫描            ✅ 已领先                   —                          —
```

### 5.3 MCP 集成

```
维度                当前状态                    目标状态                    差距级别
─────────────────────────────────────────────────────────────────────────────────────────
原生支持            无                          MCP Client                  P2 — 生态性
Context 控制        N/A                         Discovery-First             P2 — 性能性
安全防御            N/A                         签名验证 + 沙箱             P2 — 安全性
```

---

## 6. 优化路线图

### Phase 1: 记忆写入质量 (P0, 2-3 周)

**目标**: 解决 Tier-1 复读机问题，提升 extraction 质量

1. **收窄 Tier-1 关键词** (已完成)
   - 去掉宽泛单字词 ("安装", "路径", "配置")
   - 只保留高确定性多词短语 ("代理端口", "偏好使用")

2. **改进 Tier-1 归纳** (已完成)
   - 去掉 "用户" 机械前缀
   - 加入基本文本清理 (去掉 "网址:", "注意:" 等冗余)

3. **引入 Verbatim Fast-Path**
   - 对非 Tier-1 内容，先 verbatim 存储原始消息
   - 异步 (background) 调用 LLM 进行归纳
   - 归纳结果作为 "semantic layer" 关联到原始记录
   - 参考: True Memory encoding gate

### Phase 2: 召回系统升级 (P0, 4-6 周)

**目标**: 从单路向量搜索升级到多路混合检索

1. **添加 BM25 关键词索引**
   - 使用 SQLite FTS5 或 Tantivy
   - 对短对话片段 (median ~20 words) 特别有效

2. **添加时间索引**
   - 显式 timestamp 字段
   - Time-decay scoring ( MemForest/MemTier 方法)

3. **实现 RRF (Reciprocal Rank Fusion)**
   - 融合 dense vector + BM25 + temporal 三路结果
   - 可调权重 per query type

4. **添加 Cross-Encoder Reranker**
   - 轻量级模型 (如 ms-marco-MiniLM)
   - 对 Top-K 候选做精确重排序

5. **召回注入位置优化**
   - 从 system prompt 移到 dedicated memory block
   - 参考 Letta Core Memory 的 `<memory_blocks>` XML 标签方式

### Phase 3: 存储层次重构 (P1, 6-8 周)

**目标**: 引入 Letta 式的三层内存

1. **Core Memory (In-Context)**
   - 小体量 (< 2K tokens)，始终注入 prompt
   - 存放：用户身份、关键偏好、当前任务状态
   - Agent 可主动读写 (通过工具调用)

2. **Recall Memory (Near-Context)**
   - 最近 N 轮对话 + 摘要
   - 自动 compact/summarize 当超出 token 限制

3. **Archival Memory (Long-Term)**
   - LanceDB/SQLite 向量存储
   - 按需检索 (用户查询时触发)

### Phase 4: 技能系统优化 (P1, 2-3 周)

1. **渐进式披露加载**
   - Skill description (frontmatter) 常驻 context
   - Skill body 仅在触发时加载
   - 减少未使用技能的 context 开销

2. **agentskills.io 标准兼容**
   - 支持 `disable-model-invocation`, `user-invocable`
   - 支持 `{baseDir}` 路径变量

3. **混合发现机制**
   - 保留 fuzzy find + triggers 作为 fast-path
   - 添加 LLM-driven 发现作为 fallback

### Phase 5: MCP 集成 (P2, 4-6 周)

1. **MCP Client 实现**
   - 支持 stdio + SSE transport
   - JSON-RPC 2.0 协议处理

2. **Discovery-First 适配**
   - 不预加载所有 tool schema
   - 按需 `discover_servers()` + `query_tool_docs()`

3. **安全加固**
   - 签名验证 (RSA manifest signing)
   - 运行时行为监控
   - 参考 OWASP ASI04 防御措施

---

## 7. 参考文献

### 学术论文

1. Packer, C., et al. (2023). *MemGPT: Towards LLMs as Operating Systems*. arXiv:2310.08560. [942 citations]
2. Craik, F.I.M., & Lockhart, R.S. (1972). *Levels of Processing: A Framework for Memory Research*. Journal of Verbal Learning and Verbal Behavior, 11, 671-684.
3. Tulving, E. (1972). *Episodic and Semantic Memory*. In Organization of Memory.
4. Bartlett, F.C. (1932). *Remembering: A Study in Experimental and Social Psychology*. Cambridge University Press.
5. Schacter, D.L. (2001). *The Seven Sins of Memory*. Houghton Mifflin.
6. Wu, Y., et al. (2024). *LongMemEval: Benchmarking Long-term Memory in LLM-based Chat Assistants*.
7. Maharana, A., et al. (2024). *LoCoMo: Long Context Multimodal Dataset for Long Conversations*.
8. Lewis, P., et al. (2020). *Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks*. NeurIPS.
9. Malkov, Y.A., & Yashunin, D.A. (2018). *Efficient and Robust Approximate Nearest Neighbor Search Using Hierarchical Navigable Small World Graphs*. IEEE TPAMI.
10. Nogueira, R., & Cho, K. (2019). *Passage Re-ranking with BERT*. arXiv:1901.04085.
11. Cormack, G.V., Clarke, C.L.A., & Buettcher, S. (2009). *Reciprocal Rank Fusion outperforms Condorcet and individual Rank Learning Methods*. SIGIR.
12. (2026). *True Memory: A Retrieval-Centered Architecture for Agent Recall*. arXiv:2605.04897.
13. (2026). *A Critical Analysis of the MemPalace Architecture*. arXiv:2604.21284.
14. (2026). *HeLa-Mem: Hebbian Learning and Associative Memory for LLM Agents*. arXiv:2604.16839.
15. (2026). *MemForest: An Efficient Agent Memory System with Hierarchical Temporal Indexing*. arXiv:2605.23986.
16. (2026). *Tiered Memory Architecture and the Retrieval Bottleneck in Long-Running LLM Agents*. arXiv:2605.03675.
17. (2026). *A Workload-Adaptive Cascade Retrieval Substrate for Long-Term Conversational Memory*. arXiv:2605.25092.
18. (2026). *Memory is Reconstructed, Not Retrieved: Graph Memory for LLM Agents*. arXiv:2606.06036.
19. (2026). *Eywa: Provenance-Grounded Long-Term Memory for AI Agents*. arXiv:2605.30771.
20. (2026). *Scaling Self-Evolving Agents via Parametric Memory*. arXiv:2606.04536.
21. (2025). *Agent Skills for Large Language Models: Architecture, Acquisition, Security*. arXiv:2602.12430.
22. (2025). *Securing the Model Context Protocol: Defending LLMs Against Tool Poisoning*. arXiv:2512.06556.
23. (2025). *MCP-ITP: An Automated Framework for Implicit Tool Poisoning in MCP*. arXiv:2601.07395.
24. (2026). *Systematic Analysis of MCP Security*. arXiv:2508.12538.

### 行业标准与规范

25. Anthropic. (2024). *Model Context Protocol Specification*.
26. agentskills.io. (2025). *Agent Skills Open Standard*.
27. OWASP. (2025). *Top 10 for Agentic Applications (ASI)*.
28. CSA. (2026). *MCP RCE Design Vulnerability Research Note*.

### 开源项目

29. Mem0 AI. (2024). *Mem0: The Memory Layer for AI Agents*. GitHub: mem0ai/mem0.
30. Letta AI. (2023-2026). *Letta (formerly MemGPT)*. GitHub: letta-ai/letta. [~52K stars total]
31. Zep AI. (2024). *Zep / Graphiti*. GitHub: getzep/zep.
32. Hindsight AI. (2025). *Hindsight Memory*.
33. MemPalace. (2026). GitHub: [fastest-growing AI project, 47.9K stars in 2 weeks].

---

*报告结束。如需针对某个具体方向深入展开，请告知。*
