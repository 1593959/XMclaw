# Phase 7.A.1 — V1 Memory Callsite 盘点

**日期**: 2026-05-23
**Phase**: JARVIS_PLAN §7.A.1
**目的**: 把所有 V1 (`UnifiedMemorySystem` / `MemoryExtractor` / `_id` / `extractor`) 的 import / 调用点列清，按"能直接迁移"还是"V2 缺接口"分类，输出 §7.A.2 必须补的 V2 shim 清单。
**方法**: `rg "from xmclaw\.memory import|from xmclaw\.memory\.unified|from xmclaw\.memory\.extractor|from xmclaw\.memory\._id|UnifiedMemorySystem|MemoryExtractor|mint_unified_id|UnifiedWriteError"`
**覆盖**: 25 个文件命中 (含 v2/ 内部对老命名空间的 docstring 注释)。

---

## A. V1 本体（本 Phase 7.B.4 删除目标）

| 文件 | 角色 | 处置 |
|---|---|---|
| `xmclaw/memory/__init__.py` | re-export 入口 | §7.A.5 收窄为只 re-export V2 |
| `xmclaw/memory/unified.py` | `UnifiedMemorySystem` 定义 | §7.B.4 删 |
| `xmclaw/memory/_id.py` | `mint_unified_id` + `UnifiedWriteError` | §7.B.4 删（V2 自有确定性 ID）|
| `xmclaw/memory/extractor.py` | `MemoryExtractor` 定义 | §7.B.4 删（V2 有 LLMFactExtractor + KeyInfoExtractor）|

---

## B. 生产代码：read 路径（auto-recall）

| 位置 | 当前调用 | 等价 V2 API | V2 是否够用 |
|---|---|---|---|
| `xmclaw/daemon/agent_loop.py:1529-1611` | `self._unified_memory.query(semantic=user_message, limit=k)` | `MemoryService.recall(query=, k=)` | ✅ 直接换 |
| `xmclaw/daemon/agent_loop.py:155-164` (注释 + state) | 持有 `_unified_memory` 引用 | 改持 `_memory_service` | — |

**注**: agent_loop 同时也持有 `_memory_service_v2`（V2，由 app_lifespan 注入）。统一后只留一个。

---

## C. 生产代码：write 路径（auto-put）

| 位置 | 当前调用 | 等价 V2 API | V2 是否够用 |
|---|---|---|---|
| `xmclaw/daemon/hop_loop.py:1410-1470` | `self._unified_memory.put(text, layer, node_type, metadata)` | `MemoryService.remember(text, kind, scope, ...)` | ⚠️ **`node_type` → `kind` mapping 表要建**；`layer="procedural"` V2 缺 |
| `xmclaw/daemon/hop_loop.py:1432` | `self._memory_extractor.extract(user_message, assistant_response)` | V2 用 `LLMFactExtractor` 但调用形态不同（直接 remember 不返回 candidate）| ⚠️ 改造 hop_loop 切换抽取管道 |

---

## D. 生产代码：router 直接暴露 V1 API

| 路由 | 文件 | 状态 |
|---|---|---|
| `POST /memory/unified_query` | `xmclaw/daemon/routers/memory.py:823-928` | §7.A 期间保留（前端 panels 仍在用），§7.B 末删 |
| `POST /memory/unified_put` | `xmclaw/daemon/routers/memory.py:931-1030` | 同上 |

每次 request per-request 构造 `UnifiedMemorySystem(...)` 实例 — 迁移期改为转发到 V2 service，URL 暂保留向后兼容。

并存路由：`xmclaw/daemon/routers/memory_v2.py` 已有 V2 端点。

---

## E. 生产代码：cognition

| 位置 | 当前调用 | 等价 V2 API | V2 是否够用 |
|---|---|---|---|
| `xmclaw/cognition/reflection_cycle.py:14,151,313,326` | walk `unified_memory` short-term layer → promote durable items to long-term | `MemoryService.recall(layers=["working"], ...)` + `remember(layer="long_term")` | ⚠️ V2 当前层 = {working, long_term}，没有 short_term；reflection 语义需要在 §7.B.1 重新建模（或 short_term 映射成 working+timestamp 过滤）|

---

## F. 生产代码：启动 + 工具桥接

| 位置 | 用途 | 处置 |
|---|---|---|
| `xmclaw/daemon/factory.py:1972-2003` | 构造 `UnifiedMemorySystem` + `MemoryExtractor` 并注入 agent | §7.A.3 切到只构造 `MemoryService` |
| `xmclaw/daemon/app_lifespan.py:1635-1675` | wire `_unified_memory` 到 agent 上 | §7.A.3 同上 |
| `xmclaw/daemon/app_lifespan.py:2218-2257` | BuiltinTools hot-wire bridge（把 V2 注入到 builtin tools 让 `memory_search` 同时查 V1+V2）| §7.A.4 整段删 |

---

## G. 前端（static）

| 文件 | 用途 |
|---|---|
| `xmclaw/daemon/static/pages/Memory.js` | Memory page，调 `/memory/unified_query` |
| `xmclaw/daemon/static/pages/_panels/memory_unified_query.js` | 多轴查询 UI panel |
| `xmclaw/daemon/static/pages/_panels/memory_activity.js` | 记忆活动时间线 panel（订阅 MEMORY_RECALL / MEMORY_PUT_AUTO 事件）|

处置：§7.A 期间不动（后端 URL 保留），§7.B 末同步切到 V2 路由。

---

## H. 测试

| 文件 | 类型 | 处置 |
|---|---|---|
| `tests/unit/test_v2_memory_unified.py` | V1 query 单测 | §7.B.4 随 unified.py 一起删 |
| `tests/unit/test_v2_memory_unified_write.py` | V1 put/delete + UnifiedWriteError | §7.B.4 删 |
| `tests/unit/test_v2_memory_extractor.py` | V1 MemoryExtractor | §7.B.4 删 |
| `tests/unit/test_v2_agent_loop_unified_memory.py` | agent_loop ↔ V1 integration | §7.A.3 改写为 V2 集成测试 |
| `tests/unit/test_v2_reflection_cycle.py` | uses V1 stand-in (54-296) | §7.B.1 reflection_cycle 改造时改写 |
| `tests/integration/test_v2_cross_session_memory_e2e.py:70` | 注释提到 MemoryExtractor | 改注释 |
| `tests/integration/test_v2_ui_endpoint_smoke.py` | 可能 hit /memory/unified_query | §7.B.4 router 删时同步改 |

---

## I. 纯注释 / docstring（无 import，无运行时影响）

| 位置 | 修法 |
|---|---|
| `xmclaw/core/bus/events.py:157, 175, 176` | EventType 注释提到 UnifiedMemorySystem / MemoryExtractor — §7.A.3 末统一改 |
| `xmclaw/utils/paths.py:303` | graph index 路径注释 — §7.B.4 删 |
| `xmclaw/providers/memory/sqlite_vec.py:303` | 提 `UnifiedMemorySystem.put` — §7.B.4 整文件删 |
| `xmclaw/memory/v2/__init__.py:20-23` | 历史注释（"kept untouched ... until Phase 5 swap"）— §7.A.5 改写说明迁移已完成 |
| `xmclaw/memory/v2/llm_extractor.py:115` | 提 MemoryExtractor 超时类比 | §7.B.4 改 |
| `xmclaw/daemon/app_lifespan.py:1635` | 注释 | §7.A.3 改 |

---

## §7.A.2 必须补的 V2 shim 清单（P0 阻塞 §7.A.3）

启动 §7.A.3 callsite 迁移前，V2 `MemoryService` 必须先有这些 API（即使内部转发到 V1）：

| # | API | 当前 V2 状态 | shim 实现策略 |
|---|---|---|---|
| 1 | `recall(query, k, time_range=(start, end), kinds=, scopes=, layers=)` | 缺 `time_range` 参数 | §7.A.2 加参数；shim 转发到 `self._legacy_unified.query(temporal=TimeRange(start, end))` |
| 2 | `remember(text, kind, scope, layer="procedural", ...)` | `FactLayer` 没 procedural 值 | §7.A.2 加 enum 值；shim 把 `layer=procedural` 落到 `self._legacy_unified.put(layer="procedural")` |
| 3 | `delete(fact_id)` | 缺 | §7.A.2 加 LanceDB merge_insert 删除 + shim 转发删 V1 副本 |
| 4 | `query_layer(layer, limit, since=None)` for reflection_cycle | 缺 | §7.A.2 加；V2 端 = filter `layer == layer`，shim 调 V1 short_term walk |
| 5 | `MemoryServiceWriteError(indices_written, compensated)` | 缺 | §7.A.2 mirror V1 `UnifiedWriteError`；shim 期捕 V1 异常再 raise V2 |
| 6 | `node_type → kind` mapping table | 不存在 | §7.A.2 在 v2/service.py 加 `_LEGACY_NODE_TYPE_TO_KIND` dict + helper |
| 7 | hop_loop 的"抽取 → 决定 put 还是不 put"管道适配 | V2 是 fire-and-remember，不返回 candidate fact | §7.A.2 暴露 `LLMFactExtractor.extract_candidates(...)` 返回未 remember 的 candidate list，让 hop_loop 自己控时机（保留 background pattern）|

---

## §7.A.3 callsite 迁移顺序（按依赖 + 风险排序）

启动 §7.A.2 之后，按这个顺序逐个 commit：

1. **`xmclaw/daemon/factory.py`** — 入口收窄。停止构造 `UnifiedMemorySystem` / `MemoryExtractor`，只构造 `MemoryService` 实例（仍带 `_legacy_unified` 内部桥）
2. **`xmclaw/daemon/app_lifespan.py`** — 注入路径切换；删除 `_app.state.unified_memory` 等冗余字段
3. **`xmclaw/daemon/agent_loop.py`** — `_unified_memory.query` → `_memory_service.recall`
4. **`xmclaw/daemon/hop_loop.py`** — `_memory_extractor.extract` + `_unified_memory.put` → `_memory_service.{extract_candidates, remember}` 二步
5. **`xmclaw/cognition/reflection_cycle.py`** — `unified_memory` 参数改名 + 走 `query_layer` shim
6. **`xmclaw/daemon/routers/memory.py`** — `/unified_query` / `/unified_put` 改 handler 内部走 V2 service（URL 保留）
7. **测试一批改一批**：每个生产文件迁移的同一 commit 内带它的单测/集成测试改写

---

## §7.A.4 桥接退役

`xmclaw/daemon/app_lifespan.py:2218-2257` 整段（BuiltinTools hot-wire）在 §7.A.3 完成后删除 + `BuiltinTools.set_memory_v2_service()` rename → `set_memory_service()`。

---

## §7.A.5 收口

`xmclaw/memory/__init__.py` 改为：

```python
from xmclaw.memory.v2 import (
    Fact, FactKind, FactLayer, FactScope,
    MemoryService, RecallHit, Relation, RelationKind,
)
__all__ = [...]
```

老符号 `UnifiedMemorySystem` / `MemoryExtractor` / `mint_unified_id` / `UnifiedWriteError` / `MemoryEntry` / `TimeRange` / `ExtractedFact` / `TriggerKind` **不再 re-export**。

`unified.py` / `_id.py` / `extractor.py` 加 module-level `DeprecationWarning`，等 §7.B.4 物理删除。

---

## 估算

- §7.A.2 shim 补 API：**1-2 天**（7 个 P0 + 单测）
- §7.A.3 callsite 迁移：**2-3 天**（6 个生产文件，每个一 commit + 测试）
- §7.A.4 + §7.A.5：**0.5 天**

阶段 1 合计 **3.5-5.5 天**，符合 JARVIS_PLAN Phase 7 的"~1 周"估计。

---

## 风险记录

- **R1（agent_loop 上线 SLA）**: agent_loop 是热路径，迁移期 V2 shim 转发 V1 时多一跳函数调用。可接受（< 0.1ms）。
- **R2（reflection_cycle 语义漂移）**: V2 无 short_term 层。短期解：working + ts 过滤；长期解（§7.B.1）：要不要复活 short_term 还是改 reflection_cycle 语义直接读 working 由 §7.B 阶段讨论。
- **R3（前端 URL 兼容）**: `/memory/unified_query` 暂留，§7.B 末才删。如果有外部脚本依赖此端点，§7.B 前发 deprecation 通知。
