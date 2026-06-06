## 4. 存储与检索技术

### 4.1 向量存储与语义检索

#### 4.1.1 嵌入模型选型：768维 E5 vs 3072维 OpenAI，Matryoshka 截断技术

嵌入模型（Embedding Model）将文本映射至高维稠密向量，是语义检索的基石。维度选择并非越大越好：金融领域实测显示，经微调的768维 Multilingual-E5 在 Recall@1 上达到 62.8%，显著超越 3072 维的 OpenAI text-embedding-3-large（39.2%）[^e5-openai]。这一反差表明，领域适配与微调策略对检索质量的影响远超裸维度规模。当前 MTEB v2 检索榜单上，7B 参数规模的解码器嵌入模型（NV-Embed-v2、GTE-Qwen2-7B）以 68–71 的 nDCG@10 领先传统编码器约 5–6 个点，但推理成本相应增加 5–10 倍 [^7b-decoder]。

Matryoshka 表示学习（Matryoshka Representation Learning, MRL）为维度权衡提供了工程出路。该技术通过联合多任务训练，使向量前 N 维即承载主要语义信息，从而允许在推理时截断至 256/512/1024 维而仅损失 1–3% 召回率 [^matryoshka]。OpenAI text-embedding-3 系列与 Snowflake arctic-embed 已原生支持此特性，使存储与计算成本可按查询精度弹性伸缩。对于中文场景，BGE 与 GTE 系列是最常用的开源选择；其中 bge-m3 支持稠密（dense）、稀疏（sparse）与多向量（multi-vector）三种检索模式，适合需扩展至多语言的中文系统 [^bge-m3]。

#### 4.1.2 向量数据库对比：LanceDB、Qdrant、Chroma、Milvus、pgvector 的适用场景

向量数据库的选型需综合考量部署形态、扩展路径、索引生态与成本结构。下表对比了五款主流系统在 Agent 记忆场景中的核心特征。

| 数据库 | 核心架构 | 典型索引 | 部署形态 | 适用场景 |
|--------|----------|----------|----------|----------|
| LanceDB | Rust 核心 + Lance 列式格式 | HNSW、IVF-PQ、DiskANN | 本地/边缘/云原生 | 多模态数据共存、SSD 扩展十亿级向量 [^lancedb] |
| Qdrant | Rust 实现，分布式原生 | HNSW、量化 INT8/二进制 | 自托管/云 | 生产 RAG 高频低延迟查询 [^qdrant-pinecone] |
| Chroma | Python 优先，轻量封装 | HNSW（基于 hnswlib） | 本地/嵌入式 | 原型开发、快速验证 [^qdrant-pinecone] |
| Milvus | 云原生分布式，GPU 索引 | HNSW、IVF、DiskANN、GPU | Kubernetes 集群 | 十亿级企业级向量平台 [^ragperf] |
| pgvector | PostgreSQL 扩展 | HNSW、IVF | 现有 RDS/Aurora | 成本敏感、已使用 PG 生态的架构 [^pgvector] |

LanceDB 基于列式数据格式，允许向量、文档与图像共存于同一表，且支持 SSD 存储突破内存限制，在单节点实现十亿级低延迟搜索 [^lancedb]。Qdrant 与 Pinecone 是 2026 年生产 RAG 部署的最常见选择，前者以 Rust 实现提供稳定的 P95 延迟，后者以全托管降低运维负担 [^qdrant-pinecone]。pgvector 在 2025 年发布 0.8.0 后，凭借迭代扫描与查询规划器改进，成为从专用向量库迁移回 PostgreSQL 生态的成本最优解，多家企业报告"相同性能，10 倍便宜" [^pgvector]。Chroma 适合轻量原型，但其 Python 运行时与扩展性天花板使其难以直接进入生产环境。

#### 4.1.3 ANN 算法权衡：HNSW、IVF-PQ、DiskANN 的精度-延迟-规模三角

近似最近邻（Approximate Nearest Neighbor, ANN）算法构成向量检索的性能底座，三者形成清晰的精度-延迟-规模三角。HNSW（Hierarchical Navigable Small World）在内存充足时提供最高召回率（95–99%）与最低查询延迟（<1 ms），但内存消耗高、构建慢、动态更新困难 [^hnsw-ivf]。其多层图结构使查询仅需评估约 1% 的总向量即可找到 95% 的真实 Top 匹配 [^hnsw-ivf]。IVF-PQ（Inverted File Index with Product Quantization）将数据集分为约 √n 个簇，查询时仅搜索 5–50 个最近簇，将比较次数降至 0.5–1%，内存占用极低且构建快，适合十亿级磁盘友好场景 [^hnsw-ivf]。

DiskANN（Microsoft Research, NeurIPS 2019）使用 Vamana 图结构结合 PQ 压缩与 SSD 存储，可在 64 GB RAM 机器上索引十亿向量，达到 95% 1-recall@1 与 <3 ms 平均延迟；相比 HNSW，内存需求降低 10–100 倍，延迟从 <1 ms 增至 10–50 ms [^diskann]。ADSampling 技术进一步可在保持相同精度下为 HNSW 带来最高 2.65 倍加速、为 IVF 带来 5.58 倍加速，同时节省 HNSW 最多 75.3% 的评估维度 [^adsampling]。

#### 4.1.4 纯向量系统的结构性局限：语义漂移、CJK 关键词弱、时间推理缺失

纯向量记忆系统存在三大核心失效模式。其一为语义漂移（Semantic Drift）：同一概念的不同表述在嵌入空间中可能分散，导致检索不一致；即使最先进的十亿级搜索实现也难以保证跨训练运行和模型版本的嵌入一致性 [^semantic-drift]。其二为时间推理缺失：向量数据库缺乏影响检索的时间戳语义，六个月前的事实与昨天的事实若文本相似则嵌入距离相近，无法区分"下雨前施肥"与"下雨后施肥"的时序因果 [^semantic-drift]。其三为 CJK 关键词召回弱：向量搜索依赖语义匹配而非字面匹配，对于无空格分词的中文、日文、韩文，标准 BM25 几乎失效，混合搜索退化为纯向量检索 [^cjk-weak]。Synapse 论文通过实验系统验证了上述失败模式，并指出纯文本相似度检索对对话记忆召回严重不足——MADial-Bench 上最优嵌入模型 Recall@1 未超过 60% [^madial]。

### 4.2 图数据库与关系记忆

#### 4.2.1 知识图谱构建：LLMGraphTransformer、GraphRAG 的实体-关系-声明提取

知识图谱（Knowledge Graph, KG）通过显式三元组（实体-关系-实体）弥补向量存储的结构盲区。LLMGraphTransformer 已成为使用大语言模型从非结构化文本自动提取三元组的主流工具，被 LangChain、LlamaIndex 与 Neo4j 官方图谱构建器广泛采用 [^llmgraph]。其通过提示工程指导 LLM 生成带属性的节点与关系列表，支持自定义实体类型和关系类型模式。

GraphRAG（Microsoft Research, 2024）的索引阶段使用 LLM 从文本块中提取实体、关系和声明（claims），通过精确字符串匹配进行实体对齐，并将重复关系聚合为带权重的边 [^graphrag]。该过程属于抽象式摘要，关系和声明可能并未在原文中显式陈述。现代知识图谱构建需经过实体链接（Entity Linking）与指代消解（Coreference Resolution）才能将同一实体的多个提及合并，否则图谱会因重复实体膨胀并降低检索精度 [^llmgraph]。

#### 4.2.2 图数据库后端：Neo4j、Graphiti、Cognee、Kuzu 的架构差异

图数据库后端的选择决定了知识图谱的持久化能力、查询语言与扩展路径。Zep 的 Graphiti 引擎采用双时态模型（bi-temporal），每条边携带四个时间戳——真实世界有效时间与系统事务时间，实现事实的版本化与点查询（point-in-time query）[^zep-graphiti]。Graphiti 支持 Neo4j 5.26+、FalkorDB、Kuzu 0.11.2+ 和 Amazon Neptune 多种后端，P95 检索延迟低至 300 ms，且查询阶段无需 LLM 调用 [^zep-graphiti]。

Cognee 采用三重存储架构：向量存储负责语义召回，图存储负责结构推理，关系存储负责溯源与审计；其默认本地栈为 SQLite + LanceDB + Kuzu，可零配置运行，生产环境可切换为 Neo4j + Qdrant + PostgreSQL [^cognee]。Mem0 的 Graph Memory 功能被锁定在 Pro tier（249 美元/月），开源核心仅提供向量语义搜索与键值查找，且缺乏原生时态事实建模 [^mem0-pro]。NetworkX 是轻量级内存图处理的默认选择，适合 <100k 节点的原型；超过此规模通常需迁移至 Neo4j 或 FalkorDB [^networkx]。

#### 4.2.3 多跳推理与社区检测：从向量种子到图遍历的检索扩展

多跳推理（multi-hop reasoning）是图记忆系统的核心优势：从查询提取的实体出发，通过 BFS/DFS 遍历图结构收集多跳邻居三元组，将结构化路径转换为文本描述输入 LLM，显著优于纯向量检索在复杂问答上的表现 [^multihop]。Mem0 的图记忆在实体中心关系图上实现 67.1% 单跳和 51.2% 多跳准确率，同时将延迟降低 91% [^mem0-multihop]。

社区检测将图划分为语义聚类以支持全局问题回答。GraphRAG 使用 Leiden 算法进行分层社区检测，将知识图划分为多级社区层次结构（C0–C3），自下而上生成社区摘要；根级摘要比直接处理源文本减少 97% 的 token 消耗 [^leiden]。Zep/Graphiti 亦通过标签传播生成 cluster-level abstractions，Cognee 的 improve 管道则利用社区结构强化高频连接。

#### 4.2.4 实体链接与消歧：Resolution vs Deduplication 的分离必要性

LLM 生成的知识图普遍存在噪声与冗余：同一实体的不同变体（如 "LLMs"/"LLM"/"Large Language Models"）被分别提取为独立节点。现有系统主要依赖字符串匹配启发式方法，留下大量未解决的重复 [^dedup-rag]。Deg-RAG 实验表明，简单的去噪方法不仅大幅减少图规模，还持续提升多种 Graph-based RAG 变体的问答性能 [^dedup-rag]。

实体解析应分离为两个独立决策：Resolution（命名规范化——处理拼写、缩写、大小写变体，回答"我们该叫它什么？"）和 Deduplication（身份验证——基于完整节点嵌入判断是否为同一真实世界实体，回答"这两个记录是否指向同一件事物？"）[^resolution]。混淆这两个步骤会导致图静默腐烂。语义实体解析使用 LLM 直接匹配和合并 JSON 记录，通过 Chain-of-Thought 生成解释，在通用性上优于传统字符串/规则方法与纯嵌入方法 [^semantic-er]。

### 4.3 混合检索与融合策略

#### 4.3.1 多路召回架构：向量语义 + BM25 关键词 + 图遍历 + 时间窗口

2024–2025 年顶级 RAG 系统统一采用四阶段检索管线：Hybrid Retrieval（粗召回）→ Re-ranking（精排）→ LLM Routing/Filtering → Context Compression [^hybrid-rag]。粗召回层融合 dense（向量语义）、sparse（BM25/SPLADE 关键词）、graph neighborhood（图遍历）与 temporal window（时间窗口）四种信号。Hindsight 的 TEMPR 召回管线是典型实现：并行执行语义搜索（向量相似度）、BM25 精确匹配、图遍历（实体/时间/因果链接）与时间范围过滤，四路结果经融合后由 cross-encoder 重排序 [^hindsight]。在 Agent Memory Benchmark 的 10M 规模测试中，纯向量 RAG 基线仅 24.9%，而 Hindsight（结构化记忆 + 多策略检索）达 64.1%，证明当规模成为决定性约束时，多策略架构显著优于纯向量 [^hindsight]。

#### 4.3.2 融合算法：RRF (k=60) 的事实标准地位 vs 加权分数融合的脆弱性

融合算法决定多路召回结果如何合并为单一排序。下表对比了主流融合策略的核心机制与适用边界。

| 融合方法 | 数学形式 | 核心优势 | 主要脆弱性 | 生产采用度 |
|----------|----------|----------|------------|------------|
| RRF (k=60) | Σ 1/(k + rank_r(d)) | 无视分数量纲，零调参，跨检索器稳健 | 对深层排名文档区分力弱 | OpenSearch、ES、Azure AI Search、MongoDB Atlas、Weaviate 默认 [^rrf] |
| 加权分数融合 | α·dense + (1-α)·sparse | 可显式控制各通道权重 | 需归一化（min-max/z-score），对异常值敏感，α 需按领域调优 [^weighted] | 早期系统常见，正被 RRF 替代 |
| 级联漏斗 | 小模型候选 → 逐层精排 | 计算资源自适应分配，延迟可控 | 管线复杂，层间截断可能误杀高潜文档 | 法律/医疗等高精度场景 [^cascade] |

Reciprocal Rank Fusion（RRF）由 Cormack、Clarke、Büttcher 于 2009 年 SIGIR 提出，公式为 RRF_score(d) = Σ 1/(k + rank_r(d))，k=60 是跨数百个生产系统验证的标准默认值 [^rrf]。RRF 完全忽略原始分数，仅基于排名位置融合，因此天然兼容不同量纲的评分系统（BM25 与 cosine similarity）。其共识放大效应显著：当文档同时在两个检索器中排名靠前时，其 RRF 分数会指数级跃升 [^rrf-consensus]。加权分数融合需先将不同检索器分数归一化到同一量纲，再用 alpha 权重线性组合，但归一化对异常值敏感，且 alpha 需要按领域调优；无标签数据时建议优先使用 RRF [^weighted]。Swiss companies 基准测试（3,153 条记录）显示，embedding-only（nDCG 0.891）最初击败了 naive Hybrid（0.872），原因是 equal-weight RRF 让 BM25 的差结果污染了候选池；修复方法是在 RRF 与 cross-encoder 之间加入 funnel cutoff（top-30），此时 moderate weight 0.6/0.4 的 hybrid 才恢复竞争力——证明管线架构优于参数调优 [^pipeline-arch]。

#### 4.3.3 级联检索与重排序：渐进式语义漏斗、Cross-encoder、ColBERT

级联检索（Cascade Retrieval）采用渐进式语义漏斗：轻量 bi-encoder 粗选大量候选（Top-200），再用 7B 模型仅对候选集重排序，以获取大部分大模型质量同时保持小模型延迟 [^cascade]。重排序层是精度与延迟的终极战场。Cross-encoder 通过联合编码 query 和 document 实现细粒度交互，在语义相似但逻辑无关的复杂场景下显著优于其他 reranker；但延迟极高——CPU 上 100 个候选约 800 ms，GPU（batch=32）约 120 ms，是 bi-encoder 的 50–100 倍 [^cross-encoder]。

ColBERT 的 late interaction 架构在查询时仅需编码 query（一次前向传播），然后与预存的 document token-level embeddings 做 MaxSim 运算；实测中 ColBERT 查询速度是 cross-encoder 的 2.2 倍（22.6 ms vs 49.9 ms 每查询），top-5 排名重叠度达 92% [^colbert]。VikingMem 针对记忆检索的严格 p99 延迟要求（数百毫秒级）采用 ColBERT 风格的多向量重排序，并在提取阶段预计算 ColBERT 向量，应用量化与 token-merge 压缩技术，使存储开销与 dense vector 相当，同时避免 cross-encoder 的秒级 p99 延迟 [^vikingmem]。在 SciRerankBench 科学文献检索基准中，cross-encoder 在语义混淆任务上表现最优，而 sparse 模型 recall 仅约 44%，ColBERT 在反事实任务上降至 43.47%，验证了联合编码机制对捕捉细微语义差异的关键作用 [^scirerank]。

#### 4.3.4 CJK 语言挑战：jieba 预分词、bigram 索引、PGroonga polyglot 修复

CJK（中文、日文、韩文）语言的无空格特性对基于空白的 BM25 分词构成结构性障碍。标准 BM25 实现按空白分词，对中文几乎失效：查询"机器学习算法"会被当作单个 token，导致 BM25 匹配率接近零 [^cjk-bm25-fail]。在 Hindsight 的 4 路并行检索中，中文内容下 BM25 对 RRF 融合阶段几乎零贡献，使 hybrid search 退化为纯向量检索 [^cjk-bm25-fail]。下表对比了三种 CJK 检索修复策略的工程特征与效果。

| 策略 | 核心机制 | 适用场景 | 效果与代价 |
|------|----------|----------|------------|
| jieba 预分词 | 查询/文档侧使用 jieba 分词后再建索引 | 中文为主、轻量部署 | 纯中文查询"撞牆"测试中，BM25 score 从 0 提升至 0.26（Hit）[^jieba-fix] |
| CJK bigram 索引 | 标准 tokenizer + cjk_bigram filter，生成相邻两字组合 | 多语言混合、Elasticsearch/OpenSearch 生态 | 近似词边界，兼顾精确匹配与短语覆盖；可配置 output_unigrams 同时输出单字 [^cjk-bigram] |
| PGroonga polyglot | 单一多语言索引同时处理英/中/日/韩等混合内容 | 生产级多语言 RAG、PostgreSQL 生态 | Hindsight 0.7.0 引入，替代原生 BM25 backend，支持可配置语言字典 [^pgroonga] |

jieba 预分词是最轻量的修复方案，但对专有名词和新词需使用 bigram fallback [^jieba-fix]。Elasticsearch/OpenSearch 的 cjk_bigram token filter 是处理 CJK 文本的标准工程方案：将标准 tokenizer 生成的 CJK 术语形成 bigram（相邻两字组合），默认无相邻字符时以单字输出；可配置 output_unigrams=true 同时输出 unigram + bigram，以兼顾精确匹配与短语覆盖 [^cjk-bigram]。Hindsight 0.7.0（2026-05-27）针对 CJK 问题推出 PGroonga polyglot backend，用单一多语言索引同时处理英/中/日/韩等混合语言内容，并支持将 fact extractor 的输出语言与索引语言独立配置 [^pgroonga]。对于以 CJK 为主要交互语言的 Agent 记忆系统，混合检索必须在索引层解决分词问题，否则 BM25 通道将形同虚设，导致 RRF 融合严重偏向向量语义单一路径。

[^e5-openai]: Greenback Bears and Fiscal Hawks. arXiv:2411.07142. 2024-11. https://arxiv.org/pdf/2411.07142v1

[^7b-decoder]: Q2 2026 Open-Source Embedding Models Benchmark. 2026-05-16. https://iotdigitaltwinplm.com/open-source-embedding-models-benchmark-q2-2026/

[^matryoshka]: Kusupati et al., "Matryoshka Representation Learning". NeurIPS 2022; OpenAI text-embedding-3 API. 2024. https://arxiv.org/pdf/2205.13147; https://aiwiki.ai/wiki/embedding_vector

[^bge-m3]: How to Choose Common Embedding Models. KnightLi. 2026-04-23. https://knightli.com/en/2026/04/23/compare-openai-bge-e5-gte-jina-embedding-models/

[^madial]: MADial-Bench. NAACL 2025. https://aclanthology.org/2025.naacl-long.499.pdf

[^qdrant-pinecone]: Pinecone vs Weaviate vs Qdrant vs Milvus vs pgvector. aiml.qa. 2026-04-22. https://aiml.qa/vector-database-comparison-2026/

[^lancedb]: LanceDB. Y Combinator. 2023-05-05. https://www.ycombinator.com/companies/lancedb

[^pgvector]: Cheap Vector Databases in 2025. Superfox. 2025-11-19. https://www.superfox.ai/blog/top-cheap-vector-databases-2026

[^ragperf]: RAGPerf. arXiv:2603.10765. 2026-03-11. https://arxiv.org/html/2603.10765v1

[^hnsw-ivf]: HNSW vs IVF-PQ vs LSH. abhik.ai. 2025-01-23. https://www.abhik.ai/concepts/embeddings/ann-comparison; Milvus AI FAQ. 2026-02-26. https://milvus.io/ai-quick-reference/

[^diskann]: DiskANN: Fast Accurate Billion-point Nearest Neighbor Search on a Single Node. NeurIPS 2019. https://proceedings.neurips.cc/paper/2019/hash/09853c7fb1d3f8ee67a61b6bf4a7f8e6-Abstract.html; DiskANN Explained. Milvus Blog. 2025-05-20. https://milvus.io/blog/diskann-explained.md

[^adsampling]: High-Dimensional Approximate Nearest Neighbor Search. ACM TKDE. 2023. https://dl.acm.org/doi/pdf/10.1145/3589282

[^semantic-drift]: Agentic AI Memory vs Vector Database. Atlan. 2026-04-14. https://atlan.com/know/agentic-ai-memory-vs-vector-database/; Synapse. arXiv:2601.02744. 2023-07-14. https://arxiv.org/html/2601.02744v3; Semantic Drift and Embedding Inconsistency. VLIZ TechDoc. 2025. https://www.vliz.be/imisdocs/publications/417102.pdf

[^cjk-weak]: Engram AI Rust. GitHub. 2024. https://github.com/tonitangpotato/engram-ai-rust

[^llmgraph]: LangChain LLMGraphTransformer API; Neo4j LLM Graph Builder. 2025. https://python.langchain.com/api_reference/experimental/graph_transformers.html; https://neo4j.com/labs/genai-ecosystem/llm-graph-builder/

[^graphrag]: Edge et al., "From Local to Global: A GraphRAG Approach to Query-Focused Summarization". Microsoft Research. arXiv:2404.16130. 2024-04. https://arxiv.org/html/2404.16130v2

[^zep-graphiti]: Rasmussen et al., "Zep: A Temporal Knowledge Graph Architecture for Agent Memory". arXiv:2501.13956. 2025-01-20. https://arxiv.org/abs/2501.13956; Neo4j Developer Blog. 2026-03-24. https://neo4j.com/blog/developer/graphiti-knowledge-graph-memory/

[^cognee]: Cognee official. 2025-09-23. https://www.cognee.ai/; LanceDB case study. 2026-02-24. https://www.lancedb.com/blog/case-study-cognee

[^mem0-pro]: Evermind.ai. 2026-03-15. https://evermind.ai/blogs/mem0-alternative; Vectorize.io. 2026-04-02. https://vectorize.io/articles/mem0-vs-zep

[^networkx]: Safjan. 2026-02-07. https://safjan.com/simple-inmemory-knowledge-graphs-for-quick-graph-querying/

[^multihop]: StepChain GraphRAG. arXiv:2510.02827. 2025-10-03. https://arxiv.org/html/2510.02827v1; LogosKG. medRxiv. 2026-01-13. https://www.medrxiv.org/content/10.64898/2026.01.12.26343957v1

[^mem0-multihop]: APEX-MEM / Advanced Memory Architectures Survey. arXiv:2604.14362. 2026. https://arxiv.org/pdf/2604.14362

[^leiden]: Edge et al. GraphRAG. 2024; Memgraph community call. 2026-06-04. https://public-assets.memgraph.com/community-calls/microsoft-graphrag-memgraph.pdf

[^dedup-rag]: Zheng et al., "Deg-RAG: Denoised Knowledge Graphs for Retrieval Augmented Generation". arXiv:2510.14271. 2025-10-16. https://arxiv.org/html/2510.14271v1

[^resolution]: DecodingAI. 2026-06-02. https://www.decodingai.com/p/keep-knowledge-graph-clean

[^semantic-er]: Graphlet AI. 2025-08-10. https://blog.graphlet.ai/the-rise-of-semantic-entity-resolution-45c48d5eb00a

[^hybrid-rag]: 知乎专栏. 2025-11-21. https://zhuanlan.zhihu.com/p/1975149609954341648; LearnWithParam. 2026-02-05. https://www.learnwithparam.com/blog/hybrid-retrieval-rag-vector-graph-search; ORAN Hybrid GraphRAG. arXiv:2507.03608. 2025-06-30. https://arxiv.org/html/2507.03608v2

[^hindsight]: Hindsight is 20/20. arXiv:2512.12818. 2025-12. https://arxiv.org/html/2512.12818v1; Vectorize.io. 2026. https://vectorize.io/product; Hindsight Blog. 2026-03-12. https://hindsight.vectorize.io/blog/2026/03/12/spreading-activation-memory-graphs

[^rrf]: Cormack, Clarke, Büttcher, "Reciprocal Rank Fusion outperforms Condorcet and individual Rank Learning Methods". SIGIR 2009. https://bigdataboutique.com/blog/reciprocal-rank-fusion-how-it-works-and-when-to-use-it. 2026-05-18

[^rrf-consensus]: Nemorize. 2026-04-21. https://nemorize.com/roadmaps/2026-modern-ai-search-rag-roadmap/lessons/hybrid-retrieval-systems

[^weighted]: CalibreOS. 2026. https://www.calibreos.com/learn/genai-hybrid-search

[^cascade]: ACL Anthology / PROPOR 2026. 2026. https://aclanthology.org/2026.propor-1.55.pdf

[^pipeline-arch]: koray-kaya/hybrid-search-benchmark. GitHub. 2026-04-11. https://github.com/koray-kaya/hybrid-search-benchmark

[^cross-encoder]: Towards Data Science. 2026-04-13. https://towardsdatascience.com/advanced-rag-retrieval-cross-encoders-reranking/

[^colbert]: Khattab and Zaharia, "ColBERT: Efficient and Effective Passage Search via Contextualized Late Interaction over BERT". arXiv:2004.12832. 2020. https://arxiv.org/pdf/2004.12832v1.pdf; Towards Data Science. 2026-04-13. https://towardsdatascience.com/advanced-rag-retrieval-cross-encoders-reranking/

[^vikingmem]: VikingMem. arXiv:2605.29640. 2026-05-28. https://arxiv.org/html/2605.29640v1

[^scirerank]: SciRerankBench. arXiv:2508.08742. 2025. https://arxiv.org/html/2508.08742v1

[^cjk-bm25-fail]: vectorize-io/hindsight issues #1077. GitHub. 2026-04-15. https://github.com/vectorize-io/hindsight/issues/1077; rohitg00/agentmemory issues #344. GitHub. 2026-05-13. https://github.com/rohitg00/agentmemory/issues/344

[^jieba-fix]: MakiDevelop/memory-hall. GitHub. 2026-04-18. https://github.com/MakiDevelop/memory-hall

[^cjk-bigram]: OpenSearch Documentation. 2025-08-28. https://docs.opensearch.org/docs/latest/analyzers/token-filters/cjk-bigram/

[^pgroonga]: Hindsight Blog. 2026-05-27. https://hindsight.vectorize.io/blog/2026/05/27/version-0-7-0
