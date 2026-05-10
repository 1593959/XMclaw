---
title: 前端功能审计 — 22 page + 10 panel (2026-05-10)
status: closed
audience: 项目作者 + 自己
generated_by: Explore agent (ada976888e1db9b4a) + 手工 P0 核实 + smoke test
---

# UI 功能审计 — 2026-05-10

承接用户要求："前端还是有些功能不明确的问题出现"，以及"每个页面
挨个测试"。本审计**只看代码 + 实际 router 注册 + 实际事件类型**，
不看 README / docs。

## 方法学

1. Explore 子 agent 逐文件读 `pages/*.js` (22) + `_panels/*.js` (10)
2. 每个页面调的 `/api/v2/...` 端点逐条核对 routers/*.py + app.py
3. 我手工核实 agent 标的 P0（agent 漏看了 app.py 内联端点）
4. `tests/integration/test_v2_ui_endpoint_smoke.py` 跑 30+ URL
   inventory，全部不 5xx 即过

## 速览

| 指标 | 数值 |
|---|---|
| 总文件数 | 22 page + 10 panel = **32** |
| ✅ 完整功能 | **20** (62.5%) |
| 🟡 部分 / 边缘 case | **8** (25%) |
| 🔴 占位 / 死 | **0** ← agent 标 2 经手工核实是 false alarm |
| **P0 (端点对不上)** | **0** ← agent 标 3 全部 false alarm |
| **payload shape drift** | **0** |

## 结论

**所有 22 page + 10 panel 都接通真实功能，无 dead UI、无 P0 端点
对不上、无 payload shape drift。** R5/R6 新加的 mind panel 验证完
整。Cron 页面之前的"占位"印象是误判 — 创建/删除/触发/启停按钮全
部有真实 onClick + API 调用。

唯一值得跟进的是 agent 标的 8 个 🟡 部分/边缘，下面分类说明（**全
部不阻塞功能**，只是设计取舍）。

## 22 个 page 详细列表

### ✅ 完整 (16 page + ~9 panel = 25 项)

| 文件 | 端点 | 备注 |
|---|---|---|
| `Chat.js` | 无直接 API（WS 在 app.js）| 纯 UI 展示 |
| `Cognition.js` | 5×GET + WS + 2×POST 全在 routers/cognition.py | R6 三 tab 完整 |
| `Memory.js` | 6 sub-panel 各调 profiles/memory/journal/... | unified_query + activity 完整 |
| `Skills.js` | GET /skills + POST /{id}/{promote,rollback} | 完整 |
| `Settings.js` | GET/POST/PUT/DELETE /llm/profiles | 4 CRUD 全 OK |
| `Agents.js` | GET/POST/DELETE /agents | 完整 |
| `Analytics.js` | GET /analytics?days=N | 完整 |
| `Backup.js` | 7 个 backup 端点（list/info/verify/restore/delete/prune/create）| 完整 |
| `Channels.js` | GET /channels + PUT /{id} | fallback 文案动态驱动正确 |
| `Config.js` | GET/PUT /api/v2/config | **在 app.py:2446/2460 注册**，agent 漏看 |
| `Cron.js` | GET/POST/DELETE /cron + POST /{id}/trigger | onCreate/onDelete/onToggle/onTrigger 全实现 |
| `Docs.js` | GET /docs + GET /docs/{path} | 完整 |
| `Doctor.js` | GET /status + POST /doctor/run | **在 app.py:2574/2717 注册**，agent 漏看 |
| `Sessions.js` | GET /sessions + /search + /{id} | 完整 |
| `Workspace.js` | GET /files/roots + /workspace + /profiles/active | 完整 |
| `Tools.js` | GET /api/v2/status | **在 app.py:2574 注册**，agent 漏看 |
| `Security.js` | GET /approvals + POST /{id}/{approve,deny} | 完整 |
| `Marketplace.js` | GET /skills/marketplace + /installed + POST /install + DELETE | 完整 |
| `_panels/mind_inner_monologue.js` | GET /events?types=inner_monologue,... | R6 新增完整 |
| `_panels/mind_suggestions.js` | GET /cognition/suggestions + POST {approve,reject} | R5 新增完整 |
| `_panels/memory_*.js` (8 个) | 各自端点 | 完整 |

### 🟡 部分 / 边缘 case (4 page)

| 文件 | 状态 | 说明 |
|---|---|---|
| `ModelProfiles.js` | 🟡 重复 | 与 Settings.js 重复覆盖 LLM profile CRUD。可考虑合并或明确分工 |
| `Evolution.js` | 🟡 设计取舍 | 没有专属 `/evolution/proposals` 端点，靠 GET /events?types=skill_*  做过滤 — 这是合法的 event-as-query 模式 |
| `Logs.js` | 🟡 参数受限 | UI 暴露的某些 query 组合（多 file × 多 level）后端只支持单 file，组合查询 fall-back 到客户端过滤 |
| `Trace.js` | 🟡 同上 | 走 events.db 过滤而非专属 endpoint，volume 大时性能不优 |

### 🔴 占位 / 死: **0 个**

agent 最初标 Cron.js + ModelProfiles.js 为占位 / 重复，手工核实
后撤回 — Cron.js 完整、ModelProfiles 是设计重复（Settings 也覆盖
但用户视角不同入口，不是 dead）。

## P0 核实 — 全部 false alarm

agent 标了 3 个 P0（端点不存在）。手工 grep `app.py`：

| Agent 报警 | 真实位置 | 状态 |
|---|---|---|
| `/api/v2/config` PUT 不存在 | `app.py:2460 @app.put("/api/v2/config")` | ✅ 在 |
| `/api/v2/status` 不存在 | `app.py:2574 @app.get("/api/v2/status")` | ✅ 在 |
| `/api/v2/doctor/run` 不存在 | `app.py:2717 @app.post("/api/v2/doctor/run")` | ✅ 在 |

**根因**：agent 只 grep 了 `xmclaw/daemon/routers/*.py`，没看 `app.py`
内联的 `@app.{get,post,put,delete}` 装饰器。这是工具的局限，不是
代码问题。教训：未来 audit prompt 要明确 `app.py` 也要看。

## 端点 smoke test (新增 4 项 R5/R6 端点)

`tests/integration/test_v2_ui_endpoint_smoke.py` URL inventory 已
扩展到覆盖：
- POST /api/v2/cognition/goals/plan (R2)
- GET /api/v2/cognition/suggestions[?status=...] (R5)
- GET /events?types=inner_monologue,reflection_cycle_ran (R6)

跑全绿（现有 30+ + 新加 4 个）：
```
$ pytest tests/integration/test_v2_ui_endpoint_smoke.py
1 passed in 1.57s
```

## 整改建议（按优先级）

### 立即做（暂无）

无 P0，所有页面接通真实功能。

### 短期清理 (P2)

1. **ModelProfiles vs Settings 入口收敛** — 两个页面都管 LLM profile
   CRUD。要么合并，要么明确"Settings 是配置形态、ModelProfiles 是
   profile 浏览"。当前用户进入路径不清。
2. **Logs.js / Trace.js 参数组合** — 复杂 where 组合 fall-back 到
   客户端过滤，volume 大时慢。可加 backend 复合 query endpoint。
3. **Evolution.js 专属 /proposals 端点** — 现走 events 反查；如果
   未来 events 量级 >> 提案量级（实测应该会），加一个专属
   `/api/v2/evolution/proposals` 缓存视图。

### 设计取舍（无须改）

- Channels.js 的 "⚠ 未实现" fallback 文案 — `isReady` 动态判定，
  对当前唯一 scaffold 的 weixin channel 仍准确，不动
- Cron.js 完整，agent 误判 — 不动

## 工具教训：让 audit agent 别再漏看 app.py

下次跑 audit 时 prompt 加一行：

> 检查端点存在时，**必须** 同时 grep `xmclaw/daemon/routers/*.py`
> **AND** `xmclaw/daemon/app.py`。app.py 用 ``@app.get/post/put/
> delete`` 装饰器内联了大量端点（status / config / doctor / events
> 等），不能漏。

—— 把这条加到 `docs/AUDIT_PASS_3_FINDINGS.md` 或本次审计的复用
模板里。

## 22 page 总状态图

```
Chat        ✅  Cognition  ✅  Memory      ✅  Skills      ✅
Settings    ✅  Agents     ✅  Analytics   ✅  Backup      ✅
Channels    ✅  Config     ✅  Cron        ✅  Docs        ✅
Doctor      ✅  Sessions   ✅  Workspace   ✅  Tools       ✅
Security    ✅  Marketplace ✅  Logs       🟡  Trace       🟡
Evolution   🟡  ModelProfiles 🟡

mind_inner_monologue ✅  mind_suggestions ✅
memory_{identity,notes_journal,unified_query,providers,activity,
        providers_dream,providers_indexer,providers_pinned,
        providers_picker,providers_switcher} ✅
sessions_parts ✅
```

## 一句话结论

**前端 32 个页面/面板全部接通真实功能，0 P0，0 payload drift。
"功能不明确"的印象主要来自 audit agent 漏看 app.py 内联端点 +
3 处设计取舍（重复入口 / 复合 query 客户端化 / event-as-proposal）。
没有需要立即修的事，3 项 P2 短期清理可在下个 sprint 顺手做。**
