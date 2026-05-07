# XMclaw 项目冲突 / 重叠 / 死代码 / 缺口 全面审计报告

**审计时间**: 2026-05-07  
**审计方法**: 15 路并行 Explore agent 直读 `xmclaw/` 源码 (用户明确指示文档过期不可信)  
**覆盖范围**: 进化链 / 内存 / 技能 / lifespan / 前端 / LLM provider / Tool provider / Channel / Security / CLI / Bus / Multi-agent / UI components / Config / Logging / Plugin / Cron / Cost  
**总扫描文件**: ~200+ Python + ~60 JS, ~6000+ 行涉及

---

## 0. TL;DR

**好消息**:
- 没有功能性 race / deadlock — 11 个常驻 task + 多个 GRADER_VERDICT 订阅者并发, 状态机彼此独立, atomic write + per-file lock 都到位
- 启动顺序经过设计 (orchestrator → evo_agent → eval_trigger → variant_selector, shutdown 反序), 防止 trigger 调用已停的 agent
- 主要 LLM (Anthropic / OpenAI native + OpenAI-compat shims) 双向稳定, B-225 watchdog / B-229 truncation guard / B-189 timeout 都活
- 配置秘密路径 (`~/.xmclaw.secret`) 与数据路径隔离, pairing token 0600 perms, anti-req #8 守住

**坏消息** (7 大类问题):
1. **认知冲突**: 5 个 Evolution-X 类同名不同职; 6 个不同代码路径都 emit `SKILL_CANDIDATE_PROPOSED` 但 `decision` 字段语义对反 (propose vs promote)
2. **观察到的真实失败**: 4/7 persona 文件长期空置; B-273 sub-agent 异步路径不扫描; 跨 worktree daemon 端口 8765 撞车; events.db 无 autovacuum 单调增长
3. **死代码**: `xm-auto-evo` / `GeneForge` / `SkillForge` / `EvolutionEngine` 在多处注释/docstring/事件类型残留; `llm.default_provider` 配置已废但还在 example.json
4. **冗余路径**: ExtractLessonsHook + ExtractMemoriesHook 95% 重叠 (后者默认关); 双端点同义 `/llm/configure` vs `/llm/profiles`
5. **未实现 / 半实现**: 4 个 channel adapter (DingTalk/WeCom/WeChat/Telegram) 只有 manifest 没适配器; ACP 起 `NotImplementedError`; `xmclaw/plugins/` 空; Settings.js 多处 stub
6. **无配置 hot-reload subscriber**: ConfigFileWatcher 发 `CONFIG_RELOADED` 事件但没人订阅, 改了 tools / security 配置实际没效果
7. **测试覆盖洞**: XMC__ env override 无 E2E; CONFIG_RELOADED 无端到端; CostTracker 不从 config 注入; MutationOrchestrator EWMA<0.5 触发无回归测试

按"代价 vs 收益"排序的 cleanup 清单见 §11。

---

## 1. 命名冲突 (Cognitive Overload)

### 1.1 五个 `Evolution-X` 类全活, 但读代码时谁是谁分不清

| 类 | 文件 | 角色 |
|---|---|---|
| **Evolution**Agent | `daemon/evolution_agent.py:134` | observer + EWMA 聚合 + 写 state.json + emit promote/rollback |
| **Evolution**EvaluationTrigger | `daemon/evolution_evaluation_trigger.py` | GRADER_VERDICT 防抖 30s, 触发 evo_agent.evaluate() |
| **Evolution**Orchestrator | `skills/orchestrator.py` | 把 promote/rollback 翻译成 bus 事件; auto_apply 守门 |
| **Evolution**Controller | `core/evolution/controller.py:97` | 纯算法层, 阈值决策, 无 I/O 无 bus |
| **Evolution**Engine | (历史死代码) | docs / 注释还提, 已删 |

**问题**: "evolution_agent" 听着像执行者, 实际是观察者; "orchestrator" 听着像编排器, 实际只是 promote 翻译器。新人 (包括 Claude) 读代码必踩。

**建议**: 改名 `EvolutionAgent → EvolutionAggregator`, `EvolutionOrchestrator → SkillPromotionDispatcher`。

### 1.2 三个 `Memory.js` 看着是路由页, 实际是同一个

`Memory.js` (parent) + `Memory-Identity.js` (子组件 B-52 拆) + `Memory-NotesJournal.js` (子组件 B-49 拆), 因 500 行 budget 拆三文件。

**建议**: 改前缀 `_memory_identity_panel.js` / `_memory_notes_panel.js`, 表明不是顶级路由页。同样 `Settings.js` + `Settings-audio.js` + `ModelProfiles.js` (B-148 拆)。

### 1.3 多个 "trigger" 名称含义不同

- `EvolutionEvaluationTrigger` - 防抖触发 evaluate()
- `RealtimeEvolutionTrigger` - 防抖触发 dream.run_once()
- `SkillDreamCycle` 内部的 `_loop` - 周期触发 (没 "Trigger" 后缀但功能类似)

**建议**: 三者并列在同一文件 `triggers.py`, 用一致命名 `EvalTrigger / DreamTrigger / DreamCycle`。

---

## 2. 6 路提议源 emit 同一事件, decision 字段两种对反语义

### 2.1 emit `SKILL_CANDIDATE_PROPOSED` 的实际代码路径

| # | 路径 | 文件:行 | decision 字段 | 语义 |
|---|---|---|---|---|
| 1 | `SkillDreamCycle._emit_proposal` | `skill_dream.py:197` | `"propose"` | 建议**新建** skill |
| 2 | `RealtimeEvolutionTrigger` → 调 #1 | `skill_dream.py:364` | `"propose"` | 同 #1 |
| 3 | `EvolutionAgent._emit_proposal` | `evolution_agent.py:400` | `"promote"` | 建议**升级现有 skill 版本** |
| 4 | `EvolutionEvaluationTrigger` → 调 #3 | `evolution_evaluation_trigger.py:224` | `"promote"` | 同 #3 |
| 5 | `MutationOrchestrator._materialise` | `mutation_orchestrator.py:400` | `"promote"` | mutate 后建议升级 |
| 6 | `EvolutionAgent._emit_proposal` (rollback) | 同 #3 | `"rollback"` | 建议回滚 |

### 2.2 消费方按 decision 分流

```
SKILL_CANDIDATE_PROPOSED
  ├─ ProposalMaterializer (filter: decision="propose")
  │   → 写 ~/.xmclaw/skills_user/<id>/SKILL.md + register
  │
  └─ EvolutionOrchestrator (filter: 全收, auto_apply=False 默认观察)
      → registry.promote / rollback (auto_apply=True 时立即生效)
```

### 2.3 真实问题

- 看 events.db 时同一 `type='skill_candidate_proposed'` 行的 `payload.decision` 可能是 `"propose"` 或 `"promote"` 或 `"rollback"` — 三种语义对反
- 用户 / 调试者必须读 payload 才知道哪条是新建哪条是升级
- 任何按 `type` 过滤的下游 (UI, log analyzer) 默认混淆

**建议**: 拆成 3 个事件类型:
- `SKILL_CANDIDATE_DRAFTED` (新 skill, ProposalMaterializer 接)
- `SKILL_PROMOTION_RECOMMENDED` (升版本, Orchestrator 接)
- `SKILL_ROLLBACK_RECOMMENDED` (回滚, Orchestrator 接)

代价: 1-2 hr 跨 5 文件改, 加迁移把旧 `skill_candidate_proposed` 兼容映射几个月。收益: events.db 自描述, UI / CLI 分流变直白。

---

## 3. 内存子系统: 4/7 持久文件常空 + 95% 重叠的 hook

### 3.1 实际写入路径表

| 文件 | 写入者数 | 谁写 | 当前状况 |
|---|---|---|---|
| **MEMORY.md** | 5 | `ExtractMemoriesHook` (默认关) + `ExtractLessonsHook.failure_modes` + `remember` 工具 + `memory_pin` 工具 + `update_persona` + `DreamCompactor` (每日) | ✓ 106 条 |
| **USER.md** | 3 | `ProfileExtractor` (每 3 回合) + `learn_about_user` + `update_persona` | ✓ 25 条 |
| **IDENTITY.md** | 1 | 仅 `update_persona` (首装外无自动) | ✓ 1 条 |
| **AGENTS.md** | 1 | 仅 `ExtractLessonsHook.workflow` | ❌ 0 条 |
| **TOOLS.md** | 1 | 仅 `ExtractLessonsHook.tool_quirks` | ❌ 0 条 |
| **SOUL.md** | 1 (B-303 加) | 仅 `ExtractLessonsHook.values` | ❌ 0 条 |
| **LEARNING.md** | 1 (B-303 加) | 仅 `ExtractLessonsHook.rules` | ❌ 0 条 |

### 3.2 双 hook 95% 重叠

```
post_sampling 触发后并发跑两个 hook (asyncio.gather):
  ExtractMemoriesHook                ExtractLessonsHook
       │                                   │
       │ 每回合 1 次 LLM 调               │ 每回合 1 次 LLM 调
       │ 提取 durable facts              │ 提取 lessons (5 桶)
       │ → MEMORY.md "Auto-extracted"   │ → MEMORY.md "Failure Modes"
       │                                  │   AGENTS.md / TOOLS.md / SOUL.md / LEARNING.md
       │ 默认关 (enabled=False)           │ 默认开 (enabled=True)
```

两个 hook 都做 LLM 后处理 + 都写 MEMORY.md (不同 section)。ExtractMemoriesHook 默认关意味着多余的事件类型 + 配置 surface 仍然存在但没人用。

**建议**: 合并成单 hook `ExtractFactsHook`, 一次 LLM 调用 6 个桶 (failure_modes/auto-extracted-facts + 4 个 B-303 桶), 减少配置 surface 同时统一 prompt 维护。

### 3.3 ProfileExtractor 跟两个 hook 也部分重叠

ProfileExtractor 订阅 `USER_MESSAGE` + `LLM_RESPONSE`, 累 3 回合或 SESSION_LIFECYCLE 时 LLM 调一次抽 USER.md 的 preferences。这第三次 LLM 调用每 3 回合一次, 跟两个 hook 共用相同的对话上下文但分别构建自己的 prompt。

**建议**: 长期合 3 个抽取器, 一次 LLM 调用产 7 个桶 (USER preferences + 6 lesson 桶), 给 prompt 一个一致的 budget。

### 3.4 sqlite-vec 跟 markdown 双层一致性问题

B-198 Phase 3 引入 `PersonaStore`, 让 facts 先写 sqlite-vec 再 render 回 markdown。但:
- markdown 现在是**派生缓存**, 不是 truth, 然而代码里还有路径在 `ctx.persona_store is None` 时直接写 markdown
- 配置不一致时可能两个状态: vec 没数据但 markdown 有, 或反过来
- `_MANUAL_KIND` 标识手动写入的行, 但 `set_manual` 之前是否清掉旧手动行没明确

**建议**: 文档明确"vec 是 truth, markdown 是 view", 或干脆把 markdown render 单向化 (vec → md, 不反向)。

---

## 4. Skills 子系统: 装载 OK, 暴露层有死亡螺旋

### 4.1 装载链路本身健康

```
启动一次:    UserSkillsLoader.load_all() → SkillRegistry.replay_history()
每 10s:      SkillsWatcher._tick() → 重扫 + body hot-reload
事件驱动:    ProposalMaterializer._on_event() → 收 propose 事件 → 写 SKILL.md → register
```

四个装载者各干各的, 不冲突。`SkillRegistry.replay_history()` 修了 B-174 重启后 HEAD 重置 v1 的 bug。

### 4.2 工具暴露层瓶颈

```
404 个 skill              SkillToolProvider.list_tools()
       │                            │
       ▼                            ▼
  全部暴露 → 80K token tool_specs  →  LLM 注意力归零
       │                            │
       ▼                            ▼
B-238 prefilter top-K=12  →  CJK 查询 token 重叠 0 → LLM 看见 0 个 skill_*
       │                            │
       ▼                            ▼
B-299 skill_browse 元工具  →  LLM 不主动调 (B-300 加 turn-hint 部分缓解)
       │
       ▼
B-300 流量数据: 1497 verdicts / 7 天, 仅 22 条 skill-stamped (1.5%)
       │
       ▼
controller min_plays=10 永远不达 → 0 promote 被触发
```

### 4.3 死代码 (docs 里仍提)

| 名称 | 状态 |
|---|---|
| `GeneForge` | ❌ 从未存在 |
| `SkillForge` (`xmclaw/skills/skill_forge.py`) | ❌ 不存在, docs 仍提 |
| `EvolutionEngine` | ❌ 不存在, EvolutionController 是真控制器 |
| `xm-auto-evo` SKILL.md prompt 注入路径 | ❌ Phase 1 移除 |
| `_detect_skill_invocations` heuristic | ❌ 已删, 注释还在 (agent_loop.py:874, 897, 942, 964, 1050, 1325, 2944, 2977, 2130) |

**建议**: 一次 scrub 删干净, 30 min 工作量。

---

## 5. LLM Provider 层: 一致性 OK, 但 Anthropic 独享 cache_control

### 5.1 两个活的 provider, 各自健康

| 维度 | Anthropic | OpenAI |
|---|---|---|
| stream() | 通过原生 messages.stream() | chat.completions.create(stream=True) |
| complete() | messages.create() | chat.completions.create() |
| tool_call_shape | ANTHROPIC_NATIVE | OPENAI_TOOL |
| max_tokens | 4096 (8192 thinking) | 不显式设, 用 provider 默认 |
| **B-245 cache_control** | ✓ system + tools 标 ephemeral | ✗ 不支持 |
| B-229 truncation guard | ✓ stop_reason="max_tokens" 丢空 input 块 | ✓ finish_reason="length" 丢 args="" 累加器 |
| B-225 watchdog | ✓ 异步 close stream | ✓ 同上 |
| B-189 timeout | ✓ asyncio.wait_for(120s 默认) wrap stream | ✓ 同上 |

### 5.2 不对称: cache_control 仅 Anthropic 实现

OpenAI-compat (Kimi / GLM / MiniMax / Qwen / DashScope / vLLM / LiteLLM) 全用 `OpenAILLM`, 不享受 prompt cache。这意味着:
- 切到 Kimi / GLM 时每回合重传 8K-token system prompt 不缓存, token 成本 3-5x
- 跟 Anthropic 切换时用户感觉不到差别, 但帐单看出来

**建议**: OpenAI 流也支持 `cache_control` 元数据 (Kimi 文档有此扩展), 实现一个 OpenAIWithCacheLLM 子类。代价: 1-2 天。

### 5.3 dead provider 路径

无。代码里没有任何 commented-out 的 provider class, 没有 if-False blocks。注释里提的 Kimi / MiniMax / Qwen / DashScope 都是用 OpenAI-compat shim 跑, 不是单独类。

---

## 6. Tool Provider 层: 39 个内置工具, 1 个未注册自动化空挡, MCP 默认空白

### 6.1 BuiltinTools 39 个工具, schema-code 一致

无 schema/code drift (parallel specs + dispatch 在同一 builtin.py 里维护)。每个工具都有对应 schema + handler。

### 6.2 但 ChatGPT-style "computer-use" 不在 BuiltinTools

`pyautogui` / `mss` 类桌面自动化**不在** `xmclaw/providers/tool/`. 项目对 computer-use 的支持是 Claude Code MCP 层面接的, 不是 daemon 自身。

`automation.py` 里只有 cron / process / code_python — 不是 desktop automation。

**建议**: 文档明确"computer-use 走 Claude Code MCP, 不是 XMclaw 内置", 避免用户问 "为什么 XMclaw 不能截图"。

### 6.3 MCP servers 默认空白

```python
# config.example.json
"mcp_servers": {}    ← 空对象, 默认无 server
```

- 没有任何官方 MCP server 默认注入
- 用户必须手动配置 (`npx @modelcontextprotocol/server-filesystem` 等)
- Windows + npx 不在 PATH 时 fallback `MCPError("command not found")`, 走 ToolResult(ok=False), 不致命但用户看见错误

**建议**: 文档加 "Windows 用户先 `npm install -g npx` 或装 Node.js", 或者在 doctor check 里把 npx 缺失列为 MEDIUM。

### 6.4 Browser tool 状态

Playwright 7 个工具 (open/click/fill/screenshot/snapshot/eval/close). 始终在 list_tools() 出现, 即使 Playwright 没装。invoke 时报 `_PlaywrightMissing` 结构化错误。

**问题**: 始终暴露但通常没装 → token 浪费。
**建议**: 启动时 `playwright._ensure_playwright()` 失败时不要把 7 个工具加进 list_tools(), 减少每回合的 tool spec budget。

---

## 7. Channel 层: 4 个 scaffold, ACP 半实现

### 7.1 实际就绪状态

| Adapter | 状态 | 缺什么 |
|---|---|---|
| Feishu | ✓ 完整 | — |
| WS (CLI/Web UI) | ✓ 完整 | — |
| ACP (Zed/VSCode) | ⚠️ start() raise NotImplementedError | 完整端口, Phase 6.1 |
| DingTalk | ❌ 仅 manifest | 整个 adapter.py |
| WeCom | ❌ 仅 manifest | 整个 adapter.py |
| WeChat | ❌ 仅 manifest | 整个 adapter.py |
| Telegram | ❌ 仅 manifest | 整个 adapter.py |

### 7.2 真实安全洞: B-273 只在 Feishu 适配器

```python
# Feishu adapter
decision = apply_policy(text, policy=DETECT_ONLY, source=SOURCE_CHANNEL)
if decision.blocked: return  # drop
```

WS / ACP / 4 个 scaffold 都**没有**入站 prompt-injection 扫描。用户从 Telegram (一旦实现) 发的消息会绕过 B-273 守门员, 直接进 agent 上下文。

**建议**: 把 scan 抽到 `ChannelDispatcher` 层做一次, 适配器层不重复, 强保所有 channel 都覆盖。代价: 1 hr 重构。

### 7.3 channel adapter 没重试 / 退避

每个 adapter 失败后只 log + propagate, 没有 backoff retry。Feishu API 偶尔 429 / 5xx 会直接吞掉一条 reply, 用户不知道。

**建议**: 适配器层加通用 retry policy (指数退避 3 次)。

---

## 8. Security 层: 多层堡垒但 sub-agent 异步路径有洞

### 8.1 完整的扫描覆盖图

```
                    ┌─────────────────────────────────────┐
                    │  Tool result        ✓ 扫 (line 2810)│
                    │  Memory recall      ✓ 扫 (line 1962)│
                    │  Channel inbound    ✓ Feishu only   │
                    │  Sub-agent reply    ✓ chat_with_agent│
USER_MESSAGE  ───►  │  Sub-agent reply    ✗ submit_to_agent│ ← 漏
                    │  Skill body load    ✓ skill_guard    │
                    │  Skill source       ✓ skill_scanner  │
                    │  User msg (raw)     ✗ 没扫           │ ← 漏
                    └─────────────────────────────────────┘
```

### 8.2 真实的两个洞

1. **`submit_to_agent` 异步任务的 reply 不扫** — `chat_with_agent` (同步) 在 `agent_inter.py:354` 扫 SOURCE_SUB_AGENT, 但 `submit_to_agent` 后台任务的结果在 `check_agent_task` 取出时**没**走扫描。恶意 sub-agent 可以从这条路注入。

2. **用户原始 message 不扫** — `agent_loop.run_turn` 收到 user_message 后直接进消息构建, 没经过 `apply_policy`. 走 channel 的 (Feishu) 在 channel 层扫了, 但走 WS 的没扫 (WS 当作 internal 不扫合理, 但要文档化)。

**建议**: 
- `submit_to_agent` 完成后入库前调用 `apply_policy(SOURCE_SUB_AGENT)`, 把结果替换为 marker 或 block 整个 result
- 文档明确 "WS 是 trust boundary 内部, 不扫"

### 8.3 9 个 YAML rule 文件中只 6 个 CRITICAL 规则全在 hardcoded_secrets.yaml

```
hardcoded_secrets.yaml      8 rules,  6 CRITICAL  (AWS/Stripe/Google/GitHub/PrivateKey/ConnString)
command_injection.yaml      12 rules, 0 CRITICAL  (HIGH at most)
dangerous_shell_commands.yaml 20, 0 CRITICAL
其它 6 个 yaml 文件         合计 35, 0 CRITICAL
```

**问题**: rm -rf 类危险命令最高 HIGH (走 APPROVE), 不是 CRITICAL (走 DENY). 用户被频繁 prompt 而不是直接拒绝。

**建议**: 在 dangerous_shell_commands.yaml 把 "rm -rf /" / "dd if=/dev/zero of=/" 类无可挽回操作提到 CRITICAL。

---

## 9. Bus / 事件 / 持久化层: 4 个永远不发的事件 + 无 autovacuum

### 9.1 EventType 30 个枚举, 4 个 dead

```
USER_MESSAGE, LLM_REQUEST, LLM_CHUNK, LLM_THINKING_CHUNK, LLM_RESPONSE,
AGENT_ASKED_QUESTION, USER_ANSWERED_QUESTION, CONFIG_RELOADED,
TOOL_CALL_EMITTED, TOOL_INVOCATION_STARTED, TOOL_INVOCATION_FINISHED,
SKILL_EXEC_STARTED  ❌ 已死, xm-auto-evo 残留
SKILL_EXEC_FINISHED ❌ 已死, xm-auto-evo 残留
GRADER_VERDICT, COST_TICK, SESSION_LIFECYCLE,
SKILL_CANDIDATE_PROPOSED, SKILL_PROMOTED, SKILL_ROLLED_BACK,
ANTI_REQ_VIOLATION, TODO_UPDATED, PROMPT_INJECTION_DETECTED,
MEMORY_EVICTED, MEMORY_OP,
SKILL_INVOKED      ❌ 已死, _detect_skill_invocations 移除
SKILL_OUTCOME      ❌ 已死, 同上
MEMORY_DREAMED, MEMORY_INDEXED, CONTEXT_COMPRESSED, USER_PROFILE_UPDATED
```

4 个 EventType 永远不会被 publish, 但还在枚举里。订阅者 (Trace.js, JournalWriter) 还在监听它们, 浪费 filter 周期。

**建议**: 删 4 个枚举值 + 清订阅者。代价: 30 min。

### 9.2 events.db 单调增长无 autovacuum

```python
# sqlite.py:188
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
# 没有 PRAGMA auto_vacuum=INCREMENTAL
```

WAL 模式不自动 reclaim 已删行空间。今天 events.db 已经 ~100MB (每天 ~10MB+ 增长)。半年后会到 GB 级。

**建议**: 加 `PRAGMA auto_vacuum=INCREMENTAL` + 周期性 `PRAGMA incremental_vacuum(N)`. 或者每日 cron 跑一次 retention pruning (老于 30 天的事件归档/删除)。

### 9.3 in-memory session_logs 每 session 400 cap

```python
# app.py:1579
_SESSION_LOG_CAP = 400
```

WS 重连时 replay 的 in-memory 缓冲, 超 400 事件就丢最早的。**问题**: 长会话 (chat 几小时) 时早期 turn 的工具卡片在重连后看不见。

**建议**: 重连时混合 in-memory + sqlite query (后者补足超出 cap 的早期事件)。

---

## 10. Multi-agent / Pairing / Worktree: 共享 daemon, 共享 bus, 跨 worktree 端口撞车

### 10.1 共享导致的隐式耦合

```
单 daemon 进程
  ├─ 所有 workspace agent 共享 InProcessEventBus
  ├─ 所有 agent 共享 ~/.xmclaw/persona/profiles/<active>/ 7 文件
  ├─ 所有 agent 共享 ~/.xmclaw/skills_user/* registry
  ├─ 所有 agent 共享 ~/.xmclaw/v2/events.db / memory.db
  └─ 单一 pairing token 对所有 channel 生效
```

**真实风险**:
- agent A 的 evolution_observer 收到 agent B 的 GRADER_VERDICT (没 agent_id 过滤的话)
- agent A 在 USER.md 写"用户喜欢 vim", agent B 上下文也包到这条
- 一个 sub-agent 的 SKILL.md mutation 会影响所有 agent 的 HEAD

**建议**:
- 短期: 文档明确"sub-agent 默认共享主 agent 的世界, 想要 isolation 用 separate workspaces"
- 长期: per-agent 的 persona 子目录 + per-agent skill registry view

### 10.2 跨 worktree daemon 端口撞车 (我们今天实测过)

4 个 git worktree (busy-chaum / clever-lichterman / goofy-lichterman / optimistic-brown) 都默认连 `127.0.0.1:8765`. 今天 bucket C 测试时另一 worktree 的 `tests/integration_real/test_real_flow.py` 把 daemon 跑挂, 我的 probe 撞车。

**建议**:
- daemon 加 `--port` flag + `XMC_DAEMON_PORT` env 支持
- worktree 路径 hash → port (8765..8800 范围)
- integration_real 测试加 `~/.xmclaw/v2/integration_test.lock` 互斥

### 10.3 sub-agent 共享 bus, B-273 守不住 submit_to_agent

详见 §8.2。

---

## 11. CLI / Doctor / Backup / Cron: 39 命令活, 1 doctor check 缺自动 fix

### 11.1 命令完整性

39 个 typer command 全部活, 无 deprecated 残留。`xmclaw config init` 不是真 interactive (skeleton-write 模式), 用户期望 wizard 但实际是 `--api-key` flag 驱动。

**建议**: 改名 `xmclaw config init` → `xmclaw config skeleton`, 给真 wizard 一个新名字 `xmclaw onboard --interactive`。

### 11.2 24 个 doctor check, auto-fix 仅 3 个

```
ConfigCheck.fix()       创建缺失文件
WorkspaceCheck.fix()    mkdir
PairingCheck.fix()      chmod, unlink

剩 21 个 check 没 fix() 实现:
  events_db, memory_db, memory_providers,
  memory_provider_config, persona_profile, dream_cron,
  skill_runtime, evolution_runtime, evolution_pipeline, ...
```

**建议**: 至少给 `events_db` / `memory_db` 加 fix (如 PRAGMA integrity_check 失败时备份+rebuild)。代价: 中等。

---

## 12. 前端: 22 页 + 20 路由 + 几个孤儿 + 1 个 dead icon library

### 12.1 孤儿端点

- `/api/v2/workspaces` (workspaces.py router) — 无前端引用 (workspaces 是 agent presets, Settings.js 不读)
- `/api/v2/secrets` (secrets.py router) — 无前端引用 (secrets 是 CLI 命令访问)

### 12.2 双端点同义

- `/api/v2/llm/configure` (app.py inline) ↔ `/api/v2/llm/profiles` (router) — Settings.js 只用后者, 前者历史
- `/api/v2/pending_questions` (app.py inline) ↔ `/api/v2/approvals` (router) — Security.js 只用后者, 前者历史

### 12.3 死前端组件

- **icon.js** (atoms/, 111 lines) — Lucide-style SVG library, **零调用点**. 注释说"为未来扩展", 但这扩展什么时候来不清楚
- **BuddyMascot.js** (molecules/, 112 lines) — 吉祥物组件, 没在 AppShell 渲染, 没 PluginSlot 钩子
- **ACP.js** 类似的前端钩子也都未实现

### 12.4 app.js 581 行超 budget

`test_no_file_exceeds_line_budget` 一直失败, app.js 581 行 > 500 budget. 24 个页面 import (lines 82-100) 不可避免, 但 boot 序列 (lines 122-245) 可拆。

**建议**: 拆 `app/boot.js` + `app/router.js` + `app.js` (entry only)。

---

## 13. 配置 / 日志 / Plugin / Cost: 配置文件有死字段, 日志洞口

### 13.1 死配置字段 (config.example.json 有但没人读)

| Key | 状态 |
|---|---|
| `default_provider` (line 5) | B-146 起废, factory 不读, 仍在 example |
| `backup` (lines 103-109) | 文档说为 Epic #20, factory 不读, lifespan 读 |
| `channels` (lines 111-121) | factory 不读, 适配器加载层读 |
| `integrations` (lines 122-133) | factory 不读, integration router 读 |
| `mcp_servers` (line 110) | factory 不读, MCP registry 读 |
| `gateway` (lines 79-82) | factory 不读, FastAPI 设置读 |

**问题**: 用户改 `default_provider` 期望切默认 LLM, 实际不生效。其它 6 项虽然有人读但不是 factory, 错误归属难调。

**建议**: 删 `default_provider`. 其它 6 项加 docstring "consumed by lifespan / channel layer / MCP registry"。

### 13.2 ConfigFileWatcher 发事件无人订阅

```
config.json mtime 变  →  ConfigFileWatcher 5s poll  →  publish CONFIG_RELOADED
                                                          │
                                                          ▼
                                                   ┌──────┴──────┐
                                                   │  没人订阅!   │
                                                   └─────────────┘
```

`tools` / `security` / `evolution` 段技术上是"runtime 可读", 但代码里**没**任何 subscriber 在 CONFIG_RELOADED 上重读自己的 slice. 用户改 `tools.allowed_dirs` 后 BuiltinTools 仍用旧值。

**建议**: 至少给 `tools` 和 `security.guardians.policy` 加 hot-reload subscriber. 代价: 中等。

### 13.3 setup_logging 修了 (B-298) 但日志没 level override

```python
# log.py:76
root.setLevel(logging.INFO)   ← 硬编码, 没 config 入口
```

用户想看 DEBUG 看不到, 想关 INFO 静音也关不掉。

**建议**: 加 `logging.level` 配置项 + `XMC_LOG_LEVEL` env 覆盖。

### 13.4 CostTracker 不从 config 注入

```python
# cost.py:75
class CostTracker:
    budget_usd: float = 0.0  ← 默认无限, 不从 cfg.cost 读
```

anti-req #6 提"hard cap on token cost", 但实现是 caller-driven, 没 config 入口, 没 daemon-level hard cap。

**建议**: factory.py 读 `cfg.cost.{daily_budget_usd, hard_cap_usd}` 注入 AgentLoop。

### 13.5 plugins/ Epic #2 空目录

`xmclaw/plugins/` 目录不存在 / 空。Plugin discovery framework (`xmclaw.doctor.checks` entry_points) 在但没实际 plugin shipped. Epic #2 状态 unclear。

---

## 14. 测试覆盖洞

| 缺口 | 影响 |
|---|---|
| `XMC__` env override 无 E2E 测试 | 用户改 env 后行为是否生效不可证 |
| `CONFIG_RELOADED` 端到端无测试 | 改 config 是否真正 hot-reload 无回归 |
| `MutationOrchestrator` EWMA<0.5 触发 mutate 无回归 | DSPy mutator 行为漂移看不见 |
| 跨 worktree daemon collision 无 chaos 测试 | 多 worktree 用户场景生产中才知道 |
| `submit_to_agent` 异步 reply B-273 扫描 (现在没扫) 无 test | 安全洞 |
| Anthropic cache_control 命中率无 test | B-245 是否真生效不可证 |
| Auto-Dream MEMORY.md 重写后 PINS 保留无 test | DreamCompactor 误删 PINS 用户当面打脸 |

---

## 15. 全局优先级 cleanup 清单 (按代价 / 收益排序)

| # | 动作 | 预计代价 | 收益 |
|---|---|---|---|
| 1 | 一次性扫 `xm-auto-evo` / `GeneForge` / `SkillForge` / `EvolutionEngine` 字面量 + 删 dead 注释 | 30 min | 读代码不再被引导到不存在的子系统 |
| 2 | 删 4 个 dead EventType (SKILL_EXEC_STARTED/FINISHED, SKILL_INVOKED, SKILL_OUTCOME) | 15 min | 枚举与现实一致 |
| 3 | 删 `llm.default_provider` 配置项 + docstring 标注 6 个 lifespan-only key | 30 min | 配置文件自描述 |
| 4 | `Memory-Identity.js` / `Memory-NotesJournal.js` 加 `_panel_` 前缀 | 15 min | 不再误判 3 个 Memory 页 |
| 5 | events.db 加 `PRAGMA auto_vacuum=INCREMENTAL` + 每日 retention pruning cron | 1-2 hr | 长期运行 daemon 不会 GB 级膨胀 |
| 6 | dangerous_shell_commands.yaml `rm -rf /` 类提到 CRITICAL | 30 min | 危险操作真正 DENY 不是 APPROVE |
| 7 | `submit_to_agent` reply 加 B-273 扫描 (调 apply_policy SOURCE_SUB_AGENT) | 1 hr | 闭合安全洞 |
| 8 | 把 5 个 `Evolution*` 类至少 2 个改名 | 1-2 hr (跨文件 rename) | 读代码不再"哪个是哪个" |
| 9 | 拆 `SKILL_CANDIDATE_PROPOSED` 成 3 个事件类型 + 兼容映射 | 1-2 hr | events.db 自描述 |
| 10 | ExtractMemoriesHook + ExtractLessonsHook + ProfileExtractor 合 3 → 1 | 半天 | 减少每回合 3 次 LLM 调用为 1 次 |
| 11 | 删 `/api/v2/llm/configure` + `/api/v2/pending_questions` inline 端点 | 1 hr | 减少 router-vs-inline 双名 |
| 12 | daemon 加 `--port` flag + worktree-aware 端口分配 | 半天 | 跨 worktree 不再撞车 |
| 13 | `tools` + `security.guardians` 加 CONFIG_RELOADED subscriber | 半天 | hot-reload 真生效 |
| 14 | docs/EVOLUTION.md / docs/V2_DEVELOPMENT.md / README skill section 重写 | 2 天 | 文档不再骗人 |
| 15 | `logging.level` config + env override + per-component level | 1 天 | DEBUG / WARN 切换 |
| 16 | CostTracker 从 cfg.cost 注入 daemon-level 预算 | 1 天 | anti-req #6 真闭合 |
| 17 | Anthropic cache_control 命中率统计 + UI 展示 | 1 天 | B-245 ROI 可见 |
| 18 | 删 dead UI components (icon.js / BuddyMascot.js) | 30 min | 减少 5 个 maintenance 项 |
| 19 | doctor check 给 events_db / memory_db 加 fix() | 半天 | 自动修常见 SQLite 损坏 |
| 20 | tests/integration_real/* 加 lock-file 互斥 | 1 hr | 多 agent 跑测试不互相干掉 |

**总工时估**: 5-7 个工作日清掉前 12 项 (用户体验影响最大), 把 13-20 当成下一阶段 hardening。

---

## 16. 不在本份报告范围 (但需要提醒)

- **Channel 4 个 scaffold (DingTalk / WeCom / WeChat / Telegram)** — 不算 cleanup, 是"未实现", 走 roadmap
- **ACP `start()` 抛 NotImplementedError** — 同上, Phase 6.1 任务
- **plugins/ 空目录** — Epic #2 任务
- **MCP server 默认空白** — 用户配置任务, 不是 cleanup
- **frontend 22 页线 budget 失败** — 拆 app.js 是 cleanup, 其它页超是必要复杂度

这一份不动代码, 全是观察。下一份"真实架构图"在 `docs/AUDIT_2026-05-07_real_architecture.md` 一起更新。
