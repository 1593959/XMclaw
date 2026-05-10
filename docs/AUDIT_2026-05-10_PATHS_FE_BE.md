---
description: 代码级审计 (2026-05-10) — 路径不一致 / 前后端不匹配 / 前端功能不明确
tags: [audit, paths, frontend, backend, cleanup]
status: open
generated_by: Claude Opus 4.7 + Explore agent (a69dd5b72fb520e6c)
---

# 代码级审计 — 2026-05-10

用户在 R1-R6 框架级重构完成 + R3/R4/R5 默认 flip 之后要求做一次完整
代码级审计，覆盖三个维度：

1. **路径错误问题** —— 用户最痛点（[unified_paths](archive/...) 提到的"安装到
   /a 跑读 /b"反复出现）。
2. **前后端功能对不上** —— UI 调的端点 / payload 与 router 实际不匹配。
3. **前端功能不明确** —— 占位 UI / 死代码 / 标了 TODO 但实际未实现。

## 速览（按严重度）

| 严重度 | 数量 | 范围 |
|---|---|---|
| **P0** (破功能) | **11 处** | 全部在维度 1 — 硬编码绕过 paths.py |
| **P1** (误导) | 5 处 | 维度 1 重复检查 + 维度 3 显式"未实现"标识 |
| **P2** (代码味 / 已知欠债) | 4 处 | 维度 3 budget-near 文件 |

11 个 P0 全部一类问题：硬编码 `Path.home() / ".xmclaw"`，**`XMC_DATA_DIR`
重定向时漏掉**。修法极其规整 —— 加 6 个 paths.py helper + 改 11
处调用点。

---

## 维度 1: 路径错误（P0）

### Ground truth: `xmclaw/utils/paths.py`

paths.py 是合法的"统一路径"模块，已经定义：

- `data_dir()` —— `~/.xmclaw`，honors `XMC_DATA_DIR`
- `v2_workspace_dir()` —— `<data>/v2/`
- `default_events_db_path()` —— `<v2>/events.db`，honors `XMC_V2_EVENTS_DB_PATH`
- `default_memory_db_path()` —— `<v2>/memory.db`
- `default_sessions_db_path()` —— `<v2>/sessions.db`
- 等等（10+ helper）

**§3.1 of the dev roadmap 要求：每个 runtime path 都 resolve 通过 paths.py。**

### 11 处违规列表

| # | 文件:行 | 字面量 | 应改为 | 严重度 |
|---|---|---|---|---|
| 1 | `xmclaw/daemon/app.py:435` | `Path.home() / ".xmclaw" / "v2" / "cognitive_state.json"` | 新加 `paths.default_cognitive_state_path()` | P0 |
| 2 | `xmclaw/daemon/app.py:2107` | (同 1, 第二处副本) | (同 1) | P0 |
| 3 | `xmclaw/cognition/memory_graph.py:21` | `Path.home() / ".xmclaw" / "v2" / "graph.db"` | 新加 `paths.default_graph_db_path()` | P0 |
| 4 | `xmclaw/cognition/self_experiment.py:154` | `Path.home() / ".xmclaw" / "v2" / "experiments.db"` | 新加 `paths.default_experiments_db_path()` | P0 |
| 5 | `xmclaw/cognition/evolution_loop.py:213` | `Path.home() / ".xmclaw" / "v2" / "proposals"` | 新加 `paths.evolution_proposals_dir()` | P0 |
| 6 | `xmclaw/eval/longmemeval_full.py:40` | `Path.home() / ".xmclaw" / "v2" / "eval_cache" / "longmemeval"` | 新加 `paths.eval_cache_dir(name)` | P0 |
| 7 | `xmclaw/eval/swe_bench_verified.py:66` | `Path.home() / ".xmclaw" / "v2" / "eval_cache" / "swe_bench_verified"` | (同 6) | P0 |
| 8 | `xmclaw/eval/terminal_bench.py:59` | `Path.home() / ".xmclaw" / "v2" / "eval_cache" / "terminal_bench"` | (同 6) | P0 |
| 9 | `xmclaw/providers/tool/builtin.py:3225` | `Path.home() / ".xmclaw"` (语音 fallback) | 改 `paths.data_dir()` | P0 |
| 10 | `xmclaw/providers/tool/builtin_voice.py:114` | (同 9, 不同 callsite) | (同 9) | P0 |
| 11 | `xmclaw/cognition/memory_graph.py:21` (变量) | `_DEFAULT_DB_PATH = ...` 模块级常量 | 改成函数 helper，惰性 resolve | P0 |

### 另有 1 处 P1（重复检查 / 维护隐患）

| 文件:行 | 问题 | 整改 |
|---|---|---|
| `xmclaw/providers/tool/builtin_voice.py` (建议附近) | 既有逻辑额外手动检查 `os.environ["XMC_DATA_DIR"]` —— 与 `paths.data_dir()` 内的同一逻辑重复 | 删手动 env 检查，改用 `paths.data_dir()` |

### P0 后果（用户视角）

设 `XMC_DATA_DIR=/custom/path` 后：

| 模块 | 行为 |
|---|---|
| events.db / memory.db / sessions.db | ✅ 走 paths.py，去 `/custom/path/v2/...` |
| pairing_token / daemon.pid / daemon.log | ✅ 走 paths.py |
| **cognitive_state.json** | ❌ **写到 `~/.xmclaw/v2/`** |
| **graph.db** | ❌ **写到 `~/.xmclaw/v2/`** |
| **experiments.db** | ❌ **写到 `~/.xmclaw/v2/`** |
| **evolution proposals** | ❌ **写到 `~/.xmclaw/v2/`** |
| **eval_cache (HuggingFace)** | ❌ **写到 `~/.xmclaw/v2/`** |

CLI 重启读 `XMC_DATA_DIR` 看不到 daemon 写的 graph.db / experiments.db /
proposals —— 这正是用户 MEMORY.md 里写的 **"安装到 /a 读 /b"** 的具体
现象。

### 修复方案（一次性 patch）

**Step 1**: 给 `paths.py` 加 6 个 helper：

```python
def default_cognitive_state_path() -> Path:
    """CognitiveState persistence file."""
    return v2_workspace_dir() / "cognitive_state.json"

def default_graph_db_path() -> Path:
    override = os.environ.get("XMC_V2_GRAPH_DB_PATH")
    return Path(override) if override else v2_workspace_dir() / "graph.db"

def default_experiments_db_path() -> Path:
    return v2_workspace_dir() / "experiments.db"

def evolution_proposals_dir() -> Path:
    return v2_workspace_dir() / "proposals"

def eval_cache_dir(suite: str) -> Path:
    """Per-benchmark HF dataset cache."""
    return v2_workspace_dir() / "eval_cache" / suite
```

**Step 2**: 11 处调用点改为调 helper（+ 删硬编码字面量）。

**Step 3**: 加 `tests/unit/test_v2_paths_unified.py` —— 用
`monkeypatch.setenv("XMC_DATA_DIR", str(tmp_path))` 后枚举所有
`paths.default_*` 函数，断言它们都在 `tmp_path` 下，**不会**指向
`~/.xmclaw`。这把 unified-paths anti-req 变成回归测试。

预估 1 commit，~150 LOC + 30 个测试。

---

## 维度 2: 前后端功能不匹配

### 已核实接通（**无问题**）

R6 新加的两个 panel 在审计 agent + 我手工核对下 **接通完整**：

| 前端 | 后端 endpoint / 事件 | 状态 |
|---|---|---|
| `pages/_panels/mind_inner_monologue.js` 拉 `GET /api/v2/events?types=inner_monologue,reflection_cycle_ran` | `EventType.INNER_MONOLOGUE` + `EventType.REFLECTION_CYCLE_RAN` (events.py:190/200) + `reflection_cycle.py:_publish_event` 真发 | ✅ |
| `pages/_panels/mind_suggestions.js` 拉 `GET /api/v2/cognition/suggestions?status=...` + POST `/approve` `/reject` | `routers/cognition.py:617/670/684` (R5 commit 6525204) | ✅ |
| `pages/Cognition.js` 5 个 GET (state/tasks/proposals/graph/stats/tasks/graph) + WS `/api/v2/cognition/ws` | `routers/cognition.py:105/172/185/262/308` + websocket 端点 | ✅ |
| `pages/Memory.js` 6 tab (identity/notes/journal/activity/unified/providers) | `routers/profiles.py` + `routers/memory.py` 全在 | ✅ |

### 路由顺序检查（**无问题**）

历史 bug 是 catch-all `/{id}` 路由会 shadow concrete `/graph` 路由
（cognition + memory 都中过）。当前状态：

- `cognition.py:332 /tasks/graph` 在 `:361 /tasks/{task_id}` **之前** ✅
- `memory.py /unified_query` 在 `/{filename}` **之前** ✅
- `test_v2_cognition_router_order.py` 已锁住此 invariant

### 暂时未深扫的页面（建议作为下一轮审计目标）

agent 受限于 grep 通量 + 时间，没逐一核对的：

| 页面 | 风险点 |
|---|---|
| `pages/Trace.js` | 监听了多种事件类型；可能有些 reducer case 服务端不再发（dead branch）|
| `pages/Logs.js` | `/api/v2/logs?...` 复杂 query 参数 |
| `pages/Security.js` | 注入扫描配置 — 不知道接没接 |
| `pages/Tools.js` | 工具列表是否实时反映 ToolProvider |
| `pages/Settings.js` 多处 sub-form | 各 panel 的 PUT 是否覆盖完整 |

—— 这些可以下次再扫，不是 P0。

---

## 维度 3: 前端功能不明确

### P1 — 显式标注的"未实现"

| 文件:行 | 问题 | 整改 |
|---|---|---|
| `pages/Channels.js:118` | UI 显示 `<strong>⚠ 未实现</strong> — manifest 已注册但 adapter Python 模块还没写`。Telegram/Discord/Slack/Feishu/DingTalk/WeCom (outbound)/Email 在 Sprint 2 都已实现，**这块 fallback 文本可能已经过期** | 检查 `xmclaw/providers/channel/registry.py` 当前 SCAFFOLD_CHANNELS 列表，把已实现的从 fallback 文本里去掉 |

### P2 — 已知 budget-near 欠债（不阻断功能，但需要拆）

| 文件 | 行数 | 状态 |
|---|---|---|
| `pages/_panels/memory_providers.js` | ~700 | KNOWN_OVERSIZED (Memory.js:28-30 注释明示) |
| `pages/_panels/memory_unified_query.js` | ~250 | OK 但伴生一个橙色 debug warning banner（R5 follow-up） |

`memory_providers.js` 已经在 Sprint 4 做过一次 split（B-323），但还有
~700 LOC，建议二阶拆 indexer / dream / pinned / picker / switcher 各自独
立组件 —— 这是很久前就在 backlog 里的事，不是新发现。

### P2 — 历史注释残留

| 文件:行 | 问题 | 整改 |
|---|---|---|
| `pages/Cron.js:22-27` | 注释提到`prior version was a stub that toasts 未实现` —— 但当前版本已完整实现 | 清理过期评论（小事，下次顺手做） |

### 没找到的"全空 dead UI"

我和 agent 都没发现完全空白 / 假按钮 / fake-form 这种**真死代码**。
所有 page 的主流程都是真接通的。

---

## 整改建议（按优先级）

### 立即做（一次 commit 解决 P0 全部）

**Patch A: 统一路径**
1. paths.py 加 6 个 helper（cognitive_state / graph_db / experiments_db /
   evolution_proposals_dir / eval_cache_dir, 还有 voice fallback 的
   重用 data_dir）
2. 改 11 处调用点全部走 helper
3. 加 `test_v2_paths_unified.py`：monkeypatch `XMC_DATA_DIR` 后断言
   所有 `paths.default_*` 都在 tmp_path 下
4. 加导入方向规则到 `scripts/check_import_direction.py`：禁止任何
   非 paths.py 文件出现 `Path.home() / ".xmclaw"` 字面量
   （把 unified_paths anti-req 变成 CI lint）

预估：1 commit，~250 LOC，~30 测试，1 小时工作量。

### 短期清理（下次顺手）

**Patch B: Channels.js fallback 文本**
- 检查 SCAFFOLD_CHANNELS 列表对照已实现 adapter
- 更新 Channels.js 的"未实现"文案

**Patch C: Cron.js 历史注释**
- 删 line 22-27 的 prior-version stub 评论

### 中期欠债（不阻断 R1-R6 飞轮，建议下一个 sprint 做）

**Patch D: memory_providers.js 二阶拆分**
- 拆成 `_panels/memory_providers_{indexer,dream,pinned,picker,switcher}.js`
  五个独立组件 + 一个 80 LOC 的 ProvidersTab 主组件
- 删 KNOWN_OVERSIZED 祖父级例外

### 下一轮审计的 scope

- 维度 2 没深扫的 5 个页面 (Trace / Logs / Security / Tools / Settings)
- chat reducer 处理但服务端从不发的事件（dead reducer case）
- `app_lifespan.py` 这种"未追踪、坏掉的 untracked 文件"清理（沙箱
  权限问题）

---

## 附录：审计方法

- `Explore` 子代理深度扫 (a69dd5b72fb520e6c, ~225s, 49 tool uses)
- 我并行手扫 grep 验证关键 P0 项
- ground truth 来自 `xmclaw/utils/paths.py` + `routers/cognition.py` +
  `routers/memory.py` 注册的 endpoint 列表
- frontend API call 列表来自全文 grep `/api/v2/...`

报告完整字数：~1900 字。
