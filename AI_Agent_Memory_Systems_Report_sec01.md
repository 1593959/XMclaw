## 1. 执行摘要

### 1.1 研究背景与目标

#### 1.1.1 记忆系统从功能插件进化为独立基础设施层的行业趋势

AI Agent 记忆系统正从早期的上下文拼接插件演进为独立基础设施层。Atkinson-Shiffrin 三层模型经 LightMem 等框架工程化验证，已成为记忆架构设计的公认蓝图[^1]；Park 等人于 2023 年提出的 Generative Agents 则确立了“记忆流+反思合成”的奠基范式[^6]。当前工业界已形成 Working / Short-term / Long-term / Procedural 四层记忆模型的工程共识。CoALA 等认知架构框架进一步将人类记忆分类形式化为可执行的模块语义，推动记忆系统从 ad-hoc 的 RAG 拼接走向独立基础设施层。

#### 1.1.2 本报告覆盖范围：学术理论、架构实现、应用场景、评估基准、安全隐私

本报告系统覆盖学术理论映射、分层架构设计、存储与检索技术、时间推理机制、写入质量控制、记忆策展生命周期、评估基准体系、垂直应用场景、安全隐私风险及工业系统全面对比，旨在为记忆系统的选型、设计与安全部署提供循证依据。

### 1.2 核心发现概述

#### 1.2.1 混合检索（Vector+Graph+Keyword）已成为生产系统的 table stakes

纯向量检索的结构性局限已被充分暴露：MADial-Bench 上最优嵌入模型的 Recall@1 不足 60%，且存在语义漂移、时间推理缺失与 CJK 关键词召回弱等系统性缺陷[^madial][^cjk-bm25-fail]。生产系统已统一转向向量语义+BM25关键词+图遍历+时间窗口的多路召回架构，级联检索管线在精度与延迟之间取得工程平衡。

#### 1.2.2 时间推理是下一代记忆系统的分水岭，双时态建模领先

时间推理能力是区分下一代记忆系统的关键分水岭：Zep/Graphiti 的双时态四时间戳在 LongMemEval 时间子任务上显著领先纯向量架构[^zep-paper]，TSM 将 Temporal 准确率从 36.5% 提升至 69.9%[^tsm]，而 Mem0 转向 ADD-only 写时策略以换取延迟优势[^mem0-blog]，反映高频与审计场景的架构分野。

#### 1.2.3 记忆污染攻击揭示安全边界被严重低估

持久记忆在赋予 Agent 跨会话连续性的同时，也创造了长期攻击面：Sleeper Memory Poisoning 在 GPT-5.5 上实现 99.8% 污染写入率[^sleeper]，混合 RAG 的 Retrieval Pivot Risk 泄露放大因子达 160–194 倍[^rpr]，而 HaluMem 显示所有被测系统 Memory Integrity 召回率低于 60%[^halumem]。

#### 1.2.4 评估从单一准确率扩展到幻觉率+延迟+成本+安全多维空间

记忆系统评估已从单一准确率扩展至“准确率+幻觉率+延迟+成本+安全”五维空间：LongMemEval 上 Mem0 v3 达 93.4%、Hindsight 达 91.4%[^mem0-blog][^hindsight-paper]，但无系统在五维上同时最优[^agentmarketcap]，商业化方案成本结构差异达一个数量级[^mem0-pricing][^zep-pricing][^langmem-vectorize][^hindsight-paper]。

[^1]: Mem0 Blog. "The Modal Model of Memory: What AI Agents Can Learn From Cognitive Science". 2026-04-05. https://mem0.ai/blog/the-modal-model-of-memory-what-ai-agents-can-learn-from-cognitive-science
[^6]: Park et al. "Generative Agents: Interactive Simulacra of Human Behavior". UIST 2023. 2023-04-07. https://arxiv.org/abs/2304.03442
[^20]: GitHub. "mem0ai/mem0". 2026-05-19. https://github.com/mem0ai/mem0
[^22]: Packer et al. "MemGPT: Towards LLMs as Operating Systems". 2023. https://arxiv.org/abs/2310.08560
[^24]: Atlan. "Zep vs Mem0: Benchmarks, Pricing, and When to Use Each." 2026-04-08. https://atlan.com/know/zep-vs-mem0/
[^26]: DigitalOcean. "LangMem SDK for Agent Long-Term Memory." 2026-02-19. https://www.digitalocean.com/community/tutorials/langmem-sdk-agent-long-term-memory
[^madial]: He et al. "MADial-Bench: Towards Real-world Evaluation of Memory-Augmented Dialogue Generation". NAACL 2025. https://aclanthology.org/2025.naacl-long.499/
[^cjk-bm25-fail]: vectorize-io/hindsight issues #1077. GitHub. 2026-04-15. https://github.com/vectorize-io/hindsight/issues/1077
[^rrf]: Cormack, Clarke, Büttcher, "Reciprocal Rank Fusion outperforms Condorcet and individual Rank Learning Methods". SIGIR 2009. https://bigdataboutique.com/blog/reciprocal-rank-fusion-how-it-works-and-when-to-use-it. 2026-05-18
[^zep-paper]: Rasmussen et al. "Zep: A Temporal Knowledge Graph Architecture for Agent Memory". 2025. https://arxiv.org/pdf/2501.13956v1
[^tsm]: Su et al. "Beyond Dialogue Time: Temporal Semantic Memory for Personalized LLM Agents". arXiv 2601.07468, 2026.
[^mem0-blog]: Mem0 Blog. "AI Memory Benchmarks in 2026". 2026-05-11. https://mem0.ai/blog/ai-memory-benchmarks-in-2026
[^sleeper]: Pulipaka et al. "Hidden in Memory: Sleeper Memory Poisoning in LLM Agents". 2026-05-14. https://arxiv.org/abs/2605.15338
[^rpr]: "Retrieval Pivot Attacks in Hybrid RAG: Measuring and Mitigating Amplified Leakage from Vector Seeds to Graph Expansion". 2026-05-01. https://arxiv.org/html/2602.08668v1
[^provenance]: "From Agent Traces to Trust: Evidence Tracing and Execution Provenance in LLM Agents". 2026-05-25. https://arxiv.org/html/2606.04990v1
[^halumem]: Chen et al. "HaluMem: Evaluating Hallucinations in Memory Systems of Agents". 2025. https://arxiv.org/abs/2511.03506
[^hindsight-paper]: "Hindsight: Temporal Entity-aware Memory Processing & Retrieval". 2025-12. https://arxiv.org/html/2512.12818v1
[^agentmarketcap]: Agent Market Cap. "Agent Memory Vendor Landscape 2026". 2026-04-10. https://agentmarketcap.ai/blog/2026/04/10/agent-memory-vendor-landscape-2026-letta-zep-mem0-langmem
[^mem0-pricing]: Mem0 Pricing. 2026. https://mem0.ai/pricing
[^zep-pricing]: Zep Pricing. 2024-11-14. https://www.getzep.com/
[^langmem-vectorize]: Vectorize.io. "Best AI Agent Memory Systems". 2026-03-14. https://vectorize.io/articles/best-ai-agent-memory-systems
[^mem0-paper]: Chhikara et al. "Mem0: Building Production-Ready AI Agents with Scalable Long-Term Memory". 2025-04-28. https://arxiv.org/html/2504.19413v1
[^smallville]: Park et al. "Generative Agents: Interactive Simulacra of Human Behavior". UIST 2023. 2023-04. https://abhinavchinta.com/files/generative_agents_talk.pdf
[^supportgenius]: Skywork AI. "Empowering AI Agents with Mem0 and OpenClaw". 2026-03-26. https://skywork.ai/slide/en/empowering-ai-agents-mem0-openclaw-2037137461974220800
