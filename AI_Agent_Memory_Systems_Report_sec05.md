## 5. 时间推理与记忆演化

前述存储层的时间窗口检索信号揭示了时间元数据在召回中的价值，而本章将这一观察推进到推理层——探讨记忆系统如何对时间本身进行建模、查询与演化。

### 5.1 双时态建模

#### 5.1.1 有效时间与事务时间的数据库理论定义

双时态（bitemporal）数据库理论由 Snodgrass 与 Ilsoo 于 1998 年系统阐述。该理论定义了两个正交的时间维度：有效时间（valid time）指事实在模型化现实中为真的时间段；事务时间（transaction time）指事实在数据库中被记录为当前数据的时间段。二者相互独立：valid time 可自由修改且可指向未来，而 transaction time 不可晚于当前时间且不可更改。[^1] 这一正交性为后续所有时态 Agent 记忆系统奠定了形式化基础——存储层必须同时回答 "事实何时为真" 与 "系统何时知晓" 两个不同问题。

#### 5.1.2 Zep/Graphiti 的四时间戳实现

Zep 的 Graphiti 引擎将经典双时态理论工程化为每条边上的四个时间戳：t_valid（事实在现实世界中开始为真）、t_invalid（事实在现实世界中失效）、t'_created（系统记录时间）、t'_expired（系统作废时间）。[^2] 当新信息与现有事实存在语义矛盾时，Graphiti 不执行物理删除，而是将旧边的 t_invalid 设为当前时刻，在事务时间轴 T' 上保留完整审计轨迹。这种 temporal invalidation 机制使系统能够回答 "2025-12-01 时什么为真" 这类时间点查询（point-in-time query），而纯向量检索无法区分历史事实与当前事实。[^7]

#### 5.1.3 XTDB 与 BiTRDF 的形式化基础

XTDB 将 bitemporality 作为一等公民：put 事务可显式指定 valid-time（默认等于 transaction-time），文档持续有效直到被新的 put 或 delete 显式覆盖；as-of 查询允许同时约束 valid-time 与 transaction-time，实现审计视图与历史分析的分离。[^3] 在语义 Web 领域，BiTemporal RDF（BiTRDF）将 valid time 与 transaction time 引入标准 RDF，把所有资源与关系视为 inherently bitemporal，支持时态环境下的类型传播、domain-range 推理与传递关系。[^4]

**表 1 主流双时态模型对比**

| 维度 | 经典关系型（Snodgrass） | Zep/Graphiti | XTDB | BiTRDF | Mem0（基准对照） |
|:---|:---|:---|:---|:---|:---|
| Valid time 支持 | 是 | 是（t_valid / t_invalid） | 是（可显式指定） | 是 | 否（仅创建时间戳） |
| Transaction time 支持 | 是 | 是（t'_created / t'_expired） | 是（自动记录） | 是 | 否 |
| 矛盾处理 | 版本化更新 | Temporal invalidation（保留历史） | 显式覆盖 / 删除 | 形式化推理 | 破坏性 UPDATE / DELETE |
| 时间点查询 | SQL:2011 时态谓词 | 自动窗口过滤 | as-of 双轴查询 | 时态 SPARQL | 不支持 |
| 存储后端 | 关系型数据库 | Neo4j / FalkorDB 属性图 | 文档型（Crux） | RDF 三元组存储 | 向量数据库 |

上表揭示了当前工业系统的一个结构性分野：Zep、XTDB 与 BiTRDF 将双时态建模内嵌于存储语义，而 Mem0 等主流向量记忆系统仅将时间作为元数据标签，缺乏时态查询引擎。这一差异直接决定了系统能否回答 "当时何为真"，而非仅 "何时记录了此事实"。对于需要追踪用户偏好变迁、合同状态演化的 Agent 场景，双时态建模不是可选优化，而是正确性前提。

### 5.2 时间推理查询

#### 5.2.1 LongMemEval Temporal 子任务

LongMemEval 基准将 temporal reasoning 列为评估 Agent 长期记忆的五大核心能力之一，包含 133 道时间敏感问题（占 500 题总量的 26.6%），要求模型在约 115K token 的多会话历史中推断事件先后顺序、持续时间及状态变迁。[^5] 该子任务被证实为最难类别之一：TDBench 研究发现，当 RAG 检索到的上下文存在时间错位（temporal misalignment）时，模型倾向于回答 "no answer" 而非依赖自身参数知识，表明时间对齐是 RAG 向 Agent 记忆演进时必须解决的前置问题。[^8]

#### 5.2.2 TSM：语义时间线替代对话时间线

Temporal Semantic Memory（TSM）针对现有系统按对话时间而非实际发生时间组织记忆的缺陷，提出语义时间线（semantic timeline）概念。TSM 通过 spaCy 解析查询中的显式与相对时间表达，构建时间约束 T_q，并在检索阶段以时间过滤为 primary key、语义相似度为 secondary key 进行重排。在 LongMemEval-S 的 Temporal 类别上，TSM 将准确率从 Zep 的 36.50% 提升至 69.92%，相对增幅达 91.6%。[^6] 消融实验表明，移除时态模块后 Temporal 准确率下降 8.6%，证实了语义时间线对时间敏感推理的关键作用；Multi-Session 准确率提升 20.30% 则凸显了 durative memory（持续记忆）在跨会话连贯性中的价值。

#### 5.2.3 Graphiti 的时态过滤管线

Graphiti 的检索管线在查询阶段自动执行时态窗口过滤：查询 "now" 返回有效窗口尚未关闭的边；查询特定历史日期则返回该日期落在 valid-from / valid-to 区间内的边。上下文构造器将检索到的事实与其时间有效范围一起格式化，使 LLM 直接获得时间对齐的证据，无需在生成阶段自行推断事实时效性。[^7] 该管线 P95 延迟约 300ms，且检索阶段零 LLM 调用，将时态推理的成本从生成阶段前移至存储引擎层。独立基准测试显示，Zep 在 LongMemEval 上得分 63.8%，较 Mem0 的 49.0% 高出 14.8 个百分点，差距主要由时态图架构驱动。[^2]

### 5.3 记忆演化机制

#### 5.3.1 记忆巩固

Generative Agents 的记忆流（memory stream）架构是 Agent 记忆巩固的奠基范式：原始观察以自然语言记录，通过重要性、时效性、相关性三因子评分筛选；当记忆积累至阈值，Agent 调用 LLM 生成更高层次的反思（reflection），将多个观察综合为抽象语义记忆。[^8] 这一过程在认知科学中对应海马体-新皮层的系统巩固（systems consolidation）：NREM 睡眠期间，海马体通过 sharp-wave ripples 重放日间经验，与新皮层慢振荡耦合，逐步将情景记忆转化为语义图式。[^9] HeLa-Mem 等后续工作将 Hebbian 关联学习与反思蒸馏结合，显式实现了 episodic → semantic 的层级跃迁，强调知识蒸馏而非简单摘要。[^10]

#### 5.3.2 记忆衰减

MemoryBank 首次系统地将 Ebbinghaus 遗忘曲线引入 LLM Agent 记忆：记忆强度随时间指数衰减，每次召回后强度增强并将经过时间重置为零，实现类人的 "用进废退"。[^10] Engram 的 "sleep" 巩固管道直接实现该公式：strength *= decayRate ^ daysSinceLastAccess（默认 0.95^days），未召回记忆最终低于剪枝阈值并被归档。[^13]

在工程实现上，NornicDB 提出三层认知衰减模型（three-tier cognitive decay）：情景记忆（episodic）半衰期 7 天、语义记忆（semantic）半衰期 69 天、程序性记忆（procedural）半衰期 693 天，每层配置独立的 scoreFloor 以防止关键事实被完全遗忘。[^11] 然而，后续研究指出该模型存在范畴错误：事实性知识不应随时间衰减，衰减的应是注意相关性而非真值本身；将 Ebbinghaus 曲线统一应用于所有内容类型，会导致系统 "遗忘本应记住的事实" 或 "记住本应遗忘的噪音"。[^11] Mem0 在 2026 年的演进中区分了被动老化（passive aging，TTL/LRU 驱动的噪音抑制）与主动遗忘（active forgetting，LLM 驱动的矛盾消解），主张 "被动老化用于噪音，主动遗忘用于事实"。[^12]

#### 5.3.3 记忆更新：写时决策 vs 读时过滤

当前工业界在记忆更新策略上存在路线之争。Mem0 早期架构采用写时决策（write-time decision）：每条新记忆经过 AUDN（Add/Update/Delete/Noop）循环，由 LLM 判定是否构成矛盾并直接覆盖旧记忆，存储始终反映当前真值，但历史丢失且每次写入伴随 LLM 调用成本。[^15] Zep/Graphiti 则坚持读时过滤（read-time filtering）：写入时仅执行 temporal invalidation，旧边保留在图中，查询时通过 valid-time 窗口自动过滤；存储层累积历史版本，检索引擎承担时态筛选职责。[^14]

两种路线并非简单的技术优劣之分，而是信任模型与成本结构的差异。写时决策适合高可信度、低噪声场景（医疗、金融），确保存储一致性；读时排序适合高频交互、高噪声场景（个人助手、游戏），将成本转移至已发生的查询阶段。2026 年 4 月，Mem0 转向 Single-pass ADD-only 配合读时多信号排序，标志着高频场景下读时路线的胜利，但并未否定写时决策在强一致性需求领域的价值。[^15]

#### 5.3.4 矛盾消解

TOKI（2026）首次将数据库并发控制理论系统引入 Agent 记忆写路径，提出 Bitemporal Operator Algebra。该框架将四种生产级矛盾消解策略——last-writer-wins、evidence-weighted merge、await-confirmation、per-rule policy——统一为带隔离级别预条件的双时态算子族，并形式化三种 LLM 裁判特有的写时异常：replay inconsistency（同一矛盾重新裁判产生不同胜者）、belief-drift skew（隔离不足导致的信念漂移）、audit erasure（被取代事实的审计行丢失）。[^13]

跨系统测量显示：Mem0 v3 在写入 "Alice 的经理先为 Bob 后为 Carol" 时，遍历 Memory.history 找不到被取代 Bob 事实的审计条目（N3 audit erasure）；Graphiti 的 resolve_edge_contradictions LLM 调用无 decoder seed 固定，导致同一矛盾重新裁判可能产生不同胜者（N1 replay inconsistency）。[^13] TOKI 的审计行防御机制在 LoCoMo 自然工作负载切片上带来显著准确率提升，而消融 typed memory layer 后在 1,444 道可回答题目上准确率下降约 0.49。[^13]

**表 2 记忆演化机制对比**

| 机制 | 代表系统 | 核心操作 | 历史保留 | 计算成本 | 适用场景 |
|:---|:---|:---|:---|:---|:---|
| 记忆巩固 | Generative Agents, HeLa-Mem | Reflection synthesis / 知识蒸馏 | 保留原始观察 | 高（周期性 LLM 调用） | 长期个性化、知识抽象 |
| 记忆衰减 | MemoryBank, Engram, NornicDB | 指数衰减 + 召回强化 | 归档 / 软删除 | 低（公式计算） | 噪音过滤、存储预算管理 |
| 写时更新 | Mem0 AUDN | ADD/UPDATE/DELETE/NOOP | 不保留（直接覆盖） | 高（每次写入 LLM 判定） | 高可信度事实修正 |
| 读时过滤 | Zep/Graphiti | Temporal invalidation + 窗口查询 | 完整保留（审计轨迹） | 中（检索时过滤） | 高频交互、合规审计 |
| 矛盾消解 | TOKI | 双时态算子 + 隔离级别 | 审计行保留失败者 | 中（p50 ≈ 4ms） | 金融、医疗等强一致性场景 |

表 2 的分析表明，记忆演化机制的选择本质上是在一致性、成本与可审计性之间进行权衡。当前工业实践趋向于分层组合：巩固与衰减作为后台策展管道，更新与消解作为写路径策略，分别针对不同的数据可信度与查询频率进行调优。未来系统可能需要将 TOKI 的形式化保证与 Graphiti 的时态图结构结合，以同时满足高性能与强审计需求。

[^1]: Snodgrass & Ilsoo, Temporal Database Survey, VLDB 1998. https://www.vldb.org/conf/1998/p345.pdf
[^2]: Rasmussen et al., Zep: A Temporal Knowledge Graph Architecture for Agent Memory, arXiv:2501.13956, 2025.
[^3]: XTDB Authors, Bitemporality, XTDB Docs, 2018. https://v1-docs.xtdb.com/concepts/bitemporality/
[^4]: Tansel, Wu & Wang, Time Travel with the BiTemporal RDF Model, Mathematics (MDPI), 2025.
[^5]: Wu et al., LongMemEval: Benchmarking Chat Assistants on Long-Term Interactive Memory, arXiv:2410.10813, 2024.
[^6]: Su et al., Beyond Dialogue Time: Temporal Semantic Memory for Personalized LLM Agents, arXiv:2601.07468, 2026.
[^7]: Zep, What Is a Temporal Knowledge Graph?, getzep.com, 2026.
[^8]: Park et al., Generative Agents: Interactive Simulacra of Human Behavior, UIST 2023 / arXiv:2304.03442.
[^9]: Brodt et al., Sleep—A brain-state serving systems memory consolidation, Neuron 111(7), 2023. https://doi.org/10.1016/j.neuron.2023.03.005
[^10]: Zhong et al., MemoryBank: Enhancing Large Language Models with Long-Term Memory, arXiv:2305.10250, 2023.
[^11]: Roynard, The Missing Knowledge Layer in Cognitive Architectures for AI Agents, arXiv:2604.11364, 2026.
[^12]: Mem0, Memory Eviction and Forgetting in AI Agents, mem0.ai blog, 2026-05-22.
[^13]: TOKI: A Bitemporal Operator Algebra for Contradiction Resolution in LLM-Agent Persistent Memory, arXiv:2606.06240, 2026.
[^14]: Neo4j Blog, Graphiti: Knowledge graph memory for an agentic world, 2026-06-04.
[^15]: MemX, Agent Memory vs RAG: The Real Difference, 2026-06-04.
