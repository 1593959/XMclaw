## Dimension 02: 分层记忆架构
### 角度：Working / Short-term / Long-term / Procedural 的设计与实现

---

### 1. Working Memory — 上下文窗口管理

Claim: LLM 的上下文窗口被类比为 CPU 的 RAM，是 Agent 的 working memory；它动态、按推理重置，且仅包含当前加载的信息 [^1]。
Source: Atlan — Working Memory in LLMs: The Context Window as Cognitive Architecture
URL: https://atlan.com/know/working-memory-llms/
Date: 2026-04-17
Excerpt: "In working memory in AI agents, the context window is the entire workspace. Every token your model can attend to, reason over, and generate from lives here, and only here. Unlike model weights, which encode static trained knowledge, the context window is dynamic and per-inference: it resets between sessions, holds exactly what you load into it, and disappears the moment the call ends."
Context: 将认知科学中 Baddeley & Hitch (1974) 的 working memory 模型映射到 Transformer 架构：token 序列对应 phonological loop，multi-head attention 对应 central executive，in-context few-shot examples 对应 episodic buffer。
Confidence: high

Claim: 上下文压缩技术分为三大族：abstractive summarization（重写）、semantic chunking（选择）、token-level compression（修剪）；生产系统通常组合使用 [^2]。
Source: SurePrompts — Context Compression Techniques (2026)
URL: https://sureprompts.com/blog/context-compression-techniques
Date: 2026-04-20
Excerpt: "Context compression trades fidelity for token budget. Summarization rewrites; semantic chunking selects; token-level compression trims ... Real systems combine them — structured summary plus verbatim tail plus top-k retrieval plus a final token-level pass — and measure at every step, because regressions are silent and confidence is cheap."
Context: 即使 1M token 上下文窗口，成本、延迟和 "lost in the middle" 问题仍然存在，压缩仍是必要手段。
Confidence: high

Claim: Anthropic 的 Claude Code 使用 compaction 策略：当上下文接近上限时，对对话或任务历史进行摘要，保留关键细节（架构决策、未解决问题），丢弃冗余工具输出和已被取代的推理 [^3]。
Source: DataHub — Context Window Optimization Strategies
URL: https://datahub.com/blog/context-window-optimization/
Date: 2026-05-28
Excerpt: "Compaction addresses this by summarizing the conversation or task history when it nears the context window limit and restarting with a compressed version. The compressed context preserves critical details (architectural decisions, unresolved issues, key findings) while discarding redundant tool outputs and superseded reasoning."
Context: 最佳实现按信息耐久性分层：目标和约束持久保留，近期结果保持完整细节，较旧的中间输出逐步摘要。
Confidence: high

Claim: 自适应上下文压缩框架通过动态 token budget 分配（Bt = Bmax − λ·Ht）和多目标优化（任务质量 + 连贯性保持 + token 减少）来平衡记忆保持与效率 [^4]。
Source: arXiv — Developing Adaptive Context Compression Techniques for LLMs in Long-Running Interactions
URL: https://arxiv.org/html/2603.29193v1
Date: 2026-03-31
Excerpt: "Equation 3 defines a dynamic budget where Ht represents dialogue entropy. Higher uncertainty increases available context, while stable interactions allow stronger compression... Equation 4 combines task quality, coherence preservation, and token reduction."
Context: 在 LOCOMO、LOCCO 和 LongBench 上的实验表明，该方法在答案质量、检索性能和连贯性方面均有提升，同时实现了显著的 token 减少和延迟降低。
Confidence: medium

---

### 2. Short-term / Session Memory — 会话级状态保持

Claim: 生产 Agent 的 session memory 需要区分 in-thread 会话历史（checkpointer）与 cross-thread 用户级长期事实（BaseStore）；二者不可混淆 [^5]。
Source: Atlan — How to Add Long-Term Memory to LangChain Agents
URL: https://atlan.com/know/long-term-memory-langchain-agents/
Date: 2026-04-08
Excerpt: "LangGraph introduced two primitives that fix this cleanly: Checkpointer: manages conversation history within a single thread_id... BaseStore: manages facts that survive across ALL sessions for a user. Namespaced by user identity, not by thread."
Context: LangChain 的 ConversationBufferMemory 已弃用，因其无法处理 tool-calling agents、仅存储于进程内存、无多用户/多线程概念。
Confidence: high

Claim: Session memory 与 external memory 的交互遵循 "promotion" 模式：RAG 将长期存储中的相关信息注入会话上下文（Session Memory / Context Window），实现 just-in-time 可用性 [^6]。
Source: Vinish.dev — Session Memory vs External Memory in AI Systems
URL: https://vinish.dev/session-memory-vs-external-memory-in-ai
Date: 2025-12-23
Excerpt: "This process effectively promotes information from Long-Term storage (External) to Working Memory (Session) just in time for it to be useful."
Context: 混合方法包括 Summary Buffering（将原始对话摘要存入 Session Memory，完整记录移至 External Memory）和 Recursive Memory Structures（在 Session Memory 中保留 scratchpad/core memory 块存放永久笔记）。
Confidence: high

Claim: Mem0 将会话记忆按生命周期分层：conversation memory（单响应）、session memory（分钟到小时，显式清除）、user/organizational memory（长期存活）；短期工作上下文通过 promotion 流入长期语义记忆，而非无限累积 [^7]。
Source: Mem0 — Memory Eviction and Forgetting in AI Agents
URL: https://mem0.ai/blog/memory-eviction-and-forgetting-in-ai-agents
Date: 2026-05-22
Excerpt: "The memory layer also separates lifetimes by tier. Conversation memory is single-response. Session memory persists for minutes to hours and is cleared explicitly. User and organizational memory are long-lived. That tiering matches the cognitive model: short-term, episodic working context flows into longer-term, semantic memory through promotion rather than indefinite accumulation."
Context: Mem0 的 Memory Decay 是搜索时重排序层（非删除）：最近访问记忆最高 1.5x 分数提升，未使用记忆衰减至 0.3x；事实仍在存储中，只是更难被召回。
Confidence: high

---

### 3. Long-term Memory — 持久化存储与召回

Claim: 生产级 Agent 长期记忆需要三层架构：In-Context Memory（短期/工作记忆，类似 RAM）、Vector Store Memory（语义长期记忆，RAG）、Structured File/Database Storage（精确长期记忆，用于元数据和配置）[^8]。
Source: MindStudio — How to Build an AI Agent with Persistent Memory Using RAG and Vector Search
URL: https://www.mindstudio.ai/blog/ai-agent-persistent-memory-rag-vector-search/
Date: 2026-05-16
Excerpt: "Persistent memory for AI agents requires a three-layer architecture: in-context (short-term), vector store (semantic long-term), and structured file storage (exact long-term)."
Context: 向量存储适合模糊语义匹配（"预算约束"匹配"成本限制"），结构化存储适合精确检索（用户账户 ID、配置值）。
Confidence: high

Claim: 现代 Agent 记忆架构采用多种存储结构：Vector Databases（语义召回）、Knowledge Graphs（结构化关系推理）、Key-Value Stores（快速精确查找）、Relational/NoSQL Databases（状态日志和结构化知识）[^9]。
Source: AI Agent Memory Explained: Architectures, Mechanisms, and Persistent Context
URL: https://aiagentmemory.org/articles/ai-agent-memory-explained/
Date: 2026-03-24
Excerpt: "Key-Value Stores: Simple and effective for direct retrieval... Vector Databases: Increasingly popular, especially with LLMs... Knowledge Graphs: Represent knowledge as a network of entities and relationships... Databases (Relational, NoSQL): Traditional databases can be used to store agent states, logs, and structured knowledge."
Context: 长期存储需要比工作记忆更结构化的方法，需明确组织信息以支持高效检索和更新。
Confidence: high

Claim: Redis 等平台提供统一的 Agent 记忆基础设施层，同时支持即时上下文（short-term）和跨会话存储（long-term），解决纯上下文窗口无法实现的 session persistence、cross-session learning 和 selective context access [^10]。
Source: Redis — AI agent memory: types, architecture & implementation
URL: https://redis.io/blog/ai-agent-memory-stateful-systems/
Date: 2026-02-03
Excerpt: "Even with frontier models offering very large context windows (hundreds of thousands of tokens), you need memory architecture for several reasons: session persistence across days and weeks, cross-session learning that builds knowledge over time, and selective context access where the agent pulls only relevant information rather than processing everything."
Context: Short-term memory 通过 checkpoint 机制（如 Redis 或 in-memory savers）持久化线程级状态；长期记忆通过向量搜索和外部数据库实现跨会话召回。
Confidence: high

---

### 4. Procedural Memory — 技能/工作流记忆

Claim: Procedural memory 编码 Agent 的行为方式（技能、决策规则、工作流模式），与语义记忆（事实知识）有本质区别；CoALA 框架识别出三种存储基质：in-weights（LLM 参数）、code-embedded（执行器逻辑）、explicit instruction sets（系统提示/规则库）[^11]。
Source: Atlan — Semantic Memory vs Procedural Memory for AI Agents (2026)
URL: https://atlan.com/know/semantic-memory-vs-procedural-memory-ai-agents/
Date: 2026-04-17
Excerpt: "Procedural memory in AI agents encodes how the agent behaves: its skills, decision rules, workflow patterns, and behavioral constraints. Unlike semantic memory, it is not retrieved at inference time; it operates automatically, shaping every action the agent takes... The CoALA framework (arXiv:2309.02427) identifies three distinct storage substrates for procedural memory."
Context: 三种基质的更新机制不同：fine-tuning（in-weights，慢且贵）、prompt engineering（explicit instructions，快但脆弱）、code deployment（executor-embedded，可审计）。
Confidence: high

Claim: Agent Workflow Memory (AWM, Wang et al., 2024) 从成功轨迹中提取可复用的多步工作流模板作为参数化过程记忆，在 Mind2Web 和 WebArena 上分别实现 24.6% 和 51.1% 的相对提升；但 AWM 仅从成功轨迹学习，缺乏从失败中提取教训的机制 [^12]。
Source: arXiv — Trajectory-Informed Memory Generation for Self-Improving Agent Systems
URL: https://arxiv.org/html/2603.10600v1
Date: 2026-03-11
Excerpt: "Agent Workflow Memory (AWM) (Wang et al., 2024) extracts reusable multi-step workflows from successful agent trajectories in web navigation, achieving 24.6% and 51.1% relative improvements on Mind2Web and WebArena respectively... However, AWM only learns from successful trajectories—it has no mechanism for extracting lessons from failures, recoveries, or inefficient executions."
Context: AWM 展示了"雪球效应"：简单工作流可组合成更复杂的工作流。后续工作 Memp (Fang et al., 2025) 将过程记忆视为一级优化对象，系统探索构建、检索和更新策略。
Confidence: high

Claim: Memp 框架专注于跨轨迹（cross-trajectory）过程记忆：将过去成功工作流蒸馏为可复用的过程先验（procedural priors），并引入更新机制使过程记忆持续改进；在 ALFWorld 和 TravelPlanner 上，配备 Memp 的 Agent 成功率更高、步骤和 token 消耗大幅减少 [^13]。
Source: VentureBeat / iStartValley — How procedural memory can cut the cost and complexity of AI agents
URL: https://venturebeat.com/ai/how-procedural-memory-can-cut-the-cost-and-complexity-of-ai-agents
Date: 2025-12-22
Excerpt: "Mem0 and A-MEM are excellent works… but they focus on remembering salient content within a single trajectory or conversation... Memp, by contrast, targets cross-trajectory procedural memory... By distilling past successful workflows into reusable procedural priors, Memp raises success rates and shortens steps... we also introduce an update mechanism so that this procedural memory keeps improving."
Context: 过程记忆具有可迁移性：GPT-4o 生成的过程记忆可赋予较小的 Qwen2.5-14B 模型，显著提升其成功率和减少步骤数。
Confidence: high

Claim: LangMem 是唯一主流框架，原生支持在运行时更新 explicit instruction set 基质（procedural memory）：Agent 可通过 prompt optimization 算法更新自身系统提示指令，实现自我改进 [^14]。
Source: Atlan — Semantic Memory vs Procedural Memory for AI Agents (2026)
URL: https://atlan.com/know/semantic-memory-vs-procedural-memory-ai-agents/
Date: 2026-04-17
Excerpt: "LangMem is currently the only major framework with first-class support for updating the explicit instruction set substrate at runtime."
Context: LangMem 的 procedural memory 使 Agent 能够根据累积经验重写自身系统提示（如"删除前始终确认"），这是 Mem0 或 Zep 不具备的能力。
Confidence: high

---

### 5. 分层间的数据流动 — 写入路径、晋升机制、遗忘策略

Claim: 生产 Agent 的异步记忆流水线是 2026 年的关键模式：episodic 写入可内联，但 semantic 和 procedural 写入是 LLM 驱动且慢，应通过队列（NATS/SQS）异步处理，以保持 p95 延迟低且使记忆富集幂等和可重跑 [^15]。
Source: CallSphere — Agent Memory Patterns: Episodic, Semantic, and Procedural Stores in Production
URL: https://callsphere.ai/blog/agent-memory-patterns-episodic-semantic-procedural-2026
Date: 2026-05-31
Excerpt: "The single biggest mistake in 2026 production agents is doing memory writes inline with the user-facing request. Episodic writes can be inline (low cost), but semantic and procedural writes are LLM-driven and slow. Run them on a queue... This keeps p95 latency low and makes memory enrichment idempotent and re-runnable."
Context: 遗忘和冲突解决的三种实践模式：TTL on episodic（保留 30-90 天后删除）、Provenance on semantic（每个事实附带源 episode ID，冲突时由 LLM judge 合并或取代）、Versioned procedural（技能版本化，失败降低置信度，低于阈值退役）。
Confidence: high

Claim: 智能遗忘策略包括四种机制：TTL / age-based（合规驱动）、LRU / recency（高周转场景）、Salience scoring（重要性驱动）、Semantic supersession（新事实取代旧事实）；生产系统通常采用混合策略 [^16]。
Source: Mem0 — Memory Eviction and Forgetting in AI Agents
URL: https://mem0.ai/blog/memory-eviction-and-forgetting-in-ai-agents
Date: 2026-05-22
Excerpt: "The right answer is usually a hybrid. Passive aging keeps maintenance cheap. Active forgetting catches the cases passive aging gets wrong. A reasonable production policy is something like: TTL on long-tail entries to bound storage, LRU-style decay on retrieval scores to bound interference, and active supersession on every write so contradictions never accumulate."
Context: 常见失败模式：Over-eager forgetting（TTL 太短）、Stale facts surviving（纯语义取代系统等待矛盾事实到达，旧事实 lingering）、Stacked contradictions（未进行写时协调导致新旧事实共存）。
Confidence: high

Claim: UltraMemory / memory-lancedb-pro 实现三-tier promotion/demotion：Peripheral ↔ Working ↔ Core，基于访问计数、复合分数（Recency 40% + Frequency 30% + Intrinsic 30%）、重要性和年龄；使用 Weibull 拉伸指数衰减模型 [^17]。
Source: GitHub — CortexReach/memory-lancedb-pro
URL: https://github.com/CortexReach/memory-lancedb-pro
Date: 2026-03-23
Excerpt: "3 tiers: Core (β=0.8, floor=0.9) / Working (β=1.0, floor=0.7) / Peripheral (β=1.3, floor=0.5). Promotion/demotion rules based on access count, composite score, importance, age. Composite score: Recency 40% + Frequency 30% + Intrinsic 30%."
Context: 该架构还包括 L0/L1/L2 分层存储（Abstract / Overview / Full Content）和两阶段去重（向量预过滤 + LLM 决策）。
Confidence: high

Claim: memX 采用三-tier promotion（Core / Working / Peripheral）替代扁平重要性评分，确保身份级事实（Core, floor 0.9）永不衰减至召回阈值以下，而项目级上下文（Working）在无强化时自然老化退出 [^18]。
Source: GitHub — toby-bridges/memx-memory
URL: https://github.com/toby-bridges/memx-memory
Date: 2026-01-02
Excerpt: "The three-tier system (Core / Working / Peripheral) with decay floors ensures that identity-level facts (Core, floor 0.9) never fade below the recall threshold, while project-level context (Working) naturally ages out when no longer reinforced."
Context: 该设计借鉴了 MemGPT 和 BudgetMem 的分层记忆模型，但将其应用于用户建模而非 Agent 自我管理。
Confidence: medium

Claim: opencode-mem0 实现 STM/LTM 自动晋升：STM 衰减期 7 天，晋升阈值 0.7；LTM 衰减期 90 天；当 STM 记忆分数超过晋升阈值时自动毕业至 LTM，低于归档阈值的记忆在配置期后安全归档 [^19]。
Source: GitHub — ZeR020/opencode-mem0
URL: https://github.com/ZeR020/opencode-mem0
Date: 2026-05-17
Excerpt: "When STM memories score above the promotion threshold, they graduate to LTM automatically. Memories below the archive threshold are safely archived after the configured period."
Context: 冲突检测通过 LLM 辅助语义检查和启发式回退实现，支持保留新记忆、保留两者、合并或手动解决。
Confidence: medium

---

### 6. 系统实现对比

#### 6.1 Mem0

Claim: Mem0 采用混合数据存储架构（graph + vector + key-value），支持长期、短期、语义和 episodic 记忆；2026 年 4 月新算法实现单次 ADD-only 提取（不覆盖）、实体链接、多信号检索（语义 + BM25 + 实体匹配）和时间推理，在 LoCoMo 上达到 91.6 分 [^20]。
Source: GitHub — mem0ai/mem0
URL: https://github.com/mem0ai/mem0
Date: 2026-05-19
Excerpt: "Single-pass ADD-only extraction -- one LLM call, no UPDATE/DELETE. Memories accumulate; nothing is overwritten... Multi-signal retrieval -- semantic, BM25 keyword, and entity matching scored in parallel and fused... Temporal Reasoning -- time-aware retrieval that ranks the right dated instance."
Context: Mem0 的 p95 延迟约 0.88-1.09s，每请求约 6.9K tokens；与 BrowserUse 集成后实现 98% 任务完成率（vs 66%）和 41% 成本降低。
Confidence: high

Claim: Mem0 允许新事实与旧事实共存，不主动覆盖或删除现有记忆，以保留时间上下文并避免过早整合导致的信息丢失；这对隐私研究有双重含义：保留时间证据，但也可能使陈旧敏感信息长期留存 [^21]。
Source: arXiv — Privacy-Preserving Methods for Agent Memory Systems (2605.09530)
URL: https://arxiv.org/pdf/2605.09530
Date: 2026-05
Excerpt: "Mem0 allows new facts and old facts to coexist, without proactively overwriting or deleting existing memories, in order to preserve temporal context and avoid information loss caused by premature consolidation."
Context: 该策略与 Zep 的 temporal invalidation 形成对比，后者在事实矛盾时显式标记旧事实失效。
Confidence: high

#### 6.2 MemGPT / Letta

Claim: MemGPT pioneered OS paging 隐喻用于 LLM 上下文管理：main context（类比 RAM，由系统指令、working context 和 FIFO 队列组成）与 external context（类比磁盘存储，包含 archival memory 和 recall memory）之间通过显式函数调用进行数据移动 [^22]。
Source: arXiv — MemGPT: Towards LLMs as Operating Systems (Packer et al., 2023)
URL: https://arxiv.org/pdf/2310.08560
Date: 2023-10
Excerpt: "MemGPT's OS-inspired multi-level memory architecture delineates between two primary memory types: main context (analogous to main memory/physical memory/RAM) and external context (analogous to disk memory/disk storage)... MemGPT provides function calls that the LLM processor to manage its own memory without any user intervention."
Context: Main context 的 prompt tokens 分为三部分：system instructions（只读）、working context（固定大小读写块，存储关键事实和偏好）、FIFO queue（滚动消息历史，首索引包含被驱逐消息的递归摘要）。
Confidence: high

Claim: MemGPT 在 Deep Memory Retrieval (DMR) 基准上实现显著提升：标准 GPT-4 基线在 Multi-Session Chat 上仅 32.1% 准确率，MemGPT 提升至 92.5%；在嵌套 KV 检索 stress test 中，标准 GPT-4 在三层嵌套时降至 0%，MemGPT 通过迭代 archival 查找维持性能 [^23]。
Source: Bean Labs — MemGPT: Virtual Context Management for LLM Agents
URL: https://beancount.io/bean-labs/research-logs/2026/05/02/memgpt-towards-llms-as-operating-systems
Date: 2026-05-02
Excerpt: "With GPT-4, the standard fixed-context baseline achieves 32.1% accuracy; MemGPT jumps it to 92.5%. GPT-4 Turbo baseline: 35.3% → 93.4%... Standard GPT-4 hits 0% accuracy at three levels of nesting; MemGPT with GPT-4 sustains performance by making iterative archival lookups."
Context: 后续演进为 Letta 框架（2024 年 9 月），增加异步 "sleep-time compute" 用于记忆整合，并扩展为完整 Agent 平台（Letta Code、Conversations API、ADE）。
Confidence: high

#### 6.3 Zep / Graphiti

Claim: Zep 基于 Graphiti 构建双时态（bi-temporal）知识图谱：每条边携带两个时间戳——valid time（世界中何时为真）和 transaction time（何时被摄入）；事实具有显式 validity windows，旧边被标记失效而非删除，支持"三月时的合同状态是什么？"等查询 [^24]。
Source: Atlan — Zep vs Mem0: Benchmarks, Pricing, and When to Use Each
URL: https://atlan.com/know/zep-vs-mem0/
Date: 2026-04-08
Excerpt: "Bi-temporal modeling gives every fact two timestamps: valid time (when it was true in the world) and transaction time (when Graphiti ingested it). This supports queries like 'What was the contract status in March?', a capability that pure vector retrieval cannot provide."
Context: Graphiti 的三层子图：Episodic layer（原始会话）、Semantic layer（提取实体，9 节点类型，8 关系类型）、Community layer（高阶聚类）。Zep 的 DMR 基准分 94.8%，P95 图搜索延迟从 600ms 优化至 150ms。
Confidence: high

Claim: Zep 的 temporal invalidation 是现有框架中最复杂的整合机制：当新事实与现有事实矛盾时，Graphiti 使旧事实失效同时保留历史记录；LongMemEval 基准上 Zep 得分 63.8% 对比 Mem0 的 49.0%（GPT-4o），反映时间图谱架构优势 [^25]。
Source: Atlan — Episodic Memory for AI Agents: How It Works and Why It Matters
URL: https://atlan.com/know/episodic-memory-ai-agents/
Date: 2026-04-17
Excerpt: "When a new fact contradicts an existing one, Graphiti invalidates the old fact while retaining the historical record. This approximates episodic-to-semantic consolidation for conversational facts and is the most sophisticated consolidation mechanism of any current framework."
Context: 检索结合语义嵌入、BM25 关键词搜索和图遍历，P95 延迟约 300ms，检索时无需 LLM 调用。
Confidence: high

#### 6.4 LangMem

Claim: LangMem 支持 hot-path（Agent 在对话中主动调用记忆工具）和 background reflection（对话结束后或空闲期间自动提取、整合、更新记忆）两种记忆形成机制；定义语义、情景和过程三种记忆类型 [^26]。
Source: DigitalOcean — LangMem SDK for Agent Long-Term Memory
URL: https://www.digitalocean.com/community/tutorials/langmem-sdk-agent-long-term-memory
Date: 2026-02-19
Excerpt: "LangMem supports both hot-path and background reflection mechanisms for memory formation. The former allows the agent to actively invoke memory tools during an ongoing dialogue... The latter enables the system, after the conversation ends or during idle periods, to automatically extract, consolidate, and update memory."
Context: LangMem 的 p95 搜索延迟在 LOCOMO 基准上为 59.82 秒，不适合实时交互 Agent；但它是唯一支持 procedural memory（Agent 重写自身系统提示）的框架。
Confidence: high

#### 6.5 Generative Agents (Park et al., 2023)

Claim: Generative Agents 引入 memory stream（按时间顺序记录所有观察、计划和反思的综合列表）和 reflection（周期性将记忆综合为更高层次抽象）机制；检索模型结合 recency（指数衰减）、importance（LLM 评分）和 relevance（余弦相似度）三个信号 [^27]。
Source: arXiv — Generative Agents: Interactive Simulacra of Human Behavior (Park et al., 2023)
URL: https://arxiv.org/abs/2304.03442
Date: 2023-04-07
Excerpt: "Reflections are higher-level, more abstract thoughts generated by the agent... Reflections are generated periodically; in our implementation, we generate reflections when the sum of the importance scores for the latest events perceived by the agents exceeds a threshold (150 in our implementation)."
Context: Memory stream 中的检索评分函数：score(Mi|Q) = α_rec·r_i + α_imp·p_i + α_rel·ρ_i，其中 r_i 为 recency，p_i 为 importance，ρ_i 为 relevance。Reflection tree 将观察递归综合为高级自我概念。
Confidence: high

---

### 7. 综合对比表

| 维度 | Mem0 | MemGPT / Letta | Zep / Graphiti | LangMem | Generative Agents |
|------|------|----------------|----------------|---------|-------------------|
| 核心架构 | 混合存储（向量+图+KV） | OS 分页隐喻（Main ↔ External） | 双时态知识图谱 | LangGraph 原生记忆 SDK | Memory Stream + Reflection |
| 记忆类型 | 长期/短期/语义/情景 | Core/Archival/Recall | Episodic/Semantic/Community | 语义/情景/过程 | 观察/反思/计划 |
| 晋升机制 | 分层生命周期 + 衰减 | 显式函数调用（page in/out） | Temporal invalidation | Hot-path + Background | Importance 阈值触发反思 |
| 遗忘策略 | Memory Decay（搜索时重排序）+ TTL | 递归摘要 + 驱逐队列 | 失效标记（非删除） | 合并/解决矛盾 | 无显式遗忘，依赖检索过滤 |
| 延迟 (p95) | ~0.88-1.09s | 依赖检索轮次 | ~150-300ms | ~59.82s (LOCOMo) | 模拟环境，非生产 |
| 独特优势 | 时间推理 + 多信号检索 | 自主记忆管理（Agent 决定 page） | 事实变更历史查询 | Procedural memory（自改写提示） | 认知科学基础（反思树） |

---

### 参考文献索引

[^1]: Atlan, "Working Memory in LLMs", 2026-04-17  
[^2]: SurePrompts, "Context Compression Techniques", 2026-04-20  
[^3]: DataHub, "Context Window Optimization Strategies", 2026-05-28  
[^4]: arXiv 2603.29193, "Adaptive Context Compression for LLMs", 2026-03-31  
[^5]: Atlan, "How to Add Long-Term Memory to LangChain Agents", 2026-04-08  
[^6]: Vinish.dev, "Session Memory vs External Memory", 2025-12-23  
[^7]: Mem0, "Memory Eviction and Forgetting in AI Agents", 2026-05-22  
[^8]: MindStudio, "AI Agent Persistent Memory RAG Vector Search", 2026-05-16  
[^9]: AIAgentMemory.org, "AI Agent Memory Explained", 2026-03-24  
[^10]: Redis, "AI Agent Memory Types Architecture", 2026-02-03  
[^11]: Atlan, "Semantic vs Procedural Memory for AI Agents", 2026-04-17  
[^12]: arXiv 2603.10600, "Trajectory-Informed Memory Generation", 2026-03-11  
[^13]: VentureBeat, "How Procedural Memory Can Cut Cost and Complexity", 2025-12-22  
[^14]: Atlan, "Semantic vs Procedural Memory", 2026-04-17  
[^15]: CallSphere, "Agent Memory Patterns in Production", 2026-05-31  
[^16]: Mem0, "Memory Eviction and Forgetting", 2026-05-22  
[^17]: GitHub CortexReach/memory-lancedb-pro, 2026-03-23  
[^18]: GitHub toby-bridges/memx-memory, 2026-01-02  
[^19]: GitHub ZeR020/opencode-mem0, 2026-05-17  
[^20]: GitHub mem0ai/mem0, 2026-05-19  
[^21]: arXiv 2605.09530, "Privacy-Preserving Methods for Agent Memory", 2026-05  
[^22]: arXiv 2310.08560, "MemGPT: Towards LLMs as Operating Systems", 2023-10  
[^23]: Bean Labs, "MemGPT: Virtual Context Management", 2026-05-02  
[^24]: Atlan, "Zep vs Mem0", 2026-04-08  
[^25]: Atlan, "Episodic Memory for AI Agents", 2026-04-17  
[^26]: DigitalOcean, "LangMem SDK for Agent Long-Term Memory", 2026-02-19  
[^27]: arXiv 2304.03442, "Generative Agents", 2023-04-07  
