## 7. 记忆策展与生命周期管理

记忆系统的长期可用性不仅取决于写入时的提取质量，更取决于持续运行的策展（curation）机制。去重、矛盾消解、语义结晶与遗忘策略共同构成记忆生命周期管理的四大支柱。2026 年的工业共识表明，hybrid retrieval、contradiction handling 与 consolidation 已成为记忆基础设施的 table stakes，各系统的差异主要体现在实现路径与成本结构上[^aegis]。

### 7.1 去重与矛盾检测

#### 7.1.1 双层去重架构：向量聚类 + LLM 语义裁决

生产级记忆系统普遍采用双层漏斗架构完成去重。第一层以向量相似度快速筛选候选，cosine 阈值通常设定在 0.85–0.92 区间；第二层对落入模糊区的候选对调用 LLM 进行语义裁决，防止同义改写（paraphrase）漏网[^dedup-guide]。

该架构的算法逻辑在于：向量相似度对表面形式差异敏感，但对逻辑否定和微妙技术差异的区分能力不足。当 cosine 相似度处于 0.70–0.92 的模糊区时，系统必须引入推理型模型作为裁决者；而高于 0.92 的候选对可直接合并，低于 0.70 的则安全保留[^arxiv-deep-knowledge]。Vitamem 在此基础上引入双阈值体系：deduplicationThreshold 设为 0.92，supersedeThreshold 设为 0.75，将“精确重复—更新替代—全新事实”三类决策通过单一相似度度量区分开来[^vitamem]。

#### 7.1.2 增量去重 vs 全量扫描

去重时机的选择直接影响系统延迟与存储一致性。Codexfi 在每次 assistant turn 后的写入路径上执行实时 cosine 去重，一般类型阈值 0.12、结构类型 0.25，重复时触发 UPDATE 而非 INSERT，实现增量级去重[^codexfi]。与之相对，Mnemosyne 的 L2 管道在 50 ms 内完成 12 步算法级去重与冲突检测，全程零 LLM 调用，证明去重可以在 ingestion 阶段以确定性方式完成[^mnemosyne]。TeleMem 则采用 LLM-based semantic clustering，在 ZH-4O 中文长对话基准上达到 86.33% 的去重准确率，较 Mem0 的 70.20% 提升 19%[^telemem]。

| 系统 | 去重模式 | 阈值/机制 | 延迟特征 | LLM 依赖 |
|------|---------|----------|----------|----------|
| Codexfi | 增量写入时去重 | cosine 0.12（general）/ 0.25（structural） | 实时（per-turn） | 无 |
| Mnemosyne L2 | 算法级 12 步管道 | 确定性规则 + 实体匹配 | ≤ 50 ms | 零调用 |
| Vitamem | 双层阈值裁决 | dedup 0.92 / supersede 0.75 | 实时 | 仅模糊区 |
| TeleMem | LLM 语义聚类 | 聚类后 LLM 合并 | 批处理 | 全链路 |
| Mem0/XMclaw | 向量相似度合并 | cosine ≥ 0.86 | 实时 | 可选 |

上表揭示了去重策略在延迟、精度与成本之间的结构性权衡。Codexfi 与 Mnemosyne 的增量/算法级方案适合高频写入场景，将去重成本摊薄到每次交互中；TeleMem 的 LLM 全链路方案精度最高但成本显著，更适合批处理后台任务。工业部署的普遍做法是将实时轻量去重（向量层）与后台深度去重（LLM 层）结合，形成分层互补。

#### 7.1.3 矛盾检测标准：向量预筛选 + kind 过滤 + LLM 判断

矛盾检测的工业标准流程包含三级过滤：首先通过向量距离预筛选潜在冲突候选，其次以 kind 过滤仅对 correction/claim 类记忆触发深度检测，最后由 LLM 判断矛盾对并执行消解。Mem0 内部采用 AUDN（Add/Update/Delete/Noop）循环解决冲突，在写入时即完成决策，而非留到后台策展阶段[^mem0-eviction]。Zep/Graphiti 则采用 temporal invalidation 策略，通过双时间轴（valid_at / invalid_at）标记记忆失效时间，保留完整历史并支持时态查询，如回答“去年十月客户的地址是什么”[^memx-rag]。qmemory 的 reflect service 自动检测冲突事实并停用过时版本，通过 prev_version 边实现软删除与版本链追溯[^qmemory]。

#### 7.1.4 阈值差异问题：从 0.12 到 0.92 的行业分歧

去重阈值的选择在工业界存在数量级差异。Codexfi 的 0.12 阈值面向代码优化嵌入（Voyage voyage-code-3，1024 维），其低阈值源于高维嵌入空间中相似度的自然分布；Vitamem 的 0.92 阈值则面向通用事实记忆，追求高置信度的精确匹配[^codexfi][^vitamem]。

### 7.2 记忆结晶与压缩

#### 7.2.1 语义结晶：从多条碎片提炼规范表述的 LLM 驱动合成

记忆结晶（crystallization / consolidation）指从多条碎片化记录中提炼规范表述的过程。Hindsight 的四杠杆框架（importance, merge, decay, eviction）将 merge 视为结晶的核心环节，通过 LLM 驱动的语义合成将相关事实统一为单一 canonical record[^hindsight]。Mnemosyne L5 的 4-phase active consolidation 进一步系统化：contradiction detection → near-duplicate merge → popular promotion → stale demotion，实现从工作记忆到长期记忆的自动晋升与降级[^mnemosyne]。

#### 7.2.2 对话压缩：递归摘要、episode-based 压缩、Agent-Memory-Compressor 三策略

对话历史压缩的主流策略可分为三类。递归摘要（recursively summarizing）是处理超长对话的标准方法：LLM 先对短对话生成摘要，随后将旧摘要与新对话结合持续更新，最终形成跨会话的 global summary[^arxiv-recursive-summ]。Episode-based 压缩将连续的用户/助手轮次分组为 episode，每个 episode 压缩为 compact ContextItem，在 token budget 使用超过 80% 时触发，目标实现 2–3 倍有效历史保留率[^contextweaver]。Agent-Memory-Compressor 采用多信号重要性评分（Recency × 0.4 + TypeWeight × 0.4 + KeywordBoost × 0.2）驱动三种压缩策略：summarize（LLM 三句摘要）、extract_facts（事实 bullet list）、archive（占位符保留原始内容于 compression_history），在 turn interval 或 token threshold 超限时自动启动[^agent-compressor]。

#### 7.2.3 记忆形成 vs 摘要：Mem0 对两者边界的工程处理

Mem0 明确区分 summarization 与 memory formation：前者是有损压缩，试图以压缩形式保留全部信息；后者是选择性保留，仅将关键事实持久化。通过实时检测关键事实并写入情景记忆（episodic memory），Mem0 可减少 80–90% 的 token 成本，同时避免全量摘要导致的信息损失[^mem0-summ-guide]。这一边界处理对长运行 Agent 至关重要——工作记忆（当前会话）负责即时上下文，情景记忆（跨会话事实）负责长期知识，两者分离使系统在不牺牲召回的前提下控制上下文膨胀。

### 7.3 遗忘策略与调度机制

#### 7.3.1 四种遗忘模式：物理删除、逻辑失效、动态降权、搜索时重排序

生产系统对“遗忘”的实现可分为四种模式。TTL（time-to-live）自动清理适用于会话级短期存储与合规敏感数据（如 PII），SUPEROPTIX AI 的 FileBackend/SQLiteBackend/RedisBackend 均内置 TTL 与 LRU 自动清理[^superoptix]。逻辑失效（invalid_at）以 Zep/Graphiti 的双时态模型为代表，通过标记失效时间保留历史，支持审计与时态推理[^memx-rag]。动态降权（confidence floor）以 memx-memory 的三层衰减地板为典型：Core 0.9（稳定身份）、Working 0.7（活跃上下文）、Peripheral 0.5（老化/低优先级），低价值记忆降权但不删除[^memx-memory]。搜索时重排序则通过检索阶段的 recency-weighted scoring 动态压低旧记忆排名，Mem0 采用 1.5 倍/0.3 倍的多信号排序实现类似效果[^mem0-eviction]。

| 遗忘模式 | 代表系统 | 机制 | 适用场景 | 数据可恢复性 |
|----------|---------|------|----------|-------------|
| 物理删除（TTL） | SUPEROPTIX AI, Mem0 | 到期硬删除 + LRU 淘汰 | 会话存储、PII 合规 | 不可恢复 |
| 逻辑失效（invalid_at） | Zep/Graphiti | 双时态标记，保留历史 | 审计、时态推理 | 可查询历史态 |
| 动态降权（confidence floor） | memx-memory | 三层衰减地板 | 长期身份/画像维护 | 降权后可回升 |
| 搜索时重排序 | Mem0, Hindsight | recency-weighted scoring | 高频交互、噪声抑制 | 始终存在 |

四种模式并非互斥，而是构成互补的遗忘光谱。Memx 提出的生产策略建议混合使用：TTL 约束长尾条目存储上限，LRU 式衰减抑制检索干扰，active supersession 在每次写入时消解矛盾[^mem0-eviction]。物理删除满足 GDPR 等合规要求的“删除权”，但牺牲了历史可追溯性；逻辑失效保留完整审计链，却增加存储开销；动态降权与搜索时重排序在不删除的前提下降低噪声，适合需要长期用户画像的场景。系统设计者应根据数据敏感度、合规要求与存储预算选择组合策略。

#### 7.3.2 Ebbinghaus 指数衰减的工程实现与三层 decay floor

YourMemory 在 LoCoMo 数据集上基于该模型实现 100% 的 stale memory precision，而 Mem0 原生无衰减机制导致旧记忆与新记忆权重相同，precision 为 0%[^yourmemory]。2024 年 EMNLP 遗忘曲线论文进一步提出 fine-grained / coarse-grained / amnesia 三阶段模型，将精确复制（>99%）→ 粗略记忆 → 完全遗忘的梯度映射到外部记忆系统的工作记忆→长期摘要→完全淘汰生命周期[^emnlp-forgetting]。

#### 7.3.3 调度机制：墙钟时间持久化 vs 增量水印扫描

记忆策展的调度可靠性直接决定生命周期管理的有效性。JiuwenSwarm 的 Dreaming 调度采用 in-process Orchestrator（interval_seconds + 120 s 初始延迟）+ busy backoff（Agent 活跃时跳过）+ 增量 checkpoint 扫描，实现低成本后台维护[^jiuwen]。

增量扫描与全量扫描的权衡在存储规模扩大后尤为关键。XMclaw 的 _MAINTENANCE_SCAN_LIMIT = 5000 意味着超过 5000 条事实后策展不完整；数据库领域的增量物化视图维护（IVM）原则可直接迁移到记忆系统——记录上次扫描水位线，仅处理新增/变更记忆[^narrative-ivm]。

#### 7.3.4 时间预算制：每阶段检查 deadline 的策展流水线设计

记忆策展作为后台管道，必须在有限时间预算内完成多阶段处理。XMclaw MemoryCurator 的 20 秒总预算需在 dedup → prune → contradict → crystallize 四阶段间分配，每阶段应检查 deadline 并具备优雅降级能力[^wgrow]。

[^dedup-guide]: 123ofai.com — Deduplication in ML Systems Complete Guide. 2026-02-07. https://123ofai.com/qnalab/system-design/blocks/deduplication
[^nemo-curator]: NVIDIA NeMo Curator / Cerebras SlimPajama / BigCode — Exact → Fuzzy → Semantic three-layer dedup pipeline. 2026. https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/mlops/nemo-curator/references/deduplication.md
[^arxiv-deep-knowledge]: arxiv.org — Interactive Agentic Framework for Deep Knowledge Extraction. 2026-05-26. https://arxiv.org/html/2602.00959v2
[^vitamem]: vitamem.dev — Deduplication Concepts / Two-Tier Threshold System. 2026. https://vitamem.dev/concepts/deduplication/
[^codexfi]: codexfi.com — How It Works / Deduplication & Aging Rules. 2026. https://codexfi.com/docs/how-it-works/overview
[^mnemosyne]: GitHub - 28naem-del/mnemosyne — 5-Layer Cognitive OS comparison. 2026. https://github.com/28naem-del/mnemosyne/blob/main/docs/comparison.md
[^telemem]: GitHub - TeleAI-UAGI/telemem — TeleMem vs Mem0 comparison. 2024. https://github.com/TeleAI-UAGI/telemem
[^mem0-eviction]: mem0.ai — Memory Eviction and Forgetting in AI Agents. 2026-05-22. https://mem0.ai/blog/memory-eviction-and-forgetting-in-ai-agents
[^memx-rag]: memx.app — Agent Memory vs RAG: The Real Difference. 2026-06-04. https://memx.app/blog/agent-memory-vs-rag-the-real-difference/
[^qmemory]: GitHub - QusaiiSaleem/qmemory — Deduplication & Accuracy / reflect service. 2026-06-01. https://github.com/QusaiiSaleem/qmemory
[^hindsight]: hindsight.vectorize.io — The Consolidation Problem in Agent Memory. 2026-05-21. https://hindsight.vectorize.io/blog/2026/05/21/agent-memory-consolidation
[^jiuwen]: GitHub - openJiuwen-ai/jiuwenclaw — Dreaming: Sleep-Time Memory Consolidation. 2026-03-05. https://github.com/openJiuwen-ai/jiuwenclaw/blob/develop/docs/en/Memory.md
[^arxiv-recursive-summ]: arxiv.org — Recursively Summarizing Enables Long-Term Dialogue Memory. 2023-08-26. https://arxiv.org/html/2308.15022v3
[^contextweaver]: GitHub - dgenio/contextweaver — Conversation compression issue #118. 2026-03-05. https://github.com/dgenio/contextweaver/issues/118
[^agent-compressor]: GitHub - dakshjain-1616/Agent-Memory-Compressor. 2026-04-21. https://github.com/dakshjain-1616/Agent-Memory-Compressor
[^mem0-summ-guide]: mem0.ai — LLM Chat History Summarization Guide. 2026-03-23. https://mem0.ai/blog/llm-chat-history-summarization-guide-2025
[^superoptix]: superagenticai.github.io — Memory Systems guide. 2026. https://superagenticai.github.io/superoptix-ai/guides/memory/
[^memx-memory]: GitHub - toby-bridges/memx-memory — Key Features / Three-tier system. 2026. https://github.com/toby-bridges/memx-memory
[^memorybank]: arxiv.org — MemoryBank: Enhancing LLMs with Long-Term Memory. 2023. https://arxiv.org/pdf/2305.10250.pdf
[^yourmemory]: oo.news — I built memory decay for AI agents using Ebbinghaus. 2026-03-15. https://oo.news/de/news/cb954a150033
[^emnlp-forgetting]: arxiv.org — Forgetting Curve: Evaluating Memorization Capability. EMNLP 2024. 2024-10-07. https://arxiv.org/html/2410.04727v1
[^openclaw-cron]: docs.openclaw.ai — Scheduled tasks / Cron persistence. 2026-02-01. https://docs.openclaw.ai/automation/cron-jobs
[^systemd-timer]: linuxteck.com — Master Systemd Timers. 2026-05-26. https://www.linuxteck.com/switch-from-cron-jobs-to-systemd-timers/
[^narrative-ivm]: narrative.io — Incremental View Maintenance. 2023-06-09. https://www.narrative.io/knowledge-base/nql/incremental-view-maintenance
[^wgrow]: wgrow.com — The Memory Bottleneck: Why Your Curator Agent Dictates AI Success. 2026-05-14. https://www.wgrow.com/field-notes/the-memory-bottleneck-why-your-curator-agent-dictates-ai-success/
[^aegis]: GitHub - quantifylabs/aegis-memory — Quick Feature Comparison. 2026. https://github.com/quantifylabs/aegis-memory/blob/main/README.md
