# XMclaw 记忆系统读写逻辑总览

> **版本**: Waves 1-4 完成后状态  
> **日期**: 2026-06-06  
> **覆盖文件**: `service.py`, `auto_recall.py`, `backend_lancedb.py`, `bm25.py`, `curator.py`, `sync.py`

---

## 一、读取路径（Read Path）

XMclaw 的记忆读取是**双轴架构**：

```
┌─────────────────────────────────────────────────────────────┐
│                    读取路径总览                               │
├─────────────────────────────────────────────────────────────┤
│  轴 1: 结构轴（Structural）—— 每轮必载，缓存友好            │
│      render_for_prompt() → 按 bucket 分类的 always-on 事实 │
│      直接注入 system prompt                                   │
├─────────────────────────────────────────────────────────────┤
│  轴 2: 相似轴（Similarity）—— 动态召回，本轮相关             │
│      recall_for_message() → 向量/BM25 召回 → 注入 user msg   │
│      不打断 prompt 缓存                                       │
└─────────────────────────────────────────────────────────────┘
```

### 1.1 结构轴：render_for_prompt()

**入口**: `MemoryService.render_for_prompt(query, k=8)`

**流程**:

```
1. 并发召回 4 类 always-on 事实 (asyncio.gather)
   ├── user_t   : kinds=[preference, identity, correction], scopes=[user],   k=20
   ├── project_t: kinds=[project, commitment],            scopes=[project], k=20
   ├── decision_t: kinds=[decision],                       k=10
   └── task_t   : kinds=[task_context],                    scopes=[project], k=3  ← Wave-2

2. 条件召回 query-conditioned 事实
   └── relevant_t: recall(query, k=k, include_relations=True)  ← 仅当 query 非空

3. 三因子重排序 (Phase 8 ⑦)
   └── score = 0.4*recency + 0.3*importance + 0.3*relevance

4. 组装 Markdown 区块
   ├── ### 用户档案 (USER)
   ├── ### 项目档案 (PROJECT)
   ├── ### 决定记录 (DECISIONS)
   ├── ### 进行中任务          ← Wave-2 (task_context)
   └── ### 与本轮相关的事实 (top-K, 向量召回)
       └── 距离阈值过滤: distance ≤ _recall_distance_threshold (默认 0.40)  ← Wave-1

5. 强化学习反馈 (Phase 8 ⑪)
   └── _reinforce_facts(new_hits) → 异步更新 ts_last
```

**Waves 修改影响**:
- **Wave-1**: `_recall_distance_threshold` 可配置（默认 0.40），防止"需要"类查询污染上下文
- **Wave-2**: 新增 `task_context` 召回段，支持跨会话任务连续性
- **Wave-4**: `recall()` 新增 `valid_at` 参数，支持 point-in-time 历史查询

---

### 1.2 相似轴：recall_for_message()

**入口**: `auto_recall.recall_for_message(memory_service, user_message, ...)`

**流程**:

```
1. 前置过滤
   ├── 消息长度 < 4 字符 → 返回 []
   ├── memory_service is None → 返回 []
   └── 排除 bucket ∈ {agent_identity, user_identity, user_preference, values}
       （这些已由结构轴渲染，避免重复）

2. 选择召回策略
   ├── use_hybrid=True (默认, Wave-2) → recall_hybrid(text, k=16)
   │   ├── 向量路径: recall(text, k=48, include_relations=False)
   │   ├── BM25 路径:
   │   │   ├── 尝试 PrebuiltBM25Index (Wave-4, M-7)
   │   │   └── 回退: 扫描 corpus_cap=500 条 + 构建 BM25Index
   │   └── RRF 融合 (k=60) → 取 top-k  ← Wave-2
   └── use_hybrid=False → recall(text/embedding, k=16)

3. 超时保护
   └── asyncio.wait_for(recall_coro, timeout=3.0s)  ← Wave-1 (1.0s→3.0s)
       ├── TimeoutError → 记录 recall_timeout_count 指标
       └── 返回 []（本轮无召回，不阻塞 LLM）

4. 后处理
   ├── distance → similarity 转换: sim = 1 - distance
   ├── sim < 0.65 → 过滤
   └── 返回 RecalledFact(fid, text, bucket, kind, ts_first, similarity)
```

**Waves 修改影响**:
- **Wave-1**: 超时 1.0s→3.0s，新增 `recall_timeout_count` 指标
- **Wave-2**: `use_hybrid` 默认 True，RRF 融合替代加权融合
- **Wave-4**: `recall_hybrid()` 透传 `valid_at` 参数；预构建 BM25 索引优先

---

### 1.3 底层召回：recall()

**入口**: `MemoryService.recall(query, ..., valid_at=None)`

**流程**:

```
1. 构建 where 子句（SQL/LanceDB 过滤）
   ├── kinds     → kind IN (...)
   ├── scopes    → scope IN (...)
   ├── buckets   → bucket IN (...)
   ├── min_confidence → confidence >= X
   ├── only_layer     → layer = 'X'
   ├── include_superseded=False → superseded_by = ''
   └── time_range     → ts_last >= start AND ts_last <= end

2. 查询输入解析
   ├── query=None     → 纯过滤列表（ts_last DESC）
   ├── query=str      → embed(query) 或 keyword_only 回退
   └── query=list[float] → 直接作为向量

3. 后端搜索
   └── _vec.search(search_query, where=where, limit=k)
       ├── LanceDB: ANN + SQL where 预过滤
       └── InMemory: 线性扫描 + eval(where)

4. 后过滤（backend-agnostic）
   ├── include_invalidated=False → 过滤 invalid_at ≤ now 的事实
   └── valid_at 过滤 (Wave-4) → 只保留 valid_at ≤ t < invalid_at 的事实

5. 关系富化（并发）
   ├── asyncio.gather + Semaphore(20)
   └── 对每个 hit: _graph.neighbors(fact.id, max_hops=1)

6. 返回 RecallHit(fact, distance, related_relations)
```

---

## 二、写入路径（Write Path）

### 2.1 主写入：remember()

**入口**: `MemoryService.remember(text, kind, scope, confidence, ... provenance="unknown")`

**流程**:

```
1. 输入验证
   ├── text 为空 → ValueError
   └── 输入消毒 (Wave-3) → sanitizer.check(text, provenance)
       ├── 高风险内容 → MemoryServiceWriteError
       └── 高信任来源 (manual_ui, persona_file) → 绕过

2. 计算 fact_id
   └── Fact.compute_id(kind, scope, text)  ← 确定性哈希，保证幂等

3. 嵌入文本
   ├── embedder.embed(text) → vector
   └── EmbeddingFailure → 记录日志，embedding=None

4. 查重与合并（Near-Duplicate Detection）
   ├── 精确匹配: _vec.get(fact_id)
   │   └── 存在 → 合并: evidence_count++, confidence 提升, ts_last 更新
   └── 语义查重: _find_near_duplicate(embedding, kind, scope)
       └── cosine distance < 0.15 → 视为同一事实，证据投票

5. 自动晋升（Auto-Promote）
   └── evidence_count ≥ 5 → layer 从 working → long_term

6. 关系扫描（Relation Scan）
   ├── 向量近邻搜索（top-10, 跨 kind）
   ├── SAME_TOPIC: distance ≤ 0.10 → same_topic_ids
   ├── CONTRADICTS: kind=correction AND distance ≤ 0.15 → contradicts_ids
   └── 共享实体桥接: _shared_entity_links() → 补充 same_topic_ids

7. Bucket 解析（3 级回退）
   └── effective_bucket = caller_explicit > existing.bucket > _infer_bucket() > "misc"

8. 构建 Fact 对象
   └── Fact(id, kind, scope, text, confidence, evidence_count, embedding,
            source_event_id, contradicts, superseded_by, layer, bucket,
            provenance, ts_first, ts_last, valid_at, invalid_at)
            ↑ provenance 由 caller 传入 (Wave-3)

9. 持久化
   ├── _vec.upsert([new_fact])     → LanceDB/InMemory
   ├── _graph.add_relations(...)   → SAME_TOPIC / CONTRADICTS / CAUSED_BY
   └── entity_store.register_fact_text(id, text)  ← 实体反向索引

10. 事件发布（可选）
    └── _publish_curation("remembered", {...})
```

**Waves 修改影响**:
- **Wave-1**: `provenance` 字段加入 Schema（为 Wave-3 铺路）
- **Wave-3**: 
  - 新增 `provenance` 参数（默认 "unknown"）
  - 新增输入消毒层 `MemorySanitizer`
  - 5 条写入路径全部传入正确 provenance
- **Wave-4**: `valid_at` / `invalid_at` 已存在于 Fact 模型，recall 侧已启用过滤

---

### 2.2 写入来源（5 条 provenance 路径）

| 来源 | 代码位置 | provenance 值 | Wave 状态 |
|------|----------|---------------|-----------|
| Regex 提取 | `service.py:remember()` 调用处 | `auto_extract_regex` | ✅ Wave-3 |
| LLM 提取 | `service.py:remember()` 调用处 | `auto_extract_llm` | ✅ Wave-3 |
| Tool 调用 | `service.py:remember()` 调用处 | `tool_call` | ✅ Wave-3 |
| UI 手动 | `service.py:remember()` 调用处 | `manual_ui` / `user_confirmed` | ✅ Wave-3 |
| Persona 文件 | `service.py:remember()` 调用处 | `persona_file` | ✅ Wave-3 |

---

### 2.3 策展写入（Curation Write）

**入口**: `MemoryCurator.curate()`

**流程**:

```
1. 增量水印扫描 (Wave-2, M-5)
   ├── 读取 _last_curate_ts（持久化到文件）
   ├── 统计变更事实数: _count_changed_since(_last_curate_ts)
   └── 变更数 < 10 → 跳过 contradict + crystallize（节省 LLM 调用）

2. 矛盾检测（Contradict）
   └── 扫描高置信度事实对，标记 invalid_at

3. 结晶化（Crystallize）
   └── working → long_term 晋升

4. 去重（Deduplicate）
   └── 语义聚类 + 合并

5. 保存水印
   └── _save_watermark(time.time())
```

---

## 三、后端交互矩阵

| 操作 | LanceDB 后端 | InMemory 后端 | Waves 修改 |
|------|-------------|---------------|-----------|
| `upsert` | `merge_insert` + 重试 | dict 覆盖 | Wave-4: 瞬态错误 3 次重试 |
| `search` | ANN + SQL where | 线性扫描 + eval | Wave-4: 瞬态错误处理 |
| `get` | `where id = 'x'` | dict 查找 | Wave-4: 瞬态错误处理 |
| `delete` | `table.delete(where)` | dict pop | Wave-4: 瞬态错误处理 |
| `count` | `count_rows()` | len(dict) | Wave-4: 瞬态错误处理 |
| `graph.add` | `merge_insert` | list append | Wave-4: 瞬态错误处理 |
| `graph.neighbors` | BFS + SQL | dict BFS | Wave-4: 瞬态错误处理 |

---

## 四、数据流图

```
┌─────────────┐     ┌─────────────┐     ┌─────────────────┐
│  User Msg   │────→│ auto_recall │────→│ recall_hybrid() │
└─────────────┘     └─────────────┘     └─────────────────┘
                                                │
                    ┌───────────────────────────┼───────────┐
                    ↓                           ↓           ↓
            ┌───────────┐              ┌──────────┐  ┌──────────┐
            │  recall() │              │ Prebuilt │  │ Per-query│
            │ (vector)  │              │ BM25     │  │ BM25     │
            └───────────┘              │ (Wave-4) │  │ (legacy) │
                    │                  └──────────┘  └──────────┘
                    ↓                           │
            ┌───────────────┐                   │
            │ LanceDB ANN   │←──────────────────┘
            │ + SQL where     │
            └───────────────┘
                    │
                    ↓
            ┌───────────────┐
            │  Validity     │←── valid_at / invalid_at (Wave-4)
            │  Filter       │
            └───────────────┘
                    │
                    ↓
            ┌───────────────┐
            │ Relation Enrich│←── asyncio.gather + Semaphore(20)
            │ (neighbors)    │
            └───────────────┘
                    │
                    ↓
            ┌───────────────┐     ┌───────────────┐
            │ render_for_   │────→│ System Prompt │
            │ prompt()      │     │ (structural)  │
            └───────────────┘     └───────────────┘
                    │
                    ↓
            ┌───────────────┐
            │ <recalled>    │←── 注入 user message (similarity)
            │ Block         │
            └───────────────┘
```

---

## 五、潜在问题与后续建议

### 5.1 已识别问题

| 问题 | 位置 | 影响 | 建议 |
|------|------|------|------|
| `render_for_prompt` 的 4 路并发召回未加 `valid_at` | `service.py:2662` | 历史查询时 always-on 段可能混入未来事实 | 为 user_t/project_t/decision_t/task_t 添加 `valid_at=valid_at` 参数透传 |
| `auto_recall` 未透传 `valid_at` | `auto_recall.py:160` | 相似轴无法做 point-in-time 查询 | 在 `recall_for_message` 签名中添加 `valid_at` 并透传 |
| `PrebuiltBM25Index` 未在 bulk write 后 invalidate | `service.py:remember()` | 预构建索引可能返回 stale 结果 | 在 `remember()` / `sweep()` / `import` 后调用 `_bm25_index.invalidate()` |
| `remember()` 未自动设置 `invalid_at` 于被取代事实 | `service.py` | 双时态引擎依赖 curator 或手动设置 | 在 contradict 检测命中时自动 stamp `invalid_at` |

### 5.2 监控指标

| 指标 | 来源 | 用途 |
|------|------|------|
| `recall_timeout_count` | `auto_recall.py:197` | Wave-1: 监控大存储超时频率 |
| `memory.remember.sanitizer_blocked` | `service.py:680` | Wave-3: 监控注入尝试 |
| `prebuilt_bm25.rebuilt` | `bm25.py` | Wave-4: 监控索引刷新频率 |
| `lancedb.transient_retry` | `backend_lancedb.py:280` | Wave-4: 监控瞬态错误重试 |

---

*文档结束*
