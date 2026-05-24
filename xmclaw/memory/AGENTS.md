# AGENTS.md — `xmclaw/memory/`

## 1. 职责

Memory 用户态入口。当前处于 **V1→V2 收口过渡期**（JARVIS_PLAN Phase 7，
2026-05-23 立项）。

- **`v2/`** — 终态。`MemoryService`（remember / recall / relate /
  neighbors）+ LanceDB（Vector + Graph backend）+ 类型化 Fact / Relation
  模型 + 确定性 ID + contradicts 检测 + LLM 抽取管道。**所有新代码必须只
  调用 `xmclaw.memory.v2`。**
- **`unified.py` + `_id.py` + `extractor.py`** — V1 遗产，待退役。
  `UnifiedMemorySystem` 包 sqlite_vec（providers/memory/）+ MemoryGraph，
  提供 4 层（working / short_term / long_term / procedural）+ 时序索引 +
  跨轴写补偿。**禁止新代码 import**；现有 callsite 按 Phase 7.A 逐个迁
  到 V2。

数据落盘：
- V1: `~/.xmclaw/v2/memory.db`（Phase 7.B 完成后迁走 + 删除）
- V2: `~/.xmclaw/v2/facts/`（LanceDB dataset）

## 2. 依赖规则

- ✅ MAY import: `xmclaw.core.*`、`xmclaw.utils.*`、`xmclaw.providers.memory.*`
  （V2 经 `backend_lancedb.py` 直接读写 LanceDB；不再依赖 sqlite_vec
  provider 的高阶包装）、stdlib、`lancedb`、`pyarrow`、embedding SDK。
- ❌ MUST NOT import: `xmclaw.daemon.*`、`xmclaw.cli.*`、`xmclaw.skills.*`、
  `xmclaw.cognition.*`。memory 是被调用方，反向是非法依赖。
- ❌ **新代码不许 `from xmclaw.memory import UnifiedMemorySystem`** 或
  `from xmclaw.memory.unified import ...`。`check_import_direction.py`
  会在 Phase 7.A 末加 V1 import 黑名单检查。

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

### V1（待退役 — Phase 7.B 删除）
- `unified.py` — `UnifiedMemorySystem`（query / put / delete 三轴）。
- `_id.py` — `mint_unified_id` + `UnifiedWriteError`。
- `extractor.py` — 老的 `MemoryExtractor`。

## 6. Phase 7 当前状态

见 `docs/JARVIS_IMPLEMENTATION_PLAN_2026.md` §Phase 7。
- §7.A facade 收口（V2 作为唯一入口）— ⬜ 未启动
- §7.B 后端替换（删 sqlite_vec + MemoryGraph）— ⬜ 未启动

任何对本目录的改动 commit message 必须前缀 `Phase 7.A:` / `Phase 7.B:`，
并按 CLAUDE.md §开发纪律更新 Phase 7 章节的 checkbox + 进度日志。
