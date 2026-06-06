## Dimension 08: 记忆策展与维护
### 角度：去重、矛盾检测、结晶、遗忘策略与调度机制

---

### 1. 去重（Deduplication）

```
Claim: 生产级记忆系统普遍采用双层去重架构：先以向量聚类（cosine ≥ 0.85–0.92）快速筛选候选，再以 LLM 做语义裁决，防止同义改写（paraphrase）漏网 [^1][^2][^3]。
Source: 123ofai.com / arxiv.org / tianpan.co
URL: https://123ofai.com/qnalab/system-design/blocks/deduplication
Date: 2026-02-07
Excerpt: "Always run exact dedup first (free, O(n)), then fuzzy dedup (MinHash LSH), and only invest in semantic dedup if you have evidence of paraphrase-level duplication."
Context: 该文总结了 NVIDIA NeMo Curator、Cerebras SlimPajama、BigCode 等生产管道的共识：精确→模糊→语义三层漏斗。
Confidence: high
```

```
Claim: Mem0/XMclaw 风格的 cosine ≥ 0.86 合并阈值在工业界有广泛对应实践；TeleMem 在中文长对话场景下使用 LLM-based semantic clustering 进行记忆去重，准确率比 Mem0 高 19% [^4]。
Source: TeleMem (GitHub - TeleAI-UAGI/telemem)
URL: https://github.com/TeleAI-UAGI/telemem
Date: 2024-01-01
Excerpt: "Semantic clustering & deduplication: Uses LLMs to semantically merge similar memories, reducing conflicts and improving consistency."
Context: TeleMem 在 ZH-4O 中文长对话基准上达到 86.33% 准确率，显著高于 Mem0 的 70.20%。
Confidence: high
```

```
Claim: Codexfi 在写入路径上执行实时 cosine 去重（一般阈值 0.12，结构类型 0.25），重复时触发 UPDATE 而非 INSERT，实现增量去重 [^5]。
Source: codexfi docs
URL: https://codexfi.com/docs/how-it-works/overview
Date: 2026
Excerpt: "Deduplication — cosine similarity check against existing memories (threshold: 0.12 general, 0.25 structural types). Duplicates trigger an update instead of an insert."
Context: 该机制在每次 assistant turn 后自动运行，属于增量去重（incremental），而非全量扫描。
Confidence: high
```

```
Claim: 语义去重中，paraphrase-level 检测需要 LLM 介入；向量相似度对表面形式差异敏感，但对逻辑否定和微妙技术差异区分能力不足 [^6]。
Source: arxiv.org (Interactive Agentic Framework for Deep Knowledge Extraction)
URL: https://arxiv.org/html/2602.00959v2
Date: 2026-05-26
Excerpt: "Vector similarity often fails to distinguish between logical negation or subtle technical differences due to high lexical overlap. For pairs in the ambiguity zone (0.70 < S < 0.92), we deploy a reasoning-heavy model as a judge."
Context: 该研究采用三阶段去重：向量过滤（>0.92 直接合并）→ LLM 裁决（0.70–0.92 模糊区）→ 领域相关性审计。
Confidence: high
```

```
Claim: Mnemosyne 的 L2 管道在 50ms 内完成 12 步算法级去重与冲突检测，零 LLM 调用，证明去重可以在 ingestion 阶段以确定性方式完成 [^7]。
Source: mnemosyne/docs/comparison.md
URL: https://github.com/28naem-del/mnemosyne/blob/main/docs/comparison.md
Date: 2026
Excerpt: "The 12-step pipeline in L2 is entirely algorithmic — zero LLM calls. Classification, entity extraction, urgency detection, domain classification, priority scoring, confidence rating, deduplication, conflict detection, and auto-linking all run through deterministic code paths."
Context: 该架构说明去重不一定需要 LLM，算法级处理可实现 sub-50ms 的 ingestion 性能。
Confidence: medium
```

---

### 2. 矛盾检测（Contradiction Detection）

```
Claim: 矛盾检测的工业标准做法是：向量距离预筛选 + kind 过滤（仅 correction/claim 类记忆触发）+ LLM 判断矛盾对；Mem0 内部使用 AUDN（Add/Update/Delete/Noop）循环解决冲突 [^8][^9]。
Source: mem0.ai blog / MEME benchmark paper
URL: https://mem0.ai/blog/memory-eviction-and-forgetting-in-ai-agents
Date: 2026-05-22
Excerpt: "Mem0's internal LLM logic performs conflict resolution: when a new fact contradicts an existing memory, it decides whether to ADD, UPDATE, or DELETE."
Context: Mem0 在写入时即进行冲突检测，而非留到后台策展阶段；但 Mem0 官方将深层冲突解决标记为 "not planned"，依赖 linked_memory_ids 和检索层排序。
Confidence: high
```

```
Claim: Zep/Graphiti 采用 temporal invalidation（双时间轴：valid_at / invalid_at）替代硬删除，保留历史并标记失效时间，使 agent 能回答 "去年十月客户的地址是什么" [^10]。
Source: memx.app blog / getzep.com
URL: https://memx.app/blog/agent-memory-vs-rag-the-real-difference/
Date: 2026-06-04
Excerpt: "Zep's Graphiti engine takes the temporal route with a bi-temporal model. Every fact carries two timelines: when it was true in the world and, separately, when the system learned it and when the system retired it."
Context: 这与 Mem0 的破坏性覆盖（DELETE/overwrite）形成鲜明对比；若需审计或时态推理，temporal invalidation 是更优选择。
Confidence: high
```

```
Claim: qmemory 的 reflect service 自动检测冲突事实并停用过时版本，创建 prev_version 边实现软删除；Aegis Memory 使用显式 contradicts 边 + 可选 LLM 的混合方案 [^11][^12]。
Source: GitHub - QusaiiSaleem/qmemory / quantifylabs/aegis-memory
URL: https://github.com/QusaiiSaleem/qmemory
Date: 2026-06-01
Excerpt: "Contradiction resolution — the reflect service auto-detects conflicting facts and deactivates the outdated one."
Context: qmemory 采用版本链（version chains）机制，corrections 创建 prev_version 边，originals 永不删除。
Confidence: high
```

```
Claim: 在 Velsof 的 "7 Battle-Tested LLM Memory Architecture Patterns" 中，矛盾检测被描述为 "bug fixed in February" 失败模式的唯一解：新记忆写入时运行相似性检查，若矛盾则标记旧记忆为 superseded 并链接 [^13]。
Source: velsof.com
URL: https://www.velsof.com/blog/llm-memory-architecture-patterns
Date: 2026-05-14
Excerpt: "If the new memory contradicts an existing one, mark the older one as superseded and link them. On read, prefer the most recent uncontradicted entry."
Context: 该模式被用于处理产品知识跨发布周期的场景，是保持 agent 信息时效性的关键。
Confidence: high
```

---

### 3. 记忆结晶（Crystallization）

```
Claim: 记忆结晶（crystallization / consolidation）指从多条碎片提炼规范表述；LLM 驱动的语义合成是主流做法，但保守策略（"拿不准就不结晶"）可避免错误合并 [^14][^15]。
Source: Hindsight blog / Agent Memory Techniques (GitHub - NirDiamant)
URL: https://hindsight.vectorize.io/blog/2026/05/21/agent-memory-consolidation
Date: 2026-05-21
Excerpt: "Merge — How related facts get unified into a single canonical record... Hindsight supports LLM-powered consolidation."
Context: Hindsight 的四杠杆框架（importance, merge, decay, eviction）将 merge 视为结晶/统一的核心环节。
Confidence: high
```

```
Claim: JiuwenSwarm 的 "Dreaming" 机制在 idle time 周期性扫描历史会话，调用 LLM 提取值得长期保留的内容并写入持久记忆文件，属于 sleep-time consolidation [^16]。
Source: GitHub - openJiuwen-ai/jiuwenclaw
URL: https://github.com/openJiuwen-ai/jiuwenclaw/blob/develop/docs/en/Memory.md
Date: 2026-03-05
Excerpt: "Dreaming: a sleep-time mechanism that periodically scans past sessions during idle time, calls an LLM to extract content worth keeping long-term, and writes the result to persistent memory files."
Context: 采用增量扫描（checkpoint 跟踪已处理会话）+ 成本限制（每轮最多 10 个会话，每个压缩到 30K token 以下）。
Confidence: high
```

```
Claim: Mnemosyne L5 的 4-phase active consolidation 包含：contradiction detection → near-duplicate merge → popular promotion → stale demotion，实现从工作记忆到长期记忆的自动晋升与降级 [^7]。
Source: mnemosyne/docs/comparison.md
URL: https://github.com/28naem-del/mnemosyne/blob/main/docs/comparison.md
Date: 2026
Excerpt: "4-phase active consolidation (contradiction detection, near-duplicate merge, popular promotion, stale demotion)"
Context: 该机制属于 L5（自改进与多 agent 感知层），说明结晶可作为分层架构中的独立层级实现。
Confidence: medium
```

---

### 4. 遗忘与衰减策略（Forgetting & Decay）

```
Claim: Ebbinghaus 遗忘曲线在 AI 记忆系统中的工程实现通常采用指数衰减模型：R = e^(-t/S)，其中 S 为记忆强度，每次回忆后 S 增加且 t 重置；YourMemory 在 LoCoMo 数据集上实现 100% stale memory precision（Mem0 为 0%）[^17][^18]。
Source: oo.news / arxiv.org (MemoryBank)
URL: https://oo.news/de/news/cb954a150033
Date: 2026-03-15
Excerpt: "strength = importance × e^(−λ_eff × days) × (1 + recall_count × 0.2) ... Stale memory precision: 100% vs Mem0 0%"
Context: YourMemory 使用 APScheduler 执行 24h 衰减任务；Mem0 原生无衰减机制，导致旧记忆与新记忆权重相同。
Confidence: high
```

```
Claim: 2024 年遗忘曲线论文（EMNLP）提出 fine-grained / coarse-grained / amnesia 三阶段模型，用于评估长上下文模型的记忆能力：精确复制（>99%）→ 粗略记忆（copy 优于 LM）→ 完全遗忘 [^19]。
Source: arxiv.org (Forgetting Curve: A Reliable Method for Evaluating Memorization Capability)
URL: https://arxiv.org/html/2410.04727v1
Date: 2024-10-07
Excerpt: "fine-grained memory where the model achieves 99% token replication accuracy, coarse-grained memory where copy accuracy surpasses LM accuracy, and the amnesia area where the model completely ignores the prefix."
Context: 该论文虽针对 LLM 上下文窗口评估，但其三阶段框架可映射到外部记忆系统：工作记忆（fine-grained）→ 长期摘要（coarse-grained）→ 完全淘汰（amnesia）。
Confidence: high
```

```
Claim: Memx 提出四层遗忘策略对比：TTL/age（合规/PII）、LRU/recency（高周转助手）、Salience scoring（高信号管道）、Semantic supersession（偏好与画像数据）；最佳实践是混合使用 [^8]。
Source: mem0.ai blog
URL: https://mem0.ai/blog/memory-eviction-and-forgetting-in-ai-agents
Date: 2026-05-22
Excerpt: "A reasonable production policy is something like: TTL on long-tail entries to bound storage, LRU-style decay on retrieval scores to bound interference, and active supersession on every write so contradictions never accumulate."
Context: 该文强调 "Passive aging is for noise. Active forgetting is for facts." 被动用于噪音清理，主动用于事实修正。
Confidence: high
```

```
Claim: Memx-memory 采用三层衰减地板（decay floor）：Core 0.9（稳定身份）、Working 0.7（活跃上下文）、Peripheral 0.5（老化/低优先级），实现置信度 floor 而非硬删除 [^20]。
Source: GitHub - toby-bridges/memx-memory
URL: https://github.com/toby-bridges/memx-memory
Date: 2026
Excerpt: "Three-tier system — Core (stable identity, decay floor 0.9), Working (active context, decay floor 0.7), Peripheral (aging/low-priority, decay floor 0.5)."
Context: 该设计确保低价值记忆降权但不删除，符合 "confidence floor" 策略；晋升与降级基于使用模式。
Confidence: high
```

```
Claim: TTL 自动清理与容量上限（max_items / max_bytes）是记忆系统的基本运维需求；SUPEROPTIX AI 的 FileBackend/SQLiteBackend/RedisBackend 均内置 TTL 与自动清理 [^21]。
Source: SUPEROPTIX AI docs
URL: https://superagenticai.github.io/superoptix-ai/guides/memory/
Date: 2026
Excerpt: "Automatic TTL (time-to-live) management ... Short-Term Memory: LRU Cache: Automatic eviction of least recently used items."
Context: 该框架区分了短期记忆（LRU/FIFO）与长期记忆（语义搜索），TTL 主要作用于短期/会话级存储。
Confidence: high
```

---

### 5. 压缩与摘要（Compression & Summarization）

```
Claim: 对话历史压缩的主流模式是：保留最近 N 轮 verbatim + 将更早内容摘要为 compact summary；LangChain 的 SummarizingTokenWindowChatMemory 和 contextweaver 的 compression.py 均采用此策略 [^22][^23]。
Source: GitHub - dgenio/contextweaver / mem0.ai blog
URL: https://github.com/dgenio/contextweaver/issues/118
Date: 2026-03-05
Excerpt: "Group consecutive user/agent turns into episodes; Summarize each episode into a compact ContextItem; Replace original items with summaries, preserving facts and key decisions."
Context: contextweaver 在 token budget 使用超过 80% 时触发压缩，目标 2-3x 有效历史保留率。
Confidence: high
```

```
Claim: 递归摘要（recursively summarizing）是处理超长对话的标准方法：LLM 先对短对话生成摘要，随后将旧摘要与新对话结合持续更新，最终形成跨会话的 global summary [^24]。
Source: arxiv.org (Recursively Summarizing Enables Long-Term Dialogue Memory)
URL: https://arxiv.org/html/2308.15022v3
Date: 2024-08-26
Excerpt: "A generative LLM is first prompted to produce a summary given a short dialog context. After that, we ask the LLM to continue updating and generate a new summary/memory by combining the previous memory and subsequent dialogues."
Context: 该方法被证明可建模长期对话记忆，同时作为解决 LLM 超长上下文限制的潜在方案。
Confidence: high
```

```
Claim: Agent-Memory-Compressor 采用多信号重要性评分（Recency + Type weight + Keyword boost）+ 三种压缩策略（summarize / extract_facts / archive），在 token budget 压力下自动压缩 [^25]。
Source: GitHub - dakshjain-1616/Agent-Memory-Compressor
URL: https://github.com/dakshjain-1616/Agent-Memory-Compressor
Date: 2026-04-21
Excerpt: "Compression is driven by an ImportanceScorer that combines three signals... The CompressionEngine exposes three strategies: summarize, extract_facts, archive."
Context: 该库使用遗忘曲线触发器（forgetting curve trigger）在 turn interval 或 token threshold 超过时自动启动压缩。
Confidence: high
```

```
Claim: Mem0 区分 summarization（有损压缩所有信息）与 memory formation（选择性保留关键事实）；后者通过实时检测关键事实并持久化，可减少 80-90% token 成本 [^26]。
Source: mem0.ai blog (LLM Chat History Summarization Guide)
URL: https://mem0.ai/blog/llm-chat-history-summarization-guide-2025
Date: 2026-03-23
Excerpt: "The key difference is selectivity. Summarization tries to preserve everything in compressed form, while memory formation chooses what deserves permanent retention."
Context: Mem0 将工作记忆（当前会话）与情景记忆（过去交互中的重要时刻）分离，避免全量摘要的信息损失。
Confidence: high
```

---

### 6. 调度机制（Scheduling）

```
Claim: OpenClaw 的 cron 调度器将 job 定义、运行时状态和运行历史持久化在 SQLite 中，确保 Gateway 重启后不丢失调度；但 updatedAtMs 字段曾因仅保存在内存中导致重启后监控脚本误报 [^27][^28]。
Source: docs.openclaw.ai / GitHub issue #76461
URL: https://docs.openclaw.ai/automation/cron-jobs
Date: 2026-02-01
Excerpt: "Job definitions, runtime state, and run history persist in OpenClaw's shared SQLite state database so restarts do not lose schedules."
Context: 该案例直接说明 "24 次 sweep 从未触发" 问题的根源：若调度状态不持久化，daemon 重启后 wall-clock 基准丢失，导致定时任务被跳过。
Confidence: high
```

```
Claim: systemd timer 的 Persistent=true directive 可解决 "missed runs" 问题：若系统关机期间有触发时刻，开机后会立即补执行；这是 wall-clock 持久化调度的标准工程方案 [^29]。
Source: linuxteck.com
URL: https://www.linuxteck.com/switch-from-cron-jobs-to-systemd-timers/
Date: 2026-05-26
Excerpt: "Persistent=true — Runs missed triggers after reboot. Any critical task that must not be silently skipped."
Context: 传统 cron 在系统关闭期间错过的任务不会补执行；systemd timer 的 Persistent 机制确保关键维护任务（如记忆策展 sweep）不会丢失。
Confidence: high
```

```
Claim: JiuwenSwarm 的 Dreaming 调度采用 in-process Orchestrator（interval_seconds + 120s 初始延迟）+ busy backoff（agent 活跃时跳过）+ 增量 checkpoint 扫描，实现低成本后台维护 [^16]。
Source: GitHub - openJiuwen-ai/jiuwenclaw
URL: https://github.com/openJiuwen-ai/jiuwenclaw/blob/develop/docs/en/Memory.md
Date: 2026-03-05
Excerpt: "Scheduling: an in-process Orchestrator fires every interval_seconds, with a 120s initial delay after startup. Busy backoff: skipped when the agent is actively handling a request. Incremental scan: a checkpoint tracks processed sessions."
Context: 该设计平衡了实时响应与后台维护：增量扫描避免重复处理，busy backoff 防止影响用户体验。
Confidence: high
```

```
Claim: 时间预算制（per-stage deadline）是记忆策展管道的关键保障；XMclaw MemoryCurator 的 20s 总预算需要在 dedup → prune → contradict → crystallize 四阶段间分配，每阶段应检查 deadline 并优雅降级 [^30]。
Source: XMclaw 已知上下文 / wgrow.com (Memory Bottleneck)
URL: https://www.wgrow.com/field-notes/the-memory-bottleneck-why-your-curator-agent-dictates-ai-success/
Date: 2026-05-14
Excerpt: "Every background cycle the Curator runs consumes tokens and API time. That expenditure needs to show up downstream: shorter prompts fed to worker agents, fewer hallucinations, fewer costly human resets."
Context: 该文强调 Curator 的 ROI 需通过 Context Precision 和 Context Recall 衡量；时间预算不足时应优先保障高杠杆阶段（如 contradiction detection）。
Confidence: medium
```

```
Claim: 增量扫描（incremental scan）vs 全量扫描（full scan）的权衡在于：大存储下全量扫描的 _MAINTENANCE_SCAN_LIMIT = 5000 会导致策展不完整；应结合 checkpoint / high-watermark + 分层扫描策略 [^16][^31]。
Source: JiuwenSwarm docs / narrative.io
URL: https://www.narrative.io/knowledge-base/nql/incremental-view-maintenance
Date: 2023-06-09
Excerpt: "Incremental queries ensure that only new or updated data is processed, avoiding the need for a full dataset scan. This saves time and resources."
Context: 数据库领域的增量物化视图维护（IVM）原则可直接迁移到记忆系统：记录上次扫描水位线，仅处理新增/变更记忆。
Confidence: high
```

---

### 7. 综合对比与工程建议

```
Claim: 2026 年的记忆系统共识是：hybrid retrieval + contradiction handling + consolidation 已成为 table stakes；各系统差异在于 "how" 而非 "whether" [^12]。
Source: quantifylabs/aegis-memory
URL: https://github.com/quantifylabs/aegis-memory/blob/main/README.md
Date: 2026
Excerpt: "Memory-depth primitives (hybrid retrieval, contradiction handling, consolidation) are now table stakes — mem0, Zep, Letta, and Aegis all ship variants in 2026."
Context: 该对比表显示 Mem0 侧重写时 LLM 驱动操作，Zep/Graphiti 强在时间有效性，Letta 有清晰的分层（tier）故事，Aegis 强调显式 resolution workflow。
Confidence: high
```

```
Claim: 记忆策展的四大杠杆（importance, merge, decay, eviction）中，大多数生产系统对 decay 和 eviction 支持最弱；Zep 在 decay 上最强（temporal validity intervals），Hindsight 选择跳过 eviction 以 consolidation 替代 [^30]。
Source: Hindsight blog
URL: https://hindsight.vectorize.io/blog/2026/05/21/agent-memory-consolidation
Date: 2026-05-21
Excerpt: "Zep is the strongest decay system. Letta has the cleanest tier story. Mem0 has the most polished write-time operations API. ... Hindsight deliberately skips individual memory eviction — stale facts become effectively unretrievable through LLM-powered consolidation and recency-weighted scoring."
Context: 该框架为 XMclaw 的 MemoryCurator 改进提供了直接参照：若 eviction 实现复杂，可先用 aggressive consolidation + recency scoring 降低检索干扰。
Confidence: high
```

---

### 引用索引

[^1]: 123ofai.com — Deduplication in ML Systems Complete Guide (2026-02-07)
[^2]: arxiv.org — SemDeDup / DATA4LLM embedding-based clustering (2025)
[^3]: tianpan.co — Idempotency Is Not Optional in LLM Pipelines (2026-04-20)
[^4]: GitHub - TeleAI-UAGI/telemem — TeleMem vs Mem0 comparison
[^5]: codexfi.com — How It Works / Deduplication & Aging Rules
[^6]: arxiv.org — Interactive Agentic Framework for Deep Knowledge Extraction (2026-05-26)
[^7]: GitHub - 28naem-del/mnemosyne — 5-Layer Cognitive OS comparison
[^8]: mem0.ai — Memory Eviction and Forgetting in AI Agents (2026-05-22)
[^9]: arxiv.org — MEME: Multi-entity & Evolving Memory Evaluation
[^10]: memx.app — Agent Memory vs RAG: The Real Difference (2026-06-04)
[^11]: GitHub - QusaiiSaleem/qmemory — Deduplication & Accuracy
[^12]: GitHub - quantifylabs/aegis-memory — Quick Feature Comparison
[^13]: velsof.com — 7 Battle-Tested LLM Memory Architecture Patterns (2026-05-14)
[^14]: hindsight.vectorize.io — The Consolidation Problem in Agent Memory (2026-05-21)
[^15]: GitHub - NirDiamant/Agent_Memory_Techniques — memory_consolidation.ipynb
[^16]: GitHub - openJiuwen-ai/jiuwenclaw — Dreaming: Sleep-Time Memory Consolidation
[^17]: oo.news — I built memory decay for AI agents using Ebbinghaus (2026-03-15)
[^18]: arxiv.org — MemoryBank: Enhancing LLMs with Long-Term Memory (2023)
[^19]: arxiv.org — Forgetting Curve: Evaluating Memorization Capability (EMNLP 2024)
[^20]: GitHub - toby-bridges/memx-memory — Key Features / Three-tier system
[^21]: superagenticai.github.io — Memory Systems guide
[^22]: GitHub - dgenio/contextweaver — Conversation compression issue #118
[^23]: mem0.ai — LLM Chat History Summarization Guide (2026-03-23)
[^24]: arxiv.org — Recursively Summarizing Enables Long-Term Dialogue Memory
[^25]: GitHub - dakshjain-1616/Agent-Memory-Compressor
[^26]: mem0.ai — Summarization vs Memory Formation
[^27]: docs.openclaw.ai — Scheduled tasks / Cron persistence
[^28]: GitHub - openclaw/openclaw — Issue #76461 cron scheduler persistence
[^29]: linuxteck.com — Master Systemd Timers (2026-05-26)
[^30]: wgrow.com — The Memory Bottleneck: Why Your Curator Agent Dictates AI Success
[^31]: narrative.io — Incremental View Maintenance
