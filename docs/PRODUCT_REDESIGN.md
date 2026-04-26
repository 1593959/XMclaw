# XMclaw 产品重新设计

**版本**：v1（2026-04-26）
**作者**：基于对 4 个核心竞品（OpenClaw / Hermes Agent / Hermes-Self-Evolution / QwenPaw）+ 5 个 commodity 层竞品（Cline / Continue / Aider / AutoGPT / Open-WebUI）的源码级深度调研。
**状态**：定稿，按本文实施。

---

## 0. 战略定位 ⭐

XMclaw **唯一**应该自己写的东西：

1. **Honest Grader**（硬检查 ≥0.80 + LLM 上限 ≤0.20）
2. **SkillScheduler**（UCB1 over candidates）
3. **EvolutionController**（promotion gate：plays / mean / gap-vs-head / gap-vs-second）
4. **SkillRegistry**（版本化 + rollback）
5. **Mutation Engine**（**目前缺，Phase 0 必补**）
6. **BehavioralEvent 语义**（事件类型表 = 产品契约；传输层可换）

**其余全部抄。** Chat UI、流式渲染、工作区概念、配置文件布局、模型 picker、跨会话记忆、MCP hub、审批分级、多智能体、渠道适配器 —— 每一项都有现成最优解，不要原创。

### 对各竞品定位的一句话

| 竞品 | 形态 | 强项 | 弱点 | 我们抄什么 |
|---|---|---|---|---|
| **OpenClaw** | TS + 25+ 渠道 + Lit UI | 渠道插件协议、Canvas | 沙箱表演性、agent loop 外包给一人维护的库、无 evolution | 渠道协议、Canvas live-reload、skill loader 安全检查 |
| **Hermes Agent** | Python + 17 渠道 + ACP/MCP | ACP 集成（白嫖 Zed/VSCode）、cron、6 种 terminal backend | 12.8k 行 god file、cache fragility 禁止 hot reload、skill scanner 默认 OFF | ACP server、cron 调度器、FTS5 跨会话 |
| **Hermes-Self-Evolution** | DSPy/GEPA wrapper | 唯一有 mutation 的 | Tier 1 实现，2-5 全空；fitness 是关键词重叠 LLM-on-LLM；PR 生成代码不存在；批量 CLI 不能 hot reload | **DSPy/GEPA mutation engine 的集成模板**、合成数据集生成器、历史挖掘器、train/val/holdout 50/25/25 |
| **QwenPaw** | Python + React + 20 渠道 + 多智能体 | 真正 working 的多智能体、中文 5 渠道（Feishu/DingTalk/WeCom/微信/Telegram）、cloudflared 自动隧道 | inter-agent 走 HTTP loopback（同 daemon 也走）、无 evolution、ReActAgent 外包给 AgentScope、Windows chromadb 降级 | 多智能体 4 项约定、中文渠道适配器、cloudflared、UI drag-reorder |
| **Open-WebUI** | Python FastAPI + Svelte | 流式 markdown 渲染（rAF + marked.lexer）、tool card 显示、Socket.IO + auto reconnect | 无 evolution，无 workspace 概念 | `Markdown.svelte`、`ToolCallDisplay.svelte`、`chat_memory_handler` |
| **Cline** | TS + VSCode 扩展 | MCP hub（多服务 + file watcher + OAuth）、子智能体并发状态行、auto-approve 分级菜单 | VSCode 锁定 | MCP hub、子智能体 status row、审批菜单 |
| **Continue** | TS + VSCode/JetBrains | 配置 = 7 类 yaml 文件夹（agents/models/rules/prompts/mcpServers/docs/context） | VSCode 锁定 | `<workspace>/.xmclaw/<type>/*.yaml` 完整目录约定、参数感知策略 |
| **Aider** | Python CLI | append-only `.aider.chat.history.md`，重启即恢复；ChatSummary LLM 摘要老消息 | 终端无 UI | 单文件追加持久化 + LLM 老消息摘要 |

---

## 1. 工作区逻辑（Workspace）

### 现状（错的）
"工作区"页是个把技能 / 智能体 / 人格 / 记忆 / 工作区配置文件**全堆在一起的文件树编辑器**。**所有竞品都不这么做。**

### 目标设计（抄 Cline + Continue）

**工作区 = 一个目录**，就这样。数据结构（来自 `cline/src/shared/multi-root/types.ts:11-16`）：

```python
@dataclass(frozen=True, slots=True)
class WorkspaceRoot:
    path: Path                 # 绝对路径
    name: str                  # 显示名（默认 path.name）
    vcs: Literal["git","none"] = "none"
    commit_hash: str | None = None
```

### 流程

1. **首次启动**：daemon 读 `~/.xmclaw/state.json` 里 `workspace_roots[]` 和 `primary_index`。空就用 `Path.cwd()`。
2. **用户切工作区**：顶栏目录 picker。点击 → fileinput → 写 `state.json` → daemon 不重启，agent loop 下一轮就在新 cwd。
3. **每会话 tag**：`SessionStore` 每条 session 记录 `cwd_on_init`（抄 `cline/src/shared/HistoryItem.ts:14`）。会话列表可按 workspace 过滤。

### 配置文件布局（抄 Continue 7 块）

```
~/.xmclaw/                       # 全局
├── state.json                   # workspace_roots + primary_index
├── config.json                  # daemon 配置（API keys 等）
├── agents/*.yaml                # 全局智能体
├── models/*.yaml                # 模型 profile
├── rules/*.md                   # 全局规则
├── prompts/*.prompt             # 全局 prompt 模板
├── mcpServers/*.yaml            # 全局 MCP 服务
├── skills/<name>/SKILL.md       # 全局技能
├── memory/MEMORY.md             # 全局长期记忆
├── memory.db                    # SqliteVec 向量记忆
├── sessions.db                  # 会话历史
└── v2/                          # daemon 运行时数据

<workspace>/.xmclaw/             # 项目级（覆盖全局）
├── agents/*.yaml                # 项目专用智能体
├── rules/*.md                   # 项目规则
├── prompts/*.prompt
├── mcpServers/*.yaml
├── skills/<name>/SKILL.md
└── memory/MEMORY.md             # 项目记忆
```

合并规则（同 Continue `loadLocalAssistants.ts:104-125`）：项目级覆盖全局；同名时项目级胜出。

### 当前要删的代码
- `xmclaw/daemon/static/pages/Workspace.js` 现版（文件树编辑器）→ 重写为 folder picker
- `xmclaw/daemon/routers/workspaces.py`（前端 0 调用，无 daemon 集成）→ 改为 `/api/v2/workspace/{roots,switch}` 真接 `state.json`

---

## 2. 智能体逻辑（Agent）

### 三种"agent"概念，必须分清

| 概念 | 是什么 | 例子 |
|---|---|---|
| **AgentLoop** | 单次 turn 编排器（user msg → LLM hop → tool → 终态） | 一个进程一个 |
| **Agent profile** | 一份 yaml 配置（name + model + system_prompt + tools allow-list + skills enabled） | `~/.xmclaw/agents/coder.yaml` |
| **Live Agent**（多智能体） | 当前运行中 profile 的实例，含会话状态 | `MultiAgentManager` 里 `agents["coder"]` |

### Agent profile yaml 格式（抄 Continue `agents/*.yaml`）

```yaml
name: coder
description: 写代码 / 调试 / 跑测试
model: claude-opus-4-7
system_prompt: |
  You are XMclaw in coding mode. ...
tools:
  allow: [file_read, file_write, apply_patch, bash, list_dir]
  deny: [web_fetch]
skills:
  - software-development
mcp_servers: [filesystem]
```

### 多智能体逻辑（抄 QwenPaw 4 项约定）

QwenPaw 的多智能体是 working 的，因为他们守了**4 项约定**：

1. **`X-Agent-Id` header**：每个 HTTP / WS 请求带 agent id（`app/agent_context.py`）
2. **ContextVar 透传**：异步栈里不丢 agent 身份（`app/agent_scoped.py`）
3. **lazy-locked workspace dict**：`MultiAgentManager` 用 `_pending_starts: Dict[str, asyncio.Event]` 去重并发启动
4. **identity-prefix loop guard**：跨 agent 调用消息加 `[Agent X requesting]` 前缀防自调用

XMclaw 当前 `MultiAgentManager` **只有 (3)**，缺另外 3 项 → Agents 页是只读的不能用。

### Inter-agent 工具集（抄 QwenPaw `agents/tools/agent_management.py`）

四个工具，**但 transport 改成内部 bus 调用**（不抄他们的 HTTP loopback）：
- `list_agents()` → 返回当前 profile 列表
- `chat_with_agent(agent_id, message)` → 同步等回复
- `submit_to_agent(agent_id, message)` → 异步 task_id
- `check_agent_task(task_id)` → 拉异步结果

### 流程

1. 用户在 UI 顶栏选 `coder`，前端 set `localStorage.activeAgent = "coder"`
2. WS 连接带 `X-Agent-Id: coder`
3. daemon 收到 → 中间件读 header → 设 ContextVar → `MultiAgentManager.get("coder")` 拿 AgentLoop 实例
4. `AgentLoop` 用 coder profile 的 model + system_prompt + tools 跑 turn
5. coder agent 自己调 `submit_to_agent("researcher", "查 X")` → 内部 bus 调用另一个 AgentLoop 实例（不出 daemon）

---

## 3. 会话与记忆逻辑（Session + Memory）

### 三层记忆

| 层 | 存什么 | 何时写 | 何时读 |
|---|---|---|---|
| **会话历史** | 当前 session 的 message[] | 每 turn 后 `SessionStore.save` | turn 开始时 `SessionStore.load(session_id)` |
| **跨会话向量记忆** | 历史 turn 的 embedding | 每 turn 结束后 `memory.put(turn)` | 每次 LLM 调用前 `memory.query(last_user_msg, top_k=3)` 注入 |
| **MEMORY.md（用户/项目记忆）** | 用户手写或 agent 写的长期事实 | 用户编辑 / agent 调 `memory_save` 工具 | 每 turn 前注入 system prompt（截断到 2KB） |

### 跨会话记忆流程（抄 open-webui `chat_memory_handler` `utils/middleware.py:1473-1505`）

```python
# 在 agent_loop.run_turn 调 LLM 之前 ~10 行：
async def _inject_cross_session_memory(messages, session_id):
    if memory is None: return messages
    last_user = next(m.content for m in reversed(messages) if m.role == "user")
    hits = await memory.query(text=last_user, k=3, threshold=0.5,
                              filters={"session_id": {"$ne": session_id}})
    if not hits: return messages
    ctx = "\n".join(f"{i+1}. [{h.ts:%Y-%m-%d}] {h.text}"
                    for i, h in enumerate(hits))
    # 注入到 system message 末尾，不创建新 message（保护 prompt cache）
    return _append_to_system(messages, f"\n\n# 相关历史回忆\n{ctx}\n")
```

```python
# turn 结束后 ~3 行：
async def _persist_turn(turn_text, session_id):
    if memory is None: return
    await memory.put(layer="long",
                     item=MemoryItem(text=turn_text,
                                     metadata={"session_id": session_id, "ts": time.time()}))
```

### 会话裁切（抄 aider `history.py:33-90` `ChatSummary.summarize_real`）

当 messages token > `history_cap`（默认 ~30k）时：
1. 切两半：head（旧）/ tail（新）各 50%
2. LLM 调用一次，把 head 摘成一段 ~500-token 总结
3. messages = `[system] + [synthesized summary as system msg] + tail`

放在后台线程，不阻塞当前 turn。

### Aider 备份模式（备用方案）
如果 SqliteVec 性能不够好，**降级方案**：每 turn `f.write(text)` 追加到 `~/.xmclaw/sessions/<id>.md`，启动时全读回来 split 成 messages。aider 跑了 3 年这套，能用。

---

## 4. 技能与进化逻辑（Skills + Evolution）

### 技能数据模型

```yaml
# ~/.xmclaw/skills/<name>/SKILL.md（abuts Hermes/OpenClaw 现成约定）
---
name: software-development
description: 软件开发流程
version: 1.2.0
metadata:
  xmclaw:
    tags: [coding, debug]
    permissions:           # ← 这是 XMclaw 差异点（peer 都没有）
      fs: [read, write]
      net: deny
      exec: confirm
    related_skills: [writing-plans, testing]
---
# Body 内容（注入为 user message，protect prompt cache）
...
```

### 进化流程（XMclaw 真本钱，必须做对）

```
                 用户消息
                    │
                    ▼
            ┌───────────────┐
            │  AgentLoop    │
   ┌────────│  run_turn     │────────┐
   │        └───────────────┘        │
   │                                  │
   │  bus: TOOL_INVOCATION_FINISHED   │
   ▼                                  ▼
┌──────────┐                  ┌────────────────┐
│ Honest   │                  │ SkillScheduler │  UCB1 选当前 turn 用哪个变体
│ Grader   │                  └────────────────┘
└──────────┘
   │
   │ verdict {hard:0.85, llm_opinion:0.20}
   ▼
   bus: GRADER_VERDICT
   │
   ▼
┌────────────────────┐    plays >= 10
│ Evolution          │    mean >= 0.65    promote
│ Controller         │───►gap_head >= 0.05 ─►SkillRegistry HEAD = candidate
└────────────────────┘    gap_2nd >= 0.03      bus: SKILL_PROMOTED
   │                       
   │ 当 Honest Grader 多次 verdict 持续低
   ▼ ────► trigger Mutation
┌────────────────────┐
│ Mutation Engine    │  ← Phase 0 要补：dspy.GEPA().compile() wrapper
│ (DSPy/GEPA wrapper)│    输入：当前 head skill + 历史 grader_verdict events
└────────────────────┘    输出：新 candidate variants
   │                       
   ▼
   bus: SKILL_CANDIDATE_PROPOSED
   │
   ▼
   SkillScheduler 把新 candidate 加入 UCB 池
```

### Mutation Engine（**Phase 0 必补**）

抄 `hermes-self-evolution/evolution/skills/evolve_skill.py:157-177` 的 DSPy/GEPA wrapper，但 **fitness 换成 XMclaw 的 grader**：

```python
# xmclaw/core/evolution/mutator.py（新文件）
import dspy

class XMclawSkillModule(dspy.Module):
    """把 SKILL.md body 当成 1-field 优化参数。"""
    def __init__(self, baseline_text: str):
        super().__init__()
        self.skill = dspy.ChainOfThought("user_msg -> response", instructions=baseline_text)

def xmclaw_fitness(example, prediction, trace=None) -> float:
    """用 XMclaw Honest Grader（不是 Hermes 的 LLM-on-LLM rubric）。"""
    from xmclaw.core.grader import grade
    verdict = grade(prediction, example.expected)
    return verdict.score   # 0.80 hard checks + 0.20 LLM opinion

def evolve(skill_id: str, *, iterations: int = 10, holdout: float = 0.25):
    baseline = load_skill(skill_id)
    trainset, valset, holdoutset = build_dataset_from_history(skill_id, holdout=holdout)
    
    optimizer = dspy.GEPA(metric=xmclaw_fitness, max_steps=iterations)
    evolved = optimizer.compile(XMclawSkillModule(baseline.body),
                                 trainset=trainset, valset=valset)
    
    # Constraint validation（抄 hermes-self-evolution/constraints.py:30-174）
    if not validate_size_growth_structure(evolved):
        return None
    
    # Holdout score
    holdout_score = mean(xmclaw_fitness(ex, evolved(ex.user_msg)) for ex in holdoutset)
    
    return Candidate(text=evolved.skill.instructions,
                     holdout_score=holdout_score,
                     baseline_score=baseline.score)
```

### Dataset 来源（抄 `hermes-self-evolution/external_importers.py:157-416`）

XMclaw 的 grader 已经在每 turn 跑 → 历史 verdict events 全在 `events.db`。Dataset = 过去 N 天 (user_msg, hard_check_pass) pairs。**不需要合成数据集**（除非 cold start）。

### Promotion gate（已实现，不动）
`xmclaw/core/evolution/controller.py:154-201`：plays ≥ 10、mean ≥ 0.65、gap_vs_head ≥ 0.05、gap_vs_second ≥ 0.03 → promote。

### Rollback（要补）
`EvolutionDecision.ROLLBACK` 是 reserved 但没实现。补：当 promoted 后下 N 局 mean 跌 > 0.10 → 自动 rollback to prior version。

---

## 5. 工具与 MCP 逻辑（Tools + MCP）

### 工具分层

```
ToolProvider（ABC）
├── BuiltinTools（fs/exec/web）             ← 已有
├── AgentInterTools（chat_with_agent 等）   ← 已有但只在 primary 上挂
├── MCPHub（多个 MCP 服务）                  ← Phase 1：抄 cline McpHub
└── PluginTools（用户自定义）                ← 删 plugins/loader.py 空壳
```

### MCP Hub（抄 cline `services/mcp/McpHub.ts:213-273`）

```python
# xmclaw/providers/tool/mcp_hub.py（新文件）
class McpHub:
    """多 MCP 服务器管理，抄 Cline。"""
    def __init__(self, settings_path: Path):
        self._connections: dict[str, MCPConnection] = {}
        self._settings_path = settings_path
        self._watcher = FileSystemWatcher(settings_path, on_change=self.reload)
    
    def reload(self):
        spec = json.loads(self._settings_path.read_text())
        # spec.mcpServers = {name: {command|url, args, env, autoApprove[], disabled, timeout}}
        for name, cfg in spec.get("mcpServers", {}).items():
            if cfg.get("disabled"): continue
            if name not in self._connections:
                self._connections[name] = MCPConnection(name, cfg).start()
        # 删除消失的
        for name in list(self._connections):
            if name not in spec.get("mcpServers", {}):
                self._connections.pop(name).stop()
```

设置文件 `~/.xmclaw/mcpServers.json` 同 cline 格式（兼容 Claude Desktop）：
```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/home/user"],
      "autoApprove": ["read_file"],
      "disabled": false
    }
  }
}
```

### 工具名重整（抄 cline `ClineToolSet.ts:198-257`）
MCP 工具名 = `f"{server_uid}__{tool_name}"`，64 字符上限。

### 审批分级（抄 cline `AutoApproveBar.tsx` + `AutoApproveMenuItem.tsx:31-61`）

四个动作类，每类有子动作 checkbox：
- **read**：file_read, list_dir
- **edit**：file_write, apply_patch
- **exec**：bash, browser
- **net**：web_fetch, web_search

YOLO toggle 全开。每 MCP 工具单独 auto_approve 列表（已在 schema 里）。

### 参数感知策略（抄 continue `tools/definitions/readFile.ts:47-59`）
工具有基础策略 + `evaluate(args, processed_args) -> Policy` 函数。例如 `file_write` 默认 `allowedWithPermission`，但写到 `<workspace>/.xmclaw/` 内部的文件升级到 `allowedWithoutPermission`。

---

## 6. 流式 / Markdown / 工具卡片渲染逻辑（Frontend）

### 现状（错的）
- `lib/markdown.js:58-73` 用 regex 全量重渲，每 chunk 触发整个 bubble DOM 重建
- `MessageBubble.js:31` `<details open=${call.status === "running"}>` —— 跑的时候开、跑完自动收（**peer 的逻辑是反的**）
- `chat_reducer.js:90-122` 把 `correlation_id` 当 bubble id —— 一个 turn 出多条 assistant 消息会互相覆盖

### 目标（抄 open-webui `Markdown.svelte:39-82` + `ToolCallDisplay.svelte:127-138`）

#### markdown.js 重写
```js
import { marked } from "https://esm.sh/marked@12";

let pendingFrame = null;
let lastSrc = "";
let lastTokens = [];

export function lex(src) {
  if (src === lastSrc) return lastTokens;
  lastSrc = src;
  lastTokens = marked.lexer(src);
  return lastTokens;
}

export function lexThrottled(src, callback) {
  if (pendingFrame) return;
  pendingFrame = requestAnimationFrame(() => {
    pendingFrame = null;
    callback(lex(src));
  });
}
```

#### MessageBubble.js 改
1. 用 `lex(content)` 得 token 数组
2. token 数组 keyed render —— 只有变了的 token 重渲（Preact 自带 keyed diff）
3. 工具卡片 `<details>` 不绑定 `open=running`，改为用户可控；跑的时候用 spinner + shimmer class

#### chat_reducer.js 改
- bubble.id ≠ correlation_id
- 同一 correlation_id 的多条消息（reasoning + final）各自一个 bubble
- 角色变化时新建 bubble（抄 continue `sessionSlice.ts:589-605`）

---

## 7. 渠道逻辑（Channels）

### 目标抄 OpenClaw plugin contract + QwenPaw 实现

**Plugin contract**（抄 OpenClaw `extensions/<channel>/index.ts`）：
```python
# xmclaw/providers/channel/<name>/__init__.py
from xmclaw.providers.channel.base import ChannelPlugin

plugin = ChannelPlugin(
    id="feishu",
    inbound=FeishuInbound,        # WS / webhook
    outbound=FeishuOutbound,      # send message
    config_schema=FeishuConfigSchema,
)
```

**实现**：直接 port QwenPaw `app/channels/<name>/`：
1. `feishu/` —— lark-oapi WebSocket（不需要公网 IP）
2. `dingtalk/` —— dingtalk_stream + AI 卡片
3. `wecom/` —— 企业微信
4. `weixin/` —— 个人微信
5. `telegram/` —— python-telegram-bot

**Tunnel**（抄 QwenPaw `tunnel/cloudflare.py`）：daemon 启动时若检测到 webhook 渠道开启，自动起 cloudflared 进程暴露 :8765。

**统一队列**（抄 QwenPaw `unified_queue_manager.py`）：所有渠道入站消息丢同一个 queue → 路由到正确的 agent_id（基于配置） → AgentLoop 处理。

---

## 8. 前端信息架构（Frontend IA）

### 现状侧栏（12 项，混乱）
对话 / 工作区 / 智能体 / 技能 / 进化 / 记忆 / 工具 / 安全 / 备份 / 诊断 / 洞察 / 设置

### 目标侧栏（9 项 + 顶栏，按使用频率排）

```
顶栏：[XM logo] [工作区: ~/code/myproject ▼] [智能体: coder ▼] [模型: claude-opus-4-7 ▼]  ······  [sid] [conn]

侧栏：
  💬 对话           ← 主入口
  🤖 智能体         ← agent profile 列表（每个一个 yaml）
  📚 技能           ← skills 列表 + 进化状态
  ⭐ 进化           ← 实时事件流 + VFM sparkline
  🧰 工具           ← builtin + MCP 服务统一列表
  🔌 MCP            ← MCP 服务器管理（add/remove/restart）
  🧠 记忆           ← MEMORY.md 编辑 + 向量库浏览
  🔒 安全           ← 审批 / approval queue / inject log
  🩺 诊断           ← doctor / 备份 / 洞察 合并
  ⚙️ 设置           ← API keys / 渠道配置 / preferences
```

### 删 / 改 / 拆

| 当前 | 处理 | 原因 |
|---|---|---|
| 工作区页（文件树编辑） | **删**，改成顶栏 picker | peer 没人这么做 |
| ModelProfiles 独立页 | **删**，并入顶栏模型 picker + 设置页 | open-webui 模式 |
| Backup + Doctor + Insights 三页 | **合并**为"诊断"一页（tab 切换） | 用户低频访问 |
| Memory 页 | 保留但加入向量库浏览（query top-k） | 现在只编辑 MEMORY.md，太薄 |
| Tools 页 | 加 MCP 管理 tab | 当前是只读列表 |
| 新建 MCP 页 | 抄 cline `McpConfigurationView.tsx`（marketplace / addRemote / configure 三 tab） | 新功能 |

---

## 9. 实施 Phase 顺序

### Phase 0 ⭐：进化层补 Mutation Engine（3 天）
**为什么先做**：没有 mutator，"continuous streaming evolution" 是空话。

- `xmclaw/core/evolution/mutator.py`：DSPy/GEPA wrapper，fitness 接 XMclaw grader
- `xmclaw/core/evolution/dataset.py`：从 events.db 构 train/val/holdout
- `xmclaw/core/evolution/constraints.py`：抄 hermes-self-evolution/constraints.py
- 单元测试 + 一次 end-to-end 验证（用 demo skill 跑通 mutate → grader → controller → promote）

### Phase 1 ⭐：用户感知最强的前端 + 跨会话记忆（2 天）

**前端**（本 session 做）：
1. ✅ 重写 `lib/markdown.js`（marked + rAF）
2. ✅ 修 `MessageBubble.js`（token-keyed + 工具卡片 open/close 反转）
3. ✅ 修 `chat_reducer.js`（bubble id ≠ correlation_id）
4. ✅ 重构 `Workspace.js`（folder picker，删文件树）
5. ✅ 侧栏重组（12→9 项）

**后端**：
6. 跨会话记忆：`agent_loop.run_turn` 调 LLM 前注入 `chat_memory_handler` 30 行
7. 跨会话记忆 write path：turn 结束后 `memory.put(turn)`
8. AgentLoop 收 `memory=` 参数，factory 传入

### Phase 2：多智能体 + 配置文件布局（2 天）
1. `MultiAgentManager` 补 4 项约定：X-Agent-Id 头、ContextVar、identity-prefix
2. `<workspace>/.xmclaw/{agents,models,rules,prompts,mcpServers,skills,memory}/` 目录约定 + loader
3. Agents 页可写：新建 / 编辑 yaml + 切换
4. 顶栏 agent picker

### Phase 3：MCP Hub + 审批分级（2 天）
1. `xmclaw/providers/tool/mcp_hub.py`（抄 cline McpHub）
2. `~/.xmclaw/mcpServers.json` 配置文件 + file watcher
3. `xmclaw/security/approval_service.py` 加分级动作类
4. MCP 页 UI（add/remove/restart/auto-approve toggle）

### Phase 4：渠道（3 天）
按 Feishu → DingTalk → WeCom → Telegram 顺序，每个 80% port QwenPaw `app/channels/<name>/`。
+ Cloudflared tunnel + 统一队列。

### Phase 5：半成品大扫除（散见各 Phase 中 + 最后整理）
- 删 `xmclaw/plugins/loader.py`（空壳）
- 删 `xmclaw/daemon/routers/workspaces.py`（前端 0 调用）
- backup_scheduler 接 lifespan 或删
- secret store 整合 daemon
- TODO_UPDATED 加 UI 或停发
- ACP server（抄 hermes `acp_adapter/server.py`）—— 白嫖 Zed/VSCode 集成
- Canvas-style 渲染（抄 OpenClaw `canvas-host/`）

---

## 10. XMclaw 真本钱（绝对不动）

每动一行下面这些代码都要慎重：

1. **`xmclaw/core/grader/`** —— 硬检查 0.80 + LLM ≤0.20。Hermes 是关键词重叠 LLM-on-LLM；QwenPaw / OpenClaw 没有 grader。这是产品差异化。
2. **`xmclaw/core/evolution/`** —— SkillRegistry + EvolutionController promotion gate（plays/mean/gap）。Hermes 没 runtime 进化；QwenPaw / OpenClaw 完全没。
3. **`xmclaw/core/scheduler/online.py`** —— UCB1 over candidates。
4. **`xmclaw/core/bus/events.py`** —— BehavioralEvent 类型表。这是产品契约，传输层（裸 WS → Socket.IO）可换，**事件语义不动**。
5. **`xmclaw/security/`** —— prompt injection scanner + redactor 默认 ON。Hermes 默认 OFF。
6. **`xmclaw/daemon/static/` 无 build**（Preact + htm + ESM CDN）。三家竞品全要 build。这是 install 故事的真本钱。
7. **CLI `xmclaw doctor --fix`**。peer 都没。
8. **AGENTS.md per-subdir + import 方向 CI 门**。peer 都没。
9. **Windows-first**。Hermes 没原生 Windows，QwenPaw 上 chromadb 降级。

---

## 附录 A：竞品文件 → XMclaw 落点全表

每行都是"抄什么 → 抄到哪里"。Implementer 直接 grep 对应 peer 文件就有现成代码。

| 抄什么（peer 文件） | 落点（XMclaw 文件） | Phase |
|---|---|---|
| `hermes-self-evolution/evolution/skills/evolve_skill.py:157-177` | `xmclaw/core/evolution/mutator.py` | 0 |
| `hermes-self-evolution/evolution/datasets/dataset_builder.py:96-169` | `xmclaw/core/evolution/dataset.py` | 0 |
| `hermes-self-evolution/evolution/skills/constraints.py:30-174` | `xmclaw/core/evolution/constraints.py` | 0 |
| `hermes-self-evolution/external_importers.py:157-416` | `xmclaw/core/evolution/seed.py` | 0 |
| `open-webui/src/lib/components/chat/Messages/Markdown.svelte:39-82` | `xmclaw/daemon/static/lib/markdown.js` | 1 |
| `open-webui/src/lib/components/common/ToolCallDisplay.svelte:113-178` | `xmclaw/daemon/static/components/molecules/MessageBubble.js` (ToolCard) | 1 |
| `cline/webview-ui/src/components/common/MarkdownBlock.tsx:110-113` | `MessageBubble.js`（per-block memo） | 1 |
| `continue/gui/src/redux/slices/sessionSlice.ts:525-605` | `xmclaw/daemon/static/lib/chat_reducer.js`（bubble id 修正） | 1 |
| `cline/src/core/workspace/WorkspaceRootManager.ts:11-42` | `xmclaw/core/workspace/manager.py` + `Workspace.js` 重写 | 1 |
| `open-webui/backend/open_webui/utils/middleware.py:1473-1505` | `xmclaw/daemon/agent_loop.py`（cross-session memory inject） | 1 |
| `open-webui/backend/open_webui/routers/memories.py:113-179` | `xmclaw/providers/memory/sqlite_vec.py` query 加阈值 + 时间窗 | 1 |
| `aider/aider/history.py:33-90` `ChatSummary.summarize_real` | `xmclaw/core/session/summarizer.py` | 1（可后置） |
| `qwenpaw/src/qwenpaw/app/multi_agent_manager.py:22-130` | `xmclaw/core/multi_agent/manager.py` | 2 |
| `qwenpaw/src/qwenpaw/app/agent_context.py` + `routers/agent_scoped.py` | `xmclaw/core/context.py` + `xmclaw/daemon/middleware/agent_scope.py` | 2 |
| `qwenpaw/src/qwenpaw/agents/tools/agent_management.py:18-200` | `xmclaw/providers/tool/inter_agent.py`（HTTP→bus） | 2 |
| `continue/core/config/loadLocalAssistants.ts:104-125` | `xmclaw/core/workspace/loader.py`（7 块约定） | 2 |
| `open-webui/src/lib/components/chat/ModelSelector/Selector.svelte:43-60` | 顶栏模型 picker | 2 |
| `cline/src/services/mcp/McpHub.ts:213-273` | `xmclaw/providers/tool/mcp_hub.py` | 3 |
| `cline/src/services/mcp/schemas.ts:5-93` | `xmclaw/providers/tool/mcp_schema.py`（Pydantic 版） | 3 |
| `cline/src/core/prompts/system-prompt/registry/ClineToolSet.ts:151-192` | `xmclaw/providers/llm/tool_translator.py`（3 家 spec 翻译器） | 3 |
| `cline/webview-ui/src/components/chat/auto-approve-menu/AutoApproveMenuItem.tsx:31-61` | `xmclaw/daemon/static/pages/Security.js`（审批分级菜单） | 3 |
| `continue/core/tools/definitions/readFile.ts:47-59` | `xmclaw/security/approval_service.py`（参数感知策略） | 3 |
| `cline/webview-ui/src/components/chat/SubagentStatusRow.tsx:75-285` | `xmclaw/daemon/static/components/molecules/SubAgentRow.js` | 3 |
| `qwenpaw/src/qwenpaw/app/channels/feishu/` | `xmclaw/providers/channel/feishu/` | 4 |
| `qwenpaw/src/qwenpaw/app/channels/dingtalk/` | `xmclaw/providers/channel/dingtalk/` | 4 |
| `qwenpaw/src/qwenpaw/app/channels/wecom/` | `xmclaw/providers/channel/wecom/` | 4 |
| `qwenpaw/src/qwenpaw/app/channels/weixin/` | `xmclaw/providers/channel/weixin/` | 4 |
| `qwenpaw/src/qwenpaw/app/channels/discord_/` | `xmclaw/providers/channel/discord/` | 4 |
| `qwenpaw/src/qwenpaw/app/channels/telegram/` | `xmclaw/providers/channel/telegram/` | 4 |
| `qwenpaw/src/qwenpaw/tunnel/cloudflare.py` | `xmclaw/utils/tunnel.py` | 4 |
| `openclaw/extensions/<channel>/index.ts` (plugin contract) | `xmclaw/providers/channel/base.py` | 4 |
| `openclaw/src/canvas-host/server.ts` | `xmclaw/providers/runtime/canvas_host.py` | 5 |
| `hermes-agent/acp_adapter/server.py` | `xmclaw/providers/channel/acp.py` | 5 |
| `hermes-agent/cron/scheduler.py` + `cron/jobs.py` | `xmclaw/core/scheduler/cron.py`（删之前的半成品） | 5 |

---

## 附录 B：要删的代码

| 文件 | 原因 | Phase |
|---|---|---|
| `xmclaw/plugins/loader.py` | NotImplementedError 空壳，0 调用者 | 5 |
| `xmclaw/daemon/routers/workspaces.py` | 前端 0 调用，无 daemon 集成 | 1 |
| `xmclaw/daemon/static/pages/Workspace.js`（旧版文件树） | 概念错误 | 1 |
| `xmclaw/daemon/static/pages/ModelProfiles.js` | 并入顶栏 picker + 设置页 | 2 |
| `xmclaw/daemon/static/pages/{Backup,Doctor,Insights}.js` 三独立页 | 合并为诊断页 tab | 5 |
| `MEMORY_EVICTED` 事件无 subscriber | 加 subscriber 或停发 | 5 |
| `TODO_UPDATED` 事件无 UI | 加 UI 或停发 | 5 |
| backup_scheduler.py 不接 lifespan | 接 lifespan 或删 | 5 |
| secret store CLI 工具不接 daemon | 整合 daemon | 5 |

---

**End of doc.** Implementer 直接 follow Phase 0 → 1 → 2 → 3 → 4 → 5。每 Phase 完成后 `git commit -m "Phase N: ..."` 并在 `docs/DEV_ROADMAP.md` Epic #23 进度日志加一行。
