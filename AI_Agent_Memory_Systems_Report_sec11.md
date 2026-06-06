## 11. 工业系统全面对比

在审视了记忆污染攻击向量与防御机制之后，本章从工业实践角度评估主流系统如何在架构设计中内化这些安全要求。

### 11.1 架构与存储后端

#### 11.1.1 Mem0：Dual-store（Vector+Graph+KV），混合检索，企业合规

Mem0 采用双存储架构——向量数据库作为主存储，知识图谱（Pro tier）与 KV 存储作为辅助层，支持用户、会话、Agent 三级作用域隔离[^mem0-paper]。2026 年 4 月，其算法从写时四操作决策（ADD/UPDATE/DELETE/NOOP）重构为 Single-pass ADD-only 提取，将每次写入压缩至单次 LLM 调用，同时引入实体链接与多信号并行检索（语义相似度 + BM25 关键词 + 实体匹配三路打分融合）[^mem0-blog]。在 LOCOMO 数据集上，其搜索延迟 p50 为 0.148 秒、p95 为 0.200 秒，总延迟 p50 0.708 秒，为对比系统中最低[^mem0-paper]。

#### 11.1.2 Zep/Graphiti：Temporal Knowledge Graph，双时态边，异步预计算

Zep 以 Graphiti 引擎为核心，基于 Neo4j 构建双时态知识图谱（Temporal Knowledge Graph），每条边携带四个时间戳（created、expired、valid、invalid），实现"what was true then / what's true now"的 point-in-time 查询[^zep-paper]。事实、实体摘要与社区摘要均在后台异步预计算，检索路径本身不调用 LLM，因此 p95 延迟可稳定控制在 200 毫秒以内，且随图规模从 1 万节点扩展至 1 亿节点时，延迟仅从 148 毫秒增长至 168 毫秒[^zep-pricing]。

#### 11.1.3 MemGPT/Letta：OS 分页式三层，Agent 自主管理，DMR 92.5–93.4%

Letta（原 MemGPT）源自 UC Berkeley BAIR Lab，采用操作系统虚拟内存分页启发的三层架构：Core Memory（常驻上下文，类比 RAM）、Recall Memory（对话历史向量索引，类比磁盘缓存）、Archival Memory（长期外部数据库存储，类比冷存储）[^letta-research]。其差异化在于 Agent 通过 function call 自主决定何时在各层之间换入换出，甚至可自编辑 persona 与 human 块，实现"LLM as its own memory controller"的自治模式[^memgpt-paper]。DMR 基准上，原版 MemGPT 得分 93.4%，后被 Zep 以 94.8% 超越[^zep-paper]。

#### 11.1.4 LangMem：LangGraph 原生扁平 KV，程序性记忆，免费开源

LangMem 深度绑定 LangGraph 生态，采用扁平 KV 项加向量搜索的极简架构，存储于 LangGraph BaseStore 或 PostgreSQL 中，无原生知识图谱与实体提取[^langmem-vectorize]。其独家能力是程序性记忆（Procedural Memory）——Agent 可基于对话反馈自动改写自身的 system prompt 与工具调用规则，实现行为层面的自我优化[^langmem-vectorize]。代价是检索延迟极高，p50 达 17.99 秒，不适合交互式场景[^agentmarketcap]。

#### 11.1.5 Hindsight：单一 PostgreSQL 四逻辑网络，TEMPR 多策略，MIT 开源

Hindsight 以单一 PostgreSQL 为存储后端，通过 pgvector、HNSW、BM25、图索引与时态索引的复合，在物理层统一支撑四逻辑网络：World Facts、Experiences、Entity Summaries、Evolving Beliefs[^hindsight-paper]。其 TEMPR（Temporal Entity-aware Memory Processing & Retrieval）策略并行运行语义搜索、BM25、图遍历与时态检索四种方法，经 RRF 重排序后输出[^hindsight-paper]。该架构在开源 20B 模型下将 LongMemEval 从全上下文基线的 39.0% 提升至 83.6%，换用 Gemini-3 后达 91.4%[^hindsight-paper]。

**表 11-1 五系统架构对比**

| 维度 | Mem0 | Zep/Graphiti | Letta (MemGPT) | LangMem | Hindsight |
|:---|:---|:---|:---|:---|:---|
| 存储后端 | 向量 + 图(Pro) + KV | Neo4j 双时态图 | 向量 + 数据库 + 上下文块 | LangGraph BaseStore / Postgres | PostgreSQL + pgvector |
| 嵌入策略 | Single-pass ADD-only | 异步预计算 | Agent 自主换页 | 背景提取 + 合并 | Retain→Recall→Reflect |
| 检索策略 | 语义+BM25+实体 三路融合 | 语义+BM25+图遍历+RRF | Agentic tool call | 单路向量相似度 | 4策略并行(TEMPR)+RRF |
| 记忆分层 | User/Session/Agent 三级 | Episodic→Semantic→Community | Core/Recall/Archival OS 三层 | Namespace 隔离 | 四逻辑网络 |
| 开源许可 | Apache 2.0 | Graphiti MIT / Cloud 商业 | Apache 2.0 | MIT | MIT |
| 延迟(p50/p95) | 0.148s/0.200s [^mem0-paper] | <0.2s 稳定 [^zep-pricing] | 取决于配置 | 17.99s/59.82s [^agentmarketcap] | 0.1–0.6s [^zep-vs-hindsight] |

上表揭示了 2026 年工业记忆系统的两条架构路线：Mem0、Zep、Hindsight 走向"后端复杂化"——通过多存储、多信号、多策略提升检索质量；Letta 与 LangMem 则走向"前端自治化"——将记忆控制权交给 Agent 或框架本身。前者以基础设施深度换取准确率，后者以架构简洁性换取集成便利性。值得注意的是，LangMem 的 17.99 秒 p50 延迟表明，极简架构在规模化检索时可能付出沉重的性能代价[^agentmarketcap]。

### 11.2 性能与成本

#### 11.2.1 LongMemEval / DMR 性能矩阵与延迟分位对比

在 LongMemEval 基准上，Mem0 新算法以 93.4% 跃居工业系统首位（旧版仅 49%），Hindsight 以 Gemini-3 达到 91.4%，Zep 为 90.2%[^mem0-blog][^hindsight-paper][^zep-vs-hindsight]。LoCoMo 多会话测试中，Zep 以 94.7% 领先，Mem0 新算法 91.6%，Hindsight 89.61%，LangMem 仅 58.10%[^turion]。DMR 基准上 Zep 以 94.8% 超越 MemGPT 的 93.4%，成为该指标的新 SOTA[^zep-paper]。时态推理子任务中，Zep 以 63.8% 显著领先 Mem0 旧版的 49.0%，差距达 15 个百分点，印证了双时态图在时间敏感查询上的结构性优势[^turion]。

#### 11.2.2 定价结构：Mem0 Free→$19→$249、Zep Flex $25、LangMem/Hindsight 免费

Mem0 采用阶梯式功能锁定：Free（1 万记忆/月）→ Starter $19/月（5 万记忆，纯向量）→ Pro $249/月（50 万记忆 + 图 + 分析）→ Enterprise 定制[^mem0-pricing]。从 $19 到 $249 的 13 倍跃升意味着中等规模团队若需图查询能力，必须直接承担企业级定价[^evermind]。Zep 采用信用点数制：Flex $25/月（2 万 credits）起，所有 tier 均开放完整功能（含时态图），按量计费[^zep-pricing]。LangMem 与 Hindsight 均为 MIT 开源，无托管云服务，平台订阅费为零，但运维成本由用户自行承担[^langmem-vectorize][^hindsight-paper]。Letta 免费 tier 含 3 个 Agent 与 BYOK，Pro $20/月，Max $200/月，定位更偏向 Agent Runtime 而非纯记忆层[^letta-research]。

#### 11.2.3 总拥有成本：自托管 vs 托管 vs 企业 SaaS 的 5 年成本模型

以 10 万记忆/月的生产规模估算五年 TCO：Mem0 Pro 托管方案约 $14,940（$249×60 月）；Zep Flex 按量计费约 $15,000–$18,000；Letta/Hindsight/LangMem 自托管方案基础设施成本约 $30,000（$500/月 云资源 × 60 月），但需叠加 DevOps 人力成本[^ranksquire][^techsy]。RankSquire 的 Sovereign Migration Trigger 指出，日活 7,500 任务以上时自托管比 Mem0 Pro 便宜；低于 5,000 任务/天且无专职 DevOps 时，托管方案便宜约 40%[^ranksquire]。因此，成本敏感型个人开发者与初创团队应优先选择开源方案，而中大型企业若追求合规与 SLA，托管 SaaS 的综合成本反而更低。

**表 11-2 性能与成本对比**

| 指标 | Mem0 | Zep/Graphiti | Letta (MemGPT) | LangMem | Hindsight |
|:---|:---|:---|:---|:---|:---|
| LongMemEval | 93.4% (新) [^mem0-blog] / 49% (旧) | 90.2% [^zep-vs-hindsight] | 未公布 | 未公布 | 91.4% [^hindsight-paper] |
| LoCoMo | 91.6% (新) [^turion] | 94.7% [^turion] | 未公布 | 58.1% [^turion] | 89.61% [^hindsight-paper] |
| DMR | 未公布 | 94.8% [^zep-paper] | 93.4% [^zep-paper] | 未公布 | 未公布 |
| 检索预算(tokens) | ~7.0K [^mem0-blog] | ~4.4K [^zep-vs-hindsight] | 取决于配置 | 未公布 | ~8.2K [^zep-vs-hindsight] |
| 托管月费(10万规模) | $249 | ~$200–$300 | $20–$200 | $0 | $0 |
| 五年TCO(托管) | ~$14,940 | ~$15,000–$18,000 | ~$1,200–$12,000 | $0 | $0 |
| 五年TCO(自托管) | ~$30,000+ | ~$30,000+ | ~$30,000+ | ~$30,000+ | ~$30,000+ |

该矩阵显示，准确率与成本之间存在非线性权衡。Mem0 与 Hindsight 在 LongMemEval 上均突破 91%，但 Mem0 的 token 效率（~7.0K）优于 Hindsight（~8.2K），而 Zep 以 ~4.4K 的最低检索预算实现 90.2% 准确率，在 token 经济性上表现最佳[^zep-vs-hindsight]。LangMem 虽未公布 LongMemEval 分数，但其 58.1% 的 LoCoMo 得分已表明纯向量扁平架构在长程多会话场景下的天花板有限。

### 11.3 生态与集成

#### 11.3.1 SDK 支持：Python/JS/Go 覆盖度与框架绑定深度

Mem0 提供 Python 与 JavaScript SDK，覆盖全栈团队；Zep 提供 Python、TypeScript 与 Go SDK，对后端团队友好；Letta 以 Python 为主，2026 年起逐步扩展 TypeScript 与 Rust SDK[^turion][^weavai-letta]；LangMem 与 Hindsight 均仅提供 Python SDK[^langmem-vectorize]。多语言 SDK 的完备性直接影响企业采纳广度——Zep 的 Go SDK 使其在微服务生态中具备差异化优势，而 LangMem 的单语言支持将其潜在用户群限制在 Python 后端团队。

#### 11.3.2 社区规模：GitHub stars、开发者数、企业客户案例

截至 2026 年第二季度，Mem0 以约 4.8 万 GitHub stars 居首，Letta 约 2.1 万，Zep/Graphiti 约 2.4 万，Hindsight 约 4,000，LangMem 约 1,300[^turion][^atlan]。融资层面，Mem0 获 Y Combinator 与 Peak XV 领投的 $24M Series A，Letta 获 Felicis $10M 种子轮，Zep 亦为 YC 背景公司[^agentmarketcap]。Mem0 的 AWS Strands Agents SDK 独家合作（2025 年 10 月起）是其企业渠道的核心壁垒；Zep 则与 Microsoft AutoGen 有官方集成文档，强调框架无关性[^atlan][^autogen-zep]。

#### 11.3.3 适用场景矩阵：个人开发者、企业部署、时间敏感、成本敏感

通用企业部署与快速集成首选 Mem0——其 5 分钟上手体验、SOC 2 Type II 与 HIPAA 合规认证，以及最大开发者社区，使其成为"安全默认选项"[^techsy]。时间敏感场景（审计、医疗记录追踪、项目管理）首选 Zep——双时态图的 point-in-time 查询能力在竞品中独一无二，自动事实失效机制可避免过时信息污染[^zep-paper]。长期自主运行 Agent 首选 Letta——OS 分页式架构赋予 Agent 对记忆的完全自治权，适合需连续运行数周的任务型 Agent[^letta-research]。已采用 LangGraph 的团队首选 LangMem——零基础设施的框架原生集成使其摩擦成本最低，但跨框架迁移价值骤减[^langmem-vectorize]。成本敏感且具备 DevOps 能力的团队首选 Hindsight——MIT 许可证、单一 Postgres 依赖、开源系统中最高的 LongMemEval 分数（91.4%），使其成为自托管场景的最优解[^hindsight-paper]。

**表 11-3 生态与适用场景矩阵**

| 维度 | Mem0 | Zep/Graphiti | Letta (MemGPT) | LangMem | Hindsight |
|:---|:---|:---|:---|:---|:---|
| GitHub Stars | ~48K [^atlan] | ~24K [^turion] | ~21K [^letta-research] | ~1.3K [^turion] | ~4K [^vectorize-hindsight] |
| SDK 语言 | Python, JS | Python, TS, Go | Python 为主 | Python | Python |
| 核心框架绑定 | 框架无关 | 框架无关 | Letta Runtime | LangGraph 深度绑定 | 框架无关 |
| 合规认证 | SOC 2, HIPAA | SOC 2 Type II, HIPAA | 自托管负责 | 无 | 无 |
| 企业集成 | AWS 独家合作 | AutoGen 官方集成 | 研究/初创为主 | LangChain 生态 | Vectorize.io 维护 |
| 最佳适用场景 | 通用企业记忆 | 时间敏感/审计合规 | 长期自主 Agent | LangGraph 团队 | 成本敏感/自托管 |
| 不推荐场景 | 深度时态推理 | 纯向量轻量查询 | 仅需记忆层 | 非 LangGraph 栈 | 无 DevOps 团队 |

该矩阵表明，2026 年的记忆系统选型已不再是单纯的技术比较，而是团队技术栈、预算约束、合规需求与时态推理需求四维度的综合权衡。Mem0 凭借生态广度与合规深度占据企业托管市场；Zep 以时态建模的不可替代性锁定金融、医疗等审计密集型行业；Letta、LangMem、Hindsight 则在开源长尾中分别服务于 Agent 自治、框架原生与成本极致敏感三类细分需求。对于正在构建自研记忆层的团队而言，工业头部系统的分化路径提示了一个关键设计原则：记忆系统正从"功能插件"进化为"独立基础设施层"，其存储后端、检索引擎与安全边界应尽早考虑服务化独立部署，而非内嵌于 Agent 循环之中[^atlan]。

[^mem0-paper]: Mem0: A Scalable Memory-Centric Architecture. ECAI 2025. https://arxiv.org/abs/2504.19413
[^zep-paper]: Zep: A Temporal Knowledge Graph Architecture for Agent Memory. 2025. https://arxiv.org/pdf/2501.13956v1
[^letta-research]: Ry Walker Research. Letta (formerly MemGPT) Research. 2026-02-22. https://rywalker.com/research/letta
[^langmem-vectorize]: Vectorize.io. Best AI Agent Memory Systems. 2026-03-14. https://vectorize.io/articles/best-ai-agent-memory-systems
[^hindsight-paper]: Hindsight: Temporal Entity-aware Memory Processing & Retrieval. 2025-12. https://arxiv.org/html/2512.12818v1
[^mem0-blog]: Mem0 Blog. AI Memory Benchmarks in 2026. 2026-05-11. https://mem0.ai/blog/ai-memory-benchmarks-in-2026
[^autogen-zep]: Microsoft AutoGen. Agent Memory with Zep. 2024-09-04. https://microsoft.github.io/autogen/0.2/docs/ecosystem/agent-memory-with-zep/
[^zep-vs-hindsight]: Zep vs Hindsight. Vectorize.io. 2026-05-31. https://www.getzep.com/vectorize-hindsight-alternative/
[^mem0-pricing]: Mem0 Pricing. 2026. https://mem0.ai/pricing
[^zep-pricing]: Zep Pricing. 2024-11-14. https://www.getzep.com/
[^turion]: Turion.ai. Mem0 vs Zep vs LangMem. 2026-05-21. https://turion.ai/blog/mem0-vs-zep-vs-langmem-agent-memory-comparison-2026/
[^atlan]: Atlan. Best AI Agent Memory Frameworks 2026. 2026-04-02. https://atlan.com/know/best-ai-agent-memory-frameworks-2026/
[^agentmarketcap]: Agent Market Cap. Agent Memory Vendor Landscape 2026. 2026-04-10. https://agentmarketcap.ai/blog/2026/04/10/agent-memory-vendor-landscape-2026-letta-zep-mem0-langmem
[^techsy]: Techsy.io. Best AI Agent Memory Tools. 2026-05-12. https://techsy.io/en/blog/best-ai-agent-memory-tools
[^ranksquire]: RankSquire. Long-Term Memory for AI Agents. 2026-05-06. https://ranksquire.com/2026/05/06/long-term-memory-for-ai-agents/
[^evermind]: EverMind.ai. Mem0 Alternative. 2026-05-27. https://evermind.ai/blogs/mem0-alternative
[^weavai-letta]: Weavai.app. Letta MemGPT Review 2026. 2026-05-09. https://weavai.app/blog/2026/05/09/letta-memgpt-review-2026/
[^memgpt-paper]: MemGPT: Towards OS-inspired LLM Memory Management. 2023. https://www.leoniemonigatti.com/papers/memgpt.html
