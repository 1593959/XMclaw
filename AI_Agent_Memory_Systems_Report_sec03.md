## 3. 记忆架构的分层设计

AI Agent 的记忆系统并非单一存储池，而是借鉴认知科学中的多层记忆理论，演化出具有明确功能边界与数据通路的工程架构。Atkinson-Shiffrin 三层模型（感觉记忆→短时记忆→长时记忆）已被学术界和工业界广泛接受为记忆系统设计的概念蓝图[^1][^7]。当前生产级系统通常将其扩展为四层结构：Working Memory（工作记忆）、Short-term / Session Memory（会话记忆）、Long-term Memory（长期记忆）与 Procedural Memory（程序性记忆）。每一层在存储介质、生命周期、访问延迟和更新机制上存在本质差异，层间通过晋升（promotion）、压缩（compression）与遗忘（forgetting）机制实现动态数据流动。

### 3.1 四层记忆模型

#### 3.1.1 Working Memory：上下文窗口作为"认知工作区"的管理与压缩技术

在 Transformer 架构中，上下文窗口（Context Window）被类比为 CPU 的 RAM，构成 Agent 的 Working Memory：它动态加载、按推理重置，且仅包含当前调用中显式注入的信息[^1]。即使前沿模型已支持百万级 token 上下文，成本、延迟与 "lost in the middle" 问题仍使压缩成为必要手段[^2]。

上下文压缩技术可分为三大族：abstractive summarization（重写式摘要）、semantic chunking（语义分块选择）与 token-level compression（修剪）。生产系统通常组合使用——以结构化摘要保留关键决策与未解决问题，以逐字尾部保留近期细节，再经 token 级压缩最终裁剪[^2]。Anthropic 的 Claude Code 采用 compaction 策略：当上下文接近上限时，对对话历史进行摘要，保留架构决策与未解决问题，丢弃冗余工具输出和已被取代的推理[^3]。自适应压缩框架进一步引入动态 token budget 分配（$B_t = B_{max} - \lambda \cdot H_t$），通过对话熵 $H_t$ 调节可用上下文量，在 LOCOMO 等长程交互基准上实现了质量提升与显著 token 减少[^4]。

#### 3.1.2 Short-term / Session Memory：跨 turn 状态保持与 promotion 模式

Session Memory 负责在单次会话的多轮交互中保持状态，需严格区分 in-thread 会话历史（checkpointer）与 cross-thread 用户级长期事实（BaseStore）[^5]。LangGraph 的 Checkpointer 管理单线程对话历史，BaseStore 则按用户身份命名空间管理跨会话事实，二者不可混淆[^5]。

Session Memory 与外部长期存储的交互遵循 "promotion" 模式：RAG 将长期存储中的相关信息按需注入会话上下文，实现 just-in-time 可用性[^6]。Mem0 将会话记忆按生命周期分层：conversation memory（单响应级）、session memory（分钟到小时级，显式清除）、user/organizational memory（长期存活）。短期工作上下文通过 promotion 流入长期语义记忆，而非无限累积[^7]。

#### 3.1.3 Long-term Memory：持久化语义存储与跨会话召回机制

生产级 Agent 的长期记忆通常采用三层存储架构：In-Context Memory（短期工作记忆，类比 RAM）、Vector Store Memory（语义长期记忆，支持 RAG 召回）与 Structured File/Database Storage（精确长期记忆，用于元数据与配置）[^8]。现代系统进一步混合多种存储结构：Vector Databases 负责模糊语义匹配，Knowledge Graphs 支持结构化关系推理，Key-Value Stores 提供快速精确查找，Relational/NoSQL Databases 承载状态日志与结构化知识[^9]。Redis 等平台提供统一的记忆基础设施层，同时支持即时上下文与跨会话存储，解决纯上下文窗口无法实现的 session persistence、cross-session learning 与 selective context access[^10]。

#### 3.1.4 Procedural Memory：技能/工作流记忆的工程实现短板

Procedural Memory 编码 Agent 的行为方式——技能、决策规则与工作流模式，与语义记忆（事实知识）有本质区别。CoALA 框架识别出三种存储基质：in-weights（LLM 参数）、code-embedded（执行器逻辑）与 explicit instruction sets（系统提示/规则库）[^11]。三者的更新机制差异显著：fine-tuning 慢且昂贵，prompt engineering 快速但脆弱，code deployment 可审计但需发布周期[^11]。

Agent Workflow Memory（AWM）从成功轨迹中提取可复用的多步工作流模板，在 Mind2Web 和 WebArena 上分别实现 24.6% 和 51.1% 的相对提升[^12]。Memp 框架进一步聚焦跨轨迹过程记忆，将过去成功工作流蒸馏为可复用的过程先验（procedural priors），并引入更新机制使过程记忆持续改进[^13]。然而，Procedural Memory 仍是理论最薄弱但行为影响最大的记忆类型——LangMem 是目前唯一主流框架，原生支持在运行时通过 prompt optimization 算法更新 explicit instruction set 基质，使 Agent 能够根据累积经验重写自身系统提示[^14]。

| 记忆层级 | 存储介质 | 典型寿命 | 容量约束 | 更新机制 | 代表实现 |
|---------|---------|---------|---------|---------|---------|
| Working Memory | 上下文窗口（RAM 类比） | 单次推理 | 模型上下文长度（4K–1M token） | 动态加载/Compaction 摘要 | Claude Code Compaction[^3] |
| Short-term / Session Memory | 进程内存/Checkpoint 存储 | 分钟至小时 | 单会话历史（通常 <100 轮） | 显式清除/Promotion 晋升 | LangGraph Checkpointer[^5] |
| Long-term Memory | 向量库+图+KV+关系库 | 永久（跨会话） | TB 级（外部存储） | 异步写入/增量更新 | Mem0 混合存储[^9] |
| Procedural Memory | 模型权重/代码/提示词 | 随版本迭代 | 受限于提示长度或代码体积 | Fine-tuning/Prompt 优化 | LangMem 自改写提示[^14] |

上表揭示了四层记忆在工程实现上的结构性分野。Working Memory 受限于模型架构的上下文长度，其管理核心在于压缩与筛选；Session Memory 的瓶颈在于线程隔离与状态持久化；Long-term Memory 的扩展性最优，但面临检索精度与存储成本的多目标权衡；Procedural Memory 则因更新机制复杂（涉及模型权重、代码部署或提示工程），成为当前生态中支持最不充分的层级。值得注意的是，前三层已有较为成熟的商用基础设施，而 Procedural Memory 仍停留在研究原型阶段，仅有 LangMem 提供了运行时自改写的闭环能力[^14]。

### 3.2 分层间的数据流动

#### 3.2.1 晋升机制：从 working 到 long-term 的自动触发条件

记忆晋升（promotion）指低层级记忆在满足特定条件后向高层级迁移的过程。生产系统通常采用复合评分机制驱动晋升。UltraMemory / memory-lancedb-pro 实现三-tier promotion/demotion：Peripheral ↔ Working ↔ Core，基于访问计数、复合分数（Recency 40% + Frequency 30% + Intrinsic 30%）与重要性，并使用 Weibull 拉伸指数衰减模型[^17]。memX 采用类似的三-tier 设计，确保身份级事实（Core, floor 0.9）永不衰减至召回阈值以下，而项目级上下文（Working）在无强化时自然老化退出[^18]。opencode-mem0 实现 STM/LTM 自动晋升：STM 衰减期 7 天，晋升阈值 0.7；LTM 衰减期 90 天；当 STM 记忆分数超过阈值时自动毕业至 LTM[^19]。

异步记忆流水线已成为 2026 年生产系统的关键模式：episodic 写入可内联处理，但 semantic 和 procedural 写入由 LLM 驱动且延迟高，应通过队列（NATS/SQS）异步执行，以保持 p95 延迟低且使记忆富集幂等和可重跑[^15]。

#### 3.2.2 遗忘策略：物理删除、逻辑失效、动态降权三种模式的对比

遗忘策略在架构层面分为三种模式。物理删除以 TTL（Time-To-Live）为代表：episodic 记忆保留 30–90 天后直接删除，适用于合规驱动的高周转场景[^15][^16]。逻辑失效以 Zep 的 temporal invalidation 为典型：当新事实与现有事实矛盾时，旧边被标记失效（invalid_at）而非删除，保留完整历史记录以支持 point-in-time 查询[^24]。动态降权以 Mem0 的 Memory Decay 为代表：搜索时对最近访问记忆给予最高 1.5x 分数提升，未使用记忆衰减至 0.3x；事实仍在存储中，只是更难被召回[^7]。

生产系统通常采用混合策略：TTL 约束长尾条目以控制存储边界，LRU 式检索分数衰减以控制干扰，写时主动取代（supersession）以避免矛盾累积[^16]。常见失败模式包括：Over-eager forgetting（TTL 过短导致有用信息丢失）、Stale facts surviving（纯语义取代系统等待矛盾事实到达，旧事实 lingering）与 Stacked contradictions（未进行写时协调导致新旧事实共存）[^16]。

#### 3.2.3 压缩与摘要：对话历史压缩、递归摘要、episode-based 压缩技术

压缩与摘要技术贯穿记忆全生命周期。在 Working Memory 层，compaction 通过重写式摘要将对话历史压缩为关键决策与未解决问题[^3]。在 Session Memory 层，Summary Buffering 将原始对话摘要存入会话记忆，完整记录移至外部存储[^6]。在 Long-term Memory 层，MemGPT 采用递归摘要：被驱逐出 FIFO 队列的消息在队列首索引处保留递归摘要，使 Agent 可通过迭代检索重建历史脉络[^22]。Generative Agents 的 reflection 机制则周期性将记忆综合为更高层次抽象，形成 reflection tree，将观察递归综合为高级自我概念[^27]。

### 3.3 主流系统实现对比

#### 3.3.1 Mem0：混合存储（Vector+Graph+KV）与生命周期分层

Mem0 采用混合数据存储架构（graph + vector + key-value），支持长期、短期、语义与 episodic 记忆。2026 年 4 月新算法实现单次 ADD-only 提取（不覆盖）、实体链接、多信号检索（语义 + BM25 + 实体匹配）与时间推理，在 LoCoMo 上达到 91.6 分[^20]。Mem0 允许新事实与旧事实共存，不主动覆盖或删除现有记忆，以保留时间上下文并避免过早整合导致的信息丢失[^21]。其 p95 延迟约 0.88–1.09 秒，每请求约 6.9K tokens[^20]。

#### 3.3.2 MemGPT/Letta：OS 分页式三层架构（Core/Archival/Recall）

MemGPT  pioneered OS 分页隐喻用于 LLM 上下文管理：main context（类比 RAM，由系统指令、working context 和 FIFO 队列组成）与 external context（类比磁盘存储，包含 archival memory 和 recall memory）之间通过显式函数调用进行数据移动[^22]。在 Deep Memory Retrieval (DMR) 基准上，标准 GPT-4 基线在 Multi-Session Chat 上仅 32.1% 准确率，MemGPT 提升至 92.5%；在嵌套 KV 检索 stress test 中，标准 GPT-4 在三层嵌套时降至 0%，MemGPT 通过迭代 archival 查找维持性能[^23]。后续演进为 Letta 框架，增加异步 "sleep-time compute" 用于记忆整合，并扩展为完整 Agent 平台[^23]。

#### 3.3.3 Zep/Graphiti：时态分层（Episodic→Semantic→Community）

Zep 基于 Graphiti 构建双时态（bi-temporal）知识图谱：每条边携带两个时间戳——valid time（世界中何时为真）和 transaction time（何时被摄入）；事实具有显式 validity windows，旧边被标记失效而非删除，支持"三月时的合同状态是什么？"等 point-in-time 查询[^24]。Graphiti 的三层子图包括 Episodic layer（原始会话）、Semantic layer（提取实体，9 节点类型，8 关系类型）与 Community layer（高阶聚类）。Zep 的 DMR 基准分 94.8%，P95 图搜索延迟从 600ms 优化至 150ms[^24]。在 LongMemEval 基准上 Zep 得分 63.8%，对比 Mem0 的 49.0%（GPT-4o），反映时间图谱架构优势[^25]。

#### 3.3.4 LangMem：LangGraph 原生扁平 KV + 向量搜索

LangMem 支持 hot-path（Agent 在对话中主动调用记忆工具）和 background reflection（对话结束后或空闲期间自动提取、整合、更新记忆）两种记忆形成机制；定义语义、情景和过程三种记忆类型[^26]。其独特优势在于原生支持 procedural memory：Agent 可通过 prompt optimization 算法更新自身系统提示指令，实现自我改进[^14]。然而，LangMem 的 p95 搜索延迟在 LOCOMO 基准上为 59.82 秒，不适合实时交互 Agent[^26]。

| 系统 | 核心架构 | 记忆分层 | 晋升机制 | 遗忘策略 | p95 延迟 | 独特优势 |
|------|---------|---------|---------|---------|---------|---------|
| Mem0 | 混合存储（向量+图+KV） | 对话/会话/用户/组织四级生命周期 | 分层生命周期 + 搜索时衰减 | Memory Decay（1.5x/0.3x 动态重排序）+ TTL | ~0.88–1.09s[^20] | 时间推理 + 多信号检索 |
| MemGPT/Letta | OS 分页隐喻（Main ↔ External） | Core / Archival / Recall | 显式函数调用（page in/out） | 递归摘要 + FIFO 驱逐队列 | 依赖检索轮次[^23] | 自主记忆管理（Agent 决定 page） |
| Zep/Graphiti | 双时态知识图谱 | Episodic / Semantic / Community | Temporal invalidation（写时标记失效） | 失效标记（非删除） | ~150–300ms[^24] | 事实变更历史查询（point-in-time） |
| LangMem | LangGraph 原生记忆 SDK | 语义 / 情景 / 过程 | Hot-path + Background reflection | 合并/解决矛盾 | ~59.82s（LOCOMO）[^26] | Procedural memory（自改写提示） |

上表对比显示，四种主流系统在架构哲学上存在显著分野。Mem0 以混合检索和低延迟见长，适合高频交互场景；MemGPT/Letta 以 OS 分页隐喻赋予 Agent 自主记忆管理能力，在嵌套检索 stress test 中表现突出；Zep/Graphiti 以双时态知识图谱实现最强时间推理能力，但存储与查询复杂度更高；LangMem 是唯一原生支持 procedural memory 的框架，但延迟使其更适合后台批处理而非实时对话。从晋升机制看，Mem0 和 memX 采用自动评分晋升，MemGPT 依赖显式函数调用，Zep 通过时态失效实现隐式降级，LangMem 则依赖 Agent 主动调用与后台反射的结合。遗忘策略同样分化：Mem0 选择动态降权（不删除），Zep 选择逻辑失效（保留历史），而物理删除（TTL）多见于底层 episodic 存储而非上层语义记忆。这种分化并非技术优劣之争，而是信任模型与合规需求的差异映射——高频交互场景倾向低成本降权，高可信度场景（医疗、金融）倾向保留完整审计轨迹[^16][^24]。

[^1]: Atlan. "Working Memory in LLMs: The Context Window as Cognitive Architecture." 2026-04-17. https://atlan.com/know/working-memory-llms/
[^2]: SurePrompts. "Context Compression Techniques." 2026-04-20. https://sureprompts.com/blog/context-compression-techniques
[^3]: DataHub. "Context Window Optimization Strategies." 2026-05-28. https://datahub.com/blog/context-window-optimization/
[^4]: arXiv. "Developing Adaptive Context Compression Techniques for LLMs in Long-Running Interactions" (arXiv:2603.29193). 2026-03-31. https://arxiv.org/html/2603.29193v1
[^5]: Atlan. "How to Add Long-Term Memory to LangChain Agents." 2026-04-08. https://atlan.com/know/long-term-memory-langchain-agents/
[^6]: Vinish.dev. "Session Memory vs External Memory in AI Systems." 2025-12-23. https://vinish.dev/session-memory-vs-external-memory-in-ai
[^7]: Mem0. "Memory Eviction and Forgetting in AI Agents." 2026-05-22. https://mem0.ai/blog/memory-eviction-and-forgetting-in-ai-agents
[^8]: MindStudio. "How to Build an AI Agent with Persistent Memory Using RAG and Vector Search." 2026-05-16. https://www.mindstudio.ai/blog/ai-agent-persistent-memory-rag-vector-search/
[^9]: AIAgentMemory.org. "AI Agent Memory Explained: Architectures, Mechanisms, and Persistent Context." 2026-03-24. https://aiagentmemory.org/articles/ai-agent-memory-explained/
[^10]: Redis. "AI agent memory: types, architecture & implementation." 2026-02-03. https://redis.io/blog/ai-agent-memory-stateful-systems/
[^11]: Atlan. "Semantic Memory vs Procedural Memory for AI Agents." 2026-04-17. https://atlan.com/know/semantic-memory-vs-procedural-memory-ai-agents/
[^12]: arXiv. "Trajectory-Informed Memory Generation for Self-Improving Agent Systems" (arXiv:2603.10600). 2026-03-11. https://arxiv.org/html/2603.10600v1
[^13]: VentureBeat. "How procedural memory can cut the cost and complexity of AI agents." 2025-12-22. https://venturebeat.com/ai/how-procedural-memory-can-cut-the-cost-and-complexity-of-ai-agents
[^14]: Atlan. "Semantic Memory vs Procedural Memory for AI Agents." 2026-04-17. https://atlan.com/know/semantic-memory-vs-procedural-memory-ai-agents/
[^15]: CallSphere. "Agent Memory Patterns: Episodic, Semantic, and Procedural Stores in Production." 2026-05-31. https://callsphere.ai/blog/agent-memory-patterns-episodic-semantic-procedural-2026
[^16]: Mem0. "Memory Eviction and Forgetting in AI Agents." 2026-05-22. https://mem0.ai/blog/memory-eviction-and-forgetting-in-ai-agents
[^17]: GitHub. "CortexReach/memory-lancedb-pro." 2026-03-23. https://github.com/CortexReach/memory-lancedb-pro
[^18]: GitHub. "toby-bridges/memx-memory." 2026-01-02. https://github.com/toby-bridges/memx-memory
[^19]: GitHub. "ZeR020/opencode-mem0." 2026-05-17. https://github.com/ZeR020/opencode-mem0
[^20]: GitHub. "mem0ai/mem0." 2026-05-19. https://github.com/mem0ai/mem0
[^21]: arXiv. "Privacy-Preserving Methods for Agent Memory Systems" (arXiv:2605.09530). 2026-05. https://arxiv.org/pdf/2605.09530
[^22]: arXiv. "MemGPT: Towards LLMs as Operating Systems" (arXiv:2310.08560). 2023-10. https://arxiv.org/pdf/2310.08560
[^23]: Bean Labs. "MemGPT: Virtual Context Management for LLM Agents." 2026-05-02. https://beancount.io/bean-labs/research-logs/2026/05/02/memgpt-towards-llms-as-operating-systems
[^24]: Atlan. "Zep vs Mem0: Benchmarks, Pricing, and When to Use Each." 2026-04-08. https://atlan.com/know/zep-vs-mem0/
[^25]: Atlan. "Episodic Memory for AI Agents: How It Works and Why It Matters." 2026-04-17. https://atlan.com/know/episodic-memory-ai-agents/
[^26]: DigitalOcean. "LangMem SDK for Agent Long-Term Memory." 2026-02-19. https://www.digitalocean.com/community/tutorials/langmem-sdk-agent-long-term-memory
[^27]: arXiv. "Generative Agents: Interactive Simulacra of Human Behavior" (arXiv:2304.03442). 2023-04-07. https://arxiv.org/abs/2304.03442
