# Agent 记忆系统全栈调研（真实源码 + 论文）

> 2026-05-30。范围:**整个记忆系统的每一层**——不只是"管理/去重"。
> 对每一层都答三个问题:(a) 头部系统怎么做(带论文 + 真实源码出处);
> (b) XMclaw 现在怎么做(读真实代码,不是猜);(c) 差距 + 建议。
>
> 调研对象(全部有开源代码 + 论文):Mem0、Zep/Graphiti、MemGPT/Letta、
> Generative Agents、A-MEM、HippoRAG、MemoryBank、MIRIX,以及 LoCoMo /
> LongMemEval 两个 benchmark 和 2025/2026 的几篇 survey。

---

## 0. 一张全栈对照表(先看结论)

| 层 | 头部代表做法 | XMclaw 现状 | 差距 |
|---|---|---|---|
| ① 记忆类型 | episodic/semantic/procedural/working/core 多模块 (MIRIX) | 10 FactKind + 3 layer,大致覆盖三类 | 🟢 基本够 |
| ② 抽取入库 | LLM 抽取 salient facts (Mem0) | 双层:regex(KeyInfo)+ LLM(后台) | 🟢 接近 |
| ③ 表示/schema | 双时间轴边 (Zep)、Zettelkasten note (A-MEM) | Fact + Relation(6 边),只有 ts_first/ts_last | 🟡 缺时间有效区间 |
| ④ 存储 | 向量库 / 图库 / KV | LanceDB 向量 + 图后端 | 🟢 够 |
| ⑤ 索引 | dense + sparse + graph | embedding + BM25(hybrid.py) | 🟢 够 |
| ⑥ 检索 | PPR 单步多跳 (HippoRAG)、三路 (Zep) | KNN cosine + BM25 + 1-hop 关系展开 | 🟡 无多跳/PageRank |
| ⑦ 排序 | recency×importance×relevance (Gen-Agents) | query cosine + confidence | 🔴 **无 recency 衰减、无 importance** |
| ⑧ 注入上下文 | 分层 + paging (MemGPT)、token 预算 | render_for_prompt + cache_control 断点 | 🟢 够 |
| ⑨ 更新管理 | **写入时 ADD/UPDATE/DELETE/NOOP** (Mem0) | 写入时 near-dup 证据投票;UPDATE/DELETE 靠事后 curator | 🔴 **关键缺口** |
| ⑩ 矛盾 | **时间失效保留** (Zep) | 仅 correction 标 CONTRADICTS;curator floor 置信 | 🔴 无时间失效 |
| ⑪ 遗忘 | Ebbinghaus 召回即强化 (MemoryBank) | sweep TTL + cap + prune 降权 | 🟡 无召回强化曲线 |
| ⑫ 评估 | LoCoMo / LongMemEval 跑分 | 无记忆专项 benchmark | 🔴 **没法量化好坏** |

**最该投资的三处(🔴):⑨ 写入时决策、⑩ 时间失效、⑦ 三因子排序;⑫ 评估是"看不见
就改不动"的元问题。**

---

## ① 记忆类型(存什么)

- **头部**:MIRIX(2025 survey 引用)分 Core / Episodic / Semantic / Procedural /
  Resource / Knowledge Vault 六模块,各有 type-specific 字段和访问策略。认知科学
  三分法 episodic(事件)/ semantic(事实)/ procedural(技能)是所有 survey 的骨架
  (*Memory in the Age of AI Agents: A Survey*,`github.com/Shichun-Liu/Agent-Memory-Paper-List`)。
- **XMclaw**:10 个 `FactKind`(preference/decision/identity/commitment/correction/
  project/episode/lesson/persona_manual/topic)+ 3 `FactLayer`(working/long_term/
  procedural)。episode≈episodic,preference/identity/project≈semantic,lesson/
  persona≈procedural。([models.py:36](xmclaw/memory/v2/models.py))
- **差距**:🟢 类型划分其实比多数开源系统还细。无需改。

## ② 抽取入库(原文怎么变成记忆)

- **头部**:Mem0 两阶段,extraction 输入 = `(滚动摘要 + 最近 10 条消息 + 当前消息
  对)`,LLM 抽 salient facts(arXiv:2504.19413 §3)。
- **XMclaw**:双层抽取 —— Layer1 `KeyInfoExtractor`(regex,同步:URL/账号/数字目标/
  显式"记住");Layer2 `LLMFactExtractor`(纯后台:语义身份/隐含事实/软偏好)。
  (JARVIS_PLAN §1.4.2 自动提取管道)
- **差距**:🟢 思路与 Mem0 一致,甚至多了 regex 快路。可补:extraction 也喂"滚动
  摘要"做上下文(目前主要喂当前消息)。

## ③ 表示 / schema(记忆长什么样)

- **头部**:
  - Zep:边带**双时间轴** `t_valid / t_invalid`(现实有效区间)+ `t_created /
    t_expired`(系统记录区间)(arXiv:2501.13956)。
  - A-MEM:note 7 属性 `{原文, 时间, 关键词, 标签, 上下文描述, embedding, 链接集}`
    (arXiv:2502.12110)。
- **XMclaw**:`Fact{id, kind, scope, text, confidence, evidence_count, embedding,
  contradicts, superseded_by, layer, bucket, ts_first, ts_last}` + `Relation`
  6 种边(CONTRADICTS/SUPERSEDES/CAUSED_BY/PART_OF/REFERS_TO/SAME_TOPIC)。
  ([models.py:166](xmclaw/memory/v2/models.py))
- **差距**:🟡 已有 confidence/evidence_count(比 Mem0 强)、有图边(比 Mem0 强)。
  **缺的是双时间轴**——只有"系统时间"`ts_first/ts_last`,没有"现实有效区间"
  `valid_at/invalid_at`。补这两个字段是 ⑩ 矛盾处理的前提。

## ④ 存储后端

- **头部**:Mem0=向量库;Zep/HippoRAG/A-MEM=向量 + 图(Neo4j/ChromaDB);MemGPT=
  分层(上下文/recall/archival)。
- **XMclaw**:LanceDB 向量 + 独立 GraphBackend(`backend_lancedb.py` / `entity.py`)。
- **差距**:🟢 向量+图双后端,架构上够头部水平。

## ⑤ 索引

- **头部**:dense embedding + sparse(BM25)+ 图索引三件套。
- **XMclaw**:embedding + BM25(`bm25.py`),hybrid 检索已落地(JARVIS_PLAN Phase 3.2)。
- **差距**:🟢 够。

## ⑥ 检索(怎么找回来)

- **头部**:
  - **HippoRAG**(NeurIPS'24,arXiv:2405.14831,`github.com/OSU-NLP-Group/HippoRAG`):
    把 query 概念当种子,在知识图上跑 **Personalized PageRank**,一步完成多跳检索;
    再用 node specificity(类 IDF)调节。多跳 QA 上比 SOTA RAG 高 ~20%。
  - **Zep**:semantic + keyword + graph **三路**并取。
- **XMclaw**:KNN cosine + BM25 hybrid + 召回后 1-hop 关系展开(CONTRADICTS/
  SUPERSEDES)。([service.py:2187 render_for_prompt](xmclaw/memory/v2/service.py))
- **差距**:🟡 有 hybrid + 1-hop,但**没有多跳图检索/PageRank**。对"用户在 A 公司、
  A 公司做电商、电商要看 GMV → 推断该关注 GMV"这类多跳串联,目前接不上。中期可上
  HippoRAG 式 PPR(已有图后端,增量不大)。

## ⑦ 排序 / 打分(哪条优先进上下文)—— 🔴 最便宜的高收益缺口

- **头部**:**Generative Agents**(arXiv:2304.03442,UIST'23)的经典三因子:
  `score = recency + importance + relevance`
  - recency = 上次访问的**指数衰减**;
  - importance = LLM 打 **1–10** 分;
  - relevance = query·memory cosine。
  被后续几乎所有系统继承。
- **XMclaw**:`render_for_prompt` 只做 query cosine 重排 + 显示 confidence;always-on
  段落按 `ts_last DESC`。([service.py:2278 `_score`](xmclaw/memory/v2/service.py))
- **差距**:🔴 **只有 relevance,没有 recency 衰减、没有 importance**。
  - recency:已有 `ts_last`,加一个 `exp(-λ·Δt)` 几乎零成本。
  - importance:可复用 confidence,或让 extractor 顺手打 1–10 分。
  这是投入产出比最高的一处。

## ⑧ 注入上下文(怎么塞进 prompt)

- **头部**:MemGPT OS 式分层 + 上下文压力 paging(memory_replace/insert/rethink
  工具,archival_memory_search)。
- **XMclaw**:`render_for_prompt` 出 `<memory-v2-facts>` 块,分"用户档案/项目档案/
  决定记录/本轮相关 top-K"四段,带 `cache_control` 断点(Phase 3.1),⚠ 标注
  contradicts/supersedes。([service.py:2339](xmclaw/memory/v2/service.py))
- **差距**:🟢 结构清晰、有缓存断点、有关系标注。够用。

## ⑨ 更新 / 管理(去重、合并、删除)—— 🔴 核心缺口

- **头部**:**Mem0** 在**写入时**对每条新 fact 检索 top-10 邻居 → LLM 选
  **ADD / UPDATE / DELETE / NOOP**(`mem0/configs/prompts.py`)。Letta 用后台
  **sleep-time agent** 兜底整合(docs.letta.com/.../sleeptime)。A-MEM 写入时触发
  邻居 evolution。
  > Mem0 真实 prompt:ADD=*"new information not present → add with new ID"*;
  > UPDATE=*"already present but totally different → update"*,同义更详则
  > *"retain the fact which has the most information"*;DELETE=*"contradicts
  > the information present → delete"*;NONE=*"already present → no change"*。
- **XMclaw**:写入时做的是**near-dup 证据投票**——cosine 命中近重复就给老 fact
  `evidence_count += 1`、`confidence` 提升、跨阈值升 long_term,而不是新建。
  ([service.py:594-634](xmclaw/memory/v2/service.py))真正的 UPDATE(合并互补信息)/
  DELETE(删矛盾)被推到**事后的 curator / dedup_scope**。
- **差距**:🔴 我们写入时只覆盖了 Mem0 四操作里的 **ADD + 一种 NOOP(证据投票)**,
  **UPDATE / DELETE 没有在写入时做**,全靠事后批量——这正是"1760 条堆积然后
  超时"的根因。建议把 `remember()` 升级成完整四操作 LLM 决策(已有 `_find_near_
  duplicate` KNN 钩子,改成把 top-k 邻居喂 LLM 决策即可)。curator 降级为兜底。

## ⑩ 矛盾处理 —— 🔴

- **头部**:**Zep/Graphiti** 矛盾**不删除**:新事实赢,旧边打 `invalid_at` 时间戳
  **保留为历史**(双时间轴)。实体去重 prompt(`dedupe_nodes.py`)严格:*"only
  duplicates if they refer to the same real-world object"*,禁止把 related-but-
  distinct 标重复。
- **XMclaw**:只有 `kind=correction` 的写入会标 `CONTRADICTS`(其余只标
  SAME_TOPIC,避免"与 3 条矛盾"误报);curator LLM pass 找矛盾后**floor 低置信侧**。
  ([service.py:647-674](xmclaw/memory/v2/service.py))
- **差距**:🔴 "floor 低置信侧"靠猜谁对,不可靠。应改 Zep 路线:给 Fact 加
  `valid_at/invalid_at`,矛盾时**新事实赢、旧事实打失效时间戳保留**,recall 默认
  只返回未失效的。这样"2 月喜欢咖啡 / 5 月戒了"两条都对,只是有效区间不同。

## ⑪ 遗忘 / 衰减

- **头部**:**MemoryBank**(AAAI'24,arXiv:2305.10250,`github.com/zhongwanjun/
  MemoryBank-SiliconFriend`)按 **Ebbinghaus 遗忘曲线**:记忆随时间衰减,但**被召回
  就强化**(reset 衰减),重要的留得久。日级事件摘要 + 全局画像分层。
- **XMclaw**:`sweep()` 做 TTL + max_items/max_bytes cap 驱逐;curator `prune` 把
  老+单证据+低置信的 floor 到 0.15。procedural/identity 永久豁免。
  ([service.py:1933 sweep](xmclaw/memory/v2/service.py))
- **差距**:🟡 有遗忘(TTL/cap/prune)但**没有"召回即强化"的衰减曲线**。现在
  evidence_count 只增不"衰",ts_last 也只在写入更新、**召回不更新**。补一个
  "recall 命中就 bump ts_last / evidence"就能拿到 MemoryBank 的强化效应,且天然
  喂给 ⑦ 的 recency 打分。

## ⑫ 作用域 / 多会话

- **头部**:MIRIX 多模块 + 访问策略;Letta 共享记忆块。
- **XMclaw**:`FactScope` user/project/session 三级,session 会被 DreamCompactor
  过期。([models.py:87](xmclaw/memory/v2/models.py))
- **差距**:🟢 三级作用域是对的。

## ⑬ 评估(怎么知道改好了)—— 🔴 元问题

- **头部 benchmark**:
  - **LoCoMo**(arXiv:2402.17753,snap-research.github.io/locomo):多会话超长对话
    (平均 300 轮 / 9K token / 最多 35 session / 跨 6–12 个月),5 类 QA:单跳/多跳/
    时序/开放域/对抗。指标 F1 / ROUGE。
  - **LongMemEval**:长期记忆专项,Mem0/Zep 都拿它当主战场。
- **XMclaw**:无记忆专项 benchmark;只有零散单测。
- **差距**:🔴 **没有量化标尺就没法判断"全方位管理"到底有没有变好**。建议引入
  LoCoMo 的子集(或自建 20–50 条多会话 QA fixture)做回归,每次记忆改动都跑。
  这是让后续所有优化"可证伪"的前提,也契合项目的 HonestGrader 哲学。

---

## 总结:给决策的优先级

按"根因级 + 投入产出比"排序:

1. **🔴 ⑨ 写入时 ADD/UPDATE/DELETE/NOOP(Mem0 路线)** — 真正治"堆积"的根因。
   `remember()` 已有 KNN 近邻钩子,升级成四操作 LLM 决策。curator 退为兜底。
2. **🔴 ⑩ Fact 加 `valid_at/invalid_at`,矛盾用时间失效(Zep 路线)** — 比"floor
   置信度"可靠;和 ⑨ 的 DELETE 是一套。
3. **🔴 ⑦ recall 三因子打分(Gen-Agents)** — recency 衰减 + importance,近乎零
   成本,立刻改善召回质量。
4. **🟡 ⑪ 召回即强化** — recall 命中 bump ts_last/evidence,顺带喂 ⑦。
5. **🔴 ⑫ 引入 LoCoMo 式回归 fixture** — 让 1–4 的效果可量化(否则又变成"以前也
   是这个但没用"的无凭据循环)。
6. **🟡 ⑥ 多跳图检索(HippoRAG PPR)** — 中期,已有图后端,增量可控。

> **一句话**:XMclaw 的记忆系统在**存储/索引/类型/注入**这些"静态层"已是头部水平,
> 真正落后的是**动态层**——写入时不做 UPDATE/DELETE(⑨)、矛盾不用时间失效(⑩)、
> 召回不算 recency/importance(⑦)、改了好坏无从量化(⑫)。这四处是"全方位管理
> 记忆"真正的短板,且都有明确的论文 + 源码可抄。

---

## 出处

- Mem0 — arXiv:2504.19413 / `github.com/mem0ai/mem0`(`mem0/configs/prompts.py`)
- Zep/Graphiti — arXiv:2501.13956 / `github.com/getzep/graphiti`(`prompts/dedupe_nodes.py`, `prompts/extract_edges.py`)
- MemGPT — arXiv:2310.08560 / Letta sleep-time:docs.letta.com/guides/agents/architectures/sleeptime
- Generative Agents — arXiv:2304.03442(UIST 2023)
- A-MEM — arXiv:2502.12110(NeurIPS 2025)/ `github.com/agiresearch/A-mem`
- HippoRAG — arXiv:2405.14831(NeurIPS 2024)/ `github.com/OSU-NLP-Group/HippoRAG`
- MemoryBank — arXiv:2305.10250(AAAI 2024)/ `github.com/zhongwanjun/MemoryBank-SiliconFriend`
- LoCoMo — arXiv:2402.17753 / snap-research.github.io/locomo
- Survey — *Memory in the Age of AI Agents* / `github.com/Shichun-Liu/Agent-Memory-Paper-List`;*Memory for Autonomous LLM Agents*(arXiv:2603.07670)
