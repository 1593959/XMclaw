## Dimension 06: 时间推理与记忆演化
### 角度：双时态建模、记忆巩固/衰减/更新、时态知识图谱

---

### 1. 双时态建模（Bi-temporal Modeling）

```
Claim: Zep/Graphiti 在每条边上维护四个时间戳，实现完整的双时态建模：t_valid（事实在现实世界中开始为真）、t_invalid（事实在现实世界中失效）、t'_created（系统记录时间）、t'_expired（系统作废时间），从而支持时间点查询与自动失效。[^1]
Source: Zep: A Temporal Knowledge Graph Architecture for Agent Memory (arXiv 2501.13956)
URL: https://arxiv.org/html/2501.13956v1
Date: 2025-01-20
Excerpt: "The system tracks four timestamps: t' created and t' expired ∈ T' monitor when facts are created or invalidated in the system, while t_valid and t_invalid ∈ T track the temporal range during which facts held true."
Context: Zep 的 Graphiti 引擎为 AI Agent 记忆构建时态知识图谱，区别于传统向量 RAG 的核心创新。
Confidence: high
```

```
Claim: 双时态（bitemporal）数据库的两个正交时间维度定义如下：valid time 是事实在模型化现实中为真的时间段；transaction time 是事实在数据库中被记录为当前数据的时间段。二者独立记录，valid time 可自由修改且可指向未来，transaction time 不可晚于当前时间且不可更改。[^2]
Source: Snodgrass & Ilsoo / Temporal Database Survey (VLDB 1998)
URL: https://www.vldb.org/conf/1998/p345.pdf
Date: 1998
Excerpt: "The valid time of a database fact is the time when the fact is true in the modeled reality, while the fact's transaction time is the time during which it is current in the database. Valid and transaction time are orthogonal in that each could be independently recorded."
Context: 数据库理论经典定义，被 Zep、XTDB、Datomic 等系统直接继承。
Confidence: high
```

```
Claim: XTDB 将 bitemporal 作为一等公民：put 事务可指定 valid-time（默认等于 transaction-time），文档持续有效直到被新的 put 或 delete 显式更新；支持 as-of 查询同时指定 valid-time 与 transaction-time，实现审计与历史分析分离。[^3]
Source: XTDB Bitemporality Documentation
URL: https://v1-docs.xtdb.com/concepts/bitemporality/
Date: 2018-12-31
Excerpt: "XTDB is optimised for efficient and globally consistent point-in-time queries using a pair of transaction-time and valid-time timestamps... valid-time is an arbitrary time that can originate from an upstream system, or by default is set to transaction-time."
Context: 生产级 bitemporal 文档数据库，为 Agent 记忆系统提供底层存储范式参考。
Confidence: high
```

```
Claim: BiTemporal RDF (BiTRDF) 将 valid time 与 transaction time 引入标准 RDF，把所有资源与关系视为 inherently bitemporal，支持时态环境下的类型传播、domain-range 推理与传递关系。[^4]
Source: Tansel, Wu & Wang — Time Travel with the BiTemporal RDF Model (Mathematics, MDPI 2025)
URL: https://ideas.repec.org/a/gam/jmathe/v13y2025i13p2109-d1689136.html
Date: 2025-02-02
Excerpt: "BiTRDF treats all resources and relationships as inherently bitemporal, enabling the representation and reasoning of complex temporal relationships in RDF."
Context: 语义 Web 与时态数据库的交叉研究，为 Agent 记忆的图表示提供形式化基础。
Confidence: medium
```

---

### 2. 时间推理查询（Temporal Reasoning Queries）

```
Claim: LongMemEval 基准测试将 temporal reasoning 列为五大核心能力之一（其余为信息抽取、多会话推理、知识更新、弃权），包含 133 道时间推理题，要求模型在约 115K token 的多会话历史中回答时间敏感问题。[^5]
Source: LongMemEval: Benchmarking Chat Assistants on Long-Term Interactive Memory (Wu et al., 2024)
URL: https://arxiv.org/pdf/2410.10813v2
Date: 2024-10
Excerpt: "We introduced LongMemEval, a comprehensive benchmark designed to evaluate the long-term memory abilities of chat assistants across five core memory tasks: information extraction, multi-session reasoning, temporal reasoning, knowledge updates, and abstention."
Context: 当前评估 Agent 长期记忆能力最权威的基准之一；Temporal Reasoning 子任务专门测试时间依赖推理。
Confidence: high
```

```
Claim: Temporal Semantic Memory (TSM) 提出语义时间线（semantic timeline）替代对话时间线，通过 spaCy 解析查询中的显式与相对时间表达，构建时间约束 T_q，并在检索阶段以时间过滤为 primary key、语义相似度为 secondary key 进行重排，在 LongMemEval-S 的 Temporal 类别上取得 69.92% 准确率，较 Zep (36.50%) 提升近一倍。[^6]
Source: Su et al. — Beyond Dialogue Time: Temporal Semantic Memory for Personalized LLM Agents (arXiv 2601.07468)
URL: https://arxiv.org/html/2601.07468v1
Date: 2026-01-12
Excerpt: "TSM achieves the highest overall accuracy of 74.80%... We set new state-of-the-art results on Temporal, Multi-Session, and Knowledge-Update questions... The notable improvement in Multi-Session accuracy (+20.30%) underscores the crucial role of durative memory."
Context: TSM 针对现有记忆系统按对话时间而非实际发生时间组织记忆的缺陷，提出 durative memory 概念。
Confidence: high
```

```
Claim: Graphiti 的检索管线在查询时自动过滤 valid-from/valid-to 窗口：查询 "now" 返回开放窗口的边；查询 "2025-12-01" 返回该日期落在有效区间内的边；上下文构造器将事实与其时间有效范围一起格式化，使 LLM 直接获得时间对齐的证据。[^7]
Source: Zep — What Is a Temporal Knowledge Graph?
URL: https://www.getzep.com/ai-agents/temporal-knowledge-graph/
Date: 2026-05-31
Excerpt: "A query for 'what was true on 2025-12-01' filters to edges whose valid-from/valid-to window contains that date; a query for 'now' filters to open windows. Because superseded facts are closed, not deleted, the history stays auditable."
Context: Zep 官方文档对时态查询机制的解释；P95 检索延迟约 300ms。
Confidence: high
```

```
Claim: TDBench 提出利用时态数据库系统评估 LLM 的事实时间敏感问答能力，自动验证答案与时间引用，发现 RAG 检索到的上下文若存在时间错位（temporal misalignment），模型倾向于回答 "no answer" 而非依赖自身知识。[^8]
Source: Harnessing Temporal Databases for Systematic Evaluation of Factual Time-Sensitive QA in LLMs (arXiv 2508.02045)
URL: https://arxiv.org/html/2508.02045v2
Date: 2026-03-02
Excerpt: "When the additional contexts retrieved via RAG do not contain the gold answer – due to the temporal misalignment issue – we observe that models tend to respond with 'no answer' rather than relying on their own knowledge."
Context: 时间对齐（temporal alignment）是 RAG 向 Agent 记忆演进时必须解决的前置问题。
Confidence: medium
```

---

### 3. 记忆演化机制（Memory Evolution）

#### 3.1 记忆巩固（Consolidation）

```
Claim: Generative Agents (Park et al., 2023) 采用记忆流（memory stream）+ 周期性反思（reflection）的架构：原始观察以自然语言记录，通过重要性、时效性、相关性评分筛选；当记忆积累到一定阈值，Agent 调用 LLM 生成更高层次的反思（reflection），将多个观察综合为抽象语义记忆。[^9]
Source: Park et al. — Generative Agents: Interactive Simulacra of Human Behavior (UIST 2023)
URL: https://arxiv.org/abs/2304.03442
Date: 2023-04
Excerpt: "Generative Agents store observations in a memory stream, retrieve records using recency, importance, and relevance, and synthesize reflections for later planning and interaction."
Context: 学术界最早系统实现 Agent 记忆巩固的架构；被后续 MemGPT、Reflexion 等大量引用。
Confidence: high
```

```
Claim: HeLa-Mem 将反射巩固（Reflective Consolidation）作为三大模块之一，通过 Hebbian 关联构建动态图，再经反思蒸馏将语义知识从原始片段中抽象出来，实现 episodic → semantic 的层级跃迁。[^10]
Source: HeLa-Mem: Hebbian Learning and Associative Memory for LLM Agents (arXiv 2604.16839)
URL: https://arxiv.org/html/2604.16839v1
Date: 2026-04-18
Excerpt: "The framework consists of three modules: (1) Hebbian Association for dynamic graph construction; (2) Reflective Consolidation for semantic knowledge distillation; and (3) Retrieval and Response using a Dual-Path strategy."
Context: 将认知心理学中的 Hebbian 学习与反射巩固结合，强调知识蒸馏而非简单摘要。
Confidence: medium
```

#### 3.2 记忆衰减（Decay）

```
Claim: MemoryBank (Zhong et al., 2024) 首次系统地将 Ebbinghaus 遗忘曲线引入 LLM Agent 记忆：记忆强度随时间指数衰减，每次召回后强度增强并将经过时间重置为零，实现类人的“用进废退”。[^11]
Source: MemoryBank: Enhancing Large Language Models with Long-Term Memory (Zhong et al., 2024)
URL: https://arxiv.org/abs/2305.10250
Date: 2024
Excerpt: "MemoryBank's memory strength is enhanced by 1 each time a memory piece is recalled, simulating more human-like memory behavior and reducing the probability of forgetting the memory by setting the elapsed time to zero."
Context: 后续 Engram、OpenClaw 等系统的遗忘曲线实现均直接引用 MemoryBank。
Confidence: high
```

```
Claim: 2024 年发表的 Forgetting Curve 论文提出用遗忘曲线可靠测量长上下文模型的记忆能力：通过 copy accuracy 与 language modeling accuracy 的差值绘制遗忘曲线，区分 fine-grained memory（99% 复制精度）与 coarse-grained memory；发现 Transformer 上下文扩展方法有效，但 RNN/SSM（Mamba、RWKV）在超长序列上 fine-grained memory 几乎为零。[^12]
Source: Forgetting Curve: A Reliable Method for Evaluating Memorization Capability for Long-context Models (EMNLP 2024)
URL: https://arxiv.org/pdf/2410.04727
Date: 2024-10
Excerpt: "RNNs like Mamba and RWKV exhibit higher language modeling accuracy than copy accuracy outside training context length, suggesting an RNN model memory issue which seems to negatively affect correct token prediction at long context."
Context: 该工作将认知心理学的遗忘曲线概念转化为 LLM 长上下文评估工具，与 Agent 记忆系统的衰减机制设计形成呼应。
Confidence: high
```

```
Claim: Engram 的 "sleep" 巩固管道直接实现 Ebbinghaus 指数衰减：strength *= decayRate ^ daysSinceLastAccess（默认 0.95^days），频繁访问的记忆抵抗衰减，从未召回的记忆最终低于剪枝阈值并被归档。[^13]
Source: Engram AI Memory — Research Foundations
URL: https://github.com/foramoment/engram-ai-memory
Date: 2025
Excerpt: "The foundational law of memory decay: retention decreases exponentially over time unless reinforced. Engram's sleep consolidation implements this directly: strength *= decayRate ^ daysSinceLastAccess."
Context: Engram 明确将认知心理学理论（Ebbinghaus、ACT-R、Hebbian）工程化为 Agent 记忆系统。
Confidence: high
```

```
Claim: Mem0 在 2026 年的博客中提出记忆衰减的搜索时重排序层：最近访问的记忆获得最高 1.5x 分数提升，未使用的记忆向 0.3x 抑制；区分被动老化（TTL/LRU）与主动遗忘（LLM 驱动的矛盾消解），主张“被动老化用于噪音，主动遗忘用于事实”。[^14]
Source: Mem0 — Memory Eviction and Forgetting in AI Agents
URL: https://mem0.ai/blog/memory-eviction-and-forgetting-in-ai-agents
Date: 2026-05-22
Excerpt: "Passive aging keeps maintenance cheap. Active forgetting catches the cases passive aging gets wrong... Passive aging is for noise. Active forgetting is for facts."
Context: Mem0 从向量存储的 TTL/过期策略演进为搜索时动态衰减，反映生产系统对遗忘机制的认知升级。
Confidence: high
```

#### 3.3 记忆更新与矛盾解决（Update & Contradiction Resolution）

```
Claim: Graphiti 采用 temporal invalidation（非删除）处理矛盾：当新边与现有边存在时间重叠的语义矛盾时，系统将受影响旧边的 t_invalid 设为无效化边的 t_valid，在事务时间线 T' 上始终优先新信息；旧边保留在图中，历史可审计。[^15]
Source: Zep Paper (arXiv 2501.13956) / Neo4j Blog — Graphiti: Knowledge graph memory for an agentic world
URL: https://neo4j.com/blog/developer/graphiti-knowledge-graph-memory/
Date: 2026-06-04
Excerpt: "When new information contradicts an existing fact, the system employs temporal edge invalidation. Instead of deleting the outdated relationship, it uses the t_invalid timestamp to mark the old edge as no longer valid, effectively archiving it while preserving its historical context."
Context: 这是 Zep/Graphiti 与 Mem0 等系统的核心架构差异：temporal invalidation 保留历史 vs 直接覆盖/删除。
Confidence: high
```

```
Claim: Mem0 的更新策略是破坏性的：检测到矛盾后执行 ADD / UPDATE / DELETE / NOOP 操作，直接覆盖或删除旧记忆，历史丢失；其 graph 变体 Mem0g 通过 LLM 调解重复检测，但不建模语义张力。[^16]
Source: MemX / Agent Memory vs RAG: The Real Difference
URL: https://memx.app/blog/agent-memory-vs-rag-the-real-difference/
Date: 2026-06-04
Excerpt: "The first is destructive: detect the contradiction and DELETE or overwrite the stale memory, which is the approach in Mem0's update phase. The store always reflects the current truth, at the cost of losing history."
Context: 生产级 Agent 记忆系统必须在“简单覆盖”与“时态版本化”之间做出显式架构选择。
Confidence: high
```

```
Claim: TOKI（2026）提出 Bitemporal Operator Algebra，将四种矛盾消解策略（last-writer-wins、evidence-weighted merge、await-confirmation、per-rule policy）统一为带隔离级别预条件的双时态算子族，并形式化三种 LLM 裁判特有的异常：replay inconsistency、belief-drift skew、audit erasure。[^17]
Source: TOKI: A Bitemporal Operator Algebra for Contradiction Resolution in LLM-Agent Persistent Memory (arXiv 2606.06240)
URL: https://arxiv.org/html/2606.06240v1
Date: 2026-01-05
Excerpt: "Toki types the four heuristics as one family of bitemporal operators over a dual-row schema, each carrying an isolation precondition and a provenance annotation that keeps the losing fact in an audit row."
Context: 首次将数据库并发控制理论（Berenson-Adya 隔离层级、K-relation provenance）系统引入 Agent 记忆写路径。
Confidence: high
```

```
Claim: TOKI 的跨系统测量发现：Mem0 v3 在写入 Alice 的经理先为 Bob 后为 Carol 时，遍历 Memory.history 找不到被取代的 Bob 事实的审计条目（N3 audit erasure）；Graphiti 的 resolve_edge_contradictions LLM 调用无 decoder seed 固定，导致同一矛盾重新裁判可能产生不同胜者（N1 replay inconsistency）。[^18]
Source: TOKI Paper — Appendix D Adapter Verdict Ledger
URL: https://arxiv.org/html/2606.06240v1
Date: 2026-01-05
Excerpt: "mem0 v3 (I): writes Alice's manager is Bob then Carol, walks Memory.history, finds no audit entry naming superseded Bob fact... Graphiti (T): resolve_edge_contradictions LLM call selects winning edge with no decoder-seed pin."
Context: 形式化分析揭示了当前主流 Agent 记忆系统在矛盾消解上的具体实现缺陷。
Confidence: high
```

---

### 4. 时态知识图谱（Temporal Knowledge Graph）

```
Claim: Graphiti 构建三层层次化时态图：episodic nodes（原始消息，保留为 provenance）、semantic entities & facts（提取的知识，带双时态边有效性）、community summaries（通过标签传播派生的聚类级抽象）；检索管线组合 cosine similarity、BM25 全文与 BFS 图遍历，经 RRF/MMR 重排序后由上下文构造器格式化。[^19]
Source: Zep Paper (arXiv 2501.13956) / arXiv survey 2603.25097
URL: https://arxiv.org/pdf/2603.25097
Date: 2026-03
Excerpt: "Zep introduces Graphiti, a temporally-aware knowledge graph engine that organizes memory into three hierarchical tiers: episodic nodes, semantic entities and facts, and community summaries... retrieval pipeline composes three search methods with reranking and a context constructor that formats facts with their temporal validity ranges."
Context: 该三层架构直接扩展了 GraphRAG 的社区摘要思想，加入时态维度后成为 Agent 记忆的 SOTA 实现。
Confidence: high
```

```
Claim: 时态知识图谱中的边携带 valid_from / valid_to（或 t_valid / t_invalid）属性，使图从静态快照变为版本化历史；版本化节点（versioned nodes）策略为每次更新创建新节点并链接到 TIME 节点，支持“在 T1 时刻一切的状态”这类单模式查询，但会带来节点爆炸问题（200 资源 × 5 分钟间隔 = 每天 57,600 节点）。[^20]
Source: Anyshift — Building a Temporal Infrastructure Knowledge Graph: A Year of Working with Neo4j at Scale
URL: https://anyshift.io/blog/building-temporal-infrastructure-knowledge-graph-neo4j
Date: 2026-02-17
Excerpt: "Versioned nodes. Each update creates a new node, linked to a TIME node... At 5-minute intervals, a cluster with 200 resources generates 200 new nodes every 5 minutes. That's 57,600 nodes per day for one small cluster."
Context: 基础设施监控领域的时态图实践，与 Agent 记忆系统面临相同的存储-查询权衡。
Confidence: high
```

```
Claim: Temporal Inductive Path Neural Network (TIPNN) 提出在时态知识图谱上进行时间路径遍历推理：将历史序列中的多张子图通过记忆传递策略连接，学习查询感知的时间路径特征；但指出独立子图学习导致时间复杂度随历史长度显著增加，且单向记忆传递存在长期依赖问题。[^21]
Source: Temporal Inductive Path Neural Network for Temporal Knowledge Graph Reasoning (arXiv 2309.03251)
URL: https://arxiv.org/html/2309.03251v3
Date: 2024-01-25
Excerpt: "DaeMon independently performs graph learning on local subgraphs and then connects them through memory passing strategy... This process requires graph learning on each subgraph separately, leading to difficulties in modeling complex temporal characteristics and a significant increase in time complexity."
Context: 时态图神经网络领域的代表性工作，其“时间路径”概念可直接迁移到 Agent 记忆的跨时间推理。
Confidence: medium
```

```
Claim: KektorDB 等新兴系统结合向量搜索与时态知识图谱，实现“时间旅行”查询：每条关系版本化，软删除支持查询过去任意时刻的图状态；同时引入记忆衰减与强化机制，未访问节点自然衰减，召回时强化。[^22]
Source: KektorDB GitHub / sanonone
URL: https://github.com/sanonone/kektordb
Date: 2026-05-10
Excerpt: "Temporal Graph (Time Travel): Every relationship is versioned. Soft delete support allows querying the graph status at any point in the past. Memory Decay & Reinforcement: Nodes naturally decay in relevance if not accessed, and are reinforced upon retrieval."
Context: 开源 Agent 记忆基础设施的新方向：将时态版本化、向量检索、认知衰减三者统一。
Confidence: medium
```

---

### 5. 评估：LongMemEval 中的时间推理子任务

```
Claim: LongMemEval-S 的 500 题中，Temporal Reasoning (TR) 占 133 题（26.6%），Knowledge-Update (KU) 占 78 题（15.6%），Multi-Session (MS) 占 30 题；当前系统在这些时间强相关任务上表现显著下降，商业系统与长上下文 LLM 均面临挑战。[^23]
Source: LongMemEval Original Paper (Wu et al., 2024)
URL: https://ar5iv.labs.arxiv.org/html/2410.10813
Date: 2024-08-06
Excerpt: "We demonstrated the significant challenges posed by LongMemEval, with current systems exhibiting substantial performance drops... effective strategies such as session decomposition, fact-augmented key expansion, and time-aware query expansion."
Context: 时间推理是 LongMemEval 中最难子任务之一；time-aware query expansion 被证明有效。
Confidence: high
```

```
Claim: Memory-R1 使用强化学习（GRPO/PPO）训练记忆管理策略（ADD/UPDATE/DELETE/NOOP），在 LongMemEval 的 Temporal Reasoning (TR) 和 Single-Session-User (SSU) 任务上取得大幅提升；即使仅在 LoCoMo 上训练，零样本迁移到 LongMemEval 仍超越所有基线。[^24]
Source: Memory-R1: Enhancing LLM Agents to Manage and Utilize Memories via RL (arXiv 2508.19828)
URL: https://arxiv.org/html/2508.19828v5
Date: 2025-08-29
Excerpt: "Memory-R1 shows substantial gains on tasks requiring factual recall (SSU) and temporal reasoning (TR), while also yielding steady improvements in knowledge update (KU)... Despite this zero-shot transfer setting, Memory-R1-GRPO outperforms all baseline systems."
Context: RL 训练的记忆管理器在时态推理任务上展现强泛化能力，提示记忆操作（而非仅检索）是提升时间推理的关键。
Confidence: high
```

```
Claim: LoCoMo 基准包含 321 道 temporal reasoning 题（占总题量约 22%），覆盖单跳、多跳、时态、常识与对抗性类别；其对话通过 persona profile 与时态事件图（temporal event graph）构建，确保跨会话因果一致性。[^25]
Source: LoCoMo / ENGRAM Paper (arXiv 2511.12960)
URL: https://arxiv.org/html/2511.12960v1
Date: 2025-11
Excerpt: "LoCoMo compresses realistic two-speaker dialogues into long, multi-session conversations that probe diverse reasoning categories... The QA split labels questions into five categories: single-hop, multi-hop, temporal, commonsense/world knowledge, and adversarial/unanswerable."
Context: LoCoMo 的 temporal event graph 构造方式本身即为 Agent 记忆系统提供了“应如何建模时间”的参考。
Confidence: high
```

```
Claim: Memento（开源 bitemporal KG 记忆系统）在 LongMemEval 上达到 90.8% 整体准确率、92.2% 任务平均准确率（GPT-4o 裁判），其核心优势在于追踪事实何时为真 vs 何时被学习，通过结构化关系组合答案而非原始文本块。[^26]
Source: Memento Memory GitHub / shane-farkas
URL: https://github.com/shane-farkas/memento-memory
Date: 2025-01-31
Excerpt: "90.8% overall accuracy, 92.2% task average on LongMemEval (500 questions, end-to-end, GPT-4o judge), a benchmark for long-term conversational memory covering temporal reasoning, knowledge updates, multi-session recall, and preference tracking."
Context: Memento 验证了 bitemporal KG 在端到端 LongMemEval 评估中的显著优势。
Confidence: medium
```

---

### 6. 对 XMclaw 的启示

| 维度 | 现状（已知） | 前沿实践 | 差距与机会 |
|------|-------------|---------|-----------|
| 双时态字段 | 已实现 valid_at / invalid_at（Phase 8 ⑩） | Zep 四时间戳、XTDB bitemporal SQL、TOKI 算子代数 | 缺少 transaction-time 轴与显式隔离级别 |
| 时态查询 | 能力有限 | Graphiti 自动窗口过滤、TSM 语义时间线解析 | 需引入时间约束解析与检索时 primary-key 过滤 |
| 矛盾消解 | 未明确 | Zep temporal invalidation、Mem0 破坏性 UPDATE、TOKI 审计行 | 需选择并显式实现“失效而非删除”或“带审计的覆盖” |
| 记忆衰减 | 未明确 | Ebbinghaus 指数衰减、ACT-R base-level activation、Engram sleep | 可引入可配置 decay 公式与访问强化机制 |
| 记忆巩固 | 未明确 | Generative Agents reflection、HeLa-Mem 蒸馏、Mem0 合并 | 需设计 episodic → semantic 的显式合成管道 |
| 评估 | 未明确 | LongMemEval TR/KU/MS、LoCoMo temporal、Memento 端到端 | 建议接入 LongMemEval-S 作为回归测试 |

---

### 参考文献索引

[^1]: Rasmussen et al., Zep: A Temporal Knowledge Graph Architecture for Agent Memory, arXiv:2501.13956, 2025.
[^2]: Snodgrass & Ilsoo, Temporal Database Survey, VLDB 1998.
[^3]: XTDB Authors, Bitemporality, XTDB Docs, 2018.
[^4]: Tansel, Wu & Wang, Time Travel with the BiTemporal RDF Model, Mathematics (MDPI), 2025.
[^5]: Wu et al., LongMemEval: Benchmarking Chat Assistants on Long-Term Interactive Memory, arXiv:2410.10813, 2024.
[^6]: Su et al., Beyond Dialogue Time: Temporal Semantic Memory for Personalized LLM Agents, arXiv:2601.07468, 2026.
[^7]: Zep, What Is a Temporal Knowledge Graph?, getzep.com, 2026.
[^8]: Harnessing Temporal Databases for Systematic Evaluation of Factual Time-Sensitive QA in LLMs, arXiv:2508.02045, 2026.
[^9]: Park et al., Generative Agents: Interactive Simulacra of Human Behavior, UIST 2023 / arXiv:2304.03442.
[^10]: HeLa-Mem: Hebbian Learning and Associative Memory for LLM Agents, arXiv:2604.16839, 2026.
[^11]: Zhong et al., MemoryBank: Enhancing LLMs with Long-Term Memory, 2024.
[^12]: Forgetting Curve: A Reliable Method for Evaluating Memorization Capability for Long-context Models, EMNLP 2024 / arXiv:2410.04727.
[^13]: Engram AI Memory, Research Foundations, GitHub, 2025.
[^14]: Mem0, Memory Eviction and Forgetting in AI Agents, mem0.ai blog, 2026.
[^15]: Neo4j Blog, Graphiti: Knowledge graph memory for an agentic world, 2026.
[^16]: MemX, Agent Memory vs RAG: The Real Difference, 2026.
[^17]: TOKI: A Bitemporal Operator Algebra for Contradiction Resolution in LLM-Agent Persistent Memory, arXiv:2606.06240, 2026.
[^18]: TOKI Paper, Appendix D Adapter Verdict Ledger, 2026.
[^19]: arXiv survey 2603.25097 / Zep Paper, 2026.
[^20]: Anyshift, Building a Temporal Infrastructure Knowledge Graph, 2026.
[^21]: Temporal Inductive Path Neural Network for Temporal Knowledge Graph Reasoning, arXiv:2309.03251, 2024.
[^22]: KektorDB GitHub, sanonone, 2026.
[^23]: LongMemEval Paper, arXiv:2410.10813, 2024.
[^24]: Memory-R1 Paper, arXiv:2508.19828, 2025.
[^25]: ENGRAM Paper / LoCoMo, arXiv:2511.12960, 2025.
[^26]: Memento Memory GitHub, shane-farkas, 2025.
