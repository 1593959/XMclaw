# XMclaw 整改修复完善开发计划（Remediation Plan v2.0）

> **基于**: `docs/COMPREHENSIVE_TRIPARTITE_AUDIT_2026.md` 完整 28 章审计结论  
> **制定日期**: 2026-05-29  
> **目标版本**: v2.0（Phase 0-1 完成后）/ v2.1（Phase 2 完成后）/ v2.2（Phase 3 完成后）/ v3.0（Phase 4 完成后）  
> **总工作量估算**: Phase 0 = 1-2 周，Phase 1 = 4-6 周，Phase 2 = 8-10 周，Phase 3 = 12-16 周，Phase 4 = 16-20 周

---

## 执行摘要

本计划覆盖审计报告 **28 章** 中识别的全部 **127 项具体不足**，按五阶段推进：

| 阶段 | 主题 | 时间 | 关闭差距数 | 关键交付 |
|------|------|------|-----------|----------|
| **Phase 0** | 紧急 Bug 修复 | 1-2 周 | 8 | CI 全绿、session persistence 恢复、测试全过 |
| **Phase 1** | 核心 Wiring + 稳定性 | 4-6 周 | 23 | CognitiveDaemon 活跃、platform guidance 生效、默认值正确、错误处理增强 |
| **Phase 2** | 生态位关键缺失 | 8-10 周 | 35 | OpenAI API、配对安全、3 新通道、Docker 隔离、Schema 验证、Fallback provider |
| **Phase 3** | 竞争力追赶 | 12-16 周 | 38 | ACP IDE、自主技能、Provider 扩展、部署运维、Web UI 重写、监控体系 |
| **Phase 4** | 差异化放大 | 16-20 周 | 23 | RL 环境、多 Agent 路由、语音能力、技能市场、自动化发布 |

**当前最阻碍交付的 8 个 Bug**（Phase 0 必须修复）：

| # | Bug | 影响 | 文件 | 紧急度 |
|---|-----|------|------|--------|
| B-1 | `AgentLoop.run_turn()` 从不调用 `session_store.save()` | Session 历史无法持久化 | `agent_loop.py` | 🔴 P0 |
| B-2 | `AgentLoop.pop_last_turn()` 调用不存在的 `.put()` | `/undo` 命令崩溃 | `agent_loop.py` | 🔴 P0 |
| B-3 | `build_system_prompt()` 的 `channel_name` 参数调用方全部遗漏 | Platform guidance 完全失效 | `factory.py`, `channel_dispatcher.py` | 🔴 P0 |
| B-4 | `autonomy_level` 默认值未确认（可能为 0） | Fresh install = 普通聊天机器人 | `factory.py`, `config.example.json` | 🔴 P0 |
| B-5 | `evolution.auto_apply` 可能仍为 false | 技能无法自动晋升 | `config.example.json` | 🔴 P0 |
| B-6 | CognitiveDaemon 实例化但 AgentLoop 未引用 | 感知-推理-规划闭环不生效 | `agent_loop.py`, `factory.py` | 🔴 P0 |
| B-7 | 工具执行串行，无并发优化 | 多工具调用 wall-clock 时间翻倍 | `hop_loop.py` | 🟡 P1 |
| B-8 | `test_session_store.py` 2 测试失败 | CI 不通过，阻碍发布 | `test_session_store.py` | 🔴 P0 |

---

## 已完成的修复

> **2026-05-29 audit re-judgement**：原计划 28 项 Phase 0/1/2/3/4 大多已在
> `4e986ad` / `9eaa15e` / `95b0a98` / `bc731fd` 等近期 commit 中完成或
> 通过其他方式覆盖。本表追踪所有确认完成的项，剩余未完成的在下方
> 列出推荐做 / 不推荐做的判定。

| # | 修复项 | 状态 | commit / 文件 | 说明 |
|---|--------|------|----------|------|
| ✅ | **B-1 session_store.save 接线** | 已完成 | agent_loop.py:438, 768 (4e986ad) | run_turn 结束后通过 asyncio.to_thread 异步持久化。 |
| ✅ | **B-2 pop_last_turn .put → .save** | 已完成 | agent_loop.py:405-446 (4e986ad) | pop_last_turn 改为 async，调 .save。 |
| ✅ | **B-3 platform guidance / channel_name** | 已完成 | agent_loop.py:725, 751, 804, 1967 (4e986ad) | run_turn 接 channel_name + frozen-prompt cache key 三元组包含通道。 |
| ✅ | **B-4 autonomy_level default 50** | 已完成 | cognition.continuous_loop.autonomy_level=50 (config.example.json) | 默认值在 config.example 中明确为 50。 |
| ✅ | **B-5 evolution.auto_apply default true** | 已完成 | config.example.json (4e986ad 提交日志确认) | 默认 true。 |
| ✅ | **B-6 CognitiveDaemon ↔ AgentLoop 接线** | 已完成 | agent_loop.py:148, 151, 190 (4e986ad) | AgentLoop.__init__ 接 cognitive_daemon 参数。 |
| ✅ | **B-7 工具读并发** | 已完成 | hop_loop.py:889 (4e986ad) | asyncio.gather 并发 read 类工具。 |
| ✅ | **P1-2 / P2-5 fallback provider** | 已完成 | llm_registry.py:115 fallback_chain | LLMRegistry 支持 fallback_chain，429/5xx 时按顺序降级。 |
| ✅ | **P1-4 Schema validation**（本次）| 已完成 | **新 xmclaw/daemon/config_schema.py** + factory.load_config 接入 (本 commit) | 启动时跑静态 validator，autonomy/port/timeout/types 越界一次性聚合报错。20 测试覆盖。 |
| ✅ | **P3-4 部署模板** | 已完成 | Dockerfile + docker-compose.yml + deploy/systemd + deploy/launchd + deploy/fly | 4 种部署目标都有模板。 |
| ✅ | **P4-3 voice 基础** | 已完成 | xmclaw/providers/voice/{whisper,edge_tts}.py | STT (Whisper) + TTS (Edge TTS) 基础版可用；唤醒词 / RL 调优在 P4 完整版未做。 |
| ✅ | **P4-5 release pipeline** | 已完成 | .github/workflows/{release,python-publish,docker-publish,python-ci}.yml | tag 触发 release，PyPI + Docker 自动发布。 |
| ✅ | **OpenRouter 模型目录自动拉取 (B-387)** | 已完成 | _openrouter_discovery.py, _provider_profiles.py, cost.py, openrouter.py | 新增 _openrouter_discovery 模块，从 https://openrouter.ai/api/v1/models 自动拉取模型元数据（context_length + pricing），TTL 24h 缓存。get_model_context_length() 和 lookup_pricing() 现在优先查询动态缓存，回退到静态表。 |

## 重新判定 —— 剩余项推荐做 / 不推荐做

| # | 项 | 工作量 | 推荐 | 理由 |
|---|---|---|---|---|
| **B-8** | test_v2_persona_timeout 1 个 pre-existing 故障 | 1h | **推荐** | `_SlowStore.set_manual()` kwarg 不匹配，跟 audit 无关，但是 CI 红点 |
| **P1-1** | Planner / ReasoningEngine 接 turn 流程 | 1 周 | **推荐**（但拆小做）| cognitive_daemon 已接，Planner 进 turn 是下一步 |
| **P1-3 / P3-6** | Prometheus metrics + tracing | 2-3 周 | **推荐**（小步）| 可以先做 `/metrics` 基础导出，~150 LOC，本地观察用 |
| **P2-1** | OpenAI compat API `/v1/chat/completions` | 2-3 周 | **推荐** | 生态位关键，Continue/Cursor 零改动接入 |
| **P2-2** | 设备配对 HMAC + JWT | 2 周 | **不推荐**（暂时）| 当前 pairing-token + 127.0.0.1 binding 已够本地用 |
| **P2-3** | WhatsApp / Signal / iMessage 通道 | 4-6 周 × 3 | **不推荐**（暂时）| 8 个通道已是行业天花板水平 |
| **P2-4** | Docker sandbox runtime（区分于 daemon Dockerfile）| 1-2 周 | **可选** | 真正想跑陌生 skill 才需要；现有 allowed_dirs 已是初级 sandbox |
| **P3-1** | ACP + VS Code 扩展 | 4-6 周 | **不推荐**（暂时）| stub 已存在，未必比 Cline + xmclaw OpenAI API 接入更好 |
| **P3-2** | 自主技能创建 | 4-6 周 | **不推荐**（暂时）| SkillDreamCycle 已 draft，等真实使用数据 |
| **P3-3** | Ollama / xAI / DeepSeek native provider | 2-3 周 | **不推荐** | 全部走现有 anthropic + openai-compat shim 已可用，重复造轮 |
| **P3-5** | WebUI 重写为 React + TS | 2-3 月 | **不推荐**（暂时）| Preact+htm 够用，重写收益 vs 成本不划算 |
| **P4-1** | RL 训练环境 | 2-3 月 | **不推荐**（暂时）| evolution 真实数据未积累 |
| **P4-2** | 多 Agent 完整路由 | 2-3 月 | **可选** | sub-agent / workspace 概念架构在，启用需要单独 ADR |
| **P4-4** | 技能市场 server-side | 1-2 月 | **可选** | 当前 GitHub-backed catalog 够 MVP；服务端要等用户量 |


---

## Phase 0: 紧急 Bug 修复（1-2 周）

> **目标**: 修复阻止现有功能正常运行的代码级缺陷，确保 CI 全绿。

### B-1: 修复 Session Persistence 完全失效

**问题诊断**:
- `AgentLoop.run_turn()` 在 turn 结束后**从未调用 `session_store.save()``
- 历史只存在于 `_histories` 内存 dict，daemon 重启后全部丢失
- `test_agent_loop_persists_after_each_turn` 因此失败（store.load 只返回 2 条而非 4 条）

**修复方案**:

```python
# FILE: xmclaw/daemon/agent_loop.py
# 在 _run_turn_inner 的最后（assistant text 返回后或 max_hops 耗尽后）
# 找到 return AgentTurnResult(...) 之前，添加：

        # B-1 fix: persist session history after each turn
        if self._session_store is not None:
            try:
                history = self._histories.get(session_id, [])
                self._session_store.save(session_id, history)
            except Exception:  # noqa: BLE001
                _log.warning("session.save_failed", session_id=session_id)
```

**目标文件**: `xmclaw/daemon/agent_loop.py`  
**工作量**: 2 小时  
**验收标准**:
- [ ] `pytest tests/unit/test_session_store.py::test_agent_loop_persists_after_each_turn` 通过
- [ ] 新 turn 后 SQLite `session_history` 表中 history_json 包含完整消息列表
- [ ] `xmclaw chat --resume <id>` 能恢复跨 daemon 重启的完整历史

---

### B-2: 修复 `pop_last_turn()` 调用不存在的方法

**问题诊断**:
- `AgentLoop.pop_last_turn()` line 423 调用 `self._session_store.put(session_id, kept)`
- `SessionStore` 只有 `save()` / `load()` / `delete()`，**没有 `put()`** 方法
- 当用户调用 `/undo` 时，此方法被触发，会抛出 `AttributeError`

**修复方案**:

```python
# FILE: xmclaw/daemon/agent_loop.py
# Line 423: 更名
            self._session_store.save(session_id, kept)  # was .put()
```

**目标文件**: `xmclaw/daemon/agent_loop.py:423`  
**工作量**: 10 分钟  
**验收标准**:
- [ ] `/undo` 命令不再抛出 `AttributeError`
- [ ] undo 后 session_store 中历史正确截断

---

### B-3: Platform Guidance Wiring

**问题诊断**:
- `assembler.py` 已新增 `channel_name` 参数（支持 10 通道渲染指导）
- `factory.py:735` 和 `factory.py:1867` 两处调用均未传入 `channel_name`
- `channel_dispatcher.py:206` 调用 `agent.run_turn()` 时也未传递通道信息
- 结果：所有通道的渲染指导（Telegram HTML 格式、Feishu Card 格式等）**完全未注入 system prompt**

**修复方案**:

```python
# FILE: xmclaw/daemon/channel_dispatcher.py
# _handle_one() line 206:
        await agent.run_turn(
            session_id, msg.content,
            user_images=user_images_arg,
            channel_name=msg.target.channel,  # ← 新增
        )
```

```python
# FILE: xmclaw/daemon/agent_loop.py
# run_turn() 签名
async def run_turn(
    self, session_id: str, user_message: str,
    *, user_images: tuple[str, ...] | None = None,
    channel_name: str | None = None,
) -> AgentTurnResult:
    # ... 传给 _run_turn_inner
```

```python
# FILE: xmclaw/daemon/agent_loop.py
# _run_turn_inner 中构建 system prompt 时
        system_prompt = self._build_system_prompt(
            session_id=session_id,
            channel_name=channel_name,  # ← 新增
        )
```

```python
# FILE: xmclaw/daemon/factory.py
# Line 735: WebSocket handler 路径
            new_prompt = build_system_prompt(
                profile_dir=profile_dir,
                workspace_dir=ws_root,
                channel_name=None,  # REPL 无特定通道
            )
```

**目标文件**: `channel_dispatcher.py`, `agent_loop.py`, `factory.py`  
**工作量**: 4 小时  
**验收标准**:
- [ ] Telegram 消息中 system prompt 包含 "Telegram 使用 HTML parse_mode..."
- [ ] Feishu 消息中 system prompt 包含 "Feishu 使用 Interactive Card..."
- [ ] WebSocket (REPL) 消息中无平台指导（`channel_name=None`）
- [ ] 跨通道缓存隔离：切换通道后 cache key 变化

---

### B-4 & B-5: Factory 默认值审计与修复

**问题诊断**:
- `config.example.json` 中 `autonomy_level=50`，但 `factory.py` 的 fallback 默认值未确认
- `evolution.enabled` 在 v1.1 中已改为 true，但 `evolution.auto_apply` 可能仍为 false
- 若用户不配置 evolution，系统可能静默降级到 v1 行为

**修复方案**:

```python
# FILE: xmclaw/daemon/factory.py
# build_agent_from_config() 中添加默认值断言

cognition_cfg = cfg.get("cognition", {})
autonomy_level = cognition_cfg.get("autonomy_level", 50)  # 默认 50，不是 0
evolution_cfg = cfg.get("evolution", {})
evolution_enabled = evolution_cfg.get("enabled", True)
auto_apply = evolution_cfg.get("auto_apply", True)

log.info("config_defaults_applied", autonomy_level=autonomy_level,
         evolution_enabled=evolution_enabled, auto_apply=auto_apply)
```

```json
// FILE: daemon/config.example.json
{
  "cognition": {
    "autonomy_level": 50
  },
  "evolution": {
    "enabled": true,
    "auto_apply": true
  }
}
```

**目标文件**: `factory.py`, `daemon/config.example.json`  
**工作量**: 2 小时  
**验收标准**:
- [ ] 空配置启动时，日志显示 `autonomy_level=50`
- [ ] 空配置启动时，evolution 启用且 auto_apply=true
- [ ] `daemon/config.example.json` 中 auto_apply 明确设为 true
- [ ] 启动日志打印实际生效的配置摘要

---

### B-6: CognitiveDaemon 接线

**问题诊断**:
- `app_lifespan.py` 已实例化并启动 CognitiveDaemon
- 但 `AgentLoop` 只有 `_cognitive_state`（CognitiveState dataclass），没有 `_cognitive_daemon` 引用
- `AgentLoop.run_turn()` 中没有调用 CognitiveDaemon 的方法
- 结果：感知循环独立运行，但 AgentLoop 不响应其输出（proposals/goals）

**修复方案**:

```python
# FILE: xmclaw/daemon/agent_loop.py
# AgentLoop.__init__ 新增参数
    cognitive_daemon: Any | None = None,
# ...
    self._cognitive_daemon = cognitive_daemon
```

```python
# FILE: xmclaw/daemon/agent_loop.py
# _run_turn_inner 开始时（auto-recall 之后）
if self._cognitive_daemon is not None:
    pending = self._cognitive_daemon.pop_proposals_for(session_id)
    if pending:
        # 将 pending proposals 注入为前置提示
        proposal_note = "\n".join(f"- {p}" for p in pending)
        user_message = f"[系统提示：你有 {len(pending)} 个待处理事项]\n{proposal_note}\n\n{user_message}"
```

```python
# FILE: xmclaw/daemon/agent_loop.py
# run_turn 结束时
if self._cognitive_daemon is not None:
    self._cognitive_daemon.on_turn_completed(session_id, result)
```

```python
# FILE: xmclaw/daemon/factory.py
# build_agent_from_config() 中传入
cognitive_daemon=getattr(app_state, "cognitive_daemon", None),
```

**目标文件**: `agent_loop.py`, `factory.py`  
**工作量**: 2 天  
**验收标准**:
- [ ] `autonomy_level=50` 时，CognitiveDaemon 心跳日志正常输出
- [ ] 文件 watcher 触发 percept → goal → plan 链路在日志中可见
- [ ] AgentLoop 的 turn 中能看到 cognitive daemon 的 pending proposals
- [ ] `goal-from-percept-*` 会话被正确归类为 internal

---

### B-7: 工具并发支持

**问题诊断**:
- XMclaw 的 hop loop 串行执行工具：一个 tool call 完成后才发下一个
- Hermes 使用 `ThreadPoolExecutor` 并发执行多个独立工具
- 对于独立的读操作（如同时读取 3 个文件），串行浪费大量 wall-clock 时间

**修复方案**:

```python
# FILE: xmclaw/daemon/hop_loop.py
# 在 tool_calls 处理中，对读操作使用 asyncio.gather 并发

async def _invoke_tools(
    self, tool_calls: list[ToolCall], session_id: str
) -> list[ToolResult]:
    # 简单启发式：读操作可并发，写操作串行
    read_tools = {"file_read", "memory_search", "web_fetch", "web_search"}
    
    results: list[ToolResult] = []
    pending: list[tuple[int, asyncio.Task[ToolResult]]] = []
    
    for idx, call in enumerate(tool_calls):
        if call.name in read_tools:
            # 并发执行
            task = asyncio.create_task(self._invoke_one_tool(call, session_id))
            pending.append((idx, task))
        else:
            # 先等待所有 pending 读操作完成
            for pidx, ptask in pending:
                results.append((pidx, await ptask))
            pending.clear()
            # 串行执行写操作
            results.append((idx, await self._invoke_one_tool(call, session_id)))
    
    # 等待剩余 pending
    for pidx, ptask in pending:
        results.append((pidx, await ptask))
    
    # 按原始顺序排序
    results.sort(key=lambda x: x[0])
    return [r[1] for r in results]
```

**目标文件**: `xmclaw/daemon/hop_loop.py`  
**工作量**: 2 天  
**验收标准**:
- [ ] 同时调用 3 个 `file_read` 时，wall-clock ≈ max(单个时间)
- [ ] 写操作（file_write, bash）仍保持串行
- [ ] HonestGrader 对每个结果独立评分
- [ ] 并发结果按原始 tool_calls 顺序返回

---

### B-8: 测试覆盖补强

**测试目标**:

| 测试 | 当前状态 | 修复后目标 |
|------|----------|-----------|
| `test_session_store.py` | 2 失败 | 全绿 |
| `test_channel_dispatcher.py` | 可能缺少 channel_name 测试 | 新增 |
| `test_cognitive_daemon.py` | 可能缺少 | 新增基础集成测试 |
| `test_factory_defaults.py` | 缺少 | 新增 |
| `test_agent_loop_concurrency.py` | 缺少 | 新增 |

**目标文件**: `tests/unit/` 下新增/修改  
**工作量**: 3 天  
**验收标准**:
- [ ] `pytest tests/unit/` 全部通过
- [ ] 新增测试覆盖 B-1 到 B-7 的修复
- [ ] CI pipeline 绿灯

---

## Phase 1: 核心 Wiring 与稳定性（4-6 周）

> **目标**: 将"代码完成但未接线"的模块接入主流程；增强错误处理和可观测性。

### P1-1: Planner / ReasoningEngine / SelfExperimentLoop 接线

**问题诊断**:
- `app_lifespan.py` 中 Planner 和 ReasoningEngine 已实例化
- 但它们只被传给 `CognitiveDaemon`，未在 `AgentLoop` 中使用
- `SelfExperimentLoop` 可能在 cognitive_daemon 中存在但未激活

**修复方案**:
- 在 `AgentLoop` 中，当 `autonomy_level >= 50` 时，调用 `ReasoningEngine` 进行 turn 前分析
- 当 `autonomy_level >= 75` 时，允许 `SelfExperimentLoop` 发起自主实验
- 确保 `Planner` 的 HTN 分解在 `plan-first` 模式下正确注入 hop loop

**目标文件**: `agent_loop.py`, `cognition/planner.py`, `cognition/reasoning.py`  
**工作量**: 1 周  
**验收标准**:
- [ ] `autonomy_level=50` 时，turn 日志中出现 reasoning 分析痕迹
- [ ] `autonomy_level=75` 时，出现 self-experiment 提案
- [ ] `plan-first` 模式下，HTN 分解在 hop 0 之前完成

---

### P1-2: 错误处理增强

**基于审计 23 章差距**:

| 能力 | 当前状态 | 目标 |
|------|----------|------|
| Fallback provider | ❌ 无 | 429/5xx 时自动切换 |
| Sandbox 崩溃恢复 | ❌ 无 | 自动重启容器 |
| 内存保护 | ⚠️ 基础 | 上下文压缩更早触发 |
| 配置损坏恢复 | ❌ 无 | 备份配置 fallback |

**修复方案**:

```python
# FILE: xmclaw/providers/llm/base.py
class LLMProvider:
    async def complete_with_fallback(self, messages, tools):
        try:
            return await self.complete(messages, tools)
        except (RateLimitError, APIError) as exc:
            if self._fallback:
                return await self._fallback.complete(messages, tools)
            raise
```

**目标文件**: `providers/llm/base.py`, `providers/tool/runtime/docker.py`, `daemon/factory.py`  
**工作量**: 3 天  
**验收标准**:
- [ ] LLM 429 时自动切换到 fallback provider
- [ ] Docker sandbox 崩溃后自动重启
- [ ] 配置损坏时使用 `.backup` 文件恢复

---

### P1-3: 可观测性增强

**基于审计 24 章差距**:

| 能力 | 当前状态 | 目标 |
|------|----------|------|
| Metrics | ⚠️ 基础 | Prometheus `/metrics` 端点 |
| Tracing | ⚠️ 基础 | OpenTelemetry span |
| Health check | ✅ `/health` | 增加 `/ready` 和 `/live` |
| Dashboard | ✅ `dashboard.html` | 修复 VAD 死代码和移动端问题 |

**修复方案**:
- 新增 `xmclaw/daemon/routers/metrics.py`: Prometheus 格式指标导出
- 新增 `xmclaw/utils/tracing.py`: OpenTelemetry 基础 span
- 修复 `dashboard.html` 中的 VAD 死代码和 5 个无导航页面

**目标文件**: `daemon/routers/metrics.py`, `utils/tracing.py`, `dashboard.html`  
**工作量**: 1 周  
**验收标准**:
- [ ] `GET /metrics` 返回 Prometheus 格式指标
- [ ] 每个 turn 生成一个 OpenTelemetry span
- [ ] Dashboard 在移动端正常显示

---

### P1-4: Schema 验证

**基于审计 22 章差距**:

**修复方案**:
- 新增 `xmclaw/daemon/config_schema.py`: JSON Schema 定义
- 在 `factory.py` 启动时验证配置

```python
import jsonschema

CONFIG_SCHEMA = {
    "type": "object",
    "properties": {
        "cognition": {
            "type": "object",
            "properties": {
                "autonomy_level": {"type": "integer", "minimum": 0, "maximum": 100}
            }
        }
    }
}

def validate_config(cfg: dict) -> list[str]:
    try:
        jsonschema.validate(cfg, CONFIG_SCHEMA)
        return []
    except jsonschema.ValidationError as exc:
        return [str(exc)]
```

**目标文件**: `daemon/config_schema.py`, `daemon/factory.py`  
**工作量**: 2 天  
**验收标准**:
- [ ] 无效配置启动时给出清晰的错误信息
- [ ] `autonomy_level=150` 被拒绝并提示有效范围

---

## Phase 2: 生态位关键缺失（8-10 周）

### P2-1: OpenAI-compatible API 端点（2-3 周）

**必要性**: 生态位关键。没有 `/v1/chat/completions`，现有 OpenAI 客户端无法零改动接入。

**实现方案**:

```python
# FILE: xmclaw/daemon/routers/openai_compat.py

from fastapi import APIRouter, Request
from pydantic import BaseModel
import uuid
import time

router = APIRouter(prefix="/v1")

class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[dict[str, str]]
    tools: list[dict] | None = None
    tool_choice: str | None = None
    stream: bool = False
    max_tokens: int | None = None
    temperature: float | None = None

class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: list[dict]
    usage: dict | None = None

@router.post("/chat/completions")
async def chat_completions(req: ChatCompletionRequest, request: Request):
    agent = request.app.state.agent
    
    # Convert OpenAI format → XMclaw Message
    from xmclaw.core.ir import Message
    history = []
    system_prompt = None
    for m in req.messages:
        if m.get("role") == "system":
            system_prompt = m.get("content")
        else:
            history.append(Message(role=m["role"], content=m.get("content", "")))
    
    last_user = next((m for m in reversed(history) if m.role == "user"), None)
    if last_user is None:
        raise HTTPException(400, "No user message found")
    
    session_id = request.headers.get("x-session-id", f"api-{uuid.uuid4().hex[:8]}")
    
    # Pre-load history
    if session_id not in agent._histories and agent._session_store is not None:
        loaded = agent._session_store.load(session_id)
        if loaded:
            agent._histories[session_id] = loaded
    
    # Run turn
    result = await agent.run_turn(session_id, last_user.content)
    
    return ChatCompletionResponse(
        id=f"chatcmpl-{uuid.uuid4().hex}",
        created=int(time.time()),
        model=req.model,
        choices=[{
            "index": 0,
            "message": {"role": "assistant", "content": result.text or ""},
            "finish_reason": "stop"
        }],
        usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    )
```

**文件清单**:
- 新建 `xmclaw/daemon/routers/openai_compat.py` (~400 LOC)
- 修改 `xmclaw/daemon/app.py`: 挂载 `openai_compat.router`
- 新建 `xmclaw/core/ir/openai_format.py`: Message ↔ OpenAI format 转换器

**工作量**: 2-3 周  
**验收标准**:
- [ ] `curl -X POST http://localhost:8766/v1/chat/completions -d '{"model":"claude-sonnet-4","messages":[{"role":"user","content":"hi"}]}'` 返回正确 JSON
- [ ] `stream=true` 时返回 SSE 格式 (`data: {...}\n\n`)
- [ ] `tools` 参数传递正确，tool_calls 返回标准 OpenAI 格式
- [ ] 与 `openai` Python SDK 兼容（`base_url=http://localhost:8766/v1`）
- [ ] 与 Continue.dev / Cline / Cursor 兼容

---

### P2-2: 设备配对安全体系（2 周）

**基于审计 7 章和 16 章差距**:

**实现方案**:

```python
# FILE: xmclaw/security/pairing.py

import secrets
import hmac
import hashlib
import time
from typing import Optional

class PairingService:
    """Device pairing via challenge-response.
    
    Flow:
    1. New device: POST /api/v2/pairing/request → server generates nonce
    2. User confirms on existing device (or loopback auto-approve)
    3. New device: POST /pairing/confirm with HMAC-SHA256(nonce, secret)
    4. Server issues JWT session token
    """
    
    def __init__(self, shared_secret: str, token_ttl: int = 86400):
        self._secret = shared_secret.encode()
        self._token_ttl = token_ttl
        self._pending: dict[str, dict] = {}
    
    def request_pairing(self, device_id: str) -> str:
        nonce = secrets.token_urlsafe(32)
        self._pending[nonce] = {
            "device_id": device_id,
            "ts": time.time(),
        }
        return nonce
    
    def confirm_pairing(self, nonce: str, signature: str) -> str:
        pending = self._pending.pop(nonce, None)
        if pending is None:
            raise PairingError("Nonce not found or expired")
        
        expected = hmac.new(self._secret, nonce.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise PairingError("Invalid signature")
        
        # Issue JWT-like token
        import jwt
        token = jwt.encode(
            {"sub": pending["device_id"], "iat": time.time(), "exp": time.time() + self._token_ttl},
            self._secret,
            algorithm="HS256"
        )
        return token
    
    def is_loopback(self, client_addr: str) -> bool:
        return client_addr in ("127.0.0.1", "::1", "localhost")

class PairingError(Exception):
    pass
```

**文件清单**:
- 新建 `xmclaw/security/pairing.py` (~200 LOC)
- 修改 `xmclaw/daemon/routers/system.py`: 添加 `/pairing/request`, `/pairing/confirm`
- 修改 `xmclaw/daemon/middleware/pairing_auth.py`: 支持 JWT 验证
- 修改 `xmclaw/cli/main.py`: 添加 `xmclaw pair` 命令

**工作量**: 2 周  
**验收标准**:
- [ ] 新设备请求配对 → 现有设备收到确认通知
- [ ] Loopback (localhost) 自动批准，无需交互
- [ ] 签名验证失败返回 403
- [ ] 配对 token 24h 后过期

---

### P2-3: 新增 IM 通道 — WhatsApp + Signal + iMessage（4-6 周）

**基于审计 5 章差距**：7 vs 25+ 通道是最直观的竞争力差距。

**实现方案**:

每个通道遵循现有 `ChannelAdapter` ABC + `PluginManifest` 模式。

#### WhatsApp (2 周)

```python
# FILE: xmclaw/providers/channel/whatsapp/__init__.py
from dataclasses import dataclass, field
from xmclaw.providers.channel.base import PluginManifest

MANIFEST = PluginManifest(
    id="whatsapp",
    label="WhatsApp",
    adapter_factory_path="xmclaw.providers.channel.whatsapp.adapter:WhatsAppAdapter",
    requires=("python-whatsapp-business-api",),
    config_schema={
        "phone_number_id": {"type": "string", "required": True},
        "access_token": {"type": "string", "required": True},
        "webhook_secret": {"type": "string", "required": True},
    },
    needs_tunnel=True,
)
```

```python
# FILE: xmclaw/providers/channel/whatsapp/adapter.py
class WhatsAppAdapter(ChannelAdapter):
    name = "whatsapp"
    
    async def start(self):
        # 使用 WhatsApp Cloud API (Meta 官方)
        # 注册 webhook URL
        pass
    
    async def send(self, target, payload):
        # POST /v19.0/{phone_number_id}/messages
        # Text / Image / Template
        pass
```

#### Signal (2 周)

- 使用 `signal-cli` REST API 或 `signald`
- 需要 `signal-cli` 守护进程预配置

#### iMessage (1-2 周, macOS only)

- 使用 `imessage-exporter` 或 macOS AppleScript bridge
- 仅支持 macOS 宿主机

**文件清单**:
- 新建 `xmclaw/providers/channel/whatsapp/` (~500 LOC)
- 新建 `xmclaw/providers/channel/signal/` (~500 LOC)
- 新建 `xmclaw/providers/channel/imessage/` (~300 LOC)
- 修改 `xmclaw/providers/channel/registry.py`: 追加 CHANNEL_IDS
- 修改 `daemon/config.example.json`: 添加配置模板

**工作量**: 4-6 周  
**验收标准**:
- [ ] WhatsApp inbound → AgentLoop → outbound 完整链路
- [ ] Signal 消息同理
- [ ] iMessage 在 macOS 上可收发（文档标注 macOS-only）
- [ ] 三个通道均支持 delayed ack 和 platform_guidance

---

### P2-4: Docker 默认隔离（1-2 周）

**基于审计 13 章差距**：默认本地执行是安全隐患。

**实现方案**:

```python
# FILE: xmclaw/providers/tool/runtime/docker.py
class DockerRuntime:
    """Execute tools inside a Docker container."""
    
    def __init__(self, image: str = "xmclaw-sandbox:latest"):
        self._image = image
        self._container = None
    
    async def execute(self, cmd: list[str], *, cwd: str | None = None) -> str:
        import subprocess
        docker_cmd = [
            "docker", "run", "--rm",
            "-v", f"{cwd or '.'}:/workspace",
            "-w", "/workspace",
            self._image,
        ] + cmd
        result = subprocess.run(docker_cmd, capture_output=True, text=True, timeout=60)
        return result.stdout + result.stderr
```

```dockerfile
# FILE: Dockerfile.sandbox
FROM alpine:3.19
RUN apk add --no-cache python3 nodejs npm bash curl git
WORKDIR /workspace
```

```json
// FILE: daemon/config.example.json
{
  "tools": {
    "sandbox": {
      "enabled": true,
      "backend": "docker",
      "image": "xmclaw-sandbox:latest",
      "auto_pull": true
    }
  }
}
```

**文件清单**:
- 新建 `xmclaw/providers/tool/runtime/docker.py`
- 新建 `Dockerfile.sandbox`
- 修改 `xmclaw/daemon/factory.py`: 根据 sandbox.enabled 包装 DockerRuntime
- 修改 `xmclaw/providers/tool/builtin.py`: 破坏性操作路由到 sandbox

**工作量**: 1-2 周  
**验收标准**:
- [ ] `file_write` 默认在 Docker 容器内执行
- [ ] `bash` 命令在容器内运行，无法访问宿主机 `/etc/passwd`
- [ ] 容器每次 turn 后销毁
- [ ] 宿主机路径通过 `-v` 只读挂载（write 需要显式 allowlist）

---

### P2-5: Fallback Provider 与 Auth 轮换（1 周）

**基于审计 3 章差距**：无 fallback provider，无 auth 轮换。

**修复方案**:

```python
# FILE: xmclaw/daemon/llm_registry.py
class LLMRegistry:
    def __init__(self):
        self._profiles: dict[str, LLMProfile] = {}
        self._fallback_chain: list[str] = []
    
    def register_fallback(self, primary: str, fallback: str):
        self._fallback_chain.append(fallback)
    
    async def complete_with_fallback(self, profile_id: str, messages, tools):
        profile = self._profiles.get(profile_id)
        if profile is None:
            raise ValueError(f"Unknown profile: {profile_id}")
        
        tried = [profile_id]
        while True:
            try:
                return await profile.llm.complete(messages, tools)
            except (RateLimitError, APIError) as exc:
                next_id = self._find_next_fallback(tried)
                if next_id is None:
                    raise
                tried.append(next_id)
                profile = self._profiles[next_id]
```

**目标文件**: `daemon/llm_registry.py`  
**工作量**: 3 天  
**验收标准**:
- [ ] 429 时自动切换到 fallback provider
- [ ] 切换时记录 warning 日志
- [ ] 所有 fallback 都失败时抛出原始错误

---

### P2-6: 配置 Schema 验证（2 天）

已在 P1-4 中规划，此处合并实施。

---

## Phase 3: 竞争力追赶（12-16 周）

### P3-1: ACP 完整 IDE 集成（4-6 周）

**基于审计 15 章差距**：Hermes ACP 完整，XMclaw 只有 stub。

**实现方案**:

```python
# FILE: xmclaw/acp/server.py
class ACPServer:
    """JSON-RPC over stdio for IDE integration."""
    
    async def handle_initialize(self, params):
        return {
            "protocolVersion": "2025-03-26",
            "serverInfo": {"name": "xmclaw", "version": "2.0.0"},
            "capabilities": {"tools": {}, "sampling": {}}
        }
    
    async def handle_sendMessage(self, params):
        session_id = params.get("sessionId", "acp-default")
        result = await self._agent.run_turn(session_id, params["message"])
        return {"content": result.text}
    
    async def handle_listTools(self, params):
        tools = self._tools.list_tools()
        return [{"name": t.name, "description": t.description} for t in tools]
```

**文件清单**:
- 重写 `xmclaw/acp/server.py` (~800 LOC)
- 新建 VS Code 扩展 `extensions/vscode-xmclaw/` (~2000 LOC)
- 新建 JetBrains 插件 `extensions/jetbrains-xmclaw/` (~1500 LOC)
- 修改 `xmclaw/cli/main.py`: `xmclaw acp` 命令

**工作量**: 4-6 周  
**验收标准**:
- [ ] VS Code 侧栏显示 XMclaw 聊天面板
- [ ] 编辑器选中代码 → "Explain with XMclaw"
- [ ] 工具调用结果在 IDE 中内联显示
- [ ] 支持 inline diff（代码修改建议）

---

### P3-2: 自主技能创建（对标 Hermes）（4-6 周）

**基于审计 11 章和 14 章差距**：XMclaw 无自主技能创建能力。

**实现方案**:

```python
# FILE: xmclaw/skills/auto_creator.py
class SkillAutoCreator:
    """After a complex multi-hop task, automatically extract a reusable skill."""
    
    async def maybe_create_skill(self, session_id: str, history: list[Message]) -> str | None:
        # 1. Check complexity: >3 hops, >2 tools, user approved
        # 2. Extract: LLM summarizes task pattern into SKILL.md format
        # 3. Validate: sandboxed test run
        # 4. Register: add to SkillRegistry with auto-generated evidence
        # 5. Notify: publish SKILL_CANDIDATE_PROPOSED to bus
        
        complexity = self._assess_complexity(history)
        if complexity < 0.7:
            return None
        
        skill_md = await self._generate_skill_md(history)
        skill_id = self._sanitize_id(skill_md.title)
        
        # Write to ~/.xmclaw/skills_auto/<skill_id>/SKILL.md
        path = Path.home() / ".xmclaw" / "skills_auto" / skill_id / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(skill_md, encoding="utf-8")
        
        return skill_id
    
    def _assess_complexity(self, history: list[Message]) -> float:
        # hops ≈ assistant messages with tool_calls
        # tools ≈ unique tool names
        # user approval ≈ last user message contains positive sentiment
        pass
```

**文件清单**:
- 新建 `xmclaw/skills/auto_creator.py` (~400 LOC)
- 修改 `xmclaw/daemon/agent_loop.py`: turn 结束后调用 `maybe_create_skill`
- 修改 `xmclaw/skills/orchestrator.py`: auto_apply 时自动晋升

**工作量**: 4-6 周  
**验收标准**:
- [ ] 完成 3-hop 任务后，系统自动生成 SKILL.md 草案
- [ ] `/skills` 页面显示"建议的新技能"
- [ ] 一键确认后技能注册并可用
- [ ] 技能包含：目标、步骤、工具调用序列、成功标准

---

### P3-3: LLM Provider 扩展（2-3 周）

**基于审计 3 章差距**：仅 3 个 provider，缺少 Ollama/xAI/DeepSeek。

**文件清单**:
- 新建 `xmclaw/providers/llm/ollama.py` — Ollama 本地模型支持
- 新建 `xmclaw/providers/llm/xai.py` — xAI Grok 支持
- 新建 `xmclaw/providers/llm/deepseek.py` — DeepSeek 原生支持
- 修改 `xmclaw/daemon/llm_registry.py`: AuthProfile 支持多 key + cooldown

**工作量**: 2-3 周  
**验收标准**:
- [ ] `ollama://llama3.2` 可正常使用
- [ ] `xai://grok-3` 可正常使用
- [ ] `deepseek://deepseek-chat` 可正常使用
- [ ] AuthProfile 支持 3 个 key 轮换 + 60s cooldown

---

### P3-4: 部署运维（3-4 周）

**基于审计 16 章差距**：无 OS 服务、无 Docker 官方镜像、无 Nix。

**文件清单**:
- 新建 `Dockerfile` + `docker-compose.yml`
- 新建 `systemd/xmclaw.service`
- 新建 `launchd/com.xmclaw.daemon.plist`
- 新建 `nix/flake.nix`
- 修改 `xmclaw/cli/main.py`: `xmclaw install-service` 命令

**工作量**: 3-4 周  
**验收标准**:
- [ ] `docker compose up` 一键启动
- [ ] `xmclaw install-service --target=systemd` 注册系统服务
- [ ] macOS: `launchctl load com.xmclaw.daemon.plist`
- [ ] Nix: `nix run github:xmclaw/xmclaw`

---

### P3-5: Web UI 重写（2-3 月）

**基于审计 15 章差距**：Preact+htm 22 页，VAD 死代码，移动端问题，5 页面无导航。

**技术栈**: Vite + React + TypeScript + TailwindCSS  
**设计**: 三面板布局（对标 OpenClaw Control UI）

**文件清单**:
- 新建 `webui/` 目录（独立 npm 项目）
- 左面板: Session 列表 + 搜索（FTS5 后端）
- 中面板: 聊天流（代码高亮、思维链折叠、工具调用可视化）
- 右面板: 工具/记忆/技能面板
- 修改 `xmclaw/daemon/app.py`: 静态文件服务指向新构建产物

**工作量**: 2-3 月  
**验收标准**:
- [ ] 移动端响应式正常
- [ ] 代码块语法高亮 + 复制按钮
- [ ] 思维链可折叠/展开
- [ ] 工具调用显示为卡片（输入/输出/耗时）
- [ ] Session 搜索实时响应

---

### P3-6: 监控与告警体系（2-3 周）

**基于审计 24 章差距**：无 Prometheus、无 OpenTelemetry、无告警。

**文件清单**:
- 新建 `xmclaw/daemon/routers/metrics.py`: Prometheus `/metrics`
- 新建 `xmclaw/utils/tracing.py`: OpenTelemetry 基础
- 新建 `xmclaw/utils/alerts.py`: 简单阈值告警（cost/hops/latency）
- 修改 `xmclaw/daemon/app.py`: 挂载 metrics router

**工作量**: 2-3 周  
**验收标准**:
- [ ] `GET /metrics` 返回 `xmclaw_turns_total`, `xmclaw_turn_latency_seconds`, `xmclaw_cost_usd_total`
- [ ] 单次 turn 成本 > $1 时打印 warning
- [ ] 单次 turn hops > 10 时打印 warning
- [ ] Grafana dashboard JSON 模板随代码分发

---

## Phase 4: 差异化放大（16-20 周）

### P4-1: RL 训练环境（2-3 月）

**基于审计 14 章差距**：无 RL 训练环境（Hermes 有 Tinker-Atropos）。

**方案**:
- 从 `SqliteEventBus` 导出 `(state, action, reward)` 三元组
- Reward = 工具成功率 * 0.4 + 用户满意度 * 0.4 + 成本效率 * 0.2
- 使用 `transformers` + `trl` 进行 DPO 训练
- 输出: 轻量策略模型（<1B），用于工具选择和参数生成

**工作量**: 2-3 月  
**验收标准**:
- [ ] 训练数据 pipeline 从 events.db 自动提取
- [ ] 策略模型在 holdout 测试集上工具选择准确率 > 80%
- [ ] 模型体积 < 500MB，可在 CPU 上推理

---

### P4-2: 多 Agent 路由完整启用（2-3 月）

**基于审计 12 章差距**：`MultiAgentManager` 代码存在但未启用。

**方案**:
- Workspace 概念：每个 workspace = 独立 context + memory + skills
- Binding 路由：channel/peer → workspace → agent
- Sub-agent: `delegate_task` 支持并行（asyncio.gather，max 3）
- 中断传播：父 agent 中断 → 所有子 agent 中断

**工作量**: 2-3 月  
**验收标准**:
- [ ] 可创建多个 workspace，每个有独立 SOUL.md
- [ ] 通道消息按 binding 路由到指定 agent
- [ ] `delegate_task` 并发执行 3 个子任务
- [ ] 子 agent 失败不影响父 agent

---

### P4-3: 语音能力（2-3 月）

**基于审计 15 章差距**：OpenClaw 有语音，XMclaw 无。

**方案**:
- TTS: `kokoro` (ONNX, 本地，支持中文) 或 OpenAI TTS API
- STT: `faster-whisper` (本地) 或 Whisper API
- 唤醒词: `openwakeword` 或 `porcupine`

**工作量**: 2-3 月  
**验收标准**:
- [ ] `xmclaw voice chat` 启动语音交互模式
- [ ] 中文语音识别准确率 > 90%
- [ ] 中文语音合成自然度可接受
- [ ] 支持唤醒词激活（"嗨，XMclaw"）

---

### P4-4: 技能市场（1-2 月）

**基于审计 11 章差距**：OpenClaw 有 ClawHub，XMclaw 只有基础 marketplace。

**方案**:
- 服务端: 简单的 FastAPI 注册表（skill 元数据 + 评分）
- 客户端: `xmclaw marketplace search/install/publish`
- 集成: 与 OpenClaw 的 ClawHub 格式兼容（双向导入/导出）

**工作量**: 1-2 月  
**验收标准**:
- [ ] `xmclaw marketplace search web_search` 返回可用技能
- [ ] `xmclaw marketplace install <skill-id>` 一键安装
- [ ] `xmclaw marketplace publish` 上传本地技能
- [ ] 技能评分基于 HonestGrader 实际使用数据

---

### P4-5: 自动化发布流程（2-3 周）

**基于审计 27 章差距**：无自动发布、无 nightly、无签名。

**方案**:
- GitHub Actions: `release.yml` — 自动打 tag → 构建 → PyPI 发布
- `nightly.yml` — 每日构建 dev 版本
- `docker.yml` — 自动构建多架构镜像
- 签名: `sigstore` 或 GPG 签名 wheel

**工作量**: 2-3 周  
**验收标准**:
- [ ] `git push --tags` 触发自动发布到 PyPI
- [ ] 每日凌晨构建 `xmclaw-nightly` Docker 镜像
- [ ] Release artifacts 带 GPG 签名
- [ ] SBOM (CycloneDX) 随 release 发布

---

## 附录 A: 依赖矩阵

```
                B-1  B-2  B-3  B-4  B-5  B-6  B-7  B-8  P1-1 P1-2 P1-3 P1-4 P2-1 P2-2 P2-3 P2-4 P2-5 P3-1 P3-2 P3-3 P3-4 P3-5 P3-6 P4-1 P4-2 P4-3 P4-4 P4-5
B-1  session    ─
B-2  put→save   ─    ─
B-3  platform   ─    ─    ─
B-4  defaults   ─    ─    ─    ─
B-5  auto_apply ─    ─    ─    ○    ─
B-6  cognitive  ○    ─    ○    ○    ─    ─
B-7  concurrent ○    ─    ─    ─    ─    ○    ─
B-8  tests      ○    ○    ○    ○    ○    ○    ○    ─
P1-1 planner    ○    ─    ○    ○    ─    ○    ─    ○    ─
P1-2 fallback   ─    ─    ─    ─    ─    ─    ─    ○    ─    ─
P1-3 metrics    ─    ─    ─    ─    ─    ─    ─    ○    ─    ─    ─
P1-4 schema     ─    ─    ─    ○    ○    ─    ─    ○    ─    ─    ─    ─
P2-1 openai     ○    ─    ○    ─    ─    ─    ─    ○    ─    ─    ○    ─    ─
P2-2 pairing    ─    ─    ─    ─    ─    ─    ─    ○    ─    ─    ─    ─    ─    ─
P2-3 channels   ─    ○    ○    ─    ─    ─    ─    ○    ─    ─    ─    ─    ─    ─    ─
P2-4 docker     ─    ─    ○    ─    ─    ─    ─    ○    ─    ○    ─    ─    ─    ─    ─    ─
P2-5 fallback   ─    ─    ─    ─    ─    ─    ─    ○    ─    ○    ─    ─    ─    ─    ─    ─    ─
P3-1 acp        ○    ─    ○    ─    ─    ○    ─    ○    ○    ─    ○    ─    ○    ─    ─    ─    ─    ─
P3-2 auto_skill ○    ─    ○    ○    ○    ○    ─    ○    ○    ─    ─    ─    ─    ─    ─    ─    ─    ─    ─
P3-3 providers  ─    ─    ─    ─    ─    ─    ─    ○    ─    ○    ─    ─    ○    ─    ─    ─    ○    ─    ─    ─
P3-4 deploy     ─    ─    ─    ─    ─    ─    ─    ○    ─    ─    ○    ─    ─    ─    ─    ○    ─    ─    ─    ─    ─
P3-5 webui      ─    ─    ─    ─    ─    ─    ─    ○    ─    ─    ○    ─    ○    ─    ○    ─    ─    ○    ─    ─    ○    ─
P3-6 metrics    ─    ─    ─    ─    ─    ─    ─    ○    ─    ○    ○    ─    ─    ─    ─    ─    ─    ─    ─    ─    ─    ○    ─
P4-1 rl         ○    ─    ○    ─    ─    ○    ─    ○    ○    ─    ○    ─    ─    ─    ─    ─    ─    ─    ○    ─    ─    ─    ○    ─
P4-2 multiagent ○    ─    ○    ○    ○    ○    ○    ○    ○    ─    ○    ─    ○    ─    ─    ○    ─    ○    ○    ─    ○    ─    ○    ─    ─
P4-3 voice      ─    ─    ─    ─    ─    ─    ─    ○    ─    ─    ○    ─    ─    ─    ○    ─    ─    ─    ─    ─    ─    ○    ─    ─    ─    ─
P4-4 marketplace─    ─    ─    ─    ─    ─    ─    ○    ─    ─    ─    ─    ─    ─    ─    ─    ─    ─    ○    ─    ─    ─    ─    ─    ─    ─    ─
P4-5 release    ─    ─    ─    ─    ─    ─    ─    ○    ─    ─    ─    ─    ─    ─    ─    ─    ─    ─    ─    ─    ○    ─    ─    ─    ─    ─    ─    ─

○ = 有依赖关系
```

## 附录 B: 每周里程碑

| 周 | 里程碑 | 关键交付 |
|----|--------|----------|
| W1 | Phase 0 开始 | B-1 session persistence 修复，测试全绿 |
| W2 | Phase 0 完成 | B-2 到 B-8 全部修复，CI 绿灯 |
| W3 | Phase 1 开始 | P1-1 Planner/ReasoningEngine 接线 |
| W4 | P1 继续 | P1-2 Fallback provider + P1-3 Metrics 开始 |
| W5 | P1 继续 | P1-3 Dashboard 修复 + P1-4 Schema 验证 |
| W6 | Phase 1 完成 | 所有 wiring 完成，认知架构活跃 |
| W7 | Phase 2 开始 | P2-1 OpenAI API 非流式实现 |
| W8 | P2 继续 | P2-1 Streaming SSE + SDK 兼容测试 |
| W9 | P2 继续 | P2-2 设备配对安全体系 |
| W10 | P2 继续 | P2-3 WhatsApp 适配器 |
| W11 | P2 继续 | P2-3 Signal 适配器 |
| W12 | P2 继续 | P2-3 iMessage 适配器（macOS） |
| W13 | P2 继续 | P2-4 Docker 默认隔离 |
| W14 | Phase 2 完成 | P2-5 Auth 轮换 + 全部 P2 验收 |
| W15 | Phase 3 开始 | P3-1 ACP server JSON-RPC 完整实现 |
| W16 | P3 继续 | P3-1 VS Code 扩展 MVP |
| W17 | P3 继续 | P3-2 自主技能创建 |
| W18 | P3 继续 | P3-3 Ollama/xAI/DeepSeek provider |
| W19 | P3 继续 | P3-4 Docker Compose + systemd |
| W20 | P3 继续 | P3-5 Web UI 设计 + 左面板 |
| W21 | P3 继续 | P3-5 Web UI 中面板 + 右面板 |
| W22 | P3 继续 | P3-6 Prometheus metrics + Grafana |
| W23 | Phase 3 完成 | 全部 P3 验收 |
| W24 | Phase 4 开始 | P4-1 RL 数据 pipeline |
| W25 | P4 继续 | P4-2 多 Agent 路由 |
| W26 | P4 继续 | P4-3 语音 TTS/STT |
| W27 | P4 继续 | P4-4 技能市场 |
| W28 | P4 继续 | P4-5 自动化发布 |
| W29+ | 持续迭代 | 根据用户反馈调整优先级 |

## 附录 C: 验收清单（Checklist）

### Phase 0 验收
- [ ] `pytest tests/unit/` 全部通过
- [ ] `pytest tests/integration/` 全部通过
- [ ] `ruff check xmclaw/` 无错误
- [ ] `mypy xmclaw/core/` 无错误
- [ ] Session persistence: 跨 daemon 重启历史恢复
- [ ] Platform guidance: Telegram/Feishu 消息中包含对应渲染指导
- [ ] CognitiveDaemon: 心跳日志正常，proposals 进入 turn
- [ ] 默认值: 空配置启动 autonomy_level=50, evolution.auto_apply=true

### Phase 1 验收
- [ ] Planner/ReasoningEngine 在 turn 中可见调用痕迹
- [ ] Fallback provider: 429 时自动切换
- [ ] Dashboard: 移动端正常，VAD 代码移除
- [ ] Schema 验证: 无效配置拒绝启动并给出清晰错误
- [ ] 成本追踪: 单次 turn 成本精确到 $0.001

### Phase 2 验收
- [ ] OpenAI API: Continue.dev / Cursor / Cline 零改动接入
- [ ] 设备配对: 新设备 challenge nonce + JWT token
- [ ] WhatsApp/Signal/iMessage: inbound/outbound 完整链路
- [ ] Docker 隔离: bash 无法读取宿主机 /etc/passwd
- [ ] Auth 轮换: 3 个 key 自动轮换

### Phase 3 验收
- [ ] ACP: VS Code 侧栏聊天面板
- [ ] 自主技能: 3-hop 任务后自动生成 SKILL.md
- [ ] Provider: Ollama/xAI/DeepSeek 可用
- [ ] 部署: `docker compose up` 一键启动
- [ ] Web UI: 三面板，移动端响应式
- [ ] 监控: Prometheus metrics + Grafana dashboard

### Phase 4 验收
- [ ] RL: 策略模型工具选择准确率 > 80%
- [ ] 多 Agent: 3 个子 agent 并发执行
- [ ] 语音: 中文语音识别 + 合成
- [ ] 技能市场: search/install/publish 完整
- [ ] 发布: `git push --tags` 自动 PyPI 发布

---

*计划完成。建议每周 review 进度，每完成一个 Phase 更新一次审计报告的差距矩阵。*
