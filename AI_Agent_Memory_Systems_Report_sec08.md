## 8. 评估基准与性能对比

记忆系统的快速迭代催生了一批专门化的评估基准。从对话式问答到多平台状态追踪，从操作级幻觉检测到认知科学驱动的对话评估，这些基准共同构成了衡量记忆系统能力的坐标系。与此同时，工业级系统在公开排行榜与受控实验条件下呈现出显著差异，暴露出评估条件敏感性这一核心问题。

### 8.1 基准测试体系

#### 8.1.1 LongMemEval / LongMemEval-V2：企业级复杂时间推理的权威基准

LongMemEval（ICLR 2025）是当前跨系统对比最广泛采用的权威基准，含 500 道人工设计问题，覆盖信息提取、多会话推理、时间推理、知识更新与弃权五种核心能力，测试对话历史平均 115K tokens，可扩展至 1.5M tokens[^1]。主流长上下文模型在该基准上性能较简单设置下降 30%–60%，商业系统在最简条件下准确率仅 30%–70%[^1]。LongMemEval-V2（2026）将评估扩展到多模态 Web Agent 轨迹，从 WebArena 等采集 941 条轨迹，平均 28.1 个状态，含 451 道人工标注问题，覆盖静态/动态状态追踪、工作流知识等五种能力；前沿模型无轨迹证据时准确率最高仅 14.1%[^2]。

#### 8.1.2 DMR (Deep Memory Retrieval)：MemGPT-era 的多会话对话检索基准

DMR 由 MemGPT 团队建立，含 500 段多会话对话（每段 5 会话、每会话最多 12 条消息），用于评估深度记忆检索[^3]。MemGPT 在 GPT-4 Turbo 上达 93.4%，显著优于递归摘要基线 35.3%[^3]。但 Zep 团队指出 DMR 规模过小（每段仅 60 条消息），现代 LLM 可轻松将其完整放入上下文，已不足以区分真实记忆系统[^3]。尽管如此，DMR 仍是早期对比的参考锚点。

#### 8.1.3 MemTrack：多平台动态 Agent 环境的状态追踪基准

MEMTRACK（Deshpande 等人，2025）是首个面向多平台动态 Agent 环境的长期记忆与状态追踪基准，模拟 Slack、Linear、Git 等企业工作流，引入跨平台异步事件、冲突信息与代码库理解任务，定义 Correctness、Efficiency、Redundancy 三大指标[^4]。实验显示，即使 GPT-5 也仅达 60% Correctness，记忆组件增益有限，工具调用效率存在系统性不足[^4]。

#### 8.1.4 HaluMem：记忆系统幻觉的首个操作级评估基准

HaluMem（Chen 等人，2025）是首个操作级（operation-level）记忆幻觉评估基准，将记忆系统性能分解为记忆提取、记忆更新与记忆问答三个阶段[^5]。该基准包含约 15K 记忆点与 3.5K 多类型问题，平均对话长度 1.5K–2.6K 轮、上下文超 1M tokens[^5]。核心发现是：现有记忆系统在提取和更新阶段产生并累积幻觉，随后传播到问答阶段；所有被测系统的 Memory Integrity 召回率低于 60%，Memory Accuracy 低于 62%，正确更新率低于 50%，且遗漏率超过 50%[^5]。

#### 8.1.5 MADial-Bench：基于认知科学的记忆增强对话评估

MADial-Bench（He 等人，2025，NAACL）是首个基于认知科学的记忆增强对话评估基准，覆盖被动/主动回忆，引入记忆注入、情绪支持、亲密度等人性化维度[^6]。实验显示，最优嵌入模型纯向量检索 Recall@1 仍不足 60%，揭示传统检索指标在真实对话场景中的局限性[^6]。

#### 8.1.6 Forgetting Curve：LLM 固有长上下文记忆能力的测量方法

"Needle in a Haystack" 对提示词高度敏感，无法全面反映真实记忆能力[^7]。Forgetting Curve（Liu 等人，EMNLP 2024）通过 copy accuracy 与 LM accuracy 曲线差值，将记忆能力分为精细记忆、粗粒度记忆与遗忘三个阶段，对任意模型尺寸及实验设置均具鲁棒性[^7]。该研究还发现困惑度与长程记忆能力无直接关联，挑战了以 PPL 评估长上下文能力的传统做法[^7]。

**表 1 六大记忆评估基准对比**

| 基准 | 发表年份 | 评估规模 | 核心能力覆盖 | 关键发现 |
|------|---------|---------|-------------|---------|
| LongMemEval | 2025 (ICLR) | 500 题，115K tokens | 信息提取、多会话推理、时间推理、知识更新、弃权 | 商业系统在最简条件下仅 30%–70% 准确率[^1] |
| LongMemEval-V2 | 2026 | 451 题，500 条轨迹 | 静态/动态状态、工作流、环境陷阱、前提感知 | 前沿模型无轨迹证据时准确率 <15%[^2] |
| DMR | MemGPT-era | 500 段对话，60 消息/段 | 多会话深度检索 | MemGPT 93.4%，但规模已不足以区分现代系统[^3] |
| MemTrack | 2025 | 210 实例，跨平台 | 状态追踪、冲突解决、跨平台依赖 | GPT-5 仅 60% Correctness[^4] |
| HaluMem | 2025 | 15K 记忆点，3.5K 问题 | 记忆提取、更新、问答三阶段幻觉 | 所有系统 Memory Integrity 召回率 <60%[^5] |
| MADial-Bench | 2025 (NAACL) | 160 对话 | 被动/主动回忆、情绪支持、亲密度 | 最优嵌入模型 Recall@1 <60%[^6] |

上述基准从不同侧面刻画了记忆系统的评估空间。LongMemEval 系列强调长程对话中的复杂推理，DMR 聚焦多会话检索精度，MemTrack 将评估推向企业级动态环境，HaluMem 首次将幻觉检测操作化，MADial-Bench 引入认知科学视角，而 Forgetting Curve 则提供了不依赖提示词的模型固有记忆能力测量方法。值得注意的是，这些基准之间尚缺乏统一的评估协议——各系统在不同测试集、不同检索预算与不同 LLM backbone 下的得分难以直接比较，这为后续跨系统对比埋下了方法论隐患。

### 8.2 工业系统性能数据

#### 8.2.1 LongMemEval leaderboard 格局

当前 LongMemEval 公开 leaderboard 呈现多极竞争格局。MemPalace 以 96.6% Recall@5 位居首位，凭借 verbatim 存储与零 LLM 写入成本获得独特成本优势[^8]。Mem0 2026 年 4 月发布的 token-efficient 算法通过单次分层提取与多信号检索，将 LongMemEval 从旧算法约 49% 跃升至 93.4%，LoCoMo 提升至 92.5%，挑战了"verbatim 存储必然优于提取式存储"的叙事[^9]。Hindsight（Vectorize，2025）在 LongMemEval 达 91.4%（Gemini-3 Pro），LoCoMo 达 89.61%，其 Retain→Recall→Reflect 流水线以多轮 LLM 调用换取检索质量[^10]。Zep 在原始论文条件（GPT-4o-mini/GPT-4o）下达到 63.8%–71.2%，其双时间轴知识图谱在时间推理上显著优于纯向量检索；后续对比中出现的更高分数可能反映 Graphiti 更新版本或不同 LLM backbone[^11]。

#### 8.2.2 延迟对比

延迟是记忆系统生产部署的关键约束。Mem0 在 LoCoMo 基准上实现 p50 搜索延迟 0.148 秒、p95 0.200 秒，为所有对比方法中最低，这得益于其选择性记忆检索机制与基础设施优化[^12]。Zep 报告 P95 检索延迟低于 200 毫秒，总延迟 p50 约 1.292 秒[^11][^12]。相比之下，LangMem 的 p50 搜索延迟高达 17.99 秒、p95 达 59.82 秒，使其在交互式应用中几乎不可行[^12]。全上下文基线虽无搜索开销，但将 26K tokens 完整对话直接输入 LLM 导致总延迟 p50 达 9.870 秒，同样无法满足实时交互需求[^12]。

#### 8.2.3 Token 效率

Token 消耗直接影响推理成本。Mem0 的 token-efficient 算法平均每次检索低于 7,000 tokens，较全上下文方法常规消耗的 25,000+ tokens 实现 90% 以上的 Token 成本节省[^9]。Zep 在 LongMemEval 上将平均上下文 Token 从 115K 压缩至 1.6K（压缩后上下文），实现 98% 的上下文 Token 减少；其检索预算约为 4.4K tokens，二者分别对应上下文压缩与检索开销[^11]。然而，Zep 的知识图谱构建在部分评估中消耗超过 60 万 tokens，其节点级全摘要缓存策略在复杂场景下可能产生显著的隐性成本[^13]。

#### 8.2.4 幻觉率

HaluMem 的评估结果对工业系统具有警示意义。所有被测系统（Mem0、Mem0-Graph、Memobase、Supermemory、Zep）在记忆提取阶段的 Memory Integrity 召回率均低于 60%，Memory Accuracy 低于 62%，正确更新率低于 50%，且遗漏率超过 50%[^5]。这意味着记忆系统不仅未能完整提取用户事实，还在更新阶段引入并累积错误，最终将这些幻觉传播至下游问答阶段。该发现表明，当前工业系统在追求准确率的同时，普遍忽视了记忆操作链路的可靠性。

**表 2 主流工业记忆系统性能对比**

| 系统 | LongMemEval | LoCoMo | 搜索延迟 (p50/p95) | Token/查询 | 架构特点 |
|------|------------|--------|-------------------|-----------|---------|
| Mem0 v3 (2026-04) | 93.4% [^9] | 92.5% [^9] | 0.148s / 0.200s [^12] | <7K [^9] | 单次分层提取 + 多信号检索 |
| Zep (Graphiti) | 63.8%–71.2% [^11] | 80.32% [^14] | <200ms P95 [^11] | ~1.6K [^11] | 双时态知识图谱 |
| Hindsight | 91.4% [^10] | 89.61% [^10] | 未公开 | ~8.2K [^15] | 四路并行检索 + 反思 |
| LangMem | 未报告 | 0.513 J-score [^12] | 17.99s / 59.82s [^12] | 未公开 | 程序性记忆原生支持 |
| MemPalace | 96.6% R@5 [^8] | 未报告 | 未公开 | ~170–900 [^8] | Verbatim 存储 + 空间隐喻 |

表 2 的数据揭示了准确率与工程成本之间的深层张力。Mem0 与 Hindsight 在准确率上处于第一梯队，但实现路径截然不同：Mem0 以低延迟、低 Token 消耗取胜，Hindsight 以四路并行检索和结构化反思换取精度，代价是更高的检索预算与多轮 LLM 调用。Zep 的准确率略低，但在时间推理与企业级治理方面具有结构性优势。LangMem 的高延迟使其目前仅适合后台批处理场景。需要强调的是，这些数字并非在统一条件下测得——公开榜与受控实验的差异可能高达 60 个百分点，直接比较需谨慎。

### 8.3 多维评估框架

#### 8.3.1 从单一准确率到"准确率+幻觉率+延迟+成本+安全"的五维空间

传统信息检索指标（Precision@k、nDCG）仅能判断"是否检索到了正确文档"，无法评估"Agent 是否正确使用了检索到的记忆"，更无法衡量时效性、矛盾处理、遗忘质量与治理合规等维度[^16]。2024–2025 年的评估主要关注召回准确率；2025–2026 年新增了幻觉评估（HaluMem）、状态追踪（MemTrack）、安全基准（MemFail）、延迟分位（p50/p95/p99）与 Token 效率[^16]。没有任何系统在所有维度上同时最优——Mem0 延迟最低但旧算法准确率曾垫底；Hindsight 准确率最高但生态规模较小；Zep 时间推理最强但成本结构复杂[^17]。

**表 3 记忆系统多维评估指标框架**

| 维度 | 核心指标 | 代表基准/方法 | 工业意义 |
|------|---------|-------------|---------|
| 准确率 | 端到端 QA 准确率、Recall@k | LongMemEval、LoCoMo、DMR | 直接决定任务完成质量 |
| 幻觉率 | Memory Integrity 召回率、Memory Accuracy、正确更新率 | HaluMem | 影响用户信任与决策安全 |
| 延迟 | 搜索延迟 p50/p95、总响应延迟 | Mem0 论文、Zep 工程报告 | 决定交互体验与实时可用性 |
| 成本 | Token/查询、API 调用次数、存储开销 | Mem0 Blog、Zep 论文 | 决定规模化部署经济性 |
| 安全 | 记忆污染抵抗、访问控制、审计溯源 | MemFail、MemGuard | 决定企业级合规与风险边界 |

表 3 所示的五维框架将记忆系统评估从单一优化问题转化为多目标权衡问题。不同场景对各维度的权重差异显著：个人助手重视延迟与 Token 效率；企业知识管理重视准确率与安全；游戏 NPC 重视涌现行为与一致性。生产环境监控指标体系应进一步分层：输出质量（准确性、忠实度、完整性）、行为质量（任务完成率、工具选择准确率）、用户体验（解决率、首 token 延迟、会话放弃率）与业务指标（转人工率、用户留存率）[^18]。

#### 8.3.2 评估条件敏感性：公开榜与受控实验的鸿沟

记忆系统评估的核心挑战是条件敏感性。公开 leaderboard 显示 MemPalace 96.6%、Mem0 93.4%，但这些系统执行全上下文整合与调优提示；受控实验固定检索预算（如 k=5）时，同一 Mem0 从 93.4% 骤降至 31.8%——下降 61.6 个百分点[^19]。这表明评估设置（检索预算、整合策略、LLM backbone）而非方法本身驱动了分数差距[^19]。Zhou 与 Han（2025）进一步发现简单检索基线（EMem、EMem-G）在 LoCoMo 和 LongMemEval 上 outperform 复杂记忆结构，说明这些基准更测试检索能力而非记忆架构复杂度[^20]。

#### 8.3.3 被动回忆 vs 主动应用的鸿沟

2026 年记忆系统评估正从被动回忆（passive recall）向主动决策相关记忆（active, decision-relevant memory use）转变。MemoryArena 发现，那些在 LoCoMo 上接近完美的模型在完整 Agentic 任务中暴跌至 40–60%，暴露了被动回忆与主动应用之间的深层鸿沟[^21]。MemoryArena 将记忆评估嵌入完整 Agentic 任务（Web 导航、偏好约束规划、渐进式信息搜索、序列形式推理），后续子任务依赖前面学到的内容，要求 Agent 将经验蒸馏为记忆并指导未来行动[^21]。

该鸿沟的存在意味着：一个在对话问答中表现优异的记忆系统，未必能在需要跨会话因果依赖的复杂任务中有效工作。现有基准多为静态 QA，无法反映多会话、多天的真实交互中记忆的增量积累与动态应用[^19]。未来评估基础设施需向持续评估流水线演进，模拟增量式记忆积累、冲突解决与技能迁移，方能更真实地度量记忆系统在实际 Agent 工作流中的价值。

[^1]: Wu et al., LongMemEval: Benchmarking Chat Assistants on Long-Term Interactive Memory. ICLR 2025. https://arxiv.org/pdf/2410.10813v2
[^2]: Wu et al., LongMemEval-V2: Evaluating Long-Term Agent Memory Toward Experienced Colleagues. 2026. https://arxiv.org/html/2605.12493v1
[^3]: Rasmussen et al., ZEP: A Temporal Knowledge Graph Architecture for Agent Memory. 2025. https://arxiv.org/pdf/2501.13956
[^4]: Deshpande et al., MEMTRACK: Evaluating Long-Term Memory and State Tracking in Multi-Platform Dynamic Agent Environments. 2025. https://arxiv.org/abs/2510.01353
[^5]: Chen et al., HaluMem: Evaluating Hallucinations in Memory Systems of Agents. 2025. https://arxiv.org/abs/2511.03506
[^6]: He et al., MADial-Bench: Towards Real-world Evaluation of Memory-Augmented Dialogue Generation. NAACL 2025. https://aclanthology.org/2025.naacl-long.499/
[^7]: Liu et al., Forgetting Curve: A Reliable Method for Evaluating Memorization Capability for Long-context Models. EMNLP 2024. https://aclanthology.org/2024.emnlp-main.269.pdf
[^8]: Dey & Viradecha, A Critical Analysis of the MemPalace Architecture. 2026. https://arxiv.org/html/2604.21284v1
[^9]: Mem0, The Token-Efficient Memory Algorithm Now Has Temporal Reasoning. Mem0 Blog, 2026-05. https://mem0.ai/blog/the-token-efficient-memory-algorithm-now-has-temporal-reasoning
[^10]: Vectorize, Hindsight: The Open-Source Memory System That Lets AI Agents Actually Learn. 2026. https://emelia.io/hub/hindsight-ai-agent-memory
[^11]: Rasmussen et al., ZEP: A Temporal Knowledge Graph Architecture for Agent Memory. 2025. https://arxiv.org/pdf/2501.13956
[^12]: Chhikara et al., Mem0: Building Production-Ready AI Agents with Scalable Long-Term Memory. 2025. https://arxiv.org/abs/2504.19413
[^13]: AgentMarketCap, Agent Long-Term Memory in 2026: Letta, Mem0, Zep, and LangMem Compared. 2026-04. https://agentmarketcap.ai/blog/2026/04/08/agent-long-term-memory-architecture-letta-memgpt-langmem-zep
[^14]: The AI Agent Index, Zep Review (2026). 2026-05. https://theaiagentindex.com/agents/zep
[^15]: Get-Hermes, Memory Providers for AI Agents — 2026 Guide. 2026. https://get-hermes.ai/memory/
[^16]: Mechanisms, Evaluation, and Emerging Frontiers. 2026. https://arxiv.org/html/2603.07670v1
[^17]: Agentic Memory Systems Trade-off Analysis. 2026. https://www.arxiv.org/pdf/2602.13594
[^18]: 2025-2026 AI Agent 开发岗面试真题大全. 2026. https://blog.csdn.net/weixin_43726381/article/details/160897821
[^19]: ZenBrain: A Neuroscience-Inspired 7-Layer Memory Architecture. 2026. https://arxiv.org/html/2604.23878v2
[^20]: Benchmarking Long-term Memory Frameworks. 2026. https://arxiv.org/pdf/2602.11243
[^21]: He et al., MemoryArena: Benchmarking Agent Memory in Interdependent Multi-Session Agentic Tasks. 2026. https://arxiv.org/abs/2602.16313
