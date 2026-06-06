## 2. 学术基础与理论框架

### 2.1 人类记忆模型的 AI 映射

#### 2.1.1 Atkinson-Shiffrin 三层模型（Sensory→STM→LTM）的工程化实现

认知科学中，Atkinson 与 Shiffrin 于 1968 年提出的模态模型（Modal Model）将人类记忆划分为感觉登记（Sensory Register）、短时存储（Short-Term Store, STM）与长时存储（Long-Term Store, LTM）三个层级，并通过注意、编码、复述与提取等控制过程实现信息流转 [^1][^2]。该模型在六十余年的实验心理学研究中积累了大量定量证据，近年来被 AI 研究者重新发现为记忆系统的工程蓝图：感觉登记对应输入过滤与预压缩层，短时存储对应大语言模型（Large Language Model, LLM）的上下文窗口或 KV 缓存，长时存储则对应向量数据库、知识图谱及外部持久化存储 [^1]。

2025 年提出的 LightMem 框架将这一理论映射转化为完整的工程流水线：Sensory Pre-compression 采用 LLMLingua-2 对原始输入进行语义压缩；STM Topic-aware Segmentation 在上下文窗口内执行主题感知切分；LTM Sleep-Time Consolidation 在离线阶段完成记忆巩固。实验表明，该架构在对话 Agent 场景中实现了 117 倍的 token 削减与 10.9% 的问答准确率提升，同时将 API 调用次数降低 159 倍 [^3]，验证了人类记忆层次理论对 AI 架构设计的可迁移性。

#### 2.1.2 Baddeley 工作记忆模型对 LLM Agent 上下文管理的启发与局限

Baddeley 与 Hitch 于 1974 年提出的工作记忆模型突破了单一 STM 概念，将其解构为中央执行器（Central Executive）、语音环路（Phonological Loop）、视觉空间画板（Visuospatial Sketchpad）以及后来的情景缓冲器（Episodic Buffer）四个功能组件 [^4]。该多组件结构为 LLM Agent 的上下文管理提供了直接灵感：中央执行器对应 Agent 的调度与决策模块，语音环路和视觉空间画板对应多模态输入的并行编码通道，情景缓冲器则对应跨交互的连续经验整合。

然而，将人类中心的概念直接翻译到人工系统存在固有局限。标准 LLM Agent 缺乏鲁棒的情景记忆与跨会话连续性，其上下文窗口本质上是一个无结构的文本缓冲区，而非 Baddeley 模型中具有主动维持与刷新机制的动态工作空间 [^4]。为此，研究者提出集中式工作记忆中枢（Centralized Working Memory Hub）加情景缓冲器访问（Episodic Buffer Access）的增强架构，通过显式的记忆读写操作弥补 LLM 在状态维持上的不足。

#### 2.1.3 Tulving 情景/语义/程序性记忆分类在 CoALA 框架中的形式化

Endel Tulving 于 1972 年提出情景记忆（Episodic Memory，对特定事件的记忆）与语义记忆（Semantic Memory，对一般事实的记忆）的二分法；Larry Squire 于 1987 年进一步补充程序性记忆（Procedural Memory，对技能和操作程序的记忆），形成人类长时记忆的三大支柱 [^5]。普林斯顿大学与 Google 研究团队于 2023 年提出的 CoALA（Cognitive Architectures for Language Agents）框架将这一分类直接形式化为 LLM Agent 的四大记忆类型：上下文记忆（In-Context/Working Memory，活跃信息）、情景记忆（Episodic Memory，经验记录）、语义记忆（Semantic Memory，事实知识）与程序性记忆（Procedural Memory，任务执行规则）[^5][^11]。CoALA 不仅定义了每种记忆的存储位置，还规定了对应的检索方式与更新策略，使认知科学的抽象分类首次在语言 Agent 中获得可执行的架构语义。

下表系统总结了人类记忆模型与 AI 工程架构的跨领域映射关系：

| 人类记忆层级 | 认知科学核心机制 | AI 工程对应组件 | 代表系统与实证 |
|:---|:---|:---|:---|
| 感觉登记（Sensory） | 毫秒级缓冲，选择性注意 | 输入过滤、语义预压缩 | LightMem LLMLingua-2 压缩 [^3] |
| 短时存储（STM） | 秒至分钟级维持，复述刷新 | 上下文窗口、KV Cache、工作记忆中枢 | MemGPT Core Memory [^9] |
| 长时存储（LTM） | 持久巩固，提取线索竞争 | 向量数据库、时序知识图谱、外部存储 | Mem0、Zep Graphiti [^1][^24] |
| 工作记忆（Working） | 中央执行器 + 多从属系统 | Agent 调度模块、多模态编码通道 | Baddeley-inspired Hub [^4] |
| 情景记忆（Episodic） | 事件编码，时间戳标记 | 完整交互记录、反思合成输入 | Generative Agents Memory Stream [^6] |
| 语义记忆（Semantic） | 概念网络，事实提取 | 嵌入向量、知识图谱节点 | CoALA Semantic Memory [^11] |
| 程序性记忆（Procedural） | 技能自动化，内隐学习 | 模型权重、Agent 代码、显式指令集 | CoALA Procedural Substrates [^5] |

该映射并非简单的术语借用，而是涉及控制过程与存储特性的深层结构对应。人类 LTM 的提取依赖线索竞争（cue competition），这与 AI 检索系统中的相关性-重要性-新近度三因子评分机制具有同构性 [^6]。值得注意的是，程序性记忆在两类系统中均呈现“理论薄弱但行为关键”的特征：人类认知中程序性记忆难以用语言显式报告，而 AI 系统中程序性记忆的工程化实现同样落后于情景与语义记忆 [^5][^16]。

### 2.2 奠基性学术论文谱系

#### 2.2.1 Generative Agents (Park et al., 2023, UIST)：记忆流+反思合成的奠基范式

Park 等人于 2023 年在 UIST 发表的 *Generative Agents: Interactive Simulacra of Human Behavior* 开创了基于 LLM 的社会模拟 Agent 架构 [^6]。其核心贡献在于提出“记忆流”（Memory Stream）与“反思”（Reflection）双层机制：记忆流以自然语言形式存储 Agent 的完整经验记录，反思则通过 LLM 将原始观察合成为高阶洞察。检索阶段采用相关性、重要性与新近度的加权评分，动态召回最适配当前情境的记忆子集。

该机制与人类记忆巩固中的系统巩固理论存在深层对应。Generative Agents 的反思合成过程在功能上等价于海马体-皮层对话：原始观察对应快速情景编码，反思摘要对应慢速语义巩固 [^7][^8]。后续研究进一步将这一对应形式化为三步流水线：语义相似性聚类检测 → LLM 合并摘要生成 → 原始片段归档替换 [^7]。

#### 2.2.2 MemGPT (Packer et al., 2023)：OS 虚拟内存分页隐喻的开创

Packer 等人于 2023 年提出的 MemGPT 将操作系统虚拟内存的分页（Paging）机制引入 LLM 上下文管理 [^9]。其核心隐喻将 LLM 的有限上下文窗口类比为计算机主存（RAM），将外部向量存储类比为硬盘，通过显式的 Agent 中断（Interrupt）实现信息在上下文与外部存储之间的换入（Page-in）与换出（Page-out）。MemGPT 定义了三层记忆：Core Memory（常驻上下文）、Recall Memory（可快速检索的近期记忆）与 Archival Memory（深度归档的长期记忆），分别对应人类记忆的感觉-工作-长期分层 [^9]。

该架构开创了“显式记忆管理”的先河：不同于被动依赖检索增强生成（Retrieval-Augmented Generation, RAG）的系统，MemGPT 赋予 Agent 主动管理自身记忆边界的权限。后续工作如 MemTier（2026）在此基础上将同步中断触发改进为异步守护进程驱动，并引入基于强化学习的自适应检索策略 [^10]。

#### 2.2.3 Cognitive Architectures for Language Agents (Sumers et al., 2024, TMLR)

Sumers 等人于 2024 年发表在 *Transactions on Machine Learning Research*（TMLR）的 CoALA 框架将认知科学与符号 AI 的洞察与当代 LLM 进行系统性整合 [^11]。CoALA 沿三个维度组织语言 Agent：信息存储（记忆系统）、动作空间（内部操作与外部执行的明确划分）以及决策过程（规划与执行的交互循环）。其记忆组件直接映射人类认知架构：工作记忆维持活跃信息，程序性记忆存储任务执行规则，语义记忆承载事实知识，情景记忆记录经验轨迹。

CoALA 的理论价值在于为语言 Agent 提供了通用认知架构，使不同研究群体的实现能够在统一的概念框架下进行比较与组合。Zhang 等人于 2024 年发表的综述进一步系统回顾了记忆模块的设计模式与评估方法，指出记忆是 Agent 环境交互与自我演进能力的关键组件，该综述已被引用超过 600 次，为领域提供了系统性知识基础 [^21]。

下表梳理了本领域四篇奠基性论文的核心贡献及其记忆类型映射：

| 论文 | 作者/年份/venue | 核心贡献 | 记忆类型映射 | 关键量化结果 |
|:---|:---|:---|:---|:---|
| Generative Agents | Park et al., 2023, UIST | Memory Stream + Reflection Synthesis | 情景记忆→语义记忆巩固 | 25 Agent 社会模拟，信息扩散率 32%→52% [^6] |
| MemGPT | Packer et al., 2023 | OS 虚拟内存分页隐喻 | 核心/召回/归档三层 | 无限上下文对话，零上下文截断损失 [^9] |
| CoALA | Sumers et al., 2024, TMLR | 模块化记忆系统与内外动作空间 | 工作/情景/语义/程序性 | 统一认知架构形式化 [^11] |
| Reflexion | Shinn et al., 2023, NeurIPS | 语言强化学习与情景记忆缓冲区 | 短期轨迹 + 长期反思 | HumanEval 91% pass@1 [^17] |

上述论文构成了当前 AI Agent 记忆系统的理论根基。Generative Agents 确立了“记录-巩固-检索”的闭环范式；MemGPT 证明了显式记忆管理的工程可行性；CoALA 提供了跨系统的概念整合框架；Reflexion 则展示了无需权重更新的语言强化学习路径。四者共同揭示了一个核心趋势：AI 记忆系统的设计正从 ad-hoc 的上下文拼接走向具有明确认知科学对应关系的模块化架构。

### 2.3 记忆分类学的统一框架

#### 2.3.1 Hu et al. (2025) 的三维分类：Forms×Functions×Dynamics

2025 年 12 月，Hu 等 47 位作者发表的综述 *Memory in the Age of AI Agents* 提出了统一的三维记忆分类学，将碎片化的 Agent 记忆研究整合为连贯的概念框架 [^13]。三个维度分别为：形态（Forms，记忆以何种物理形式存在）、功能（Functions，记忆服务于何种认知目的）与动态（Dynamics，记忆如何随时间形成、演化与提取）。

三个维度分别识别出 token-level、parametric 与 latent 三种形态[^13]，事实、经验与工作三种功能类型[^13][^14]，以及形成-演化-检索的动态全过程，其中“情景到语义”的毕业机制将重复情景记忆压缩为稳定事实记忆 [^8][^23]。

#### 2.3.2 Token-level / Parametric / Latent 三种记忆载体的特性对比

三种记忆形态在存储成本、更新灵活性与检索精度上呈现显著差异。Token-level 记忆以原始文本形式驻留于上下文窗口，其优势在于保留完整语义细节且无需额外计算即可访问，但受限于上下文长度瓶颈，且无法跨会话持久化 [^13][^15]。Parametric 记忆通过预训练、微调或人类反馈强化学习（Reinforcement Learning from Human Feedback, RLHF）固化于模型权重，具备最强的泛化能力与最快的访问速度，但任何知识更新都需要重新训练或参数高效微调，动态性最差 [^13]。Latent 记忆占据中间地带：嵌入向量与隐藏状态提供语义压缩与结构化索引，支持动态更新与近似检索，但其有效性高度依赖编码器质量与检索策略 [^15]。

这一区分与人类记忆研究中的容量-持久性权衡（capacity-durability tradeoff）高度一致：token-level 对应工作记忆的有限容量与高保真，parametric 对应程序性记忆的自动化与难变性，latent 对应长时记忆巩固过程中的压缩表征与模式提取 [^13][^15]。

#### 2.3.3 Factual / Experiential / Working Memory 的功能边界

在功能维度上，Hu et al. 的分类学明确了三种记忆类型的认知边界：事实记忆存储稳定知识，经验记忆记录交互事件，工作记忆承载当前任务状态 [^13][^14]。该功能分类揭示了生产环境中的关键缺口：程序性记忆——即“如何做事”的技能与规则存储——在 CoALA 框架中被识别为四种基本记忆类型之一，但在当前工业实现中理论支持最少、工具覆盖最弱 [^5][^16]。CodeMem（2025）等新兴工作正尝试通过将 LLM 重构为可执行工作流架构师来缓解这一缺口 [^16]。

[^1]: Mem0 Blog. "The Modal Model of Memory: What AI Agents Can Learn From Cognitive Science". 2026-04-05. https://mem0.ai/blog/the-modal-model-of-memory-what-ai-agents-can-learn-from-cognitive-science
[^2]: arXiv 2411.00489. "Human-inspired Perspectives: A Survey on AI Long-term Memory". 2024. https://arxiv.org/html/2411.00489v2
[^3]: LightMem. "Atkinson Shiffrin Model: Revolutionizing AI Memory Architecture". arXiv 2025. https://skywork.ai/slide/en/atkinson-shiffrin-model-ai-memory-2034912092062494720
[^4]: arXiv 2312.17259. "Empowering Working Memory for Large Language Model Agents". 2023-12. https://export.arxiv.org/ftp/arxiv/papers/2312/2312.17259.pdf
[^5]: Atlan. "Types of AI Agent Memory: Episodic, Semantic, Procedural". 2026-04-02. https://atlan.com/know/types-of-ai-agent-memory/
[^6]: Park et al. "Generative Agents: Interactive Simulacra of Human Behavior". UIST 2023. 2023-04-07. https://arxiv.org/abs/2304.03442
[^7]: TypeGraph.ai. "Designing Agent Memory That Forgets: Time-Decay Scoring and Memory Consolidation". 2026-04-05. https://typegraph.ai/blog/agent-memory-time-decay-consolidation
[^8]: ACM CHI 2024. "My agent understands me better": Integrating Dynamic Human-like Memory Recall and Consolidation. 2024-05-11. https://dl.acm.org/doi/10.1145/3613905.3650839
[^9]: Packer et al. "MemGPT: Towards LLMs as Operating Systems". 2023. https://arxiv.org/abs/2310.08560
[^10]: MemTier. arXiv 2605.03675. 2026.
[^11]: Sumers et al. "Cognitive Architectures for Language Agents". TMLR 2024. 2024. https://arxiv.org/abs/2309.02427
[^13]: Hu et al. "Memory in the Age of AI Agents: A Survey". 2025-12-15. https://arxiv.org/abs/2512.13564
[^14]: arunbaby.com. "Agent Memory Taxonomy in 2026". 2026-04-18. https://www.arunbaby.com/ai-agents/0097-agent-memory-taxonomy-2026/
[^15]: Agentic Brew. "AI Memory Systems for Agents". 2026-04-29. https://www.agenticbrew.ai/news/1851b23a-e0dc-40b7-a69b-bd93653c4f5c/ai-memory-systems-for-agents
[^16]: CodeMem. arXiv 2512.15813. 2025.
[^17]: Shinn et al. "Reflexion: Language Agents with Verbal Reinforcement Learning". NeurIPS 2023. 2023-03-20. https://arxiv.org/abs/2303.11366
[^19]: Physiology Reviews. "Sleep and Memory Consolidation". 2025. https://journals.physiology.org/doi/pdf/10.1152/physrev.00007.2025
[^21]: Zhang et al. "A Survey on the Memory Mechanism of Large Language Model based Agents". 2024-04-21. https://arxiv.org/abs/2404.13501
[^23]: TypeGraph.ai. "Memory Consolidation". 2026-04-05. https://typegraph.ai/blog/agent-memory-time-decay-consolidation
[^24]: Mem0 Blog. "Episodic Memory for AI Agents". 2026-05-22. https://mem0.ai/blog/episodic-memory-for-ai-agents
