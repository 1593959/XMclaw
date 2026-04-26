# XMclaw 开发文档（DEV_PLAN）

**版本**：v1.0 — 2026-04-26
**状态**：定稿，按本文执行
**作者**：基于 4 个核心竞品（OpenClaw / Hermes / Hermes-Self-Evolution / QwenPaw）+ 5 个 commodity 竞品（cline / continue / aider / AutoGPT / open-webui）的源码级深度调研。
**取代**：本文档 supersedes `docs/PRODUCT_REDESIGN.md`（保留作高层定位参考）。

---

## 0. 战略指令（用户原话提炼）

1. **核心差异化**：自我进化系统（Honest Grader + SkillScheduler + EvolutionController + SkillRegistry + Mutation Engine + BehavioralEvent 语义）。**只有这层自己写**。
2. **三家都有的功能**：OpenClaw / Hermes / QwenPaw 三家共同有的能力 → **直接抄**原代码原逻辑。
3. **某家独家的功能**：任意一家独有的强项 → **直接抄**。
4. **最后整合**，让 XMclaw 同时拥有"三家所有 commodity 能力"+"我们的进化层"。
5. **不要原创设计**。

> 本文档把每个功能拆到"哪家有 / 抄哪个文件 / 落在 XMclaw 哪里"。Implementer 直接 grep 对照。

---

## 1. 三家共有清单（必抄，按用户痛点优先级排）

每行格式：`功能 → 哪三家都有的证据（peer 文件） → XMclaw 当前状态 → 落点`。

### 1.1 灵魂 / 身份系统（SOUL.md + IDENTITY.md + AGENTS.md）★ P0
**三家都有**：
- **OpenClaw**：`src/agents/system-prompt.ts:44-52` 定义 `CONTEXT_FILE_ORDER = {agents.md:10, soul.md:20, identity.md:30, user.md:40, tools.md:50, bootstrap.md:60, memory.md:70}`，按优先级载入 workspace 7 个文件
- **Hermes**：`agent/prompt_builder.py:1067-1072` 扫 `~/.hermes/SOUL.md` + `.hermes.md` + `AGENTS.md` + `CLAUDE.md` + `.cursorrules`，`run_agent.py:4463 _build_system_prompt` 是装配函数
- **QwenPaw**：`src/qwenpaw/agents/prompt.py:35-39` `PromptBuilder.build()` 载入 `AGENTS.md` + `SOUL.md` + `PROFILE.md`

**XMclaw 当前**：只有硬编码的 `_default_system_prompt()`（agent_loop.py:63）。`factory.py::_load_persona_addendum` 是我前一轮塞的半成品 —— **不够**，没有完整的 7 文件优先级 + bootstrap 流程。

**抄什么**：OpenClaw 的 7 文件优先级载入（`workspace.ts:19-86` cache by mtime）+ QwenPaw 的 bootstrap interview（`prompt.py:323-372` `build_bootstrap_guidance`）+ Hermes 的 prompt-injection scanner（`prompt_builder.py:36-71` `_CONTEXT_THREAT_PATTERNS`）。

**落点**：
- `xmclaw/core/persona/` 新模块 = `loader.py` + `assembler.py` + `bootstrap.py`
- `xmclaw/core/persona/templates/` 内置模板（SOUL/IDENTITY/USER/AGENTS/TOOLS/MEMORY/BOOTSTRAP）
- 装载位置：`~/.xmclaw/persona/profiles/<active>/{SOUL,IDENTITY,USER,...}.md` 全局 + `<workspace>/.xmclaw/persona/{SOUL,IDENTITY,...}.md` 项目级
- `factory.build_agent_from_config` 改为调 `assembler.build_system_prompt(workspace_root, persona_id)` 替代当前的 `_load_persona_addendum`

### 1.2 跨会话记忆（embedding + 注入 + 写回）★ P0
**三家都有**：
- **OpenClaw**：`extensions/memory-core/src/tools.ts:191-193` `memory_search` 工具 + `extensions/memory-core/src/dreaming.ts:29-37` 定时 promotion + sqlite-vec hybrid rank（`memory-search.ts:104-112` vector 0.7 + text 0.3）
- **Hermes**：`agent/memory_manager.py:17-26` 定义生命周期（`build_system_prompt` / `prefetch_all` / `sync_all` / `queue_prefetch_all`），`run_agent.py:9550-9712` 装配 + 注入到当前 user message 末尾用 `<memory-context>` fence
- **QwenPaw**：`agents/context/light_context_manager.py:623-672` `pre_reply` hook 自动检索 + `agents/memory/prompts.py:7-51` `MEMORY_GUIDANCE` 教 LLM 用 `read_file`/`memory_search`

**XMclaw 当前**：`SqliteVecMemory` 已建出来（`xmclaw/providers/memory/sqlite_vec.py`），但 `agent_loop.run_turn` **零调用**。`AgentLoop.__init__` 不收 `memory=` 参数。**完全死状态**。

**抄什么**：Open-WebUI 的 `chat_memory_handler`（`backend/open_webui/utils/middleware.py:1473-1505`）作为最简模板 + Hermes 的 `<memory-context>` fence（避免被认成新输入）。

**落点**：
- `xmclaw/daemon/agent_loop.py::run_turn` 在第一次 LLM 调用前注入 ~20 行 prefetch
- turn 结束后 ~5 行 write-back
- `factory.build_agent_from_config` 把 memory 传进 `AgentLoop(memory=...)`
- embedder 选 Doubao（`doubao-embedding-large-text-240915`，1024 dim），fallback 到本地 `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`
- 配置：`config.json` 加 `memory.embedding.{provider, api_key, model}` 段

### 1.3 SKILL.md YAML frontmatter 约定 ★ P1
**三家都有**：
- **OpenClaw**：`src/agents/skills/local-loader.ts:44-94` 解析 frontmatter（name + description + metadata.openclaw.requires/install）
- **Hermes**：`tools/skill_manager_tool.py:22-32` 子目录有 `references/` `templates/` `scripts/` `assets/`
- **QwenPaw**：`src/qwenpaw/agents/skills_manager.py:48-59` `ALL_SKILL_ROUTING_CHANNELS` + 双语 `<name>-{en,zh}/SKILL.md`

**XMclaw 当前**：`xmclaw/skills/` 有 SkillBase ABC，**但和 SKILL.md 文件层无任何对接**。`xmclaw/skills/registry.py` 是 in-memory 的。

**抄什么**：OpenClaw 的 `local-loader.ts:44-94`（含 symlink 拒绝、frontmatter 解析、cache by mtime）+ XMclaw 自己加 permission 字段（`permissions: {fs, net, exec}`，peer 没人有，是我们的差异化）。

**落点**：`xmclaw/skills/loader.py` 新模块。

### 1.4 工作区 = 一个目录 ★ P1
**三家都有（同 commodity 层 cline/continue 也都有）**：
- **OpenClaw**：cwd + `MEMORY.md` + `memory/*.md`，无显式 picker
- **Hermes**：`.hermes.md` 沿 git_root 走
- **QwenPaw**：每 agent 的 `working_dir`
- **cline 数据结构**：`WorkspaceRoot = {path, name?, vcs, commitHash?}`（`shared/multi-root/types.ts:11-16`）

**XMclaw 当前**：之前的"工作区"页是文件树 mess，已经简化为顶部 picker（Phase 1 已交付前端）；**后端 daemon 还没接 state.json**。

**抄什么**：cline `WorkspaceRoot` 数据结构 + Continue 的 `<root>/.continue/<type>/*.{yaml,md}` 7 块约定（`core/config/loadLocalAssistants.ts:104-125`）。

**落点**：
- `xmclaw/core/workspace/manager.py` 维护 `~/.xmclaw/state.json`
- 7 块约定：`<root>/.xmclaw/{agents,skills,rules,prompts,mcpServers,docs,memory}/`
- `xmclaw/daemon/routers/workspace.py` 新 GET/PUT `/api/v2/workspace`

### 1.5 多智能体（agent profile + 切换） ★ P1
**三家都有**：
- **OpenClaw**：`src/gateway/server-methods/agents.ts` `agents.list/select`，每个 agent 自己的 workspace dir
- **QwenPaw**：`src/qwenpaw/app/multi_agent_manager.py:22-130` MultiAgentManager + `console/src/pages/Settings/Agents/{index,Modal,SortableRow}.tsx` 拖拽排序 UI
- **Hermes**：`~/.hermes/SOUL.md` 单 agent + `.hermes.md` 项目 overlay（**最弱 — 不真正多 agent**）

**XMclaw 当前**：`MultiAgentManager` 骨架 + REST 端点，**前端只读**，4 项约定只有 1 项（lazy-locked dict）。

**抄什么**：QwenPaw 的 4 项约定 verbatim：
1. `X-Agent-Id` header（`app/agent_context.py`）
2. ContextVar 透传（`app/routers/agent_scoped.py`）
3. lazy-locked workspace dict（`app/multi_agent_manager.py:22-130` `_pending_starts`）
4. identity-prefix loop guard（`agents/tools/agent_management.py:107-121`）

**Inter-agent transport 不抄 QwenPaw 的 HTTP loopback**（30s 超时太脆弱），改用内部 bus 调用同 daemon 内的 agent，跨 daemon 才走 HTTP。

**落点**：
- `xmclaw/core/multi_agent/{manager,context,middleware}.py`
- `xmclaw/providers/tool/inter_agent.py`（`chat_with_agent` / `submit_to_agent` / `check_agent_task` / `list_agents` 4 工具）
- `xmclaw/daemon/static/pages/Agents.js` 重写为可写 + drag-reorder

### 1.6 多渠道（特别是中文 4 渠道） ★ P1
**三家都有**：
- **OpenClaw**：25+ `extensions/<channel>/`
- **Hermes**：17 `gateway/platforms/<name>/`
- **QwenPaw**：20 `app/channels/<name>/`

**中文重要的 4 个**（XMclaw 必须有）：
- **Feishu**：QwenPaw `app/channels/feishu/`（用 lark-oapi WebSocket，不需要公网 IP）
- **DingTalk**：QwenPaw `app/channels/dingtalk/`（dingtalk_stream + AI 卡片）
- **WeCom**：QwenPaw `app/channels/wecom/`
- **个人微信**：QwenPaw `app/channels/weixin/`

**海外 1 主流**：
- **Telegram**：QwenPaw `app/channels/telegram/`（python-telegram-bot）

**XMclaw 当前**：**完全没有渠道层**。

**抄什么**：QwenPaw 的实现（最完整）+ OpenClaw 的 plugin contract 形状（`extensions/<channel>/index.ts:4-24` `defineBundledChannelEntry`，让每渠道能独立 PyPI 包发布）。

**落点**：
- `xmclaw/providers/channel/base.py`：抄 OpenClaw 的 plugin contract
- `xmclaw/providers/channel/<feishu|dingtalk|wecom|weixin|telegram>/`：抄 QwenPaw 实现
- `xmclaw/providers/channel/queue.py`：抄 QwenPaw `unified_queue_manager.py`

### 1.7 Cron 定时任务 ★ P2
**三家都有**：
- **OpenClaw**：`skills/cron/SKILL.md`（提示词驱动）
- **Hermes**：`cron/scheduler.py:1-9` `tick()` 60s polling + `cron/jobs.py:25-29` croniter + 每 job `enabled_toolsets`（`cron/scheduler.py:60-72`）
- **QwenPaw**：`crons/manager.py`

**最完整**是 Hermes（per-job toolsets + wakeAgent gate + 输出到 `~/.hermes/cron/output/{job_id}/{ts}.md`）。

**XMclaw 当前**：上一轮我自己写了 `cron_store.py` + `cron_scheduler.py`，**没接进 daemon 即被弃用**。

**抄什么**：Hermes `cron/scheduler.py` + `cron/jobs.py` verbatim。

**落点**：删 `xmclaw/daemon/cron_store.py` + `cron_scheduler.py`（之前的半成品），新 `xmclaw/core/scheduler/cron.py` 抄 Hermes。

### 1.8 MCP 集成 ★ P2
**三家都有**：
- **OpenClaw**：`extensions/memory-core/` 等通过 MCP
- **Hermes**：`mcp_serve.py` 既是 server 又是 client，`tools/mcp_tool.py`
- **QwenPaw**：`agents/mcp/` + `routers/mcp.py`

**最完整**是 cline（commodity 层）：`src/services/mcp/McpHub.ts:213-273` 多服务 + file watcher + OAuth + reconnect + auto_approve。

**XMclaw 当前**：`xmclaw/providers/tool/mcp_bridge.py` 单 server、stdio-only、无 watcher。

**抄什么**：cline `McpHub.ts` + `schemas.ts:5-93`（Zod 改 Pydantic）+ `ClineToolSet.ts:198-257`（64 字符工具名重整）。

**落点**：`xmclaw/providers/tool/mcp_hub.py`（替换 `mcp_bridge.py`）。

### 1.9 Slash 命令 + Composer 富交互 ★ P2
**三家都有**：
- **OpenClaw**：`/new` `/reset` `/stop` `/model list`
- **Hermes**：`/skill-name` 注入 SKILL.md 为 user message（`agent/skill_commands.py:14-118`）
- **QwenPaw**：command suggestion 下拉

**最完整**是 cline：`webview-ui/src/components/chat/{ChatTextArea,SlashCommandMenu}.tsx` 含 `@`mention（文件 / git / MCP）+ `/` 命令 + 缩略图。

**XMclaw 当前**：Composer 是裸 textarea，无 `/` 菜单、无 `@`、无附件、无粘贴图片。

**抄什么**：cline `ChatTextArea.tsx:25-32` `shouldShowContextMenu` + `getContextMenuOptions` + `SlashCommandMenu.tsx`。

**落点**：`xmclaw/daemon/static/components/molecules/{Composer,SlashMenu,AtMention}.js`。

### 1.10 流式 Markdown + 工具卡片 ★ P0（已部分完成）
**三家都有**（commodity 层）：
- **open-webui**：`Markdown.svelte:39-82`（rAF + marked.lexer + token diff）
- **cline**：`MarkdownBlock.tsx:110-113`（per-block memo）
- **continue**：`StyledMarkdownPreview/index.tsx:227-281`（useRemark）

**XMclaw 当前**：本 session 已抄 open-webui 模式（`lib/markdown.js` + `MessageBubble.js` 改完），但代码还没 commit。

**抄什么**：已抄。还差代码块 syntax highlight + copy 按钮（`open-webui/.../Messages/CodeBlock.svelte`）+ 思考块折叠（cline `ThinkingRow`）。

**落点**：`xmclaw/daemon/static/components/molecules/MessageBubble.js` 加 CodeBlock 子组件。

### 1.11 会话列表 + 历史 + Resume ★ P2
**三家都有**：
- **OpenClaw**：session UI in `ui/`
- **Hermes**：`SessionDB` SQLite
- **QwenPaw**：session drawer + search panel

**最简**是 aider：`~/.aider.chat.history.md` append-only + `split_chat_history_markdown` 启动恢复（`base_coder.py:519-523`）。

**XMclaw 当前**：`SessionStore` 只按 session_id 主键存。无列表 UI。

**抄什么**：continue `core/util/history.ts:24-198` `HistoryManager.list/load/save/delete` + `<globalDir>/sessions/<id>.json` + `sessions.json` index。

**落点**：`xmclaw/daemon/session_store.py` 加 `list_recent()` + `xmclaw/daemon/static/components/molecules/SessionPicker.js`（侧栏抽屉）。

---

## 2. 独家功能清单（必抄，每家拿出最强项）

### 2.1 Hermes 独家
| 功能 | Peer 文件 | 落点 |
|---|---|---|
| **ACP server**（白嫖 Zed/VSCode/JetBrains 集成） | `acp_adapter/server.py:13-47` | `xmclaw/providers/channel/acp.py` |
| **8 个 plugin memory providers**（mem0 / honcho / supermemory / byterover / hindsight / holographic / openviking / retaindb） | `plugins/memory/<name>/` | `xmclaw/providers/memory/<name>/`（先抄 honcho 一个） |
| **Plugin memory 三模式**（`hybrid` / `context` / `tools`） | `plugins/memory/honcho/client.py:265-267` | `xmclaw/providers/memory/__init__.py` 配置开关 |
| **6 种 terminal backend**（local/Docker/SSH/Daytona/Singularity/Modal） | `agent/transports/` | `xmclaw/providers/runtime/` 已有 LocalSkillRuntime 和 ProcessSkillRuntime，扩 4 个 |
| **TUI（Ink + JSON-RPC）**+ web 仪表盘嵌 xterm.js | `hermes_cli/web_server.py:52-66` | XMclaw 的 web UI 已有 chat，TUI 暂不抄 |

### 2.2 OpenClaw 独家
| 功能 | Peer 文件 | 落点 |
|---|---|---|
| **Canvas live-render**（HTTP+WS 端口 18793 同步推 HTML 给 Mac/iOS/Android 设备） | `src/canvas-host/server.ts:1-39` | `xmclaw/providers/runtime/canvas_host.py` |
| **BOOTSTRAP.md interview pattern**（首次启动让 agent 主动面试用户写 IDENTITY.md） | `docs/reference/templates/BOOTSTRAP.md` + `system-prompt.ts:206-214` | `xmclaw/core/persona/bootstrap.py` |
| **Memory dreaming**（cron-driven short-term → long-term promotion） | `extensions/memory-core/src/dreaming.ts:29-37` | `xmclaw/core/evolution/memory_consolidation.py` |
| **`memory_search` + `memory_get` + `memory_edit` 三件套工具** | `extensions/memory-core/src/tools.ts` | `xmclaw/providers/tool/memory_tools.py` |
| **Channel plugin contract**（`defineBundledChannelEntry`） | `extensions/<channel>/index.ts:4-24` | `xmclaw/providers/channel/base.py` |
| **Bundled-channel SDK split**（plugin-api / runtime-api / contract-api / security-contract-api） | `openclaw/plugin-sdk/channel-core/` | `xmclaw/sdk/channel/` 新模块 |

### 2.3 QwenPaw 独家
| 功能 | Peer 文件 | 落点 |
|---|---|---|
| **真 working 多智能体的 4 项约定** | 见 §1.5 | 见 §1.5 |
| **Cloudflared tunnel 自动启动**（webhook 渠道免公网） | `src/qwenpaw/tunnel/cloudflare.py` | `xmclaw/utils/tunnel.py` |
| **Proactive trigger**（idle-timer 自动 ping 用户） | `agents/memory/proactive/proactive_trigger.py:27-48` | `xmclaw/core/proactive/trigger.py` |
| **Drag-reorder agent UI**（`@dnd-kit/sortable`） | `console/src/pages/Settings/Agents/SortableAgentRow.tsx` | `xmclaw/daemon/static/pages/Agents.js` |
| **Per-skill per-channel routing**（`enabled` flag + `channels: list`） | `agents/skills_manager.py:48-59,1547,2239` | `xmclaw/skills/registry.py` 加 channel filter |
| **DingTalk AI 卡片**（"single reply unless `sessionWebhook` present"） | `app/channels/dingtalk/channel.py:5-12` | `xmclaw/providers/channel/dingtalk/channel.py` |

### 2.4 Cline 独家（commodity 但 cline 做得最好）
| 功能 | Peer 文件 | 落点 |
|---|---|---|
| **MCP Hub multi-server**（含 file watcher / OAuth / SSE / streamableHttp） | `src/services/mcp/McpHub.ts:213-273, 286-549` | `xmclaw/providers/tool/mcp_hub.py` |
| **Auto-approve 分级菜单**（read/edit/exec/browser/mcp + 子 action checkbox） | `webview-ui/.../auto-approve-menu/AutoApproveMenuItem.tsx:31-61` | `xmclaw/daemon/static/pages/Security.js` 加 |
| **Sub-agent live status row**（per-prompt running/done/failed + token/cost/result） | `webview-ui/.../chat/SubagentStatusRow.tsx:75-285` | `xmclaw/daemon/static/components/molecules/SubAgentRow.js` |
| **64-char tool name mangling for MCP** | `ClineToolSet.ts:198-257` | `xmclaw/providers/tool/mcp_hub.py` |
| **3 家 native tool spec 翻译器**（anthropic / gemini / openai） | `ClineToolSet.ts:151-192` | `xmclaw/providers/llm/tool_translator.py` |
| **`@`mention picker**（文件 / git / MCP） | `webview-ui/.../chat/ChatTextArea.tsx:25-32` | `xmclaw/daemon/static/components/molecules/AtMention.js` |
| **Slash command menu** | `webview-ui/.../chat/SlashCommandMenu.tsx` | `xmclaw/daemon/static/components/molecules/SlashMenu.js` |
| **Per-model variant prompts**（覆盖 agent_role 防身份漂移） | `core/prompts/system-prompt/variants/{trinity,hermes,gpt-5,...}/overrides.ts` | `xmclaw/core/persona/variants/` |

### 2.5 Open-WebUI 独家
| 功能 | Peer 文件 | 落点 |
|---|---|---|
| **`chat_memory_handler`**（cross-session memory 注入模板） | `backend/open_webui/utils/middleware.py:1473-1505` | `xmclaw/daemon/agent_loop.py` 注入处 |
| **CodeBlock**（hljs + copy + 折叠 + Pyodide 执行 + Mermaid + Vega） | `src/lib/components/chat/Messages/CodeBlock.svelte` | `xmclaw/daemon/static/components/molecules/CodeBlock.js`（先抄 hljs+copy） |
| **ToolCallDisplay**（`<details>` + 用户控制开关 + status from `attributes.done`） | `src/lib/components/common/ToolCallDisplay.svelte:113-178` | `MessageBubble.js`（已部分抄） |
| **ModelSelector**（多选 + sessionStorage 持久化 + 顶部下拉） | `src/lib/components/chat/ModelSelector/Selector.svelte:43-60` | `xmclaw/daemon/static/components/molecules/ModelPicker.js`（已有，扩到 topbar） |
| **SearchModal Cmd-K** | `src/lib/components/layout/SearchModal.svelte` | `xmclaw/daemon/static/components/molecules/CommandPalette.js` |
| **`svelte-sonner` toast 系统**+`Skeleton.svelte`+`FilesOverlay`（拖拽附件） | 多文件 | `xmclaw/daemon/static/lib/toast.js`+`components/atoms/Skeleton.js` |
| **Suggestions chips 空状态** | `src/lib/components/chat/Placeholder.svelte` | `xmclaw/daemon/static/pages/Chat.js` 替换空状态 |

### 2.6 Continue 独家
| 功能 | Peer 文件 | 落点 |
|---|---|---|
| **配置块 7 类目录约定**（`<root>/.continue/{agents,models,rules,prompts,mcpServers,docs,context}/*.yaml`） | `core/config/loadLocalAssistants.ts:104-125` | `xmclaw/core/workspace/loader.py`（7 块约定） |
| **参数感知策略**（read 工具的 sandbox-外升权） | `core/tools/definitions/readFile.ts:47-59` `evaluateToolCallPolicy` | `xmclaw/security/approval_service.py` |

### 2.7 Aider 独家
| 功能 | Peer 文件 | 落点 |
|---|---|---|
| **历史摘要**（老消息 LLM 摘成 ~500 token，超阈值后台触发） | `aider/history.py:33-90` `ChatSummary.summarize_real` + `base_coder.py:1036-1046` | `xmclaw/core/session/summarizer.py` |
| **`.aider.chat.history.md` append-only**（启动恢复） | `aider/io.py:1117-1136` + `base_coder.py:519-523` | 备用方案，目前不抄 |

### 2.8 Hermes-Self-Evolution 独家（mutation engine 必抄）
| 功能 | Peer 文件 | 落点 |
|---|---|---|
| **DSPy/GEPA mutation wrapper**（每次产新 prompt candidate） | `evolution/skills/evolve_skill.py:157-177` | `xmclaw/core/evolution/mutator.py` |
| **synthetic dataset 生成器**（冷启动时让 LLM 自己造测试样本） | `evolution/datasets/dataset_builder.py:96-169` | `xmclaw/core/evolution/dataset.py` |
| **历史挖掘器**（扫 Claude Code / Copilot 历史当数据） | `evolution/external_importers.py:157-416` | `xmclaw/core/evolution/seed.py` |
| **Train/val/holdout 50/25/25 split** | `evolve_skill.py:208-227` | `xmclaw/core/evolution/dataset.py` |
| **Constraint validators**（size/growth/structure） | `constraints.py:30-174` | `xmclaw/core/evolution/constraints.py` |

> **关键**：Hermes-Self-Evolution 的 fitness 是关键词重叠 + LLM-on-LLM rubric（`fitness.py:107-136`）—— **这部分 NOT 抄**。XMclaw 用自己的 Honest Grader 替换 fitness function。这是我们对 Hermes 的真差异化。

---

## 3. XMclaw 自有不动（核心进化层）

每动一行下面这些代码都要慎重：

| 模块 | 文件 | 为什么不动 |
|---|---|---|
| **Honest Grader** | `xmclaw/core/grader/` | 硬检查 0.80 + LLM 上限 0.20 vs Hermes 关键词重叠 LLM-on-LLM。**真差异** |
| **EvolutionController** | `xmclaw/core/evolution/controller.py` | promotion gate（plays/mean/gap-vs-head/gap-vs-second）。Peer 都没 |
| **SkillScheduler** | `xmclaw/core/scheduler/online.py` | UCB1 over candidates |
| **SkillRegistry** | `xmclaw/skills/registry.py` | 版本化 + rollback |
| **BehavioralEvent** | `xmclaw/core/bus/events.py` | 事件类型表 = 产品契约。传输层（裸 WS → Socket.IO）可换，**事件语义不动** |
| **Security scanner** | `xmclaw/security/` | 默认 ON（vs Hermes 默认 OFF），prompt injection scanner + redactor |
| **Doctor + AGENTS.md import 方向 CI 门** | `xmclaw/cli/doctor*` + `scripts/check_import_direction.py` | Peer 都没 |
| **No-build Preact + htm + ESM CDN** | `xmclaw/daemon/static/` | 三家竞品全要 build。这是 install 故事的真本钱 |
| **Windows-first** | 全栈 | Hermes 没原生 Windows，QwenPaw chromadb 降级 |

---

## 4. 整合策略

### 4.1 配置文件布局（合并 Continue + Hermes + OpenClaw 约定）

```
~/.xmclaw/                                    # 全局
├── config.json                               # daemon 配置（API keys 等）
├── state.json                                # workspace_roots + active_agent
├── persona/
│   └── profiles/
│       ├── default/                          # 默认 profile
│       │   ├── SOUL.md
│       │   ├── IDENTITY.md
│       │   ├── USER.md
│       │   ├── AGENTS.md
│       │   ├── TOOLS.md
│       │   ├── MEMORY.md
│       │   └── BOOTSTRAP.md（首次启动后被删）
│       └── coder/                            # 自定义 profile
│           └── ...
├── agents/*.yaml                             # 全局 agent profiles（Continue 风）
├── skills/<name>/SKILL.md                    # 全局技能（OpenClaw/Hermes 风）
├── models/*.yaml                             # 模型 profile
├── rules/*.md                                # 全局规则
├── prompts/*.prompt                          # 模板
├── mcpServers.json                           # MCP（cline 风，单文件）
├── memory/MEMORY.md                          # 全局记忆
├── memory.db                                 # SqliteVec 向量记忆
├── sessions.db                               # 会话历史
├── cron/jobs.json                            # cron 任务（Hermes 风）
└── v2/                                       # daemon 运行时

<workspace>/.xmclaw/                          # 项目级（覆盖全局）
├── persona/
│   └── {SOUL,IDENTITY,USER,AGENTS,...}.md   # 项目人格 overlay
├── agents/*.yaml
├── skills/<name>/SKILL.md
├── rules/*.md
├── prompts/*.prompt
├── mcpServers.json
└── memory/MEMORY.md
```

### 4.2 启动流程

```
xmclaw start
├─ 1. 解析 config.json（多路径搜索 — 已修复）
├─ 2. 读 ~/.xmclaw/state.json → workspace_roots, active_agent
├─ 3. 加载 persona profile：
│   ├─ ~/.xmclaw/persona/profiles/<active>/{SOUL,IDENTITY,...}.md
│   └─ overlay <workspace>/.xmclaw/persona/{SOUL,IDENTITY,...}.md
├─ 4. 装配 system prompt（Hermes _build_system_prompt 风）：
│   ├─ slot 1: SOUL.md 或 DEFAULT_AGENT_IDENTITY 硬编码 fallback
│   ├─ slot 2: 其他 persona files（按 OpenClaw CONTEXT_FILE_ORDER）
│   ├─ slot 3: skills index（cache by mtime）
│   ├─ slot 4: 平台/渠道 hint
│   └─ 注：BOOTSTRAP.md 存在时 prepend bootstrap_guidance
├─ 5. 启动渠道（按 ~/.xmclaw/channels.json 启用列表）：
│   ├─ Console（默认）
│   ├─ Feishu / DingTalk / WeCom / Telegram（如启用，必要时启 cloudflared）
├─ 6. 启动 cron tick (60s)
├─ 7. MultiAgentManager 加载 agent profiles
├─ 8. 启动 EvolutionOrchestrator（已有）
└─ 9. WS server 监听 8765
```

### 4.3 一次完整 turn 流程

```
用户消息 (channel inbound or WS)
  ↓
unified_queue_manager 路由到 agent_id
  ↓
AgentLoop.run_turn(session_id, user_message, agent_id):
  ├─ emit USER_MESSAGE
  ├─ [新] memory.search() top-3，构 <memory-context> fence
  ├─ [新] 注入到 user_message 末尾（不污染 system prompt cache）
  ├─ messages = [system_prompt, ...history, user_message+memory_ctx]
  ├─ for hop in range(max_hops):
  │   ├─ emit LLM_REQUEST
  │   ├─ llm.complete_streaming(messages, tools, on_chunk=publish_chunk)
  │   ├─ emit LLM_RESPONSE
  │   ├─ if response.tool_calls:
  │   │   ├─ for each: emit TOOL_INVOCATION_STARTED → invoke → emit TOOL_INVOCATION_FINISHED
  │   │   ├─ [新] grader.evaluate(tool_call, result) → emit GRADER_VERDICT
  │   │   └─ messages += [assistant w/ tool_calls, tool_results...]
  │   └─ else: break
  ├─ [新] memory.put({user_msg, final_assistant_text}, session_id, ts)
  ├─ session_store.save(session_id, history)
  └─ return AgentTurnResult
  ↓
[后台] EvolutionController 累计 GRADER_VERDICT，到阈值触发：
  ├─ Mutation Engine（DSPy/GEPA wrapper）产新 candidate
  ├─ SkillScheduler 把 candidate 加入 UCB 池
  ├─ N 局后达 promotion gate → SkillRegistry promote → emit SKILL_PROMOTED
  └─ 下次 turn 自动用新版本（hot reload）
```

---

## 5. 前端 IA（信息架构）

### 5.1 目标侧栏（10 项，按使用频率排）
```
💬 对话              ← 主入口
🤖 智能体            ← agent profile 列表 (yaml)
📚 技能              ← skills 列表 + 进化状态
⭐ 进化              ← 实时事件流 + VFM sparkline
🧰 工具              ← builtin + MCP 服务统一列表
🧠 记忆              ← MEMORY.md 编辑 + 向量库浏览
📁 工作区            ← folder picker + 7 块跳转
🔒 安全              ← 审批分级 + injection log + audit
🩺 诊断              ← doctor / 备份 / 洞察 三 tab
⚙️ 设置              ← API keys / 渠道配置 / preferences
```

### 5.2 顶栏布局
```
[XM logo] [工作区: ~/myproject ▼] [智能体: coder ▼] [模型: claude-opus ▼]
                                                        [sid] [conn] [theme] [Cmd-K]
```

### 5.3 Phase 1.5 前端补强清单（按 ROI 排）

| # | 改动 | 文件 | 来源 |
|---|---|---|---|
| 1 | CodeBlock：hljs syntax highlight + copy 按钮 + 语言标签 | `components/molecules/CodeBlock.js` 新 | open-webui `CodeBlock.svelte` |
| 2 | Toast 通知系统 | `lib/toast.js` 新 + `styles/toast.css` 新 | open-webui `svelte-sonner` |
| 3 | Slash 命令下拉菜单 | `components/molecules/SlashMenu.js` 新 | cline `SlashCommandMenu.tsx` |
| 4 | TopBar `backdrop-filter:blur(12px) saturate(1.6)` | `styles/layout.css:33` | OpenClaw `layout.css:91` |
| 5 | Sidebar 折叠到 icon-rail | `app.js` Sidebar + `styles/layout.css` | OpenClaw `.shell--nav-collapsed` |
| 6 | 拖拽文件附件 overlay | `pages/Chat.js` + dragover | open-webui `FilesOverlay.svelte` |
| 7 | Cmd-K 全局搜索 | `components/molecules/CommandPalette.js` 新 | open-webui `SearchModal.svelte` |
| 8 | 主题切换按钮 + theme-transition 圆扩散 | `components/atoms/ThemeToggle.js` 新 | OpenClaw `theme-transition.ts` |
| 9 | 设置分 tab（常规/模型/工具/安全/关于） | `pages/Settings.js` 重构 | cline `SettingsView.tsx` |
| 10 | 空状态 suggestion chips（"帮我写代码"等） | `pages/Chat.js` 替换 `xmc-msglist__empty` | open-webui `Suggestions.svelte` |
| 11 | 思考块 `<details>` 折叠 | `components/molecules/MessageBubble.js` | cline `ThinkingRow` |
| 12 | message hover-only 工具条（复制/重发/编辑） | `MessageBubble.js` + CSS | cline `WithCopyButton` |
| 13 | `@`mention picker（文件 / git / MCP） | `components/molecules/AtMention.js` 新 | cline `ChatTextArea.tsx` |
| 14 | 品牌色 `--xmc-brand: #6366f1` | `styles/tokens.css` | XMclaw 自创 |
| 15 | 子智能体并发状态行 | `components/molecules/SubAgentRow.js` 新 | cline `SubagentStatusRow.tsx` |

---

## 6. Phase-by-Phase 实施

### Phase 0：进化层补 Mutation Engine（核心差异化的最后一块）★ 关键
**为什么先做**：没有 mutator，"continuous streaming evolution" 是空话。

文件：
- `xmclaw/core/evolution/mutator.py` ← 抄 `hermes-self-evolution/evolve_skill.py:157-177`，fitness 换成 XMclaw grader
- `xmclaw/core/evolution/dataset.py` ← 抄 `dataset_builder.py:96-169`
- `xmclaw/core/evolution/constraints.py` ← 抄 `constraints.py:30-174`
- `xmclaw/core/evolution/seed.py` ← 抄 `external_importers.py:157-416`

测试：跑通 demo skill 的 end-to-end mutate → grader → controller → promote。

### Phase 1：身份/灵魂 + 跨会话记忆（用户最痛的两件）★ 紧急
1. **Persona 系统**（§1.1）：
   - 新模块 `xmclaw/core/persona/{loader,assembler,bootstrap}.py`
   - 内置模板 `xmclaw/core/persona/templates/`
   - factory.py 改为调 assembler
2. **跨会话记忆**（§1.2）：
   - `xmclaw/daemon/agent_loop.py::run_turn` 注入 + 写回 ~25 行
   - `xmclaw/daemon/factory.py` 传 memory + embedder
   - `xmclaw/providers/memory/embedder.py` 新（Doubao 优先 + 本地 fallback）
   - config.json 加 `memory.embedding` 段

### Phase 2：前端 commit + 流式视觉补强（已部分完成）
- 已写完待 commit：markdown.js / MessageBubble.js / Workspace.js / app.js sidebar
- 加：CodeBlock + Toast + Slash menu（前 3 项 §5.3）

### Phase 3：多智能体 + 配置 7 块（§1.4 + §1.5）
- `xmclaw/core/workspace/manager.py` + state.json
- `xmclaw/core/multi_agent/{manager,context,middleware}.py`
- `xmclaw/providers/tool/inter_agent.py`
- 前端 Agents 页可写 + drag-reorder
- 顶栏 agent picker

### Phase 4：MCP Hub + 审批分级（§1.8 + §2.4）
- 替换 `xmclaw/providers/tool/mcp_bridge.py` 为 `mcp_hub.py`（cline 风）
- `xmclaw/security/approval_service.py` 加分级动作类
- 前端 MCP 管理页（add/remove/restart/auto-approve）
- 前端审批分级菜单

### Phase 5：渠道 + 隧道（§1.6 + §2.3）
- `xmclaw/providers/channel/base.py`（OpenClaw plugin contract 形状）
- `xmclaw/providers/channel/{feishu,dingtalk,wecom,weixin,telegram}/`（QwenPaw 实现）
- `xmclaw/providers/channel/queue.py`（unified_queue_manager）
- `xmclaw/utils/tunnel.py`（cloudflared）

### Phase 6：Cron + Plugin Memory + ACP（§1.7 + §2.1 + §2.2）
- `xmclaw/core/scheduler/cron.py`（Hermes 风），删 daemon/cron_*
- `xmclaw/providers/memory/honcho/`（plugin memory 一个范本）
- `xmclaw/providers/channel/acp.py`（Hermes ACP server）
- `xmclaw/providers/runtime/canvas_host.py`（OpenClaw Canvas）

### Phase 7：前端 Phase 1.5 剩余补强（§5.3 第 4-15 项）
完整补完前端 polish。

### Phase 8：半成品大扫除
- 删 `xmclaw/plugins/loader.py`
- 删 `xmclaw/daemon/routers/workspaces.py`（被 workspace.py 替代）
- 处理孤立事件（TODO_UPDATED / MEMORY_EVICTED）
- backup_scheduler 接 lifespan
- secret store 整合 daemon

### Phase 9：差异化加分项
- 子智能体并发 status row UI（cline）
- Memory dreaming（OpenClaw）
- Per-skill per-channel routing（QwenPaw）
- Proactive trigger（QwenPaw）

---

## 7. 测试策略

每 Phase 提交前必须过：
1. `python scripts/test_changed.py` smart-gate 全绿
2. `ruff check xmclaw/` 全绿
3. 手动验证：daemon 重启 + chat 发一句、断开重连、跨 session 记忆触达

新增 lane（`scripts/test_lanes.yaml`）：
- `persona`：`xmclaw/core/persona/**` → `tests/unit/test_persona*.py`
- `memory_inject`：`xmclaw/daemon/agent_loop.py` (memory part) → `tests/integration/test_cross_session_memory.py`
- `multi_agent_v2`：`xmclaw/core/multi_agent/**` → `tests/integration/test_multi_agent_4_conventions.py`
- `channel`：`xmclaw/providers/channel/**` → `tests/integration/test_channels.py`

---

## 8. 附录 A：完整 port 矩阵（peer 文件 → XMclaw 文件）

| Peer 文件 | XMclaw 落点 | Phase |
|---|---|---|
| `hermes-self-evolution/evolution/skills/evolve_skill.py:157-177` | `xmclaw/core/evolution/mutator.py` | 0 |
| `hermes-self-evolution/evolution/datasets/dataset_builder.py:96-169` | `xmclaw/core/evolution/dataset.py` | 0 |
| `hermes-self-evolution/evolution/skills/constraints.py:30-174` | `xmclaw/core/evolution/constraints.py` | 0 |
| `hermes-self-evolution/external_importers.py:157-416` | `xmclaw/core/evolution/seed.py` | 0 |
| `openclaw/src/agents/system-prompt.ts:44-52, 115-118, 706-707` | `xmclaw/core/persona/assembler.py` | 1 |
| `openclaw/src/agents/workspace.ts:19-86` | `xmclaw/core/persona/loader.py` | 1 |
| `openclaw/docs/reference/templates/{SOUL,IDENTITY,BOOTSTRAP,AGENTS,USER,TOOLS,MEMORY}.md` | `xmclaw/core/persona/templates/` | 1 |
| `hermes-agent/agent/prompt_builder.py:36-71, 134-142, 1067-1072` | `xmclaw/core/persona/assembler.py` (sanitizer) | 1 |
| `hermes-agent/run_agent.py:4463-4582` | `xmclaw/core/persona/assembler.py::build_system_prompt` | 1 |
| `qwenpaw/src/qwenpaw/agents/prompt.py:323-372` | `xmclaw/core/persona/bootstrap.py::build_bootstrap_guidance` | 1 |
| `open-webui/backend/open_webui/utils/middleware.py:1473-1505` | `xmclaw/daemon/agent_loop.py` (memory inject) | 1 |
| `hermes-agent/agent/memory_manager.py:17-26, 66-81` | `xmclaw/daemon/agent_loop.py` (`<memory-context>` fence) | 1 |
| `hermes-agent/run_agent.py:9550-9712` | `xmclaw/daemon/agent_loop.py` (prefetch + cache) | 1 |
| `open-webui/src/lib/components/chat/Messages/Markdown.svelte:39-82` | `xmclaw/daemon/static/lib/markdown.js` | ✅ done |
| `open-webui/src/lib/components/common/ToolCallDisplay.svelte:113-178` | `xmclaw/daemon/static/components/molecules/MessageBubble.js` | ✅ done |
| `cline/src/shared/multi-root/types.ts:11-16` | `xmclaw/core/workspace/types.py` | 3 |
| `continue/core/config/loadLocalAssistants.ts:104-125` | `xmclaw/core/workspace/loader.py` (7 块约定) | 3 |
| `qwenpaw/src/qwenpaw/app/multi_agent_manager.py:22-130` | `xmclaw/core/multi_agent/manager.py` | 3 |
| `qwenpaw/src/qwenpaw/app/agent_context.py` | `xmclaw/core/multi_agent/context.py` (ContextVar) | 3 |
| `qwenpaw/src/qwenpaw/app/routers/agent_scoped.py` | `xmclaw/daemon/middleware/agent_scope.py` | 3 |
| `qwenpaw/src/qwenpaw/agents/tools/agent_management.py:18-200, 107-121` | `xmclaw/providers/tool/inter_agent.py` (transport→bus) | 3 |
| `qwenpaw/src/qwenpaw/agents/skills/multi_agent_collaboration-{en,zh}/SKILL.md` | `xmclaw/skills/builtin/multi_agent_collab/` | 3 |
| `open-webui/src/lib/components/chat/ModelSelector/Selector.svelte:43-60` | TopBar 模型 picker | 3 |
| `cline/src/services/mcp/McpHub.ts:213-273, 286-549` | `xmclaw/providers/tool/mcp_hub.py` | 4 |
| `cline/src/services/mcp/schemas.ts:5-93` | `xmclaw/providers/tool/mcp_schema.py` | 4 |
| `cline/src/core/prompts/system-prompt/registry/ClineToolSet.ts:151-192, 198-257` | `xmclaw/providers/llm/tool_translator.py` | 4 |
| `cline/webview-ui/.../auto-approve-menu/AutoApproveMenuItem.tsx:31-61` | `xmclaw/daemon/static/pages/Security.js` | 4 |
| `continue/core/tools/definitions/readFile.ts:47-59` | `xmclaw/security/approval_service.py` | 4 |
| `cline/webview-ui/.../chat/SubagentStatusRow.tsx:75-285` | `xmclaw/daemon/static/components/molecules/SubAgentRow.js` | 4 |
| `qwenpaw/src/qwenpaw/app/channels/feishu/` | `xmclaw/providers/channel/feishu/` | 5 |
| `qwenpaw/src/qwenpaw/app/channels/dingtalk/` | `xmclaw/providers/channel/dingtalk/` | 5 |
| `qwenpaw/src/qwenpaw/app/channels/wecom/` | `xmclaw/providers/channel/wecom/` | 5 |
| `qwenpaw/src/qwenpaw/app/channels/weixin/` | `xmclaw/providers/channel/weixin/` | 5 |
| `qwenpaw/src/qwenpaw/app/channels/telegram/` | `xmclaw/providers/channel/telegram/` | 5 |
| `qwenpaw/src/qwenpaw/app/channels/base.py` | `xmclaw/providers/channel/base.py` | 5 |
| `qwenpaw/src/qwenpaw/app/channels/unified_queue_manager.py` | `xmclaw/providers/channel/queue.py` | 5 |
| `qwenpaw/src/qwenpaw/tunnel/cloudflare.py` | `xmclaw/utils/tunnel.py` | 5 |
| `openclaw/extensions/<channel>/index.ts:4-24` (defineBundledChannelEntry) | `xmclaw/providers/channel/base.py` (contract shape) | 5 |
| `hermes-agent/cron/scheduler.py + cron/jobs.py` | `xmclaw/core/scheduler/cron.py` (新建，删旧的 daemon/cron_*) | 6 |
| `hermes-agent/plugins/memory/honcho/client.py:265-267` | `xmclaw/providers/memory/honcho/client.py` | 6 |
| `hermes-agent/acp_adapter/server.py:13-47` | `xmclaw/providers/channel/acp.py` | 6 |
| `openclaw/src/canvas-host/server.ts:1-39` | `xmclaw/providers/runtime/canvas_host.py` | 6 |
| `openclaw/extensions/memory-core/src/dreaming.ts:29-37` | `xmclaw/core/evolution/memory_consolidation.py` | 9 |
| `qwenpaw/src/qwenpaw/agents/memory/proactive/proactive_trigger.py:27-48` | `xmclaw/core/proactive/trigger.py` | 9 |
| `open-webui/src/lib/components/chat/Messages/CodeBlock.svelte` | `xmclaw/daemon/static/components/molecules/CodeBlock.js` | 7 |
| `cline/webview-ui/.../chat/SlashCommandMenu.tsx` | `xmclaw/daemon/static/components/molecules/SlashMenu.js` | 7 |
| `cline/webview-ui/.../chat/ChatTextArea.tsx:25-32` | `xmclaw/daemon/static/components/molecules/AtMention.js` | 7 |
| `open-webui/src/lib/components/layout/SearchModal.svelte` | `xmclaw/daemon/static/components/molecules/CommandPalette.js` | 7 |
| `open-webui/src/lib/components/chat/Placeholder.svelte` | `xmclaw/daemon/static/pages/Chat.js` (空状态) | 7 |
| `aider/aider/history.py:33-90` | `xmclaw/core/session/summarizer.py` | 9（按需） |

---

## 9. 附录 B：要删的代码

| 文件 | 原因 | Phase |
|---|---|---|
| `xmclaw/plugins/loader.py` | NotImplementedError 空壳，0 调用者 | 8 |
| `xmclaw/daemon/routers/workspaces.py` | 被 `routers/workspace.py` 替代 | 3 |
| `xmclaw/daemon/static/pages/ModelProfiles.js` | 并入顶栏 picker + 设置页 | 3 |
| `xmclaw/daemon/cron_store.py` + `cron_scheduler.py` | 半成品，被 Hermes 风的 `core/scheduler/cron.py` 替代 | 6 |
| `MEMORY_EVICTED` 事件 0 subscriber | 加 subscriber 或停发 | 8 |
| `TODO_UPDATED` 事件无 UI | 加 UI 或停发 | 8 |
| `backup_scheduler.py` 不接 lifespan | 接 lifespan 或删 | 8 |
| Secret store CLI 不接 daemon | 整合或删 | 8 |
| 旧 `_load_persona_addendum`（factory.py） | 被新 `core/persona/assembler.py` 替代 | 1 |

---

## 10. 实施纪律

每 Phase 完成后：
1. `python scripts/test_changed.py` 全绿
2. `ruff check xmclaw/` 全绿
3. `git commit -m "Phase N: <动作>"`，commit 消息含本文档章节号 + peer 文件引用
4. `docs/DEV_ROADMAP.md` Epic #23 进度日志加一行 `YYYY-MM-DD: <摘要> (commit <sha7>)`
5. 推到 main（用户授权 direct push，免 PR）

**End of doc.** 按 Phase 0 → 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9 顺序执行。
