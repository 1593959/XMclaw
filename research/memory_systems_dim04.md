## Dimension 04: 图数据库与关系记忆
### 角度：知识图谱构建、多跳推理、图遍历与向量召回的互补性

---

### 1. 知识图谱构建：从非结构化文本提取实体-关系-实体三元组

```
Claim: LLMGraphTransformer 已成为使用大语言模型从非结构化文本自动提取知识图谱三元组（entity-relationship-entity）的主流工具，被 LangChain、LlamaIndex 和 Neo4j 官方图谱构建器广泛采用 [^1]
Source: LangChain / Neo4j LLM Graph Builder / CSDN 实践报告
URL: https://python.langchain.com/api_reference/experimental/graph_transformers.html / https://neo4j.com/labs/genai-ecosystem/llm-graph-builder/
Date: 2025
Excerpt: "It works by using documents from document loaders and instructing LLMs with a prompt and a schema description to generate structured output for a list of nodes and relationships with attributes that capture entities and their relationships from the text."
Context: LLMGraphTransformer 通过 prompt 工程指导 LLM 生成 [ENTITY1, RELATIONSHIP, ENTITY2] 格式的三元组，支持自定义实体类型和关系类型 schema。中文实践显示，同一实体在不同文本块中可能被分别识别为不同 ID，需要后续实体消歧。
Confidence: high
```

```
Claim: GraphRAG (Microsoft, 2024) 的索引阶段使用 LLM 从文本块中提取实体、关系和声明（claims），通过精确字符串匹配进行实体对齐，并将重复关系聚合为带权重的边 [^2]
Source: Microsoft Research — From Local to Global: A GraphRAG Approach to Query-Focused Summarization
URL: https://arxiv.org/html/2404.16130v2
Date: 2024-04
Excerpt: "The entity/relationship/claim extraction processes creates multiple instances of a single element because an element is typically detected and extracted multiple times across documents... Relationships are aggregated into graph edges, where the number of duplicates for a given relationship becomes edge weights."
Context: GraphRAG 的图谱构建是一种抽象式摘要（abstractive summarization），关系和声明可能并未在原文中显式陈述。实体匹配采用精确字符串匹配，但论文指出可通过调整 prompt 或代码使用更柔和的匹配方法。
Confidence: high
```

```
Claim: 现代知识图谱构建需要经过实体链接（entity linking）和指代消解（coreference resolution）才能将同一实体的多个提及合并，否则图谱会因重复实体而膨胀并降低检索精度 [^3]
Source: Politecnico di Torino thesis / Neo4j Knowledge Graph Builder
URL: https://webthesis.biblio.polito.it/38769/1/tesi.pdf
Date: 2025
Excerpt: "In practice, building a high-quality KG requires careful design, since it may involve entity linking to canonical databases, coreference resolution to merge mentions referring to the same real-world entity and ensuring important relations are captured."
Context: 简单的共现图（co-occurrence graph）虽然容易创建，但不提供显式语义。更复杂的流水线需要规则或 ML 方法识别实体间特定关系（如 "X was born in Y"），而非仅创建通用链接。
Confidence: high
```

---

### 2. 图数据库与存储后端

```
Claim: Zep 的 Graphiti 引擎是 temporal knowledge graph 的代表，采用双时态模型（bi-temporal）：每条边携带四个时间戳——真实世界有效时间（t_valid, t_invalid）和系统事务时间（t_created, t_expired），实现事实的版本化与点查询 [^4]
Source: Zep: A Temporal Knowledge Graph Architecture for Agent Memory (arXiv:2501.13956)
URL: https://arxiv.org/abs/2501.13956
Date: 2025-01-20
Excerpt: "Zep implements a bi-temporal model, where timeline T represents the chronological ordering of events, and timeline T' represents the transactional order of Zep's data ingestion... This bi-temporal approach represents a novel advancement in LLM-based knowledge graph construction."
Context: Graphiti 将记忆组织为三层：episodic nodes（原始消息）、semantic entities/facts（带双时态边有效性的提取知识）、community summaries（通过标签传播得到的聚类抽象）。检索管道组合 cosine similarity、BM25 全文和广度优先图遍历三种搜索方法。
Confidence: high
```

```
Claim: Graphiti 支持多种图数据库后端：Neo4j 5.26+、FalkorDB 1.1.2+、Kuzu 0.11.2+ 和 Amazon Neptune，P95 检索延迟低至 300ms，无需查询时 LLM 调用 [^5]
Source: Neo4j Developer Blog / CallSphere AI Guide
URL: https://neo4j.com/blog/developer/graphiti-knowledge-graph-memory/
Date: 2026-03-24 / 2026-06-01
Excerpt: "Zep's own Graphiti implementation achieves extremely low-latency retrieval, returning results at a P95 latency of 300ms. This is enabled by a hybrid search approach that combines semantic embeddings, keyword (BM25) search, and direct graph traversal — avoiding any LLM calls during retrieval."
Context: Graphiti 的增量式架构专为频繁更新设计：新 episode 到达时立即提取实体和关系，并与现有节点进行解析。当新知识与旧知识冲突时，系统关闭旧边的 valid-time 窗口并开启新边，保留完整历史。
Confidence: high
```

```
Claim: Cognee 采用三重存储架构（triple-store）：向量存储（LanceDB/Qdrant/Milvus 等负责语义召回）、图存储（Neo4j/Kuzu/FalkorDB/NetworkX 负责结构推理）、关系存储（SQLite/PostgreSQL 负责溯源与审计），实现向量与图谱的混合检索 [^6]
Source: Cognee Official / LanceDB Case Study / Open Forem
URL: https://www.cognee.ai/ / https://www.lancedb.com/blog/case-study-cognee
Date: 2025-09-23 / 2026-02-24
Excerpt: "Cognee delivers a durable memory layer for AI agents by unifying a knowledge graph with high-performance vector search... The magic happens in Hybrid Search, where the system queries the Vector Store for relevant content and the Graph Store for the contextual relationships simultaneously."
Context: Cognee 的默认本地栈为 SQLite + LanceDB + Kuzu，可零配置运行；生产环境可切换为 Neo4j + Qdrant + PostgreSQL。其 cognify() 管道执行 Extract–Cognify–Load 模型，memify() 支持自改进记忆（剪枝 stale 节点、强化高频连接）。
Confidence: high
```

```
Claim: Mem0 的 Graph Memory 功能被锁定在 Pro tier（249美元/月），其开源核心仅提供向量语义搜索和键值查找；Mem0 缺乏原生时态事实建模，无法回答"用户在改变偏好之前是什么偏好"这类时间推理问题 [^7]
Source: Evermind.ai / Vectorize.io / Atlan
URL: https://evermind.ai/blogs/mem0-alternative / https://vectorize.io/articles/mem0-vs-zep
Date: 2026-03-15 / 2026-04-02
Excerpt: "Mem0's most architecturally interesting capability—Graph Memory, which enables entity relationships and multi-hop queries—is locked behind the Pro tier at 249 per month... Mem0 lacks native temporal fact modeling. Memories are timestamped at creation, but there is no validity window or fact supersession mechanism."
Context: Mem0 采用双存储架构（vector DB + knowledge graph），在 Pro 层提取实体和关系。但所有记忆被视为同等可信，没有目标感知评分或预算约束的工作集组装。独立基准测试显示 Mem0 在 LongMemEval 上得 49.0%，而 Zep 为 63.8%。
Confidence: high
```

```
Claim: NetworkX 是轻量级 in-memory 图处理的默认选择，适合 <100k 节点的原型和小型内部工具；LangChain 的 ConversationKGMemory 早期即基于 NetworkXEntityGraph 实现 [^8]
Source: Safjan Blog / LangChain GitHub Issues / EmbodiedLGR Paper
URL: https://safjan.com/simple-inmemory-knowledge-graphs-for-quick-graph-querying/ / https://github.com/langchain-ai/langchain/issues/1161
Date: 2026-02-07 / 2023-02-21
Excerpt: "NetworkX has been my reliable companion for simple graph operations. It's incredibly intuitive and perfect for smaller knowledge graphs... The memory graph and the vector database are implemented using NetworkX and Milvus, respectively, enabling physical deployment scalability."
Context: 对于超过 100k 节点的生产环境，通常需要迁移到 Neo4j 或 FalkorDB。KGLite（2026 年新出现）是嵌入式 Rust 核心知识图，支持 Cypher 查询和三种存储模式（in-memory / mmap / disk），可在笔记本上处理 10 亿条边。
Confidence: high
```

```
Claim: Amazon Neptune 被 AWS 官方推荐用于构建 agentic AI 应用的持久记忆层，与 Mem0 开源框架和 ElastiCache for Valkey 结合使用；Neptune Analytics 支持图机器学习（GraphStorm） [^9]
Source: AWS Database Blog / Amazon Neptune Samples
URL: https://aws.amazon.com/blogs/database/build-persistent-memory-for-agentic-ai-applications-with-mem0-open-source-amazon-elasticache-for-valkey-and-amazon-neptune-analytics/
Date: 2025-11
Excerpt: "Build persistent memory for agentic ai applications with mem0 open source, amazon elasticache for valkey, and amazon neptune analytics."
Context: Neptune 提供 Database（事务性图存储）和 Analytics（图分析/ML）两种模式。2026 年 2 月 AWS 发布了 Sample GenAI Agents for Prototyping on Neptune，可自动生成用例、图模型、样本数据和查询。
Confidence: medium
```

---

### 3. 关系类型与语义边设计

```
Claim: 在 agent 记忆系统中，关系类型需要超越简单的 "related_to" 或 "co-occurs_with" 而包含显式语义：SAME_TOPIC、CONTRADICTS、SUPERSEDES、CAUSED_BY、PART_OF 等，以支持结构化推理 [^10]
Source: Decoding AI / Graphlet AI / Zep 文档
URL: https://www.decodingai.com/p/keep-knowledge-graph-clean / https://blog.graphlet.ai/the-rise-of-semantic-entity-resolution-45c48d5eb00a
Date: 2026-06-02 / 2025-09-11
Excerpt: "The system normalizes its name against existing nodes of the same type... 'NYC' resolves to 'New York City'. 'JP Morgan' resolves to 'JPMorgan Chase'... A new mention of a company gets extracted as a typed entity. The resolution step normalizes it to a canonical name."
Context: 语义关系设计通常分为 "Associated_with"（概念相关、物理相关、功能相关、空间相关、时间相关）和 "Is_a"（层级分类）。在 agent 记忆场景中，SUPERSEDES 关系对时态知识图尤为关键——它表示新事实替代旧事实，而非直接删除旧边。
Confidence: medium
```

```
Claim: Graphiti 的边失效机制（edge invalidation）本质上使用 SUPERSEDES 语义：当新 episode 与现有边冲突时，系统关闭旧边的 valid-time 窗口并创建新边，保留完整历史供点查询 [^11]
Source: CallSphere AI / Zep arXiv Paper
URL: https://callsphere.ai/blog/graphiti-temporal-knowledge-graph-ai-agents-2026
Date: 2026-06-01
Excerpt: "When a new episode contradicts an existing edge, Graphiti does not overwrite. It closes the old edge's valid-time window and opens a new one. The history stays in the graph. You can run point-in-time queries that reconstruct the world as the agent understood it at any past moment."
Context: 这与向量存储的"覆盖式更新"形成鲜明对比。时态知识图通过显式的 valid_from / valid_to 时间戳，使 agent 能够回答"我们在周二知道了什么"或"客户在升级前使用什么套餐"等历史状态查询。
Confidence: high
```

---

### 4. 多跳推理与图遍历

```
Claim: 多跳推理（multi-hop reasoning）是图记忆系统的核心优势：从查询提取的实体出发，通过 BFS/DFS 遍历图结构收集多跳邻居三元组，将结构化路径转换为文本描述输入 LLM，显著优于纯向量检索在复杂问答上的表现 [^12]
Source: StepChain GraphRAG / LogosKG / RAG vs GraphRAG Systematic Evaluation
URL: https://arxiv.org/html/2510.02827v1 / https://www.medrxiv.org/content/10.64898/2026.01.12.26343957v1
Date: 2025-10-03 / 2026-01-13
Excerpt: "We combine our BFS-RF with expansions over the current graph to tackle queries requiring multiple rounds of reasoning... After answering sub-question q_j, we query the global index conditioned on the current frontier F_j and obtain a small batch of passages, which are parsed on-the-fly to extract unseen entities and relations."
Context: LogosKG 针对生物医学大规模知识图（UMLS 407K 节点/3.4M 边，PubMedKG 54.4M 节点/86.5M 边）提出线性代数图遍历方法，将指针式数据结构替换为矩阵/张量操作，使高跳数遍历在单设备 CPU/GPU 上可行。两跳扩展从 UMLS 高度节点平均涉及 10^9 可达边。
Confidence: high
```

```
Claim: Mem0 的图记忆在实体中心关系图上实现 67.1% 单跳和 51.2% 多跳准确率，同时将延迟降低 91%；但 Mem0 缺乏丰富的属性特征，限制了深层推理能力 [^13]
Source: APEX-MEM / Advanced Memory Architectures Survey (arXiv:2604.14362)
URL: https://arxiv.org/pdf/2604.14362
Date: 2026
Excerpt: "Mem0 implemented entity-centric relational graphs achieving 67.1% single-hop and 51.2% multi-hop accuracy with 91% latency reduction; Mem0 improved temporal reasoning (58.1%) but lacked rich property attributes."
Context: 其他先进架构包括：A-MEM（自主链接，27.0% F1 单跳）、H-MEM（分层检索，多跳任务 +21 分）、MIRIX（六通道记忆存储，85.4% 准确率但复杂度高）。这些研究表明结构化记忆有益，但在复杂度、表达力和时态一致性之间存在权衡。
Confidence: medium
```

---

### 5. 社区检测：GraphRAG 的社区摘要用于全局问题回答

```
Claim: GraphRAG 使用 Leiden 算法进行分层社区检测，将知识图划分为多级社区层次结构（C0–C3），自下而上生成社区摘要；根级摘要比直接处理源文本减少 97% 的 token 消耗 [^14]
Source: Microsoft GraphRAG 论文 / Memgraph 社区报告 / Bean Labs
URL: https://arxiv.org/html/2404.16130v2 / https://public-assets.memgraph.com/community-calls/microsoft-graphrag-memgraph.pdf
Date: 2024-04 / 2026-06-04
Excerpt: "Leiden community detection is used to partition the graph index into groups of elements... The hierarchical nature of the community structure means that questions can be answered using community summaries from different levels... root-level summaries required 97% fewer tokens than processing source text directly."
Context: GraphRAG 的查询阶段采用 Map-Reduce：社区摘要被随机打乱并分块，每块独立生成中间答案和 helpfulness 评分（0–100），然后按评分排序迭代加入上下文窗口生成最终全局答案。在播客转录（~1M token，8,564 实体，20,691 边）和新闻文章（~1.7M token，15,754 实体，19,520 边）上，GraphRAG 对向量 RAG 的综合胜率 72–83%，多样性胜率 62–82%。
Confidence: high
```

```
Claim: 社区检测不仅用于 GraphRAG，也被 Zep/Graphiti 用于生成 cluster-level abstractions（通过标签传播），以及 Cognee 的 improve 管道用于强化社区结构连接 [^15]
Source: Zep arXiv / Cognee Blog / Healthcare KG Community Retrieval Paper
URL: https://arxiv.org/pdf/2603.25097 / https://arxiv.org/pdf/2410.04585v1
Date: 2025-01 / 2024-10
Excerpt: "Zep's retrieval pipeline composes three search methods... and a context constructor that formats facts with their temporal validity ranges... community summaries (cluster-level abstractions derived via label propagation)."
Context: 医疗领域研究进一步扩展了 GraphRAG 的社区摘要方法：通过多次运行 Leiden 算法（不同随机参数）探索多样化社区结构，为每个社区生成 general summary 和 theme-specific summary，使同一实体可贡献于多个摘要。
Confidence: high
```

---

### 6. 实体链接与消歧（Deduplication / Entity Resolution）

```
Claim: LLM 生成的知识图普遍存在噪声和冗余：同一实体的不同变体（如 "LLMs" / "LLM" / "llms" / "Large Language Models"）被分别提取为独立节点，现有系统（GraphRAG、LightRAG、HippoRAG）主要依赖字符串匹配启发式方法，留下大量未解决的重复 [^16]
Source: Deg-RAG: Denoised Knowledge Graphs for RAG (arXiv:2510.14271)
URL: https://arxiv.org/html/2510.14271v1
Date: 2025-10-16
Excerpt: "LLMs often struggle to consistently maintain earlier entities and relations due to limited long-context capabilities, which leads to duplicates... Existing methods, including LightRAG, MS GraphRAG, and HippoRAG, typically rely on string-matching heuristics to merge similar entities, leaving many duplicates unresolved."
Context: Deg-RAG 提出实体解析（entity resolution）消除冗余实体 + 三元组反思（triple reflection）过滤错误关系的去噪框架。实验表明，简单的去噪方法不仅大幅减少图规模，还持续提升多种 Graph-based RAG 变体的问答性能。
Confidence: high
```

```
Claim: 实体解析应分离为两个独立决策：Resolution（命名规范化——处理拼写、缩写、大小写变体，回答"我们该叫它什么？"）和 Deduplication（身份验证——基于完整节点嵌入判断是否为同一真实世界实体，回答"这两个记录是否指向同一件事物？"）[^17]
Source: Decoding AI — How to Keep Your AI Agent's Knowledge Graph Clean
URL: https://www.decodingai.com/p/keep-knowledge-graph-clean
Date: 2026-06-02
Excerpt: "Resolution and deduplication are 2 distinct decisions doing 2 distinct jobs... During resolution we find the canonical name for each entity. It answers 'what should we call this?'... We compare the embedded node against existing nodes. This decides whether it is the same real-world entity as one already in the graph."
Context: 正确的流水线为：LLM 提取 → Resolution（精确→模糊→语义匹配的短路链，仅更新 canonical_name）→ 完整节点嵌入 → Deduplication（与现有同类型节点比较）→ 合并/标记人工审核/添加新节点。混淆这两个步骤会导致图静默腐烂。
Confidence: high
```

```
Claim: 语义实体解析（semantic entity resolution）使用 LLM 直接匹配和合并 JSON 记录，通过 Chain-of-Thought 生成解释；LLM 可被信任构建知识图，也应被信任去重其实体 [^18]
Source: Graphlet AI Blog — The Rise of Semantic Entity Resolution
URL: https://blog.graphlet.ai/the-rise-of-semantic-entity-resolution-45c48d5eb00a
Date: 2025-08-10
Excerpt: "Prompting Large Language Models to both match and merge two or more records is a new and powerful technique... It is strange to think that LLMs can be trusted to build knowledge graphs whole-cloth, but can't be trusted to deduplicate their entities!"
Context: 传统实体解析工具分为三类：传统字符串/规则方法、嵌入方法（TransE/DistMult/GNN）、LLM 方法。对于 agent 记忆场景，LLM-based 方法因通用性强而日益流行，但需注意可扩展性和成本。
Confidence: medium
```

---

### 7. 图遍历与向量召回的互补性

```
Claim: 向量检索擅长语义相似性（"找到关于 X 的文档"），图遍历擅长结构关系（"服务 A 依赖服务 B，且最近三次部署到 B 都导致 A 故障"）；混合检索（hybrid RAG）将两者并行执行、融合结果，可显著优于单一模态 [^19]
Source: LearnWithParam / Hybrid RAG ORAN Benchmark / Data Science Dojo
URL: https://www.learnwithparam.com/blog/hybrid-retrieval-rag-vector-graph-search / https://arxiv.org/html/2507.03608v2
Date: 2026-02-05 / 2025-06-30
Excerpt: "The Hybrid GraphRAG technique integrates both vector-based retrieval and graph-based traversal to leverage the strengths of each approach... The final prompt is designed to guide the language model to prioritize information from Vector RAG for generating a broad and comprehensive answer, while using the GraphRAG context to supplement with structural details."
Context: ORAN 领域基准测试（600 题，覆盖 Easy/Intermediate/Hard）显示：向量 RAG 在简单问答上表现均衡；GraphRAG 在忠实度上最强；Hybrid RAG 在相关性和忠实度上均最优，且方差最低。另一项实验表明 Dual-Channel Graph Retrieval Fusion（细粒度 KG 三元组 + 粗粒度 Community Report）在 MultiHopRAG 上使集成 F1 从 75.60% 提升至 78.92%（+3.32%）。
Confidence: high
```

```
Claim: 混合 RAG 引入独特的安全故障模式——Retrieval Pivot Risk（RPR）：向量检索返回的授权种子块可通过实体链接"轴转"到未授权的敏感图邻域，导致跨租户数据泄露；在合成企业语料库上未防御的混合管道 RPR≈0.95，放大因子 AF≈160–194× [^20]
Source: Scott Thornton — Retrieval Pivot Attacks in Hybrid RAG (arXiv:2602.08668)
URL: https://arxiv.org/html/2602.08668v3
Date: 2026-02-09 / 2026-03-08
Excerpt: "A semantically retrieved 'seed' chunk can pivot via entity linking into sensitive graph neighborhoods, causing data leakage that does not exist in vector-only retrieval... The undefended hybrid pipeline exhibits RPR≈0.95 and AF(ε)≈160–194× relative to vector-only retrieval, with leakage occurring at PD=2 hops."
Context: 所有泄露均发生在恰好 2 跳（PD=2），这是二分图 chunk-entity 拓扑的结构不变量：Hop 0（授权种子块）→ Hop 1（共享实体节点，无租户归属）→ Hop 2（未授权块）。防御方案极其简单：在图扩展边界执行 per-hop 授权（重新检查租户和敏感度标签），可消除所有测量到的泄露（RPR→0.0），延迟开销 <1ms。
Confidence: high
```

```
Claim: GAM (Hierarchical Graph-based Agentic Memory, 2026) 通过显式解耦记忆编码与整合，将正在进行的对话隔离在事件进展图（event progression graph）中，仅在语义边界处才整合到主题关联网络，从而最小化瞬态噪声干扰并保留长期一致性 [^21]
Source: GAM Paper (arXiv:2604.12285)
URL: https://arxiv.org/html/2604.12285v1
Date: 2026-04-14
Excerpt: "We propose GAM, a hierarchical Graph-based Agentic Memory framework that explicitly decouples memory encoding from consolidation to effectively resolve the conflict between rapid context perception and stable knowledge retention... By isolating ongoing dialogue in an event progression graph and integrating it into a topic associative network only upon semantic shifts, our approach minimizes interference while preserving long-term consistency."
Context: GAM 引入图引导的多因素检索策略（graph-guided, multi-factor retrieval），融合时间、置信度和角色中心信号。在 LoCoMo 和 LongDialQA 基准上，GAM 在推理准确率和效率上均优于 SOTA 基线。当前局限为仅支持文本模态。
Confidence: high
```

```
Claim: LightRAG (2024) 通过双级检索范式（dual-level retrieval）平衡效率与效果：低级检索针对具体节点/边回答局部查询，高级检索聚合多实体信息回答全局查询；结合关键词提取与单跳邻域聚合，实现增量更新而无需全量重建 [^22]
Source: LightRAG Paper (arXiv:2410.05779)
URL: https://arxiv.org/abs/2410.05779
Date: 2024-10-08
Excerpt: "LightRAG incorporates graph structures into text indexing and retrieval processes... Its dual-level retrieval paradigm allows for the extraction of both specific and abstract information... An incremental update algorithm dynamically integrates new data into the graph, ensuring the index remains current without full reprocessing."
Context: LightRAG 被引用 567 次（截至搜索时），是 GraphRAG 之后最具影响力的轻量级替代方案。其检索算法从查询中提取局部关键词 k(l) 和全局关键词 k(g)，通过向量相似度匹配实体，然后收集单跳邻域节点作为最终检索结果。
Confidence: high
```

---

### 8. 对 XMclaw 的启示

```
Claim: XMclaw 使用 LanceDB 混合存储（vector + graph edges）的架构与 Cognee、Graphiti 和 Mem0 的行业趋势一致；但需警惕 Retrieval Pivot Risk——若向量检索的种子节点与图遍历之间缺乏 per-hop 授权检查，跨会话/跨租户的记忆隔离可能被结构拓扑绕过 [^23]
Source: 本报告综合 Thornton (2026) / Cognee / Zep 实践
URL: —
Date: 2026-06-06
Excerpt: "Authorization at the vector layer alone is insufficient. Authorization at the LLM layer is unreliable. Only authorization at the graph expansion layer—the pivot boundary itself—addresses the root cause."
Context: XMclaw 的 daemon/config_schema 和 cognition 模块若引入图记忆，建议：1) 在 LanceDB 向量层之上增加图边存储（Kuzu/NetworkX/Neo4j）；2) 实体提取使用 LLMGraphTransformer 模式；3) 实体消歧分离 Resolution 和 Deduplication；4) 多跳检索设置节点预算上限（≤25 节点）或 per-hop 敏感度检查；5) 关系类型显式定义时态边（valid_from/valid_to）以支持事实版本化。
Confidence: medium
```

---

### 参考文献索引

[^1]: LangChain LLMGraphTransformer API; Neo4j LLM Graph Builder; CSDN 国产大模型+LangChain+Neo4j 实践 (2025-04)
[^2]: Edge et al., "From Local to Global: A GraphRAG Approach to Query-Focused Summarization", Microsoft Research, arXiv:2404.16130 (2024)
[^3]: Politecnico di Torino thesis on GraphRAG and KG construction (2025)
[^4]: Rasmussen et al., "Zep: A Temporal Knowledge Graph Architecture for Agent Memory", arXiv:2501.13956 (2025-01)
[^5]: Neo4j Developer Blog, "Graphiti: Knowledge graph memory for an agentic world" (2026-03); CallSphere AI Guide (2026-06)
[^6]: Cognee official site (cognee.ai); LanceDB case study (2025-09); Open Forem article (2025-10)
[^7]: Evermind.ai "Best Mem0 Alternatives" (2026-04); Vectorize.io "Mem0 vs Zep" (2026-03); Atlan "Best AI Agent Memory Frameworks" (2026-04)
[^8]: Safjan "Simple In-Memory Knowledge Graphs" (2026-02); LangChain GitHub issue #1161; EmbodiedLGR paper (2026-04)
[^9]: AWS Database Blog, "Build persistent memory for agentic AI..." (2025-11); Amazon Neptune Samples (2026-02)
[^10]: DecodingAI "Keep Knowledge Graph Clean" (2026-06); Graphlet AI "Semantic Entity Resolution" (2025-08)
[^11]: CallSphere AI, "Graphiti: How Temporal Knowledge Graphs Give AI Voice Agents Persistent Memory" (2026-06)
[^12]: StepChain GraphRAG (arXiv:2510.02827, 2025-10); LogosKG (medRxiv 2026-01)
[^13]: APEX-MEM / Advanced Memory Architectures Survey (arXiv:2604.14362, 2026)
[^14]: Edge et al. GraphRAG (2024); Memgraph community call slides; Bean Labs research log (2026-06)
[^15]: Zep arXiv (2025); Healthcare KG Community Retrieval (arXiv:2410.04585, 2024)
[^16]: Zheng et al., "Deg-RAG: Denoised Knowledge Graphs for Retrieval Augmented Generation", arXiv:2510.14271 (2025-10)
[^17]: DecodingAI, "How to Keep Your AI Agent's Knowledge Graph Clean" (2026-06)
[^18]: Graphlet AI, "The Rise of Semantic Entity Resolution" (2025-08)
[^19]: LearnWithParam (2026-02); ORAN Hybrid GraphRAG benchmark (arXiv:2507.03608, 2025-06); Dual-Channel Fusion (arXiv:2603.25152, 2026)
[^20]: Thornton, "Retrieval Pivot Attacks in Hybrid RAG", arXiv:2602.08668 (2026-02/2026-03)
[^21]: Wu et al., "GAM: Hierarchical Graph-based Agentic Memory for LLM Agents", arXiv:2604.12285 (2026-04)
[^22]: Guo et al., "LightRAG: Simple and Fast Retrieval-Augmented Generation", arXiv:2410.05779 (2024-10)
[^23]: 本报告综合推断
