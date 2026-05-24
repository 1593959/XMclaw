# AGENTS.md — `xmclaw/memory/`

## 1. 职责

Memory 用户态入口。Phase 7 完成（2026-05-24）后 **V2 单栈**：

- **`v2/`** — 唯一 API。`MemoryService`（remember / recall / relate /
  neighbors / sweep / delete）+ LanceDB（Vector + Graph backend）+
  类型化 Fact / Relation 模型 + 确定性 ID + contradicts 检测 + LLM
  抽取管道。
- **`__init__.py`** — 顶层 re-export V2。`from xmclaw.memory import
  MemoryService, FactKind, ...` 直接可用。
- **`unified.py` / `_id.py` / `extractor.py`**（V1）— **已删除**
  (Phase 7.B.4, commit pending)。如果你看到任何 import 这些名字的
  代码，那是错误，立刻报告。

数据落盘：
- V2: `~/.xmclaw/v2/facts/`（LanceDB dataset）
- 老 `~/.xmclaw/v2/memory.db`（V1）：用户态 fact 已迁出，仅剩
  workspace 索引（file_chunk / code_chunk，由 `providers/memory/sqlite_vec`
  负责，不归本目录管）

## 2. 依赖规则

- ✅ MAY import: `xmclaw.core.*`、`xmclaw.utils.*`、`xmclaw.providers.memory.*`
  （V2 经 `backend_lancedb.py` 直接读写 LanceDB；不再依赖 sqlite_vec
  provider 的高阶包装）、stdlib、`lancedb`、`pyarrow`、embedding SDK。
- ❌ MUST NOT import: `xmclaw.daemon.*`、`xmclaw.cli.*`、`xmclaw.skills.*`、
  `xmclaw.cognition.*`。memory 是被调用方，反向是非法依赖。
- ❌ V1 名字已物理消失（Phase 7.B.4）。
  `from xmclaw.memory import UnifiedMemorySystem` / `MemoryExtractor` /
  `MemoryEntry` / `TimeRange` / `mint_unified_id` / `UnifiedWriteError`
  / `ExtractedFact` / `TriggerKind` 全部报 ImportError，不是 deprecation
  warning。

## 3. 测试入口

- V2: `tests/unit/test_v2_memory_v2_*.py`（service / models / backends /
  embedding / llm_extractor / key_info_extractor / topic_links）
- V2 集成: `tests/integration/test_v2_memory_v2_router.py`
- V1（保留至 Phase 7.B 完成后删除）:
  `tests/unit/test_v2_memory_unified*.py`、
  `tests/unit/test_v2_agent_loop_unified_memory.py`
- Smart-gate lane: `memory`

## 4. 禁止事项

- ❌ **不许同时写 V1 + V2 两条路径**。Phase 7.A 期间 V2 的 shim API
  内部转发到 V1；调用方一律走 V2 facade。
- ❌ 不许在 LLM 抽取 prompt 里塞业务域词汇（"陪玩店"、具体客户名等）。
  示例必须中性。见 Phase 1.2 整改原因。
- ❌ 不许绕过 `EmbeddingService` 直接调 embedding HTTP — 它管 LRU 缓
  存 + 3 次重试 + 指数退避，是写路径性能 / 成本的关键。
- ❌ 不许在 Fact `text` 里塞超过几百字的长文。Fact 是 atomic 知识单位，
  长文本拆 chunk 后入库（chunk 走 `kind=file_chunk` / `code_chunk`）。
- ❌ 不许在生产代码里 `import lancedb` 顶层；用 `v2/__init__.py` 里的
  `get_lancedb_*` 懒工厂，让没装 LanceDB 的用户能 import `xmclaw.memory.v2`。

## 5. 关键文件

### V2（终态）
- `v2/service.py` — `MemoryService`：remember / recall / relate /
  neighbors，以及 sweep / backfill / dedupe 维护接口。
- `v2/models.py` — `Fact` / `Relation` / `FactKind` / `FactScope` /
  `FactLayer` + enum 字符串别名。
- `v2/backend.py` — `VectorBackend` / `GraphBackend` Protocol。
- `v2/backend_lancedb.py` — 生产后端。
- `v2/backend_inmemory.py` — 测试后端。
- `v2/embedding.py` — `EmbeddingService`（LRU + retry + backoff）+
  `StubEmbedder` 测试 fallback。
- `v2/key_info_extractor.py` — regex 抽取器（URL / 账号 / 数字目标 /
  explicit-remember 模式），同步 + 后台两种触发。
- `v2/llm_extractor.py` — LLM 抽取器，从 user message 提身份 / 偏好 /
  隐含 fact。
- `v2/llm_topic.py` — 主题聚类，建 SAME_TOPIC 边。
- `v2/entity.py` — Entity 桥接（多 fact 指同一现实对象）。

## 6. Phase 7 当前状态

见 `docs/JARVIS_IMPLEMENTATION_PLAN_2026.md` §Phase 7。
- §7.A facade 收口（V2 作为唯一入口）— ✅ 2026-05-23 完成
- §7.B.1 sweep / retention — ✅ 2026-05-24 完成
- §7.B.2 migration script — ✅ 2026-05-24 完成
- §7.B.3 live 用户数据迁移 — ⏳ 等用户 OK
- §7.B.4 V1 物理删除（本目录三文件）— ✅ 2026-05-24 完成
- §7.B.5 文档收尾 — ⏳ 待 §7.B.3 收尾后做

注：`xmclaw/providers/memory/sqlite_vec.py` + `manager.py` **不属于
本 Phase 删除范围** —— 它们是 workspace 索引（file_chunk / code_chunk
by `MemoryFileIndexer`），与用户态 fact 存储无关。它们的退役另外规划。
