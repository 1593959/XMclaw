# XMclaw 真实技术架构 (2026-05-07, 全代码核对版)

**说明**: 本文档**只看 `xmclaw/` 实际源码**, 不引用 README / docs / 注释里的描述 (用户明确指示 docs 已过期)。每个组件后跟实际 file:line 引用。

**覆盖**: 13 层子系统的实际数据流 / 启动顺序 / 组件依赖 / 状态字段 / 真实输入输出。

---

## 0. 进程拓扑

```
┌─────────────────────────────────────────────────────────────────┐
│                      用户进程 (CLI / 浏览器)                       │
│                                                                  │
│  浏览器:  http://127.0.0.1:8765/ui/  (Preact + htm via ESM)        │
│  CLI:    xmclaw chat / xmclaw evolution show / xmclaw doctor / ... │
│                                                                  │
└────────────────┬───────────────────────────┬─────────────────────┘
                 │ HTTP /api/v2/*            │ WebSocket /agent/v2/{sid}
                 ▼                           ▼
┌─────────────────────────────────────────────────────────────────┐
│         单个 FastAPI daemon 进程 (uvicorn, 端口 8765 默认)        │
│         pid: ~/.xmclaw/v2/daemon.pid     log: ~/.xmclaw/v2/daemon.log │
│         结构日志: ~/.xmclaw/logs/xmclaw.log (B-298 setup_logging)  │
│                                                                  │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │  AgentLoop.run_turn()  (xmclaw/daemon/agent_loop.py)    │    │
│  │  ├─ 系统 prompt 构建 (含 7 persona 文件全文注入)          │    │
│  │  ├─ B-238 prefilter (top-12 skill_*) + B-300 turn-hint   │    │
│  │  ├─ LLM stream (Anthropic / OpenAI native)               │    │
│  │  ├─ tool 调用 → CompositeToolProvider 路由               │    │
│  │  ├─ HonestGrader.grade per finished tool                 │    │
│  │  └─ post_sampling_hooks dispatch (fire-and-forget)       │    │
│  └─────────────────────────────────────────────────────────┘    │
│                                                                  │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │  20 Routers /api/v2/*    + 9 inline endpoints           │    │
│  │  + AgentContextMiddleware (X-Agent-Id → ContextVar)     │    │
│  └─────────────────────────────────────────────────────────┘    │
│                                                                  │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │  16 个 Lifespan async tasks (启动 / 关闭) — 见 §1         │    │
│  │  + 11 个 bus subscribers — 见 §3                         │    │
│  └─────────────────────────────────────────────────────────┘    │
│                                                                  │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │  InProcessEventBus + SqliteEventBus 复合                 │    │
│  │  ── events.db = WAL 持久化, FTS5 全文搜                   │    │
│  │  ── in-process subscribers 同步 fan-out (asyncio.create_task)│  │
│  └─────────────────────────────────────────────────────────┘    │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────────────────────────────────────┐
│                     ~/.xmclaw/  (用户态数据)                       │
│                                                                  │
│  v2/                          ← 运行态 (可整个 rm -rf 重置)        │
│  ├─ events.db                 ← SqliteEventBus WAL                │
│  ├─ memory.db                 ← sqlite-vec, fact + 向量            │
│  ├─ daemon.pid / daemon.meta  ← lifecycle                          │
│  ├─ pairing_token.txt         ← 0600 perms (B-298 anti-req #8)     │
│  ├─ sessions.db               ← 会话历史 (XMC_V2_SESSIONS_DB_PATH 覆盖) │
│  ├─ evolution/<agent_id>/state.json    ← B-297 EWMA arms persistence │
│  ├─ evolution/skill-dream/proposals.jsonl  ← skill-dream audit       │
│  ├─ journal/YYYY-MM/<sid>.jsonl ← session 结构化 summary             │
│  └─ agents/<id>.json          ← 多 agent presets                    │
│                                                                  │
│  persona/profiles/<active>/   ← 7 个固定 markdown                  │
│  skills_user/<id>/SKILL.md    ← 用户 / 自动 skill                  │
│  memory/notes/*.md            ← agent 自由建文件 (note_write)      │
│  cron/jobs.json               ← 用户定时任务 (CronStore)            │
│  logs/xmclaw.log              ← structlog JSON (B-298)             │
│                                                                  │
│  ~/.xmclaw.secret/            ← Fernet 加密的 secrets (在 ~/.xmclaw 外, 隔离) │
│  ├─ master_key                                                     │
│  └─ secrets.json                                                   │
└─────────────────────────────────────────────────────────────────┘
```

---

## 1. 启动顺序 (xmclaw/daemon/app.py:_lifespan)

按 `await X.start()` 出现行号:

| 行号 | 任务 | 周期/触发 | 作用 |
|---|---|---|---|
| 401 | `MemorySweepTask` | 配置 retention | 老化 fact 删除 |
| 403 | `BackupSchedulerTask` | daily / 关 | 自动备份 ~/.xmclaw |
| 439 | `CronTickTask` | 60s 周期 (硬编码) | 跑用户 cron 任务 |
| 589 | `MemoryFileIndexer` | 10s 周期 | 扫 persona/journal 进 vec store |
| 619 | `ConfigFileWatcher` | 5s 周期 | 监 config.json mtime, 发 CONFIG_RELOADED |
| 666 | `DreamCron` | daily 03:00 | MEMORY.md auto-compact |
| 692 | `EvolutionOrchestrator` | bus 订阅 | 收 SKILL_CANDIDATE_PROPOSED, auto_apply 守门 |
| 720 | `EvolutionAgent` | bus 订阅 GRADER_VERDICT | 累 EWMA, 写 state.json |
| 754 | `EvolutionEvaluationTrigger` | bus 订阅 + 防抖 30s | 触发 evo_agent.evaluate() |
| 794 | `VariantSelector` | bus 订阅 GRADER_VERDICT | UCB1 plays 累计, 注入 SkillToolProvider |
| 819 | `JournalWriter` | bus 订阅 5 类事件 | 写 v2/journal/*.jsonl |
| 933 | `ProfileExtractor` | bus 订阅 USER/LLM_RESPONSE | 每 3 回合 LLM 抽 USER.md preferences |
| 1013 | `SkillDreamCycle` | 1800s 周期 | 扫 journal 起草新 skill |
| 1033 | `RealtimeEvolutionTrigger` | bus 订阅 LLM_RESPONSE 防抖 15s | 触发 dream.run_once() |
| 1065 | `SkillsWatcher` | 10s 周期 | 重扫 skills 目录 + body hot-reload |
| 1102 | `MutationOrchestrator` | bus 订阅 GRADER_VERDICT | EWMA<0.5 触发 SkillMutator |
| 1141 | `ProposalMaterializer` | bus 订阅 SKILL_CANDIDATE_PROPOSED | 写 SKILL.md + 注册 |
| 1182 | `ChannelDispatcher.start_all()` | 长连接 | Feishu/DingTalk/Telegram 适配器 |

**Shutdown 反顺序** (lines 1218-1374): eval_trigger 在 evo_agent 之前停 (防止 trigger 调已停的 agent), variant_selector 在 evo_agent 之前停, realtime_evolution 在 skill_dream 之前停, proposal_materializer 在 skills_watcher 之前停, mutation_orchestrator 在 skill_dream 之前停。

---

## 2. AgentLoop.run_turn 单回合数据流 (full)

```
WebSocket /agent/v2/{sid}
       │
       ▼
1.  AgentContextMiddleware  →  bind_log_context(sid, agent_id)
       │ (X-Agent-Id header 或 query param 设置 ContextVar)
       │
       ▼
2.  publish USER_MESSAGE 事件                           ─┐
       │                                                 │ JournalWriter 收
       ▼                                                 │ ProfileExtractor 收
3.  agent_loop.run_turn(sid, user_message,               │ RealtimeEvolutionTrigger
       llm_profile_id=None,                              │
       cancel_event=None)                                │
       │                                                 │
       ▼                                                 │
4.  build messages = [                                   │
        Message(role=system, content=system_prompt),     │ system_prompt 包:
        *prior,                                          │   - 7 个 persona 文件 (USER.md, MEMORY.md, ...)
        Message(role=user, content=user_message          │   - 当前时刻 (B-209)
                + memory_ctx + memory_files              │   - skill_browse 静态描述 (B-299)
                + curriculum_hint + skill_browse_hint)   │   - 当前会话历史 (compressed if needed)
     ]                                                   │
       │                                                 │
       ▼                                                 │
5.  tool_specs = self._tools.list_tools()                │
       │  全集: 30+ builtin + skill_browse + 404 skill_*  │
       ▼                                                 │
6.  B-238 prefilter (xmclaw/skills/prefilter.py)        │
       │  → 30 builtin (passthrough) + skill_browse +    │
       │    top-12 skill_* (token-overlap scored) ≈ 43   │
       ▼                                                 │
7.  B-300 turn-hint (agent_loop.py:2138)                 │
       │  if registry_total > 0 and survived skill_* == 0│
       │  → 在 user_message 末尾追加 hint 文本            │
       ▼                                                 │
8.  for hop in range(self._max_hops):                    │
       publish LLM_REQUEST   ──────────────────────────  ┘
       │                                                 ┌
       ▼                                                 │
       check_budget()  (CostTracker, raise BudgetExceeded if 超)
       │  → 转 ANTI_REQ_VIOLATION 事件                    │
       ▼                                                 │
       LLM stream (asyncio.wait_for 120s 默认)            │
       │  ├─ AnthropicLLM.complete_streaming() OR        │
       │  ├─ OpenAILLM.complete_streaming()              │ chunk → publish LLM_CHUNK
       │  ├─ B-225 watchdog: cancel.is_set() → close      │ (chat reducer 用)
       │  ├─ B-229 truncation guard: stop_reason="max_tokens" │ thinking → publish LLM_THINKING_CHUNK
       │  └─ B-91 thinking chunk callback                │
       │                                                 │
       ▼                                                 │
       publish LLM_RESPONSE  ───────────────────────────  ┘ ProfileExtractor 收
       │                                                   RealtimeEvolutionTrigger
       │                                                 ┌
       ▼                                                 │
       if response.tool_calls:                           │
         for call in tool_calls:                         │
           publish TOOL_CALL_EMITTED  ──────────────────  │ (UI tool card)
           │  → 包含 args (LLM 真实意图)                   │
           ▼                                             │
           result = self._tools.invoke(call)             │
           │  CompositeToolProvider 路由:                 │
           │    ├─ name == "skill_browse" → 内置 _invoke_browse │
           │    │                  (无 registry, 直接搜)   │
           │    ├─ name.startswith("skill_") →            │
           │    │   SkillToolProvider                      │
           │    │   ├─ variant_selector.pick_version?     │
           │    │   ├─ HEAD warmup 5 plays 之后才 UCB1 picked │
           │    │   └─ registry.get(skill_id, version).run()│
           │    │       (LocalSkillRuntime 默认; ProcessSkillRuntime 配置可换)│
           │    ├─ BuiltinTools 39 个工具 (file/bash/web/memory/...)│
           │    ├─ MCPBridge (stdio JSON-RPC, 可选 npx-spawned servers)│
           │    └─ GuardedToolProvider (5-path: deny/consume_approval/  │
           │         scan/needs_approval/fall through)                  │
           │                                                            │
           ▼                                                            │
           publish TOOL_INVOCATION_STARTED  (no args)  ────────────────│
           publish TOOL_INVOCATION_FINISHED  (no content)  ─────────────│ JournalWriter 收
           │                                                           │
           ▼                                                           │
           verdict = self._grader.grade(finished_event)                │ HonestGrader 0.80 hard checks
           │   (ran 30% + returned 20% + type_matched 25%               │ + 0.20 LLM cap
           │    + side_effect_observable 25%)                          │
           ▼                                                           │
           if call.name.startswith("skill_") and call.name != "skill_browse":│
             # B-295 + B-299 verdict-stamp guard                        │
             stamp skill_id + version on payload                        │
           publish GRADER_VERDICT  ───────────────────────────────────  ┘
       else:                                                            ↓
         break  (无更多 tool calls, turn 结束)                       11 个订阅者
       │                                                             见 §3
       ▼
9.  asyncio.create_task(post_sampling_registry.dispatch(ctx))
       │  fire-and-forget, 不阻塞下回合
       ▼
       并发执行 (asyncio.gather):
       ├─ ExtractMemoriesHook  (默认关; 1 LLM 调; 写 MEMORY.md "Auto-extracted")
       └─ ExtractLessonsHook   (默认开; 1 LLM 调; 5 桶 → 5 文件)
       │     workflow → AGENTS.md
       │     tool_quirks → TOOLS.md  
       │     failure_modes → MEMORY.md "Failure Modes"
       │     values → SOUL.md (B-303)
       │     rules → LEARNING.md (B-303)
       │
       ▼
10. publish SESSION_LIFECYCLE? (仅 destroy 时)
       │
       ▼
       JournalWriter 收 → 写 v2/journal/YYYY-MM/<sid>.jsonl
       ProfileExtractor 收 → flush USER.md preferences
       app.py:186 _PENDING_REFLECTIONS 反思任务 (msg_count > N 触发)
```

---

## 3. Bus 订阅总览 (实测)

```
USER_MESSAGE                                LLM_RESPONSE
├─ JournalWriter                            ├─ JournalWriter (隐含)
└─ ProfileExtractor                         ├─ ProfileExtractor
                                            └─ RealtimeEvolutionTrigger (防抖 15s)
TOOL_INVOCATION_FINISHED
└─ JournalWriter                            GRADER_VERDICT
                                            ├─ EvolutionAgent          (累 EWMA, 写 state.json)
SKILL_CANDIDATE_PROPOSED                    ├─ EvolutionEvalTrigger    (防抖 30s, cool 300s, min 10)
├─ EvolutionOrchestrator                    ├─ VariantSelector         (累 plays)
│  (auto_apply=False 时观察)                 ├─ MutationOrchestrator   (EWMA<0.5 触发 mutate)
│  (auto_apply=True 时立即 promote)          └─ JournalWriter
└─ ProposalMaterializer
   (decision="propose" 写 SKILL.md)         ANTI_REQ_VIOLATION
                                            └─ JournalWriter
SKILL_PROMOTED, SKILL_ROLLED_BACK
└─ (UI 跨 session 广播,                     SESSION_LIFECYCLE
   _GLOBAL_EVENT_TYPES app.py:1592)          ├─ JournalWriter (写 jsonl)
                                            └─ ProfileExtractor (flush USER.md)
USER_PROFILE_UPDATED
└─ app.py:955 anonymous handler             CONFIG_RELOADED
   (bump prompt_freeze_generation,          └─ (无订阅者!)
    下回合 system prompt 重建包含新 USER.md)    ⚠️ tools / security 配置改了不生效
                                            
PROMPT_INJECTION_DETECTED                   COST_TICK
└─ JournalWriter / Trace.js (UI)            └─ JournalWriter
```

**死事件** (枚举存在但 0 publisher):
- `SKILL_EXEC_STARTED`, `SKILL_EXEC_FINISHED` (xm-auto-evo 残留)
- `SKILL_INVOKED`, `SKILL_OUTCOME` (`_detect_skill_invocations` 移除残留)

---

## 4. LLM Provider 层

```
xmclaw/providers/llm/
  ├─ base.py            ABC + LLMResponse / LLMChunk / Pricing / ToolCallShape
  ├─ anthropic.py       AnthropicLLM (Anthropic-native)
  ├─ openai.py          OpenAILLM (用于 OpenAI + 所有 OpenAI-compat)
  └─ translators/
      ├─ anthropic_native.py    tool_use block ↔ ToolCall
      └─ openai_tool_shape.py   tool_calls array ↔ ToolCall

xmclaw/daemon/llm_registry.py    LLMProfile / LLMRegistry, multi-profile 路由
xmclaw/daemon/factory.py:442-499 build_llm_registry_from_config
```

### 4.1 AnthropicLLM 关键特性

```
complete_streaming() 流程:
  1. 注入 cache_control breakpoints (B-245):
     - system prompt 末尾标 ephemeral
     - 最后 1 个 tool 标 ephemeral
  2. asyncio.wait_for(120s 默认 B-189 timeout)
  3. asyncio.create_task(watchdog_task) 监 cancel.is_set()
  4. async for event in stream:
       chunk → yield LLMChunk(text)
       thinking_delta → callback on_thinking_chunk (B-91)
       tool_use → 累 tool_call
  5. stop_reason="max_tokens" 检查:
       drop tool_use blocks where input == {} (B-229)
       append "[output truncated...]" 提示
  6. cache stats (creation_input_tokens + cache_read_input_tokens) 写 LLMResponse.usage
  7. return LLMResponse(content, tool_calls, usage, stop_reason)
```

### 4.2 OpenAILLM 关键特性

```
complete_streaming() 流程:
  1. 不支持 cache_control (Kimi/GLM 等扩展未实现, 见 cleanup §5.2)
  2. asyncio.wait_for(120s)
  3. watchdog 同上
  4. async for chunk in stream:
       delta.content → yield LLMChunk(text)
       delta.tool_calls → 按 index 累 (累加 args 字符串)
       reasoning_content / reasoning / thinking / model_extra (B-214 pydantic v2 workaround)
         → callback on_thinking_chunk
  5. finish_reason="length" 检查:
       drop tool_acc entries where arguments == "" (B-229)
       (因为合法的零参数应该是 "{}" 不是 "")
  6. return LLMResponse(content, tool_calls, usage, stop_reason="length")
```

### 4.3 LLMRegistry 多 profile

```python
LLMProfile(id, label, provider_name, model, llm)
LLMRegistry.profiles: dict[str, LLMProfile]
LLMRegistry.default_id: str | None

Default 选择优先级 (B-146):
  1. cfg['llm']['default_profile_id']  (显式)
  2. "default"  (legacy block)
  3. 第一个 profile (insertion order)
  4. None  (echo mode)
```

---

## 5. Tool Provider 层

```
xmclaw/providers/tool/
  ├─ base.py             ToolProvider ABC
  ├─ composite.py        CompositeToolProvider (递归路由 + 重名检测 + close_session/shutdown 扇出)
  ├─ guarded.py          GuardedToolProvider 5-path 决策
  ├─ builtin.py          BuiltinTools (39 工具)
  ├─ browser.py          7 个 Playwright 工具 (lazy import)
  ├─ content.py          screenshot / mss 类
  ├─ automation.py       cron / process / code_python (不是桌面自动化!)
  ├─ integrations.py     8 个外部集成 (slack/email/webhook/...)
  ├─ agent_inter.py      6 个多 agent 工具
  ├─ lsp.py              语言服务器 (代码补全)
  └─ mcp/
      └─ mcp_bridge.py   subprocess JSON-RPC 2.0 桥
```

### 5.1 BuiltinTools 39 个工具分类

| 类别 | 工具 | 数 |
|---|---|---|
| 文件 | file_read, file_write, apply_patch, list_dir, glob_files, grep_files, file_delete | 7 |
| Shell | bash | 1 |
| 网络 | web_fetch, web_search | 2 |
| Todo | todo_write, todo_read | 2 |
| 内存 | remember, learn_about_user, update_persona, memory_search, memory_pin, memory_compact, recall_user_preferences | 7 |
| 笔记/日志 | note_write, journal_append, journal_recall | 3 |
| 课程 | propose_curriculum_edit, list_curriculum_proposals | 2 |
| 调度 | schedule_followup | 1 |
| SQL | sqlite_query | 1 |
| 自省 | agent_status | 1 |
| 交互 | ask_user_question | 1 |
| Worktree | enter_worktree, exit_worktree | 2 |
| Process | code_python, process_list, process_kill (在 automation.py) | 3 |
| Cron | cron_create, cron_list, cron_pause, cron_resume, cron_remove (automation.py) | 5 |
| Integrations | slack_send, telegram_send, ... (integrations.py) | ~9 |

### 5.2 SkillToolProvider (xmclaw/skills/tool_bridge.py)

```
list_tools() 出口:
  1. skill_browse (B-299, synthesized, 始终在 index 0)
  2. for sid in registry.list_skill_ids():
       → ToolSpec(name="skill_<id>", description=manifest.description, schema)
  
  全集: 1 + N 个 skill (本机 N=404)

invoke(call) 路由:
  if call.name == "skill_browse":
    return self._invoke_browse(call.args)
    # 内置 search 不走 registry
  else:
    skill_id = _tool_name_to_skill_id(call.name)
    
    # B-295 variant selection
    chosen_version = None
    if self._variant_selector is not None:
      chosen_version = variant_selector.pick_version(skill_id)
    
    skill = registry.get(skill_id, version=chosen_version)
    out = await skill.run(SkillInput(args=call.args))
    
    # B-295 metadata stamp (供 grader 收时 emit GRADER_VERDICT 带 skill_id+version)
    return ToolResult(
      ..., 
      metadata={"skill_id": skill_id, "skill_version": effective_version}
    )
```

### 5.3 GuardedToolProvider 5-path

```
invoke(call):
  1. is_denied(name)? → ToolResult(ok=False, "blocked")
  2. consume_approval(session_id, name, params)? → bypass + invoke inner
  3. scan via guardians (file_path / rule_based / shell_evasion):
     - file_path: 总是跑
     - 其它: 仅 _DEFAULT_GUARDED_TOOLS (execute_shell_command, file_*)
  4. no findings? → invoke inner (hot path)
  5. policy lookup based on max_severity:
     CRITICAL → DENY (return error)
     HIGH → APPROVE (create pending, return error="NEEDS_APPROVAL:<id>")
     MEDIUM → ALLOW (穿过)
     LOW → ALLOW
     SAFE → ALLOW
```

---

## 6. Channel 层

```
xmclaw/providers/channel/
  ├─ base.py
  ├─ ws.py                  WebSocket (CLI/Web UI), ✓ ready
  ├─ acp.py                 ACP (Zed/VSCode), ⚠️ start() raise NotImplementedError
  ├─ feishu/
  │   ├─ adapter.py         ✓ ready, 含 B-273 SOURCE_CHANNEL 扫描
  │   └─ __init__.py
  ├─ dingtalk/__init__.py   manifest only ❌
  ├─ wecom/__init__.py      manifest only ❌
  ├─ weixin/__init__.py     manifest only ❌
  └─ telegram/__init__.py   manifest only ❌

xmclaw/daemon/channel_dispatcher.py    路由 inbound → AgentLoop, outbound → adapter.send
```

### 6.1 ChannelDispatcher 数据流

```
Adapter (Feishu) 收到 P2ImMessageReceiveV1 事件
  │
  ▼
adapter._on_message → InboundMessage(target=ChannelTarget(channel="feishu", ref=chat_id), content=text)
  │
  ▼
B-273 SOURCE_CHANNEL 扫描 (仅 Feishu, 其他 channel 漏)
  │ if blocked: return (drop)
  ▼
ChannelDispatcher._on_inbound  
  │ session_id = f"{channel}:{ref}"  (例如 "feishu:oc_xxx")
  │ session_lock dict 互斥, 同一 chat 内串行
  ▼
agent.run_turn(session_id, content)
  │ (历史在 agent._histories[session_id], 跨 daemon 重启保留)
  ▼
B-195 delayed ack: 若 turn > 2s 发"收到, 思考中…"
  │
  ▼
agent 处理完 → _extract_last_assistant(agent._histories[session_id])
  │
  ▼
B-199 attachment 提取: regex _IMAGE_PATH_RE 扫 reply 找本地图片路径 (max 4)
  │
  ▼
adapter.send(target, OutboundMessage(content, reply_to=msg_id, attachments))
  │ (Feishu API ReplyMessageRequest 或 CreateMessageRequest)
  │ 失败 raise RuntimeError, dispatcher log + 不重试
```

---

## 7. 内存 / Persona / Journal 层

```
持久层:
  ~/.xmclaw/v2/memory.db   (sqlite-vec)
       ↑ 写者 (按 LLM 调用频次):
       ├─ ExtractLessonsHook  (5 桶, 每回合 1 LLM, 默认开)
       ├─ ExtractMemoriesHook (1 LLM 调, 默认关)
       ├─ ProfileExtractor   (每 3 回合, USER 桶专用)
       ├─ remember 工具       (即时, 同步)
       ├─ learn_about_user 工具 (即时)
       ├─ update_persona 工具 (即时, 7 文件任意)
       ├─ memory_pin 工具     (即时, MEMORY.md ## Pinned)
       ├─ note_write 工具     (即时, ~/.xmclaw/memory/notes/)
       └─ journal_append 工具 (即时, ~/.xmclaw/v2/journal/)

       │ render_to_disk (B-198 Phase 3, vec → markdown 单向)
       ▼

  ~/.xmclaw/persona/profiles/<active>/
       USER.md         (preference 桶)
       MEMORY.md       (lesson + Failure Modes 桶)
       AGENTS.md       (workflow 桶)         ❌ 实测 0 条
       SOUL.md         (values 桶, B-303)    ❌ 实测 0 条
       LEARNING.md     (rules 桶, B-303)     ❌ 实测 0 条
       TOOLS.md        (tool_quirks 桶)      ❌ 实测 0 条
       IDENTITY.md     (manual only)
       BOOTSTRAP.md    (manual only, 首装后删)

  ~/.xmclaw/memory/notes/*.md       (note_write 工具的 free-form 文档)
  ~/.xmclaw/v2/journal/YYYY-MM/<sid>.jsonl  (per-session structured summary)

读出层:
  memory_search 工具 (kind 参数)
       ├─ kind=preference   → USER.md 切片 (向量+元数据)
       ├─ kind=lesson       → AGENTS/TOOLS/MEMORY/SOUL/LEARNING 切片
       ├─ kind=identity     → IDENTITY.md
       ├─ kind=code_chunk   → workspace 代码索引 (MemoryFileIndexer 写入)
       └─ 不带 kind         → 全索引语义搜

  自动注入到 system prompt:
    每回合 build messages 时, 7 个 persona 文件全文注入
    (这就是 prompt 长 — 包了 USER.md / MEMORY.md / AGENTS.md 等)

每日 03:00 (DreamCron):
  DreamCompactor → MEMORY.md LLM 重写 (合并去冗 / 状态覆写 / 归纳整合 / 废弃剔除)
    备份: backup/memory_backup_YYYYMMDD-HHMMSS.md (写之前)
    ## Pinned section 不动
    publish MEMORY_DREAMED 事件
  范围: 仅 MEMORY.md (其它 6 文件不动)
```

### 7.1 sqlite-vec 内部 dedup (upsert_fact)

```
upsert_fact(text, kind, metadata, embedding):
  1. 先 nearest-neighbor 同 kind, distance_threshold=0.4
  2. 找到? → strengthen(item):
       evidence_count += 1
       last_seen = now
       confidence 平均
  3. 没找到? insert new row + put embedding
  4. 自动促 working → long layer 当:
       evidence_count >= 3 且 confidence >= 0.7
```

### 7.2 PersonaStore (B-198 Phase 3) 双层一致性

```python
# core/persona/store.py
AUTO_SECTIONS = {
  "USER.md":      ("## Auto-extracted preferences", "preference", None),
  "MEMORY.md":    ("## Failure Modes",              "lesson",     "failure_modes"),
  "AGENTS.md":    ("## Auto-extracted",             "lesson",     "workflow"),
  "TOOLS.md":     ("## Auto-extracted",             "lesson",     "tool_quirks"),
  "SOUL.md":      ("## Auto-extracted",             "lesson",     "values"),       # B-303
  "LEARNING.md":  ("## Auto-extracted",             "lesson",     "rules"),        # B-303
  "IDENTITY.md":  None,                                                            # manual only
  "BOOTSTRAP.md": None,
}
```

---

## 8. Security 层

```
xmclaw/security/
  ├─ __init__.py            公共 export (SOURCE_*, apply_policy, scan_text, redact)
  ├─ policy.py              policy 编排器 + 7 个 SOURCE 常量
  ├─ prompt_scanner.py      50+ regex 模式, 8 类 (instruction_override, role_forgery, exfiltration, ...)
  ├─ rule_loader.py         编译 9 个 yaml rules + 缓存 + 错误容忍
  ├─ skill_guard.py         SKILL.md trust-based 扫描
  ├─ skill_scanner.py       AST + regex 扫 skill source
  ├─ approval_service.py    pending/completed ledger + GC (30min/200, 60min/500)
  ├─ redact.py              16+ 个 secret 模式 (B-246)
  ├─ rules/                 9 个 YAML 规则文件
  └─ tool_guard/
      ├─ base.py            BaseToolGuardian ABC
      ├─ models.py          GuardSeverity / GuardianAction / GuardianPolicy
      ├─ engine.py          ToolGuardEngine 编排
      ├─ file_guardian.py   FilePathToolGuardian (CRITICAL)
      ├─ rule_guardian.py   RuleBasedToolGuardian (yaml 驱动)
      └─ shell_evasion_guardian.py  ShellEvasionGuardian (HIGH)
```

### 8.1 7 个 SOURCE 常量 + 4 个真实扫描点

```
SOURCE_TOOL_RESULT      → agent_loop.py:2810  (tool 输出扫一次)
SOURCE_PROFILE          → 持久 (静态文件中扫)
SOURCE_MEMORY_RECALL    → agent_loop.py:1962  (memory 检索结果扫)
SOURCE_WEB_FETCH        → web_fetch 工具内
SOURCE_SUB_AGENT        → agent_inter.py:354  (B-273, chat_with_agent reply 扫)
                          ⚠️ submit_to_agent 异步路径**不扫** — 真实安全洞
SOURCE_CHANNEL          → feishu/adapter.py 入站扫
                          ⚠️ 其他 channel (WS/ACP/4 个 scaffold) **不扫**
SOURCE_SKILL_BODY       → skill_guard.py 加载时扫
```

### 8.2 9 个 YAML rule 文件

| 文件 | 规则数 | CRITICAL 数 |
|---|---|---|
| hardcoded_secrets.yaml | 8 | 6 |
| command_injection.yaml | 12 | 0 |
| dangerous_shell_commands.yaml | 20 | 0 (rm -rf 也仅 HIGH) |
| data_exfiltration.yaml | 8 | 0 |
| unauthorized_tool_use.yaml | 3 | 0 |
| prompt_injection.yaml | 5 | 0 |
| obfuscation.yaml | 4 | 0 |
| social_engineering.yaml | 2 | 0 |
| supply_chain.yaml | 1 | 0 |
| **合计** | **63** | **6** |

### 8.3 ToolGuardEngine 5-path 决策

详见 §5.3。默认 GuardianPolicy: `CRITICAL → DENY, HIGH → APPROVE, 其他 → ALLOW`。

### 8.4 ApprovalService

```
in-memory ledger:
  pending dict   (max 200, age 30 min, GC on every mutation)
  completed dict (max 500, age 60 min)

create() → request_id (12 hex chars uuid)
approve() / deny() → 移 pending → completed
consume_approval(session_id, tool_name, params)  
  exact match → bypass guard + delete entry (one-shot)
```

REST + CLI 双面: `xmclaw approvals list/approve/deny` ↔ `/api/v2/approvals/*`

---

## 9. Bus 内部

```
xmclaw/core/bus/
  ├─ events.py     EventType (30 个), BehavioralEvent (frozen + slots), make_event(), event_as_jsonable()
  ├─ memory.py     InProcessEventBus (asyncio.create_task fanout)
  ├─ sqlite.py     SqliteEventBus (extends InProcessEventBus, WAL + FTS5)
  └─ replay.py     query() 分页, 每页 500 行, 支持 since/until/types/session_id 过滤
```

### 9.1 InProcessEventBus.publish 流程

```
async def publish(event):
  for sub in subscriptions:
    if not sub._active: continue
    try:
      if not sub.predicate(event): continue  # filter
    except Exception: log + continue
    asyncio.create_task(_dispatch(sub.handler, event))
    # 不 await, 不阻塞 publisher
    # handler 异常被独立 try-catch 捕获 + log

async def _dispatch(handler, event):
  try:
    await handler(event)
  except Exception as e:
    log.warning("bus.subscriber_failed", ...)
```

### 9.2 SqliteEventBus 扩展

```
class SqliteEventBus(InProcessEventBus):
  async def publish(event):
    async with self._write_lock:
      self._conn.execute(_INSERT_SQL, _event_to_row(event))
    await super().publish(event)  # in-process fanout
  
  async def publish_many(events):  # 批量, 一个 BEGIN/COMMIT
    ...

# Schema (SCHEMA_VERSION=1):
events (
  id TEXT PK,         # uuid4 hex
  ts REAL,            # wall-clock float
  session_id TEXT,
  agent_id TEXT,
  type TEXT,
  payload TEXT,       # JSON freeform
  correlation_id TEXT,
  parent_id TEXT,
  schema_version INTEGER
)
索引: session_ts, ts, type, correlation_id

events_fts (FTS5 virtual)  ← payload 全文索引, AI trigger 同步
sessions   ← 派生表 (auto-maintained by INSERT trigger)
```

### 9.3 Replay 路径 (WS 重连)

```
WS reconnect for session_id X:
  1. 查 in-memory session_logs[X] (app.py:1582, _SESSION_LOG_CAP=400)
     → 最近 400 个事件, 流给前端
  2. 不够? (会话超 400 事件)
     → 走 SqliteEventBus.query(session_id=X, limit=500), 分页补足
     → replay.py 异步生成器, 每页 500
  3. 然后 go live (新事件实时推)
```

---

## 10. CLI 层

39 个 typer 命令 (xmclaw/cli/main.py + 子模块):

```
顶层:
  version, ping, serve, start, stop, restart, status, tools, chat, onboard, doctor

config 子组:
  init, set, get, show, unset, set-secret, get-secret, delete-secret,
  list-secrets, migrate-secrets

memory 子组:
  stats, setup

evolution 子组:
  show, review, approve, reject, migrate-auto-evo

approvals 子组:
  list, approve, deny

curriculum 子组:
  list, show, approve, reject

session 子组:
  report, list

backup 子组:
  create, list, verify, info, delete, prune, restore

security:
  scan
```

### 10.1 xmclaw doctor 24 个 check

(见 conflicts §11.2)

3 个有 fix(): ConfigCheck (mkdir), WorkspaceCheck (mkdir), PairingCheck (chmod). 其余 21 个无自动 fix。

### 10.2 daemon lifecycle (xmclaw/daemon/lifecycle.py)

```
start_daemon():
  1. read PID file. 若 process 活 → "already running"
  2. 清 stale PID 文件
  3. spawn `xmclaw serve` 后台:
     - Windows: DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
     - POSIX: start_new_session=True
  4. 写 PID 文件
  5. poll /health 最多 10s
  6. 超时 → RuntimeError

stop_daemon():
  1. read PID
  2. Windows: taskkill /PID X → wait 5s → taskkill /F
  3. POSIX: kill SIGTERM → wait 5s
  4. 清 PID/meta 文件
```

---

## 11. 前端

22 个 page + 20 router + 9 inline endpoint:

### 11.1 Pages 路由表

```
/ (index)                                Workspace.js (or root redirect)
/chat                                    Chat.js
/sessions                                Sessions.js
/agents                                  Agents.js
/channels                                Channels.js

/skills                                  Skills.js
/tools                                   Tools.js
/memory                                  Memory.js (含 Memory-Identity.js + Memory-NotesJournal.js 子)
/evolution                               Evolution.js (含 LiveStatusPanel B-301)

/cron                                    Cron.js
/workspace                               Workspace.js

/trace                                   Trace.js
/logs                                    Logs.js
/analytics                               Analytics.js

/settings                                Settings.js (含 Settings-audio.js + ModelProfiles.js 子)
/security                                Security.js
/doctor                                  Doctor.js
/backup                                  Backup.js
/config                                  Config.js
/docs                                    Docs.js
```

### 11.2 Reducer 处理的事件类型 (chat_reducer.js)

```
USER_MESSAGE, LLM_CHUNK, LLM_THINKING_CHUNK, LLM_RESPONSE,
TOOL_CALL_EMITTED, TOOL_INVOCATION_FINISHED,
AGENT_ASKED_QUESTION, USER_ANSWERED_QUESTION,
COST_TICK, GRADER_VERDICT,
SKILL_INVOKED, SKILL_OUTCOME (4 dead 事件之 2, reducer 仍订阅但 publisher 不发),
ANTI_REQ_VIOLATION, SESSION_LIFECYCLE
```

B-218 Timeline events 数组与 message.toolCalls 并行渲染。  
B-269 cancelledTurnIds Set 丢晚来 chunk。  
B-91 thinking_chunk 折叠行 (默认 collapsed)。

### 11.3 store shape

```
app.state:
  route: { path, params }
  session: { activeSid, sids[], lifecycle, activeAgentId, agents[] }
  connection: { status, lastError, reconnectAttempt }
  auth: { token, fetched }
  chat: { 
    messages[], pendingAssistantId, composerDraft, 
    planMode, ultrathink, llmProfileId, 
    cancelledTurnIds 
  }
  ui: { theme, density, locale }
  bootstrap: { source }
  evolution: { cache, snapshotPolling }    (B-301)
```

### 11.4 Cache-bust (BOOT_VERSION)

每次 daemon 启动重生成 BOOT_VERSION = `int(time.time())`. app.py 静态文件中间件:
- HTML: `<script src="./...">` → `<script src="./...?v=BOOT_VERSION">`
- JS: `import "./...";` / `from "./..."` → 加 `?v=BOOT_VERSION`
- 响应头: `Cache-Control: no-store, no-cache, must-revalidate, max-age=0`

---

## 12. Cron / Scheduler

```
xmclaw/core/scheduler/cron.py

CronStore (singleton, ~/.xmclaw/cron/jobs.json):
  Job(id, schedule, prompt, enabled, wake_agent, run_once, 
      next_run_at, last_run_at, last_error, run_count)
  schedule: "every 5m" | cron expression "0 9 * * MON-FRI"
  atomic write: tmp + os.replace

CronTickTask:
  60s tick (硬编码)
  扫 enabled jobs 找 due
  fire: runner(job) 调 agent.run_turn(f"cron:{job.id}:{ts}", job.prompt)
  run_once=True → 删 job 后置
  失败 → 写 last_error, 不停下

输出:
  ~/.xmclaw/cron/output/{job_id}/{YYYYMMDD-HHMMSS}.md
```

REST: `/api/v2/cron`. 默认 0 个内置任务, 全是用户配。

---

## 13. Cost / Budget

```
xmclaw/utils/cost.py

CostTracker:
  spent_usd: float = 0.0
  budget_usd: float = 0.0  # ≤0 表示无限
  _ledger: list[CostEntry]
  
  record(provider, model, prompt_tokens, completion_tokens, *, pricing=None):
    cost = (prompt_tokens * input + completion_tokens * output) / 1M
    self.spent_usd += cost
    self._ledger.append(CostEntry(...))
  
  check_budget():
    if self.budget_usd > 0 and self.spent_usd > self.budget_usd:
      raise BudgetExceeded(f"...")

DEFAULT_PRICING (2026-04 prices):
  Anthropic: opus-4.7, sonnet-4.6, haiku-4.5
  OpenAI: gpt-4o, gpt-4o-mini, gpt-4.1
  unknown model → cost=0

Raise 点 (agent_loop.py:2268):
  pre-LLM call check → BudgetExceeded → ANTI_REQ_VIOLATION 事件

⚠️ NOT wired from cfg.cost — caller-driven instantiation
   anti-req #6 hard cap 实际落不到 daemon 级
```

---

## 14. Logging / Redact / Path

```
xmclaw/utils/log.py

setup_logging() (idempotent _CONFIGURED flag):
  log_dir = data_dir() / "logs"   # ~/.xmclaw/logs/
  RotatingFileHandler(xmclaw.log, maxBytes=5MB env, backups=3 env)
  StreamHandler() (stdout)
  structlog processors:
    1. contextvars.merge_contextvars     ← session_id, agent_id 自动注入
    2. filter_by_level
    3. add_logger_name, add_log_level
    4. TimeStamper(iso)
    5. StackInfoRenderer
    6. format_exc_info
    7. UnicodeDecoder
    8. _scrub_secrets   ← 走 redact.py
    9. JSONRenderer

xmclaw/utils/redact.py:
  16+ regex patterns (B-246):
    AKIA / GitHub PAT / Stripe / Discord / OpenRouter / DeepSeek / 
    JWT / PEM / OpenAI / Gemini / Anthropic / SK- / etc.

xmclaw/utils/paths.py:
  data_dir()        XMC_DATA_DIR override → ~/.xmclaw
  logs_dir()        data_dir / "logs"
  v2_workspace_dir  data_dir / "v2"
  evolution_dir     v2 / "evolution"
  journal_dir       v2 / "journal"
  skills_dir        XMC_V2_SKILLS_DIR → data_dir / "skills"
  user_skills_dir   data_dir / "skills_user"
  persona_dir       data_dir / "persona" / "profiles"
  secret_dir        XMC_SECRET_DIR → ~/.xmclaw.secret  ← 隔离!
  default_pid_path  XMC_V2_PID_PATH → v2 / "daemon.pid"
  default_memory_db v2 / "memory.db"
  default_sessions_db  v2 / "sessions.db"
```

---

## 15. 配置 / 环境变量

```
config.example.json 顶层 keys:
  llm                read by factory (B-146)
  tools              read by factory + ToolProvider build
  security           read by factory + tool_guard wiring
  evolution          read by factory + 多个 lifespan task
  memory             read by factory (memory provider build)
  workspace          read by factory (line 1016)
  persona            read by factory (line 923)
  
  default_provider   ❌ B-146 起废
  backup             read by lifespan only (BackupScheduler)
  channels           read by lifespan only (Channel adapters)
  integrations       read by lifespan only (Integration tools)
  mcp_servers        read by lifespan only (MCP registry)
  gateway            read by lifespan only (uvicorn config)

XMC__ env override (factory.py:78-120):
  prefix XMC__, 路径分隔 __, JSON 强转
  XMC__llm__anthropic__api_key=sk-xxx → cfg.llm.anthropic.api_key

ConfigFileWatcher (config_watcher.py):
  poll mtime 5s
  publish CONFIG_RELOADED with {changed_keys, top_changed, restart_required, runtime_only}
  ⚠️ 没人订阅 → 实际改了 tools/security 不生效
```

---

## 16. 真实状态快照 (2026-05-07 18:30)

```
state.json (~/.xmclaw/v2/evolution/evo-main/):
  arms: 2
    commit-message-storyteller v1   plays=1   ewma=0.800
    deploy-to-vercel              v1   plays=1   ewma=0.800
  ready_to_propose_count: 0  (min_plays=10 阈值未达)

skill_dream cycle 历史:
  最近成功 cycle: 2026-05-05 19:57
  之后 4 次 cycle 都返回 0 提议 (5-10ms 早期退出)
  原因: B-184 黑名单过滤 + journal 窗口里 generic primitives 主导

7 个 persona 文件 (用户截图实测):
  USER.md       25 entries   ✓ ProfileExtractor 在跑
  MEMORY.md    106 entries   ✓ ExtractLessonsHook + ExtractMemoriesHook + DreamCompactor
  IDENTITY.md    1 entry     ✓ 首装产物
  AGENTS.md      0  待用     ❌ ExtractLessonsHook workflow 桶常空
  TOOLS.md       0  待用     ❌ tool_quirks 桶常空
  SOUL.md        0  待用     ❌ B-303 加 values 桶, 还没真实流量
  LEARNING.md    0  待用     ❌ B-303 加 rules 桶, 还没真实流量

events.db 7 天:
  GRADER_VERDICT          1497 条
  其中 skill-stamped         22 条 (1.5%)
  SKILL_CANDIDATE_PROPOSED   56 条 全部来自 skill-dream agent (5/2 + 5/5)
  SKILL_PROMOTED              0 条 (Orchestrator auto_apply=False 默认观察)
  SKILL_ROLLED_BACK           0 条

Daemon 资源:
  events.db                ~100MB+ 单调增长 (无 autovacuum)
  memory.db                ~20MB 当前
  daemon.log               ~8MB (rotation 5MB cap, 3 backup)

Tool 暴露:
  total list_tools()      435 (30+ builtin + 1 skill_browse + 404 skill_*)
  prefilter 后给 LLM       ~43 (30+ builtin + 1 meta + top-12 skill)

LLM profile:
  active default          kimi k2.6 (OpenAI-compat shim)
  ⚠️ Kimi 走 OpenAILLM 不享受 cache_control, 每回合重传 8K-token system prompt
```

---

## 17. 一句话画像

**结构上**:  
单 daemon 进程 + 16 个常驻 async task + 11 类 bus event + 30 EventType (4 个死) + 7 层 ToolProvider 嵌套 + 4 个 GRADER_VERDICT 订阅者 + 2 个 LLM_RESPONSE 订阅者 + 5 个 evolution 组件 + 7 个 persona 文件 + 9 个 yaml security 规则 + 24 个 doctor check + 39 个 builtin tool + 22 个 UI page + 20 个 REST router + 9 个 inline endpoint。启动顺序 / 关停顺序 / atomic write / per-file lock 都到位。

**行为上**:  
链路全活, 但**输入信号薄**——agent 一周里只调过 22 次 `skill_*` (vs 1497 次工具总调用), 进化升级触发器跑 0 次, 4 个 persona 文件因为 LLM 后处理 prompt 苛刻而长期空置, 4 个 EventType 永远不发, 4 个 channel adapter 是 manifest scaffold, plugins/ 是空目录。

**安全上**:  
B-273 在 channel 入站 (Feishu) + sub-agent 同步 reply (chat_with_agent) + tool_result + memory_recall + skill_body 5 个面有覆盖, 但 `submit_to_agent` 异步 reply + WS/ACP/4 scaffold channel 没扫。9 个 yaml 规则中只 6 个 CRITICAL 全在 hardcoded_secrets, dangerous_shell_commands 类的 `rm -rf /` 还是 HIGH (走 APPROVE 不是 DENY)。

**真正的问题** 不是设计冲突, 而是:
1. agent 不主动 invoke skill_* (B-300 部分缓解)
2. ExtractLessonsHook prompt 太苛刻 (B-303 已放宽未真实流量验证)
3. min_plays=10 阈值对 1500 verdicts/周 (其中 1.5% skill-stamped) 设得偏高
4. 配置 hot-reload subscriber 缺失, ConfigFileWatcher 发了事件但没人接
5. 多 worktree 共享 daemon 端口 8765, 跨 worktree 测试互相打架
6. events.db 无 autovacuum, 长期运行会到 GB 级

下一步在 `docs/AUDIT_2026-05-07_conflicts.md` §15 有 20 项优先级清单。
