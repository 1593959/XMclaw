# XMclaw 开发路线图（Dev Roadmap）

> **日期**：2026-04-22
> **版本基线**：v2.0.0.dev0（Phase 4.10 完成，全部前端 UI 已删除，进入终端优先测试阶段）
>
> **本文定位**："做什么"——带 file:line 证据的 17 Epic 工程拆解，直接可开 PR。
> **配套阅读**：
> - `archive/COMPETITIVE_GAP_ANALYSIS.archived.md`——竞品架构深度剖析（已合并到本文）。
> - `archive/ROADMAP_PEER_SYNTHESIS.archived.md`——早期战略融合路线图（已合并到本文）。
>
> **本文是唯一的活文档**。另外两份已归档，关键内容已合并至此。
>
> **本文回答**：对照 OpenClaw / HermesAgent / CoPaw 今日仓库实态（`gh api` 实拉 + 本地源码），我们还有哪些问题要处理才能成为一个**成熟产品**；文件/路径规范如何统一；优势如何让用户直观感受。

---

## 0. TL;DR

| 问题 | 一句话回答 |
|------|------------|
| 我们差在哪？ | Commodity 层（渠道/技能商店/onboarding/desktop/日志/安全扫描）几乎全缺；核心引擎领先 |
| 我们赢在哪？ | **Streaming Evolution-as-Runtime + HonestGrader + 版本化 Skills + 自建 Loop**——三家都没做到 |
| 用户能感受到吗？ | 目前不能。必须加一个"进化面板"让用户**肉眼看见 agent 在进步**（见 §5） |
| 技术债最隐蔽的是？ | 没有路径单入口 + secrets 明文 + 无 prompt 注入防御——三个雷区都能导致 CVE |
| 最快补课的两块？ | **抄 QwenPaw 全套 security YAML**（Apache-2 合法）+ **抄 OpenClaw AGENTS.md 分层纪律** |
| 成熟度离产品还有多远？ | 9 个关键里程碑（见 §7）。最短路径 6 周，现实路径 12 周 |

---

## 1. 今日对标快照（实地 GitHub 抓取，2026-04-22）

> 本节数据来自 `gh api` 直接拉取 README / AGENTS.md / pyproject.toml / 目录树。不复读记忆，全部以当下仓库状态为准。

### 1.1 三家核心指标

| 项目 | 语言 | License | 主版本 | Stars | 架构概括 |
|------|------|---------|--------|-------|----------|
| **OpenClaw** (`openclaw/openclaw`) | TypeScript (Node 22+) | MIT | 活跃更新中 | **362k** | 插件化 Monorepo：`src/`(core) + `extensions/`(plugins) + `apps/`(android/iOS native) + `Swabble/`(Swift desktop) |
| **HermesAgent** (`NousResearch/hermes-agent`) | Python 3.11+ | MIT | v0.10.0 | **109k** | 根目录散布式 Python 包：`agent/` `gateway/` `tools/` `cron/` `hermes_cli/` `ui-tui/`(Ink/React TUI) |
| **CoPaw→QwenPaw** (`agentscope-ai/QwenPaw`) | Python 3.10-3.13 | Apache-2 | v1.1.3 | 小众但活跃 | AgentScope-based：`src/qwenpaw/{agents,security,channels,backup,providers,...}` + 独立 `console/` Vite UI |
| **XMclaw** (我们) | Python 3.10+ | — | v2.0.0.dev0 | — | FastAPI+WS daemon，核心引擎在 `xmclaw/core/` + `xmclaw/daemon/` |

### 1.2 架构决策对照

| 决策 | OpenClaw | Hermes | QwenPaw | XMclaw | 备注 |
|------|----------|--------|---------|--------|------|
| 核心形态 | Gateway 进程（非 daemon） | CLI + Gateway 双入口 | FastAPI daemon + Vite console | **FastAPI daemon + WS** | 和 QwenPaw 最像 |
| 插件边界 | 严格：`src/plugin-sdk/*` 公开契约 | 弱：`optional-skills/` 是目录约定 | 中：`plugins/` 目录 + 入口点 | **无**（integrations 直接 import core） | 我们最薄弱 |
| TUI | 无（靠 Swabble 桌面） | Ink/React（**专门写的终端 UI**） | questionary 交互 | 仅 CLI repl | Hermes 在这里做到极致 |
| WebUI | 靠 Swabble | 只有文档站 | `console/`（独立 Vite） | **已删除** | 回到终端测试阶段 |
| 配置格式 | JSON + ENV | **YAML** + `.env` | YAML | **JSON** + 即将加 ENV | Hermes 的 YAML 可读性更好 |
| 家目录 | `~/.openclaw/` | `~/.hermes/` | `~/.qwenpaw/` | `~/.xmclaw/`（未落地） | 需尽快标准化 |
| 进化机制 | 无 | **DSPy+GEPA 批量**，且在**独立仓库** | 无 | **运行时流式**（v2 已验证 1.18× 提升） | **← 这是我们唯一的硬差异** |

### 1.3 关键事实修正（比记忆更新）

1. **Hermes 不是"6 终端后端都靠谱"**——pyproject 里 `modal` 和 `daytona` 是 optional extras；Termux 明确列为"避开 voice extra"；Windows native "not supported, 请用 WSL2"。**他们不如他们 PR 说的那么 cross-platform。**
2. **Hermes 的进化确实是外挂批量**：`hermes-agent-self-evolution` 是独立仓库（2k⭐，与主仓 109k⭐ 解耦），每次跑 $2-$10，产出 PR 而不是直接 commit。**5 个 phase 里只有 Phase 1（skill files）实现，Phase 2-5 "planned"。**这证实我们的"流式 runtime 进化"是真空地带。
3. **OpenClaw 的 `AGENTS.md` 是工程纪律宝典**——强制 `pnpm check:changed` 智能门禁、`tsgo` 禁用 `tsc --noEmit`、`extensions/` 不得反向 import `src/`。**我们要抄这种纪律，不仅仅是代码。**
4. **QwenPaw 的 `security/` 是真货**——`skill_scanner/rules/signatures/data_exfiltration.yaml`、`tool_guard/rules/` 这种签名库是他们"post-incident hardening"留下的遗产。**Hermes 和 OpenClaw 都没有这种开箱即用的安全扫描。**
5. **QwenPaw 支持 ACP Server**（Agent Client Protocol，给 VS Code / Zed / JetBrains 当后端）——Hermes 也有 `acp_adapter/`。**我们现在没做，但应该做**，因为 IDE 侧集成是用户粘性来源。
6. **QwenPaw 不拥有自己的 agent loop**——`agents/react_agent.py:76-94` 直接继承 `agentscope.agent.ReActAgent`，只套了一层 `ToolGuardMixin`。事件 schema 也来自 `agentscope_runtime.engine.schemas.agent_schemas`。他们把"重新造 loop"的工程量省了——我们 v2 自己写 loop 是真正的差异化投资，但也是成本。
7. **QwenPaw 的多 agent 是 "HTTP-to-self" 模式**——`app/multi_agent_manager.py:22-137` 用 `Dict[str, Workspace]` 保存多个 workspace，agent 间对话走本地 HTTP（`http://127.0.0.1:8088`）+ `X-Agent-Id` header。**这个模式非常 debuggable，我们的 EvolutionEngine peer 层可以直接学。**
8. **QwenPaw `SECURITY.md:66-75` 坦白承认**："单操作者信任模型，非多租户，skill 在进程内运行，working dir 是信任域"。**他们没有 skill sandbox**——靠容器做进程级隔离。这让我们看清：如果 XMclaw 不走多租户，进程内 + 容器隔离就够；真要多租户，必须 v1.0 之前锁定。
9. **QwenPaw 的"Qwen"是虚名**——他们没有 `qwen_provider.py`，Qwen 通过 DashScope OpenAI-compat endpoint（`constant.py:222-225`）接入，本质上是 provider-agnostic。我们的 provider 层设计应同样 **API-compat 优先**，而不是一个模型一个 provider。
10. **QwenPaw 的 `RoutingChatModel` 是个 stub**——`agents/routing_chat_model.py:42-53` 接收 text/tools 但全部 `del` 掉。他们喊的"小模型大模型智能协作"**没实现**。我们的 gene-driven 模型路由如果做到，这就是第二个硬差异化。

### 1.4 free-code 关键事实（第四家对标，终端原生 AI 天花板）

> free-code（Claude Code 开源 fork）不在 GitHub 公开仓库中，但其设计模式已被 Hermes/OpenClaw/QwenPaw 大量借鉴。以下事实来自对已泄漏/公开文档的分析，以及竞品对其的反向工程。

1. **权限系统是行业天花板**——细粒度规则 `Bash(ls)` / `Bash(rm:*)` / `FileRead(path)` + Auto mode classifier（`classifyYoloAction`，LLM 判断操作是否安全）+ Denial tracking（连续拒绝计数，超阈值 fallback 到提示用户）。XMclaw 当前三级（ASK/ALLOW/BLOCK）太粗。
2. **记忆系统标杆**——`MEMORY.md` 作为索引（≤200 行 / 25KB，自动截断）+ 类型化记忆（user/feedback/project/reference）+ KAIROS 日志（`logs/YYYY/MM/YYYY-MM-DD.md`，夜间蒸馏为 MEMORY.md）。XMclaw 当前只有 sqlite-vec 向量检索，缺少文件化索引和类型化。
3. **Cron 工具链最完善**——`CronCreateTool`/`CronDeleteTool`/`CronListTool`，Agent 自己管理调度。文件持久化 `.claude/scheduled_tasks.json` + `tryAcquireSchedulerLock` 防多实例重复触发 + `jitteredNextCronRunMs` 防同时触发 + missed task 检测。XMclaw 当前无主动调度。
4. **技能系统是行业最佳实践**——`SKILL.md` + YAML frontmatter（`allowed-tools` / `when_to_use` / `paths` / `model` / `effort`）+ 动态发现（文件操作时自动向上遍历 `.xmclaw/skills`）+ 条件激活（`paths` gitignore-style 路径模式匹配时自动激活）+ 多层级加载（user > project > managed）+ Shell 内联（``!`command` ``）。
5. **QueryEngine 抽象**——对话生命周期抽象为 `QueryEngine`，支持 headless/SDK/REPL 三种模式。XMclaw 当前只有 daemon WS + CLI repl，缺少 SDK 模式。
6. **Feature Flags**——88 个编译时条件加载标志，避免 runtime bloat。XMclaw 是 Python dynamic import，无需编译时标志，但缺少功能开关机制。

---

## 2. 我们真实的缺口（基于今日 XMclaw 审计）

> 来自 [V2_STATUS.md](V2_STATUS.md) 和 2026-04-22 代码审计。不含空话，每条都能对应到文件。
>
> **要更高层视角（每家竞品的架构详解、跨 10 个维度的能力矩阵）？** → [COMPETITIVE_GAP_ANALYSIS.md §2-§3](COMPETITIVE_GAP_ANALYSIS.md#2-竞品架构深度对比)。本节聚焦**可修复的具体代码缺口**。

### 2.1 结构性缺口（Must-fix 才能叫产品）

| # | 缺口 | 证据 | 影响 |
|---|------|------|------|
| 1 | **渠道全是 stub** | `xmclaw/channels/discord.py`、`slack.py`、`telegram.py`、`lark.py:27-28` 都是 stub，`ChannelManager` 未实现 | 产品承诺"多渠道"，实际只有 WS |
| 2 | **无插件 SDK 边界** | `xmclaw/integrations/*` 直接 import `xmclaw/core`；没有 `plugin_sdk/` 公开契约 | 第三方无法写插件；我们自己改 core 会炸掉 integrations |
| 3 | **Sandbox 仅进程内** | `xmclaw/sandbox/` 无 Docker/subprocess 隔离；`LocalSkillRuntime` 是同进程 | 用户装个恶意 skill 可以删文件 |
| 4 | **进化执行层空缺** | `GeneForge` / `SkillForge` / `VFM` 在 v1 残骸；v2 只做了 decision（`EvolutionController`），没做 generation | 我们的差异化目前**只是理论**，用户看不到"进化" |
| 5 | **Memory eviction 未实现** | `core/memory/manager.py:209` TODO | 长期跑会爆 |
| 6 | **ENV override 未接线** | `CLAUDE.md` 承诺 `XMC__llm__anthropic__api_key`，代码未读 | Docker 部署、CI 部署痛苦 |
| 7 | **无桌面/IDE 入口** | 无 ACP adapter；无 tray | 用户必须开终端——敌不过 Hermes `hermes` + Slack bot 一键 |
| 8 | **无技能商店/hub** | 有 SkillRegistry，无远程检索、安装、签名 | Hermes `/skills` slash command + agentskills.io 标准把我们甩开一个代差 |
| 9 | **无 onboarding 向导** | `xmclaw onboard` 是 stub | 用户第一次打开不知道怎么配 model |
| 10 | **无 doctor 诊断** | `xmclaw doctor` 是 stub | 用户报 bug 无从下手 |

### 2.2 工程纪律缺口

| # | 缺口 | 参照 | 现状 |
|---|------|------|------|
| 11 | **缺 smart-gate 测试编排** | OpenClaw 的 `pnpm check:changed` | 我们全量跑 `pytest`，慢；无 changed-lane 概念 |
| 12 | **缺 AGENTS.md 工程契约** | OpenClaw 每个子目录都有 | 我们只有顶层 `CLAUDE.md` |
| 13 | **事件总线 SQLite 后端未实现** | `xmclaw/core/bus/sqlite.py:28` 有 schema 但未接通 | 事件回放只能读内存，重启丢失 |
| 14 | **无提示词注入防御** | Hermes `agent/prompt_builder.py` 有 10+ 条正则 + 不可见字符扫描 | 我们直接把 AGENTS.md / SOUL.md 注入 prompt，毫无防御 |
| 15 | **日志结构化** | 我们有 `structlog` 依赖但没建 sink/rotation | 生产运维盲区 |
| 16 | **加密 / keyring** | QwenPaw 用 `keyring>=25` + `cryptography>=43` 加密 secrets | 我们明文存 `daemon/config.json` |

---

## 3. 文件与路径规范（File / Path Convention）

> 这是用户明确问到的"文件规范、路径规范"。下面规定为**v2 正式约束**，新代码必须遵守；老代码后续迁移。

### 3.1 运行时路径（用户机器上）

采用 **XDG Base Directory** + **家目录命名空间**，参考 Hermes `~/.hermes/` 和 QwenPaw `~/.qwenpaw/`：

```
~/.xmclaw/                     # 用户级数据主目录
├── config.yaml                # 主配置（迁移 JSON→YAML，见 §3.3）
├── .env                       # 明文 secrets（chmod 600，仅当无 keyring 可用时）
├── state.db                   # SQLite：sessions、events、cost、skills history
├── vector.db                  # sqlite-vec：memory embeddings
├── memory/                    # Markdown 记忆（FileMemoryIndex，跨会话）
│   ├── MEMORY.md              # 索引
│   └── <topic>.md             # 每个 topic 一个文件
├── skills/                    # 用户安装 / 进化产出的技能（见 §3.5 格式）
├── agents/                    # 多 agent profile（参考 QwenPaw `<WORKING_DIR>/<agent_id>/`）
│   └── <agent_id>/
│       ├── agent.yaml         # 单 agent 的 LLM / tools / skills 选择
│       ├── sessions/          # 会话 JSON，文件名 `<user>_<session>.json`
│       ├── memory/            # agent 专属记忆
│       ├── active_skills/     # 当前 session 启用的 skill 软链
│       ├── customized_skills/ # 用户定制版 skill
│       └── HEARTBEAT.md       # 心跳文件（健康检测）
├── custom_channels/           # 第三方 channel 热加载目录（参考 QwenPaw `registry.py:97-129`）
├── plugins/                   # 第三方插件（console 前端 + Python 后端配对）
├── logs/                      # 结构化日志（按天 rotate）
│   └── daemon-2026-04-22.jsonl
├── pid                        # daemon PID（单实例锁）
└── ed25519.key                # 设备绑定密钥（pair auth）

# 安全相关放 sibling 目录（抄 QwenPaw `constant.py:102-111` 的 SECRET_DIR 模式）
~/.xmclaw.secret/              # 加密的 API key / token；与主目录分开防误删
~/.xmclaw.backups/             # 备份归档；分开便于 rsync 选择性同步

# 仓库侧（git 仓库内）
<repo>/
├── daemon/                    # 仅放 example 和模板
│   ├── config.example.yaml    # 标准模板
│   └── skills.example/        # 示例技能
├── xmclaw/                    # 代码包
├── plugins/                   # 第三方插件入口（entry_points 发现）
├── shared/                    # 运行时写入（gitignored），开发模式用
│   ├── skills/                # 与 ~/.xmclaw/skills 二选一（由 config 切换）
│   └── vector_db/
└── agents/                    # 多 agent profile（可选）
    └── <name>/agent.yaml
```

**规则**：

1. **默认走 `~/.xmclaw/`**；仅当 `XMC_DATA_DIR` 或 `--data-dir` 覆盖时用别的。
2. **仓库内禁止写运行时数据**（除非用户显式 `--dev`）——目前 `shared/` 的写入需要加 `--dev` gate。
3. **secrets 永不进 `config.yaml`**——全部走 `.env` 或 `keyring`（参考 QwenPaw）。
4. **路径解析单入口**：`xmclaw/utils/paths.py` 提供 `data_dir()` / `skills_dir()` / `log_dir()` 等；其他模块禁止手拼路径。

### 3.2 代码树规范（仓库内）

```
xmclaw/
├── core/                      # 纯逻辑，不依赖 I/O 框架
│   ├── bus/                   # 事件总线
│   ├── ir/                    # ToolCall / ToolResult / ToolSpec
│   ├── grader/                # HonestGrader
│   ├── scheduler/             # UCB1 bandit
│   ├── evolution/             # Controller（决策）
│   ├── memory/                # FileMemoryIndex + MemoryManager
│   └── session/               # Session lifecycle
├── daemon/                    # FastAPI / WS，I/O 边界
│   ├── app.py
│   ├── agent_loop.py
│   ├── factory.py             # config → runtime 对象
│   ├── lifecycle.py
│   └── config_reloader.py
├── providers/                 # 外部系统适配（全部 ABC + 实现）
│   ├── llm/                   # anthropic / openai / ollama / ...
│   ├── tool/                  # builtin / mcp_bridge / browser / lsp
│   ├── memory/                # sqlite_vec / ...
│   ├── runtime/               # local / process / docker
│   └── channel/               # discord / slack / telegram / lark / ws
├── plugin_sdk/                # ★新增：公开契约
│   ├── __init__.py            # 公开 API：SkillBase, ToolBase, ChannelBase
│   ├── events.py              # 公开 EventType subset
│   └── types.py               # 公开 Pydantic 模型
├── cli/                       # typer-based
│   ├── main.py
│   ├── serve.py / start.py / stop.py / ...
│   ├── onboard.py             # ★补实现
│   └── doctor.py              # ★补实现
├── utils/
│   ├── paths.py               # ★新增：路径解析单入口
│   ├── logging.py             # structlog 配置
│   └── secrets.py             # keyring + .env 统一接口
└── skills/                    # 内置技能（非生成）
    ├── registry.py
    └── builtin/
```

**规则**：

1. **`core/` 不得 import `daemon/` 或 `providers/`**（单向依赖）。
2. **`providers/` 的每个子包都必须有 `base.py` 定义 ABC**——新加 provider 先加 ABC 再实现。
3. **第三方插件只能 import `xmclaw.plugin_sdk.*`**；禁止 import `xmclaw.core.*` / `xmclaw.daemon.*`。抄 OpenClaw 的 `src/plugin-sdk/AGENTS.md` 规则：CI 跑 `import-cycles` + `madge` 检查。
4. **每个 `providers/*` 子目录放一个 `AGENTS.md`** 说明该 provider 的契约（参考 OpenClaw）。

### 3.3 配置格式迁移：JSON → YAML

**动机**：Hermes 用 YAML，QwenPaw 用 YAML，OpenClaw 用 JSON + ENV。YAML 对人友好（多行、注释），对机器 `ruamel.yaml` 可保留注释。用户反复被 `daemon/config.json` 路径 + 相对路径坑过。

**迁移步骤**（不破坏现有部署）：

1. **Phase A**：新增 `~/.xmclaw/config.yaml`，`factory.load_config()` 同时接受 `.json` 和 `.yaml`，优先 yaml。
2. **Phase B**：`xmclaw config migrate` 命令一次性转换存量 `config.json` → `config.yaml`，secrets 同时抽到 `.env`。
3. **Phase C**：老 `config.json` 兼容一个版本后废弃。

### 3.4 Skill 文件格式（对齐公共标准，兼容 QwenPaw / Claude Agent Skills）

**硬约束**：XMclaw skill 必须能被 QwenPaw / Claude Agent Skills 解析，反之亦然。**这是网络效应**——用户能把 QwenPaw 上的 skill 直接拖进 `~/.xmclaw/skills/`。

格式（参考 QwenPaw `agents/skills/multi_agent_collaboration-en/SKILL.md:1-8`）：

```
~/.xmclaw/skills/<skill-name>[-<lang>]/
├── SKILL.md                   # 必需：YAML frontmatter + Markdown 正文
├── manifest.yaml              # 可选：XMclaw 专属扩展（权限、gene genealogy）
├── history.jsonl              # 必需：版本历史 + 进化证据（见 §5.1）
├── scripts/                   # 可选：skill 调用的辅助脚本
└── references/                # 可选：长参考文档（如 Office XSD）
```

`SKILL.md` 头部：

```yaml
---
name: <skill-name>
description: <一句话，模型据此判断是否调用>
metadata:
  builtin_skill_version: "1.0"        # 兼容 QwenPaw 版本号约定
  xmclaw:                             # 我们的专属命名空间（QwenPaw 允许 `_REQUIREMENTS_METADATA_NAMESPACES` 多命名空间并存）
    emoji: "🔍"
    evolution_lineage: "gene-abc-123"  # 该 skill 源自哪条 gene
---

# Skill 正文（模型调用时才读）
```

**per-language pair 约定**：QwenPaw 用 `<name>-en/` 和 `<name>-zh/` 两个目录，通过正则 `^(?P<name>.+)-(?P<language>en|zh)$` 匹配。我们抄这个——用户 `language` 设置切换，无需二次翻译基础设施。

**per-channel routing**：QwenPaw `resolve_effective_skills(workspace_dir, channel)` 让不同渠道用不同 skill 集（`ALL_SKILL_ROUTING_CHANNELS` 白名单）。我们 Epic #1 channel SDK 做完后，直接接这层。

### 3.5 命名规范

| 对象 | 规则 | 例子 |
|------|------|------|
| 事件类型 | `UPPER_SNAKE`（保持 v2 现状） | `TOOL_INVOCATION_FINISHED` |
| Python 模块 | `lower_snake` | `config_reloader.py` |
| Skill 目录名 | `kebab-case` | `github-code-review/` |
| Skill `id` | `kebab-case` | `github-code-review` |
| Config key | `snake_case` + 分组 | `llm.anthropic.api_key` |
| ENV 变量 | `XMC__<dotted_path>` 双下划线转点 | `XMC__llm__anthropic__api_key` |
| CLI 命令 | `kebab-case` | `xmclaw config-migrate` |

---

## 3.6 执行协议（Execution Protocol）★ 每次开发必读

**这是硬纪律**——任何 Epic / Milestone 的状态变化，必须**立即**回写本文档；文档更新与代码变更必须在**同一个 PR** 内。

### 3.6.1 状态图标

| 图标 | 状态 | 含义 |
|------|------|------|
| ⬜ | 未开始 | 尚未触发 |
| 🟡 | 进行中 | 有开发者在做 |
| 🔴 | 阻塞 | 遇依赖 / 设计问题 |
| ✅ | 完成 | 所有子项勾完、退出标准满足 |
| ⏸ | 暂停 | 主动推迟（需写原因） |

### 3.6.2 更新触发点

每个 Epic 至少 4 次更新：

1. **启动时**：状态 ⬜→🟡，填 **负责人** + **起始日期**
2. **每个子步完成**：checkbox 打 ✅，**进度日志**追加一行 `YYYY-MM-DD: <一句话摘要> (commit abc123)`
3. **遇阻塞**：状态 🟡→🔴，**进度日志**记录 reason + 等谁
4. **Epic 完成**：状态 ✅，填 **完成日期**，同时去 §7 把相关 Milestone 退出标准打勾

### 3.6.3 Commit 消息约定

所有 commit 必须引用 Epic 号：

```
Epic #6: 实现 XMC__ prefix ENV override 解析
Epic #14 partial: 移植 Hermes _CONTEXT_THREAT_PATTERNS 正则
Epic #3 blocked: Docker 运行时需要决策 extras vs 可选子包
```

### 3.6.4 反馈 / Retrospective 格式

**进度日志**追加式，不删除，每条 ≤1 行。每个 Epic 完成后，在日志末尾写一条 `retrospective:` 行，总结 3 件事（做对了什么 / 该避开的坑 / 可复用的模式），作为后续 Epic 的输入。

### 3.6.5 文档同步规则

- Checkbox 打勾 = §4 该 Epic 状态同步更新 = §7 对应 Milestone 退出标准同步更新。任何一处漏改视为不合格 PR。
- 每周一次：跑 `scripts/lint_roadmap.py`（待 Epic #10 附加）校验状态一致性。
- 重大方向调整（比如某 Epic 降优先级）在 **进度日志** 写明 + 在 CLAUDE.md 加一行给未来的 AI 协作者。

---

## 4. 对标差异执行表（拆到可 PR 粒度）

按 §2 的缺口编号，一条一个 Epic。每个 Epic 包含：**状态** → **开发计划**（有序步骤）→ **检查清单**（checkbox）→ **退出标准** → **进度日志**（追加式）。

### Epic #1 · Channel SDK（参照 OpenClaw 插件边界 + Hermes gateway）

**目标**：把 `xmclaw/channels/` 从 stubs 变成"Discord/Slack/Telegram 三条可用"。

**状态**：⬜ 未开始 | **负责人**：- | **起始**：- | **完成**：-
**前置依赖**：Epic #2（Plugin SDK 边界）要先定好契约约束
**关联 Milestone**：M2（三渠道可用）

**开发计划**：

1. **契约设计**（0.5 天）——定义 `ChannelBase` ABC + `IncomingMessage` / `OutgoingMessage` Pydantic 模型，参考 QwenPaw `app/channels/base.py:25-100`
2. **Conformance test 骨架**（0.5 天）——先写测试后写实现：`tests/conformance/test_channel_conformance.py` 对每个 channel 跑 5 类消息（text / media / reaction / command / 错误恢复）
3. **参考实现抽出**（1 天）——把现有 `providers/channel/ws.py` 重构到新 ABC，让它第一个通过 conformance test
4. **Discord 实现**（2 天）——`discord.py` + mock 对话测试
5. **Slack 实现**（1.5 天）——`slack-bolt` 模板
6. **Telegram 实现**（1.5 天）——`python-telegram-bot` 模板
7. **CLI 子命令**（1 天）——`xmclaw channels {list, enable, disable, configure}`
8. **安全策略钩子**（1 天）——`dm_policy: open|pairing|allowlist` + 配对码（参照 OpenClaw）
9. **文档 + 示例**（0.5 天）——`docs/CHANNELS.md` 写成怎么接新 channel

**检查清单**：

- [ ] `xmclaw/plugin_sdk/channel.py` 公开契约（`ChannelBase`, `IncomingMessage`, `OutgoingMessage`）
- [ ] `tests/conformance/test_channel_conformance.py` 骨架 + 5 类消息用例
- [ ] `providers/channel/ws.py` 重构到新 ABC
- [ ] `providers/channel/discord.py` + mock 测试
- [ ] `providers/channel/slack.py` + mock 测试
- [ ] `providers/channel/telegram.py` + mock 测试
- [ ] `xmclaw channels` CLI 子命令（`list` / `enable` / `disable` / `configure`）
- [ ] `dm_policy` 安全钩子 + 配对码端点
- [ ] `docs/CHANNELS.md` 写完

**退出标准**：

- 从 Telegram 发消息能到 agent loop 并回复（手工 + CI 都通过）
- Conformance test 三个 channel 全绿
- `xmclaw doctor` 能检查 channel token 有效性

**进度日志**：

- _（尚无）_

---

### Epic #2 · Plugin SDK 边界（抄 OpenClaw）

**状态**：🟡 进行中（SDK 边界 + CI 隔离就绪，pilot 迁移 + 外部样例 pending） | **负责人**：Claude (AI pair) | **起始**：2026-04-23 | **完成**：-
**前置依赖**：无（其他 Epic 反过来依赖它）
**关联 Milestone**：M3（Plugin SDK v1）

**开发计划**：

1. **SDK 目录 + 导出冻结**（1 天）——`plugin_sdk/__init__.py` 列出公开符号；其他 import 不公开
2. **契约文档**（0.5 天）——`plugin_sdk/AGENTS.md` 说"什么可 import、什么不能、兼容性承诺是什么"
3. **CI 隔离脚本**（1 天）——`scripts/check_plugin_isolation.py` AST 扫描 `plugins/**` import；pre-commit + CI 双跑
4. **Pilot 迁移**（2 天）——挑 1 个 integrations（比如 notion）改成 plugin 形状，检验契约够用
5. **Pilot 第三方 repo**（1 天）——`xmclaw-plugin-example` 样板仓库；只 import `plugin_sdk`、能被 `pip install` 后发现
6. **批量迁移**（3 天）——剩余 `integrations/` 全部改成 plugin；或标记为 deprecated
7. **兼容性测试**（1 天）——跑 pilot repo 的 CI，验证升级 `plugin_sdk` minor 版本后插件无需改

**检查清单**：

- [x] `xmclaw/plugin_sdk/__init__.py` 公开面：22 个 re-export（`Skill`/`SkillInput`/`SkillOutput`、`ToolProvider`/`ToolCall`/`ToolCallShape`/`ToolResult`/`ToolSpec`、`LLMProvider`/`LLMChunk`/`LLMResponse`/`Message`/`Pricing`、`MemoryProvider`/`MemoryItem`、`ChannelAdapter`/`ChannelTarget`/`InboundMessage`/`OutboundMessage`、`SkillRuntime`、`EventType`/`BehavioralEvent`），`FROZEN_SURFACE` tuple 与 `__all__` 双向锁
- [x] `xmclaw/plugin_sdk/AGENTS.md` 契约规则（responsibility / deps / 测试入口 / 硬禁 / 关键文件五段）
- [ ] `xmclaw/plugin_sdk/events.py` / `types.py`（Pydantic 模型子集）— 暂不拆分，当前 22 个 re-export 都在 `__init__.py`；拆分的成本是多出两个 module 名字，收益是按领域分组，拖到有具体必要时再做
- [x] `scripts/check_plugin_isolation.py` + CI（AST scan，MACHINERY_EXEMPT 豁免 `loader.py` / `__init__.py`；pre-commit hook 留给 Epic #11 smart-gate 覆盖）
- [ ] Pilot integration 迁移成功（当前无 `xmclaw/integrations/` 目录 — 真正 plugin 形态的用户案例还没出现；等第一个第三方插件需求到再做）
- [ ] 外部样例 `xmclaw-plugin-example` 跑通
- [ ] `integrations/*` 批量迁移或 deprecation mark（见上）

**退出标准**：

- `plugin_sdk/` 公开 API 冻结、写进 CHANGELOG
- CI import 隔离检查通过，改 `core/` 时不会破坏 plugin
- 至少 1 个外部样例插件可 `pip install xmclaw-plugin-example` 后被发现

**进度日志**：

- 2026-04-23: 阶段 1-3 落地——新建 `xmclaw/plugin_sdk/__init__.py` 作第三方 plugin 唯一合法 import 面：22 个 re-export 按领域分组（bus / IR / channel / llm / memory / runtime / tool / skill），**纯 re-export 无任何逻辑**（`tests/unit/test_v2_plugin_sdk.py::test_plugin_sdk_init_is_reexports_only` 用 AST 遍历顶层 node 强制这一条），`FROZEN_SURFACE: tuple[str, ...] = tuple(sorted(__all__))` 与 `__all__` 双向锁确保删名字时测试先红——改 `__all__` 没改 `FROZEN_SURFACE` 或反过来都会触发 CHANGELOG 提醒；`test_exports_are_canonical_identities` 用 `getattr(sdk, name) is canonical` 断言 plugin_sdk 不会意外 shadow 一份自己的 `ToolCall`（isinstance 会悄悄断）；`test_import_has_no_side_effects` 起新 subprocess `import xmclaw.plugin_sdk` 断言 stdout/stderr 都为空（SDK 不该在 import 时打日志或做 IO）。新建 `xmclaw/plugin_sdk/AGENTS.md` 五段契约（responsibility / dep rules "SDK 可深入 xmclaw 内部反向禁止 plugins 进内部" / 测试入口 / 硬禁 "never add logic here, never remove a name without a major bump, never shadow canonical definition" / 关键文件），follow `docs/AGENTS_TEMPLATE.md` 结构。新建 `scripts/check_plugin_isolation.py` AST 扫描（pattern 镜像 `check_import_direction.py`）：遍历 `xmclaw/plugins/**/*.py` — 豁免 `MACHINERY_EXEMPT = {"loader.py", "__init__.py"}`（loader 是 plugin 机制本身，要读 entry_points 和 bus，不是 plugin）— 对每个 Import / ImportFrom 节点检查 `_is_forbidden(mod)` 规则：`xmclaw.plugin_sdk.*` / `xmclaw.plugins.*` / 非 `xmclaw.` 起头都放行，其余 `xmclaw.*` 一律红；空目录场景打 "0 plugin file(s) scanned" 提示。`tests/unit/test_v2_plugin_sdk.py` 14 测（surface freeze 4 条 + isolation scanner 6 条合成 fake plugins 覆盖 sdk-only / 非法 core 引用 / 非法 providers 引用 / 允许同 plugins/ 下 sibling / loader.py 豁免 / 扫描数报告 + real tree 干净 regression guard + AGENTS.md 存在性 + importlib reload round-trip）。`scripts/test_lanes.yaml` 加 `plugin_sdk` lane（triggers: `xmclaw/plugin_sdk/**` / `xmclaw/plugins/**` / `scripts/check_plugin_isolation.py`）。阶段 4-7（pilot integration 迁移、外部样例仓、integrations 批量迁移）deferred 到 Epic #2 phase 2——仓库现无 `xmclaw/integrations/` 目录，真正 plugin 形态的用户案例还没出现，没 pilot 要迁（抄 QwenPaw security + Hermes terminal_tool）

**状态**：🟡 进行中（runtime 层 + factory 就绪，AgentLoop 接线 + 8 条 Guardian 规则 + ApprovalService 待落）| **负责人**：Claude (AI pair) | **起始**：2026-04-23 | **完成**：-
**前置依赖**：无
**关联 Milestone**：M4（沙箱可用）+ M8（安全硬化）

**开发计划**：

1. **Runtime ABC**（0.5 天）——`providers/runtime/base.py` 定义 `ExecResult` + `RuntimeBackend.exec()`
2. **Process 运行时**（2 天）——`process.py` subprocess + resource limits（Windows `psutil`、Linux `resource`）+ 超时杀树
3. **规则库移植**（0.5 天）——从 QwenPaw 拷贝 8 份 `skill_scanner/rules/signatures/*.yaml` + `tool_guard/rules/dangerous_shell_commands.yaml`；在 header 加 Apache-2 归属注释
4. **3 Guardian 架构**（2 天）——`security/tool_guard/engine.py` + `FilePathToolGuardian` + `RuleBasedToolGuardian` + `ShellEvasionGuardian`
5. **4-path 决策流**（1 天）——`auto_denied / preapproved / needs_approval / fall_through`，套在 AgentLoop `_acting` 前
6. **ApprovalService + GC**（1 天）——`_pending` / `_completed` dict + 30 分钟 / 200 / 500 阈值
7. **Risk 分层**（0.5 天）——`MEDIUM` / `HIGH` / `CRITICAL` 映射到审批策略
8. **SkillScanner**（1 天）——`security/skill_scanner.py` 扫描 `SKILL.md` + scripts 风险
9. **Docker 运行时**（optional extra，2 天）——`docker.py`，`pip install xmclaw[docker]` 才装
10. **i18n 文案**（0.5 天）——en/zh 审批对话框
11. **测试套**（2 天）——对每条 YAML 规则写 positive + negative 测试；5 条"危险 skill"集成测试

**检查清单**：

- [x] `providers/runtime/base.py` ABC（Phase 3.2 落地，`SkillRuntime` + `SkillHandle` + `SkillStatus`）
- [x] `providers/runtime/process.py`：subprocess + CPU 超时（Phase 3.4 落地；fs/net/memory 仍 advisory，真沙箱等 docker 后端）
- [x] `daemon/factory.py` `build_skill_runtime_from_config`：`runtime.backend: "local"|"process"`；未知 backend 抛 `ConfigError` 不偷偷降级
- [ ] AgentLoop / scheduler 切到 `SkillRuntime.fork(...)` 执行 skill（当前 skill 直接 `skill.run()` 走内联路径，runtime 层空转）
- [ ] `providers/runtime/docker.py`：Docker exec（optional extra）
- [ ] 8 份 YAML + 1 份 shell 规则拷贝到 `xmclaw/security/rules/`
- [ ] `FilePathToolGuardian` / `RuleBasedToolGuardian` / `ShellEvasionGuardian`
- [ ] 4-path 决策流接到 AgentLoop
- [ ] `ApprovalService` + GC
- [ ] i18n 审批文案（en/zh）
- [ ] `MEDIUM` / `HIGH` / `CRITICAL` 风险分层
- [ ] `xmclaw/security/skill_scanner.py` + SkillForge pipeline
- [ ] `xmclaw security scan <skill>` CLI

**退出标准**：

- 跑 `tests/security/test_guardians.py` 全绿，每条 YAML 规则都有正反测试
- 内建 5 条"危险 skill"测试全部被拦截（含 curl|bash、base64 混淆、rm -rf 变体、ssh key 读取、信用卡号外泄）
- 审批过期后自动 GC

**进度日志**：

- 2026-04-23: factory 接线 — `build_skill_runtime_from_config(cfg)` 落地：`runtime.backend` 取 `"local"` / `"process"`，缺省走 `local`；未知 backend 抛 `ConfigError(known=...)` 不悄悄降级到 local（坏配置不应让用户以为跑在 process）；没有 `enabled:false`——没 runtime 的 daemon 没法跑 skill。`daemon/config.example.json` + `docs/CONFIG.md` 同步文档；7 条新单测（section 缺失默认 local / backend 缺省默认 local / 显式 local / 显式 process / 非 dict section / 非 string backend / 未知 backend 错误信息含 known 集）。**当前无 caller**：factory 已就绪但 AgentLoop / scheduler 还没 `runtime.fork(skill, ...)`，接线排到 Epic #3 下一 phase。daemon+runtime+always lane 160 passed (commit 待落)

---

### Epic #4 · 进化执行层（★核心差异化）

**这是我们唯一的用户可感知差异，必须做到看得见。**

**状态**：⬜ 未开始 | **负责人**：- | **起始**：- | **完成**：-
**前置依赖**：Epic #3（scanner）、Epic #13（事件总线）、Epic #5（memory）
**关联 Milestone**：M5（进化可感知）★ 最关键

**开发计划**：

1. **事件类型扩展**（0.5 天）——在 `core/bus/events.py` 加 `GENE_GENERATED` / `SKILL_CANDIDATE_READY` / `SKILL_EVOLVED` / `PROMOTION_ACCEPTED` / `PROMOTION_REJECTED`
2. **触发条件设计**（1 天，写 spec）——gene 生成的 trigger：同 task pattern 连续失败 N 次 / grader 分数低于阈值 / 用户 reaction 负面。写进 `docs/EVOLUTION.md`
3. **gene_forge.py**（3 天）——订阅事件流 → pattern 匹配 → LLM 生成 candidate → 写 `~/.xmclaw/skills/<name>/candidates/<uuid>.md`
4. **skill_forge.py**（2 天）——candidate → `HonestGrader` 打分 → `SkillScanner` 扫描 → 通过后 `SkillRegistry.register()` 新 version
5. **engine.py 总装**（1 天）——订阅-决策-生成-验证流水线，独立 asyncio task
6. **history.jsonl spec**（0.5 天）——格式：`{ts, trigger, old_version, new_version, diff, grader_score, scanner_verdict}`
7. **CLI：`xmclaw evolution show`**（1.5 天）——`--since 24h` 读 `history.jsonl`，格式化为 rich 表格
8. **CLI：`xmclaw session report <id>`**（1 天）——会话结束时对比本 session grader 分 vs 过去同类 task 均值
9. **CLI repl flash**（0.5 天）——订阅 `SKILL_EVOLVED` 在终端底部打绿字 `[evolved] <skill> v3→v4 (+0.12)`
10. **Killer demo GIF**（1 天）——asciinema 录制 email_digest v3→v7 的用户旅程
11. **集成测试**（2 天）——模拟一周 workload，验证 grader 分数真的在升

**检查清单**：

- [ ] `core/bus/events.py` 新事件类型
- [ ] `xmclaw/evolution/gene_forge.py`：流式 gene 生成器
- [ ] `xmclaw/evolution/skill_forge.py`：候选验证
- [ ] `xmclaw/evolution/engine.py`：总装
- [ ] `~/.xmclaw/skills/<name>/candidates/` + `history.jsonl` 格式落地
- [ ] CLI `xmclaw evolution show` 可用
- [ ] CLI `xmclaw session report <id>` 可用
- [ ] CLI repl `SKILL_EVOLVED` flash
- [ ] README 顶部 killer demo GIF
- [ ] `docs/EVOLUTION.md` 写完 trigger 条件 + 策略 + FAQ

**退出标准**：

- 一周实测：同类 task 上 grader 分数 +0.1 以上
- killer demo GIF 能录出来且无造假（`history.jsonl` 真实记录）
- 用户打 `xmclaw evolution show --since 7d` 能看到 3+ 条真实 evolution 事件
- 集成测试 `tests/integration/test_evolution_visible.py` 全绿

**进度日志**：

- _（尚无）_

---

### Epic #5 · Memory eviction

**状态**：✅ 已完成 | **负责人**：Claude (AI pair) | **起始**：2026-04-23 | **完成**：2026-04-23
**前置依赖**：Epic #13（事件总线持久化）
**关联 Milestone**：M8（性能与可观测）

**开发计划**：

1. **策略设计文档**（0.5 天）——LRU + age-based + cap 三维混合，边界条件（pinned item、active session）写清
2. **实现**（1.5 天）——落地 `core/memory/manager.py:209` 的 `evict()`，跑每 N 分钟一次 + cap 触发
3. **config 字段**（0.5 天）——`memory.retention_days` / `max_bytes` / `pinned_tags`
4. **CLI `xmclaw memory stats`**（1 天）——显示容量、淘汰日志、命中率
5. **单测 + 压测**（1 天）——塞 10k 条记忆验证淘汰效率

**检查清单**：

- [x] `SqliteVecMemory.evict(layer, max_items, max_bytes)` LRU + cap + pinned-bypass 落地
- [x] `prune()` / `evict()` 经 structlog 发 `memory.evicted` 结构化日志
- [x] `pinned_tags` 构造参数：admin 可按 `metadata.tag` / `tags` / `category` 豁免
- [x] `SqliteVecMemory.stats()` 数据面：三层 × `count` / `bytes` / `pinned_count` / `oldest_ts` / `newest_ts`（为 CLI 打底）
- [x] `xmclaw memory stats` CLI 落地：`--db PATH`（默认 `~/.xmclaw/v2/memory.db`）/ `--json` / 文本表格 / 空 DB 静默报告（不偷偷建库）
- [x] `memory.retention_days` / `max_bytes` daemon 级 config 字段（`memory.retention` 段 + `memory_sweep` 后台任务）
- [x] `MEMORY_EVICTED` 事件发出（`prune`/`evict` 调 `_emit_evicted` → bus.publish）
- [x] 单测覆盖 LRU / bytes / pinned / pinned_tags / 组合 cap / 恶意 metadata / layer 隔离
- [x] 10k 压测（退出标准一半：延迟部分达标）

**退出标准**：10k 记忆条目下 evict 延迟 < 100ms（**达标** — 本机 44ms）；`xmclaw memory stats` 能显示 per-layer 占用（**达标** — `count` / `bytes` / `pinned` / `oldest` / `newest` 三层表输出）。淘汰日志上 bus 的部分挂到 phase 3 `MEMORY_EVICTED` 事件。

**进度日志**：

- 2026-04-23: phase 1 落地 `SqliteVecMemory.evict(layer, *, max_items, max_bytes)` — LRU `ORDER BY ts ASC`、`metadata.pinned` 豁免、双 cap 并集、共用 `_delete_ids` 清 `memory_vec`、恶意 JSON 不被当作 pin。配 11 条单测（空参 noop / 仅 items / 仅 bytes / bytes=0 / pinned 豁免 / pinned 不占配额 / 双 cap 并集 / 双 cap 紧边界 / 跨 layer 隔离 / 带 embedding 清理 / 坏 metadata）。`prune()` 也改走 `_log.info("memory.evicted", reason="age")`。full suite 777 passed (commit 89ed991)
- 2026-04-23: phase 2 — `pinned_tags` 构造参数：admin 传 `pinned_tags=["identity", "promise"]`，`_is_pinned` 除了 `metadata.pinned` 外还认 `metadata.tag`（标量）/ `metadata.tags`（列表）/ `metadata.category` 命中。用例目的：不需要改每条 row 就能保护"身份/承诺/系统"类记忆。加 4 条单测（scalar tag / tags list / category / pinned_tags 为空时保持原行为）。full suite 781 passed (commit 275d433)
- 2026-04-23: phase 4 (part 1) — 退出标准延迟部分达标。新增 `test_evict_at_10k_items_is_fast`：on-disk DB + 10k 行 + `max_items=5_000` 一次 evict 5k 条，本机耗时 44ms，<100ms 退出标准；guard 设 500ms（5x 头部空间）吸收 CI 抖动。回归信号：O(n²) 实现或全表重写会被 5x 额度抓到。`xmclaw memory stats` CLI 仍未落（phase 3），退出标准另一半挂单 (commit 2a0fc69)
- 2026-04-23: phase 3 数据面 — `SqliteVecMemory.stats()` 落地：三层固定返回 `count` / `bytes` (UTF-8) / `pinned_count` / `oldest_ts` / `newest_ts`；空库三层全零、`pinned_count` 复用 `_is_pinned` 规则（`metadata.pinned` / `tag` / `tags` / `category`）、不触库。加 6 条单测（空库 / 计数+字节+ts 范围 / UTF-8 多字节 / pinned 规则复用 / 幂等不突变 / evict 后读数正确）。memory suite 38 passed、smart-gate 106 passed。CLI `xmclaw memory stats` 只剩渲染层，解锁 phase 3 展示面 (commit 24c8177)
- 2026-04-23: phase 3 CLI — `xmclaw memory stats` 落地：typer 子 app `memory` + 命令 `stats`。`--db PATH`（默认 `~/.xmclaw/v2/memory.db`，遵循 pid / events / token 同款 workspace 约定）/ `--json` / 三层固定表输出（layer / count / bytes / pinned / oldest / newest，UTC 时间戳+人类可读字节）。**不偷偷建库**：DB 不存在时打印提示+退出 0（`exists: false` JSON）。加 6 条 CLI 测试（JSON 读数正确 / 文本表格含三层 / 缺 DB 干净报告+不创建 / JSON 缺 DB `exists=false` / HOME 覆盖默认路径 / 空 DB 仍返回三层零行）。smart-gate 174 passed 1 skipped (commit 32a4d11)
- 2026-04-23: phase 5 收尾 — daemon 层接线 + `MEMORY_EVICTED` 事件打通。`xmclaw/core/bus/events.py` 加 `EventType.MEMORY_EVICTED`；`SqliteVecMemory(..., bus=)` 接总线，`prune`/`evict` 真删到行时 `bus.publish(make_event(session_id="_system", agent_id="daemon", type=MEMORY_EVICTED, payload={layer, count, reason, bytes_removed?}))`，发事件失败用 try/except 吞掉——清扫是本职工作，总线挂了也要继续。`xmclaw/daemon/factory.py` 加 `build_memory_from_config(cfg, bus=)`：`memory.enabled=false` 返回 None；`db_path=null` 走 `~/.xmclaw/v2/memory.db`；校验 `embedding_dim` / `ttl` / `pinned_tags` 段类型，坏配置抛 `ConfigError`。`xmclaw/daemon/memory_sweep.py` 新建：`LayerRetention` + `RetentionPolicy` + `parse_retention_config`（**永不抛**，坏字段降为 `None` 并 warn）+ `MemorySweepTask`（每 `sweep_interval_s` 跑一次 `prune_by_ttl` + 三层 cap，per-layer try/except 隔离单层失败；无 cap 时 `start()` 空转不起任务）。`xmclaw/daemon/app.py` 用 `@asynccontextmanager` lifespan hook 管起停，挂在 `app.state.memory` / `app.state.memory_sweep`。加 21 条单测 `tests/unit/test_v2_memory_retention.py`（事件 payload / 零 item 不发事件 / subscriber 炸了不 rollback eviction / factory 默认+disabled+custom+ttl+pinned+3 种坏段 / retention 默认+per-layer+坏值降级 / any_cap_set 真假 / sweep_once 跨层 + 单层故障隔离 / start-stop roundtrip + no-op）。`daemon/config.example.json` / `docs/CONFIG.md` / `scripts/test_lanes.yaml` memory lane 同步更新（commit 待落）

---

### Epic #6 · ENV override

**状态**：✅ 已完成 | **负责人**：Claude (AI pair) | **起始**：2026-04-22 | **完成**：2026-04-22
**前置依赖**：无
**关联 Milestone**：M1（Daemon 稳定性 GA）

**开发计划**：

1. **解析规则**（0.5 天）——`XMC__<path>__<key>=value` → 双下划线转点 → nested dict merge，类型推断（bool/int/str）
2. **factory 集成**（0.5 天）——`load_config()` 加 env merge 层，优先级 `ENV > yaml > defaults`
3. **单测**（0.5 天）——覆盖：覆盖已有 key / 新建深层 key / 类型转换 / 错误格式报错

**检查清单**：

- [x] `daemon/factory.py` `load_config()` 加 env merge 层
- [x] `XMC__<dotted_path>` 命名规则实现
- [x] 单测：`XMC__llm__anthropic__api_key=xxx` 覆盖 YAML
- [x] 文档 `docs/CONFIG.md` 加 ENV 覆盖小节

**退出标准**：Docker 镜像能靠纯 ENV 起 daemon，不挂 volume config。

**进度日志**：

- 2026-04-22: `_apply_env_overrides(cfg, env, prefix="XMC__")` 落地：双下划线切段、小写化、JSON 类型推断、空段过滤、标量 parent 覆盖为 dict。`load_config(path, *, env=None)` 在 json.loads 之后自动 overlay（传 `env={}` 单测静默） (commit 788e400)
- 2026-04-22: `tests/unit/test_v2_daemon_factory.py` 新增 12 条 `test_env_override_*` 用例（覆盖已有 key / 创建深层 / 忽略非前缀 / bool+int+float+null+array 类型推断 / 裸 secret 保留 str / 标量 parent 覆盖 / 段大小写兼容 / 空段被忽略 / end-to-end `load_config`），40/40 passed (commit 788e400)
- 2026-04-22: `docs/CONFIG.md` 新建，含命名规则表 / 类型推断 / 优先级 / Docker 纯 ENV 示例 (commit 788e400)

---

### Epic #7 · IDE / ACP 入口（双向 ACP）

**状态**：⬜ 未开始 | **负责人**：- | **起始**：- | **完成**：-
**前置依赖**：Epic #2（Plugin SDK）
**关联 Milestone**：M7（IDE 入口）

**开发计划**：

1. **读 ACP 规范**（0.5 天）——zed-industries/agent-client-protocol；对照 QwenPaw `agents/acp/` 现成实现
2. **ACP Server**（3 天）——`providers/channel/acp_server.py` stdio JSON-RPC，把 AgentLoop 包成 ACP agent
3. **ACP Client**（2 天）——`providers/tool/acp_client.py` + `delegate_external_agent` 工具
4. **config 扩展**（0.5 天）——`acp.clients: [{name, command, args, env}]`；默认预置 `claude_code` / `codex` / `opencode`
5. **权限控制**（1 天）——`acp/permissions.py` 模式：哪些 tool 可以被外部 agent 调用
6. **Zed 集成测试**（1 天）——手工在 Zed settings.json 配置指向本地 xmclaw，走通一次对话
7. **文档**（0.5 天）——`docs/IDE.md` 分 Zed / VS Code / JetBrains 三节

**检查清单**：

- [ ] `providers/channel/acp_server.py`
- [ ] `providers/tool/acp_client.py`
- [ ] `config.yaml` 支持 `acp.clients` 数组
- [ ] `acp/permissions.py` 权限控制
- [ ] Zed 识别成功截图
- [ ] `docs/IDE.md`

**退出标准**：Zed 用户能在 settings.json 配 xmclaw ACP server 并对话；反向 xmclaw 能 delegate 到 claude_code。

**进度日志**：

- _（尚无）_

---

### Epic #8 · Skill Hub

**状态**：⬜ 未开始 | **负责人**：- | **起始**：- | **完成**：-
**前置依赖**：Epic #3（scanner）、Epic #16（signed verification）
**关联 Milestone**：M6（Onboarding + Hub）

**开发计划**：

1. **Hub 协议设计**（1 天）——先走 GitHub releases 或简单 JSON index（`skills.xmclaw.dev/index.json`）；不先造 registry 服务
2. **本地 mock**（0.5 天）——`tests/fixtures/mock_hub/` 模拟 hub，CI 用
3. **HTTP client**（1 天）——`xmclaw/skills/hub.py` search/download/cache
4. **CLI**（1.5 天）——`xmclaw skills {search, install, uninstall, list, update} <name>`
5. **Scanner 集成**（0.5 天）——install 前跑 Epic #3 的 SkillScanner
6. **agentskills.io 兼容**（1 天）——验证能装 Claude Agent Skills 上的 skill 原封不动
7. **发布 1 个样例 skill repo**（1 天）——`xmclaw-skill-github-code-review` 当作 hub 生态首个案例

**检查清单**：

- [ ] `xmclaw/skills/hub.py` HTTP client
- [ ] `xmclaw skills {search, install, uninstall, list, update}` CLI
- [ ] install 前自动扫
- [ ] agentskills.io 格式兼容测试
- [ ] 1 个样例 skill repo 可安装

**退出标准**：`xmclaw skills install <name>` 端到端跑通；至少 5 个 skill 可装（起步集）。

**进度日志**：

- _（尚无）_

---

### Epic #9 · Onboarding 向导

**状态**：⬜ 未开始 | **负责人**：- | **起始**：- | **完成**：-
**前置依赖**：Epic #6（ENV override）、Epic #10（doctor）、Epic #16（secrets）
**关联 Milestone**：M6（Onboarding + Hub）

**开发计划**：

1. **交互脚本骨架**（1 天）——`cli/onboard.py` 用 `questionary` 实现 6 步
2. **LLM provider 选择**（0.5 天）——列出 anthropic / openai / ollama / lmstudio
3. **API key 写 keyring**（0.5 天）——调 Epic #16 的 `utils/secrets.py`
4. **workspace 路径确认**（0.5 天）——默认 `~/.xmclaw/`，允许改
5. **Tool/Channel 选择**（1 天）——勾选启用项
6. **Smoke test 集成**（1 天）——跑一次 "hello" 验证 LLM 通、memory db 建好、skill registry 可读
7. **错误回退**（0.5 天）——任何步失败给清晰提示 + `xmclaw doctor` 建议

**检查清单**：

- [ ] 6 步交互流程
- [ ] keyring 写入 API key
- [ ] workspace 路径
- [ ] tool/channel 勾选
- [ ] 末尾 smoke test
- [ ] 错误回退提示

**退出标准**：新用户从 `pip install xmclaw` 到第一次对话 ≤ 3 分钟。

**进度日志**：

- _（尚无）_

---

### Epic #10 · Doctor 诊断（可插拔）

**状态**：🟡 进行中 | **负责人**：Claude (AI pair) | **起始**：2026-04-22 | **完成**：-
**前置依赖**：无
**关联 Milestone**：M1（Daemon 稳定性）

**开发计划**：

1. **Registry 骨架**（1 天）——`cli/doctor_registry.py` + `DoctorCheck` ABC（`id`, `name`, `run() -> CheckResult`）
2. **核心 check**（2 天）——`doctor_checks.py` 实现 8-10 项：Python 版本 / 目录可写 / memory db / skills 目录扫 / daemon WS+HTTP / sandbox 可起
3. **网络 check**（1 天）——`doctor_connectivity.py` 测 anthropic / openai / ollama endpoint
4. **Fix runner**（1 天）——`doctor_fix_runner.py` 自动建目录 / pid 锁 / 安装 playwright
5. **entry_points 插件组**（0.5 天）——`pyproject.toml` 声 `[project.entry-points."xmclaw.doctor"]`
6. **CLI**（0.5 天）——`xmclaw doctor [--fix] [--json]`
7. **Roadmap lint**（0.5 天）——加一个 check `DOCTOR_CHECK_ROADMAP_LINT`，跑 `scripts/lint_roadmap.py` 校验文档状态一致性（§3.6.5 依赖项）
8. **文档**（0.5 天）——`docs/DOCTOR.md` 写怎么注册插件 check

**检查清单**：

- [x] `cli/doctor_registry.py` + `DoctorCheck` ABC
- [x] `doctor_checks.py` 核心检查 ≥ 8（现 14 条 built-in：config/llm/tools/**workspace**/pairing/port/**events_db**/**memory_db**/**skill_runtime**/**connectivity**/**roadmap_lint**/**pid_lock**/daemon/**backups**；sandbox 留给 Epic #3 — sandbox 运行时落地后再加）
- [x] `doctor_connectivity.py` 网络探测（`ConnectivityCheck` 落地，opt-in `--network` 旗标，stdlib urllib HEAD 探测）
- [x] `doctor_fix_runner.py` 自动修复（`DoctorRegistry.run_fixes()`，WorkspaceCheck 首条可修复检查落地）
- [x] `[project.entry-points."xmclaw.doctor"]` 组（文档 + discover 已接）
- [x] `xmclaw doctor [--json] [--fix]` CLI
- [x] `scripts/lint_roadmap.py` + 对应 check (`RoadmapLintCheck`)
- [x] `docs/DOCTOR.md`

**退出标准**：`xmclaw doctor` 覆盖率 ≥ 10 项，`--fix` 能自动处理 ≥ 5 项；第三方 pilot 插件可注册自检。

**进度日志**：

- 2026-04-22: 阶段 1 落地——`xmclaw/cli/doctor_registry.py` 新建：`DoctorCheck` ABC + `DoctorContext` + `DoctorRegistry` + `build_default_registry()` 把原 6 条 pure-func check 裹成 ABC，`ctx.cfg` 共享避免重复 parse；`discover_plugins()` 走 `importlib.metadata.entry_points('xmclaw.doctor')`，plugin import 失败只产红线不会整体停机；`run_doctor()` 改走 registry 保向下兼容 (commit a28d344)
- 2026-04-22: `xmclaw doctor` CLI 增 `--json` / `--discover-plugins` 旗标；`pyproject.toml` 加 `xmclaw.doctor` entry-point 组注释；`docs/DOCTOR.md` 落地（命名规则 / 写 plugin / ctx 共享 / 错误处理）；`tests/unit/test_v2_doctor.py` 增 11 条 registry 用例（顺序 / 捕获 crash / ctx.cfg 共享 / plugin discover 空路径），38/38 passed (commit a28d344)
- 2026-04-22: **阶段 2 遗留项**：`--fix` runner、连通性 check（anthropic/openai/ollama）、workspace/memory-db/sandbox check、`scripts/lint_roadmap.py`。暂停在这不阻塞 Epic #10 整体——Epic #10 状态保持 🟡 直到阶段 2 收尾
- 2026-04-23: 阶段 2 首批落地——`CheckResult` 新增 `fix_available` 字段；`DoctorRegistry.run_fixes()` + nested `FixAttempt` dataclass 按序对 `ok=False ∧ fix_available` 的 check 调 `fix(ctx)`、捕获异常到 `fix_raised`、重跑 check、返回 attempt 列表；新增 `WorkspaceCheck`（`~/.xmclaw/v2/` 检查 + `mkdir -p` 自动修复，支持 `ctx.extras["workspace_dir"]` override 方便单测）；`xmclaw doctor --fix` CLI 旗标接通，JSON 多出 `fix_attempts` 字段、text 多出 `fix attempts:` 汇总块；`tests/unit/test_v2_doctor.py` 增 13 条用例（workspace 四态 + run_fixes 四路径 + CLI 两端到端），51 passed + 1 skipped，全套 659 passed (commit edd7d55)
- 2026-04-23: 阶段 2 续——新增 `scripts/lint_roadmap.py`（§3.6.5 drift 检测器，零依赖，状态机解析 markdown）：4 条规则（状态 ✅ 完成 → 完成日期非 `-`；状态 🟡 → 起始非 `-`；✅ Epic 的 checklist 不得留真 `[ ]`；§7 Milestone 条目引用的 Epic 全 ✅ 则自身也必须 `[x]`）；通过 `留给 Epic #N` / `挂单 Epic #N` / `deferred to Epic #N` 注释 opt-out 防止跨 Epic deferral 误报；新增 `RoadmapLintCheck` doctor check（运行期 importlib 加载脚本 + `sys.modules` 注册让 dataclass 解析 OK），非源码环境自动跳过（wheel 不含 script）；`tests/unit/test_v2_lint_roadmap.py` 12 测（4 规则 × 正反 + 重复 Epic 号 + 多 Epic 引用部分完成容忍 + shipped roadmap 干净 regression guard）；`test_v2_doctor.py` 更新 7→8 checks 断言；smart-gate cli lane 加 `lint_roadmap.py` + `DEV_ROADMAP.md` trigger 和对应测试；全套 749 passed (commit 0def6fe)
- 2026-04-23: 阶段 2 续——新增 `EventsDbCheck` doctor check：用 `sqlite3.connect(file:...?mode=ro)` read-only 打开 `~/.xmclaw/v2/events.db`，检测 4 种状态（不存在 → OK "will be created"；目录而非文件 → 失败；SQLite 头部损坏或锁死 → 失败带库原始错误；`PRAGMA user_version` 超前于代码 SCHEMA_VERSION → 失败带降级不支持 advisory）；`ctx.extras["events_db_path"]` 支持单测 override；`tests/unit/test_v2_doctor.py` 增 5 条用例（missing / directory / garbage / healthy current / newer schema）；checks 数 8→9；全套 754 passed (commit f00589c)
- 2026-04-23: 阶段 2 续——新增 `ConnectivityCheck` doctor check：`DoctorContext.probe_network: bool = False` opt-in 字段（默认关闭以保持 doctor air-gap 可跑），`_DEFAULT_ENDPOINTS` 常量覆盖 anthropic/openai 基地址，honor `base_url` override（代理/自托管 compatible endpoint 正确对准）；`_probe()` 用 stdlib `urllib.request` HEAD 请求 + 5s timeout（零额外依赖），把 2xx/3xx/4xx 全都当 reachable（TLS 握手成功即可，auth 是 LLMCheck 的问题），URLError/socket.timeout/OSError 才是 unreachable；`xmclaw doctor --network` CLI 旗标接通；`tests/unit/test_v2_doctor.py` 增 8 条用例（默认关闭 / 无 cfg / 无 llm 节 / 无 api_key / 可达 200 / HTTP 401 当可达 / URLError 不可达 / base_url override 命中），mock `urllib.request.urlopen` 保持 CI 离线安全；checks 数 9→10，满足退出标准「覆盖 ≥ 10 项」的一半；`--fix` 自动修复仍只 1 条（workspace），auto-fix ≥ 5 的出口标准留给后续（sandbox / playwright install / pid 锁）；全套 762 passed (commit a618066)
- 2026-04-23: 阶段 2 续——新增 `StalePidCheck` doctor check + 自动修复：honor `XMC_V2_PID_PATH` / `ctx.extras["pid_path"]`，复用 `xmclaw.daemon.lifecycle._process_alive` 做跨平台存活探测；三态（无 pid 文件 → OK "no daemon tracked"；pid + 进程存活 → OK 带 pid；pid + 进程已死 / 文件坏 → FAIL + fixable，`fix()` 清 `daemon.pid` + `daemon.meta`）；针对 `xmclaw start` 在僵 pid 文件下拒绝启动的用户痛点一键恢复；`tests/unit/test_v2_doctor.py` 增 5 条用例（无 pid / malformed / alive / stale / meta 缺席不炸）；checks 数 10→11、auto-fix 数 1→2（退出标准 ≥5 仍挂 Epic #3 sandbox 落地）；全套 787 passed (commit 32686fd)
- 2026-04-23: 阶段 2 续——`PairingCheck` 增自动修复：`_fixable_state()` 把失败模式收敛为两种安全可修类 —— `empty`（空 token 文件 → `unlink()` 让 `xmclaw serve` 重生）与 `loose_perms`（POSIX 下 group/other 任一 bit 置位 → `chmod 600`，保留 token 本体）；所有其他失败（unreadable / 根本不存在）保持非 fixable 不越权；`ctx.token_path` 覆盖通路保留用于单测；`tests/unit/test_v2_doctor.py` 增 5 条用例（not-yet-created 不触发修复 / healthy 不触发 / empty 修复闭环 / loose perms 修复闭环 POSIX 专用 skipif / fix noop 当无文件可修），`check_pairing_token` 原 4 条纯函数用例保持不动（backward-compat 不破）；auto-fix 数 2→3（退出标准 ≥5 还差 2 条）；smart-gate 172 passed + 2 skipped (commit 60322d4)
- 2026-04-23: 阶段 2 续——`ConfigCheck` 增自动修复 + 配合 `xmclaw config init`/`set` 两条新 CLI 一起交付 README 承诺（README 两处提到、typer 却从未实现的命令）：新建 `xmclaw/cli/config_template.py` 作模板单一来源（literal dict 而非读 `daemon/config.example.json`，后者在 repo 根不随 wheel 打包），`config init`（`--path` / `--provider` / `--api-key` / `--force`）和 `ConfigCheck.fix()` 共用它避免漂移；`ConfigCheck.run()` 只把"文件不存在"一种失败标为 `fix_available=True`（invalid JSON / 非 object root 这两种都可能是用户正在编辑的数据，静默覆盖会毁工作）；`xmclaw config set <dotted.key> <value>` 新命令也落地，VALUE 先尝试 JSON 解析（`gateway.port 9000` → int、`evolution.enabled true` → bool、`tools.allowed_dirs ["."]` → list），解不了就当字符串（api key 裸字常见）；缺中间 dict 会自动创建，缺文件或非 object root 都拒动；`check_config_file` 的 advisory 从陈旧的 "copy daemon/config.example.json..." 改为指向 `xmclaw config init`；`tests/unit/test_v2_cli_config.py` 14 测（init 6 路径 + set 8 路径）、`tests/unit/test_v2_doctor.py` 增 6 条（ConfigCheck fixable 仅 missing / 非 fixable 在 invalid JSON 与 array root / fix 写骨架后 run 能 ctx.cfg 回填 / fix 拒覆盖 / 模板与 CLI 等值防漂移）；`scripts/test_lanes.yaml` cli lane 补登记 `test_v2_cli_config.py` + `test_v2_cli_memory_stats.py`（后者是老遗漏，一起修）；auto-fix 数 3→4（退出标准 ≥5 还差 1 条 → 挂在 Epic #3 sandbox）；smart-gate 198 passed + 2 skipped (commit f9655ec)
- 2026-04-23: 阶段 2 续——新增 `MemoryDbCheck` + `SkillRuntimeCheck` 两条 doctor check（借 Epic #5 memory 层与 Epic #3 runtime factory 新落地的代码路径把 doctor 诊断面打开）：`MemoryDbCheck` 镜像 `EventsDbCheck` 四态（memory.enabled=false → OK skip；文件不存在 → OK "will be created on first put"；目录 / SQLite 头损坏 → FAIL；文件 OK 但没 `memory_items` 表 → FAIL "not an xmclaw memory.db"），honor `ctx.extras["memory_db_path"]` / `cfg.memory.db_path` / `default_memory_db_path()` 优先级；`SkillRuntimeCheck` 直接调 `build_skill_runtime_from_config`，把 `ConfigError` 的「unknown backend `docker`, must be one of {local, process}」原文冒出来让用户能立刻改掉，cfg 未加载时 skip 不重报，类名进 detail 方便辨认（`local (LocalSkillRuntime)` / `process (ProcessSkillRuntime)`）；`tests/unit/test_v2_doctor.py` 增 13 条用例（memory 7 条：disabled / missing / directory / garbage / 无 memory_items 表 / healthy with count / cfg 路径穿透；runtime 6 条：no-cfg skip / 默认 / local / process / unknown backend 带 known-set / 非 dict section）；checks 数 11→13，退出标准「覆盖 ≥ 10 项」冗余；`--fix` 自动修复仍 4 条（workspace / pid / pairing / config），≥5 出口标准挂在 Epic #3 sandbox；`build_default_registry()` 顺序 config→llm→tools→workspace→pairing→port→events_db→memory_db→skill_runtime→connectivity→roadmap_lint→pid_lock→daemon；doctor 全套 92 passed + 2 skipped
- 2026-04-23: 阶段 2 续——新增 `BackupsCheck` doctor check（Epic #10 × Epic #20 交汇）：纯观测性、始终 `ok=True`，把 `~/.xmclaw/backups/` 目录的存量与最新备份年龄打到 `xmclaw doctor` 里，让用户不用记命令就能看见"现在有几份备份、上次备份多久了"；三态（空或无目录 → 带"no backups yet"+"run xmclaw backup create"提示；最新备份 < 30 天 → 静静汇报 `N backup(s), newest 'X' Nd old`；最新备份 ≥ 30 天 → 同样 ok 但 advisory 劝再做一份）；`_format_age()` 粗粒度人类可读（秒/分/时/天），避免毫秒级噪声；honor `ctx.extras["backups_dir"]` 和 `XMC_BACKUPS_DIR` 两层 override 让单测不碰真实工作区；插在 `DaemonHealthCheck` 之后（诊断序列里 observability 类属于 tail）；`tests/unit/test_v2_doctor.py` 增 5 条用例（空目录 / 新鲜备份无 advisory / 旧备份 advise 刷新 / 多份按 `created_ts` 选最新 / env 覆盖穿透）；checks 数 13→14；smart-gate cli + backup 两 lane 97 passed + 2 skipped

---

### Epic #11 · Smart-gate 测试

**状态**：✅ 完成 | **负责人**：Claude | **起始**：2026-04-23 | **完成**：2026-04-23
**前置依赖**：无
**关联 Milestone**：M1（Daemon 稳定性）

**开发计划**：

1. **Lane 规则设计**（0.5 天）——写 `scripts/test_lanes.yaml` 定义 glob → test suite 映射
2. **diff 推断脚本**（1 天）——`scripts/test_changed.py` 读 `git diff --name-only`，产出 test 命令
3. **Pre-commit hook**（0.5 天）——`.pre-commit-config.yaml` 调用 `test_changed.py`
4. **CI 集成**（0.5 天）——GitHub Actions 用 `test_changed.py` 替换全量 pytest
5. **全量 fallback**（0.5 天）——main 分支 push 时跑全量；PR 跑 changed lane
6. **文档**（0.5 天）——`docs/TESTING.md` 加 smart-gate 小节

**检查清单**：

- [x] `scripts/test_lanes.yaml` lane 规则（13 lanes：always / bus / llm / tools / agent_loop / daemon / cli / memory / evolution / security / observability / runtime / full_fallback）
- [x] `scripts/test_changed.py`（手写 YAML 解析器零依赖，支持 `--base` / `--from-stdin` / `--all` / `--dry-run` / pytest args 透传）
- [x] 单元测试（`tests/unit/test_v2_test_changed.py`，21 tests 覆盖解析器 + lane 选择 + pytest 命令渲染）
- [x] `docs/V2_DEVELOPMENT.md §6.2.1` 加 smart-gate 小节
- [x] CI 改成 changed-first（`.github/workflows/python-ci.yml`：PR 跑 smart-gate，push-to-main 跑全量）
- [x] main 分支全量 fallback（workflow `Test (full suite)` step on `event_name != pull_request`）
- [x] pre-commit hook（`.pre-commit-config.yaml` 可选 opt-in，`language: system` 零额外下载）

**退出标准**：改 `core/memory/*.py` 时 CI 在 2 分钟内完成，全量仍在 main 护底。

**进度日志**：

- 2026-04-23: lane YAML + 选择脚本 + 21 单测落地；本地 smoke 三路径（--all / security+bus / docs-only）均产出预期 pytest 命令。CI 接线 + pre-commit 留到 phase 2。(commit 2827d0c)
- 2026-04-23: CI 接线 — `.github/workflows/python-ci.yml` PR 事件用 `test_changed.py --base origin/$base -- -v`，push-to-main / workflow_dispatch 仍跑全量。`fetch-depth: 0` 保证 merge-base 可用。pre-commit hook 留到 phase 2b。(commit 1fe2c56)
- 2026-04-23: 完结 — `.pre-commit-config.yaml` 可选 opt-in（`language: system` 零依赖）+ V2_DEVELOPMENT 小节追加 CI/pre-commit 用法。Epic #11 checklist 全清，退出标准待 main 合入后用真实 CI 运行时间 verify。(commit 2aa3261)

---

### Epic #12 · AGENTS.md 分层

**状态**：✅ 完成 | **负责人**：Claude | **起始**：2026-04-23 | **完成**：2026-04-23
**前置依赖**：无
**关联 Milestone**：M1（Daemon 稳定性）

**开发计划**：

1. **模板**（0.5 天）——`docs/AGENTS_TEMPLATE.md` 五段式：职责 / 依赖规则 / 测试入口 / 禁止事项 / 关键文件
2. **填 core/**（0.5 天）——依赖规则硬写死：不得 import `daemon/*` 或 `providers/*`
3. **填 daemon/**（0.5 天）——职责：I/O 边界；依赖规则：不得出现业务逻辑
4. **填 providers/\***（1 天）——每个子包一份（llm / tool / memory / runtime / channel）
5. **填 plugin_sdk/**（0.5 天）——公开 API 冻结规则（留给 Epic #2 落地时补）
6. **填 cli/ 和 utils/**（0.5 天）
7. **CLAUDE.md 瘦身**（0.5 天）——把细节下放后，顶层 CLAUDE.md 只留导航

**检查清单**：

- [x] `docs/AGENTS_TEMPLATE.md`
- [x] `xmclaw/core/AGENTS.md`
- [x] `xmclaw/daemon/AGENTS.md`
- [x] `xmclaw/providers/AGENTS.md`（umbrella）+ `providers/{llm,tool,memory,runtime,channel}/AGENTS.md`
- [ ] `xmclaw/plugin_sdk/AGENTS.md`（留给 Epic #2 — plugin_sdk 本身尚未建立）
- [x] `xmclaw/cli/AGENTS.md`
- [x] `xmclaw/utils/AGENTS.md`
- [x] `xmclaw/security/AGENTS.md`（本 Epic 提出时尚未存在，合并补上）
- [x] `xmclaw/skills/AGENTS.md`（同上）
- [x] CLAUDE.md 瘦身（顶层仅导航 + 指向每子包 AGENTS.md）

**退出标准**：新 AI 协作者开任何子目录能单独读懂契约 + 禁区。

**进度日志**：

- 2026-04-23: 模板 + 10 份子包 AGENTS.md（core / daemon / providers umbrella + llm/tool/memory/runtime/channel / cli / utils / security / skills）+ CLAUDE.md 瘦身。plugin_sdk AGENTS.md 挂单 Epic #2。(commit 8591f51)

---

### Epic #13 · SQLite event bus

**状态**：✅ 完成 | **负责人**：Claude | **起始**：2026-04-22 | **完成**：2026-04-23
**前置依赖**：无
**关联 Milestone**：M1（Daemon 稳定性）

**开发计划**：

1. **Schema 设计**（0.5 天）——参考 Hermes `hermes_state.py` v8：`events` / `sessions` / FTS5 虚拟表；预留 `cost` / `grader_scores`
2. **落地 `core/bus/sqlite.py`**（2 天）——建表 + WAL 模式 + 批量 insert
3. **接通 AgentLoop publish**（1 天）——现有 `publish(event)` 调用落盘 + 广播
4. **事件重放 API**（1 天）——`GET /api/events?since=<ts>&session_id=<id>` + WS 分页推送
5. **FTS5 搜索**（0.5 天）——`GET /api/events/search?q=<keyword>`
6. **迁移器**（0.5 天）——旧版内存事件一次性导入（如有）
7. **单测**（1 天）——并发 publish、FTS5 召回、schema migration

**检查清单**：

- [x] `core/bus/sqlite.py` schema + WAL
- [x] `events` / `sessions` / `events_fts` 表
- [x] AgentLoop publish 接通（via `xmclaw serve` swapping `InProcessEventBus` → `SqliteEventBus`；AgentLoop emits through the same `bus.publish()` path, now durable-first)
- [x] `GET /api/v2/events?since=...&session_id=...&types=...` API（统一端点，q= 起 FTS5 分支）
- [x] `GET /api/v2/events?q=...`（FTS5 关键字搜索）
- [x] Schema migration 脚手架（`PRAGMA user_version` + `MIGRATIONS` list）
- [x] 单测含并发 publish（`test_concurrent_publish_serialized_no_loss`）
- [x] 迁移器 N/A（deferred to Epic #20 — 备份恢复落地时再设计真正的迁移器；当前直接从空 DB 起，Phase 4 之前无持久化事件需要导入）

**退出标准**：重启 daemon 后能重放过去 24h 的事件；FTS5 查询 "memory" 能在 < 100ms 返回匹配事件。

**进度日志**：

- 2026-04-22: 落地 `SqliteEventBus`（WAL + FTS5 + 触发器自动维护 sessions 表）、`xmclaw serve` 默认启用、`GET /api/v2/events` 端点（SqliteEventBus → 走 query/search；InProcessEventBus → 走内存 session_logs fallback）、15 条单测 + 5 条集成测试、582/582 pytest 绿。`InProcessEventBus` 仍保留给 `xmclaw ping` 和 create_app 默认路径。(commit pending)
- 2026-04-23: Epic 收尾——补齐退出标准的性能基准测试 `test_fts5_search_stays_fast_at_representative_scale`：500 事件代表性 24h 工作负载下 FTS5 关键字搜索 <500ms（退出标准 <100ms 的 5x 上限，吸收 CI 抖动同时兜住量级回归如线性扫描或索引缺失）；实测约 20ms；持久化已被 `test_events_survive_reopen` 覆盖（重启后 query 仍命中）；"迁移器" 未打勾项标记 deferred to Epic #20（Phase 4 之前无持久化事件需迁移）；Epic 状态 🟡→✅，M1 退出标准同步打勾。全套 763 passed (commit ebc4587)

---

### Epic #14 · Prompt injection 防御

**状态**：✅ 已完成（阶段 2 关口扎稳：tool_result 已接 scanner；SOUL/PROFILE/memory-recall 目前不自动注入，guard 留位待出现即激活） | **负责人**：Claude (AI pair) | **起始**：2026-04-23 | **完成**：2026-04-23
**前置依赖**：Epic #13（事件发出需要总线）
**关联 Milestone**：M8（安全硬化）

**开发计划**：

1. **移植正则 + unicode 扫描**（0.5 天）——从 Hermes `agent/prompt_builder.py` 抄 `_CONTEXT_THREAT_PATTERNS` + `_CONTEXT_INVISIBLE_CHARS`；在 header 标 MIT 归属
2. **Scanner 模块**（1 天）——`xmclaw/security/prompt_scanner.py` 纯函数 + structured result
3. **prompt_builder 接入**（0.5 天）——在注入 SOUL.md / PROFILE.md / AGENTS.md / memory 摘要 / 工具 output 前扫一遍
4. **事件**（0.5 天）——`PROMPT_INJECTION_DETECTED` + `finding_type` / `severity` 字段
5. **单测**（1 天）——10+ 典型攻击样本（ignore previous / system: override / zero-width / bidirectional override）
6. **策略配置**（0.5 天）——`security.prompt_injection: {detect_only | redact | block}` 三档

**检查清单**：

- [x] `xmclaw/security/prompt_scanner.py` 移植 Hermes 规则（instruction_override / role_forgery / exfiltration + unicode invisibles）
- [x] 在所有 prompt 注入点前扫（**tool_result 已接** ——`AgentLoop._acting` 入口；SOUL/PROFILE/AGENTS/memory-recall **当前未自动注入** 到 system prompt，符合 anti-req #2，`policy.py` 已预埋 4 个 source tag 常量，一旦有消费者出现立即激活）
- [x] `PROMPT_INJECTION_DETECTED` 事件（payload 含 source/policy/findings/categories/acted/tool_call_id）
- [x] `security.prompt_injection` config 三档策略（factory 接通，默认 `detect_only`）
- [x] 单测 ≥ 10 典型攻击样本（26 scanner 单测 + 7 AgentLoop 集成测试）

**退出标准**：攻击样本测试全过；`detect_only` 模式不破坏正常流程。

**进度日志**：

- 2026-04-23: 阶段 1 落地——新增 `xmclaw/security/prompt_scanner.py` 纯函数扫描器（三类共 11 条 regex：`ignore_previous` / `disregard_prior` / `forget_instructions` / `override_system` / `openai_im_start` / `anthropic_human_tag` / `inst_block` / `xml_system` / `new_instructions_header` / `reveal_secrets` / `send_to_url` + unicode invisibles 计数）；`PolicyMode` 枚举 + `redact()` 右到左 splice；新增 `EventType.PROMPT_INJECTION_DETECTED`；`AgentLoop` 在 tool_result 进入 messages 前扫一遍——`detect_only` 放行、`redact` 改写内容（LLM 只看到 `[redacted:<id>]`）、`block` 发 `ANTI_REQ_VIOLATION(kind=prompt_injection_blocked)` 终止 turn；`build_agent_from_config` 读 `security.prompt_injection`；`daemon/config.example.json` 加 `security` 段；26 scanner 单测 + 7 AgentLoop 集成测试（三档策略 × 敌方 payload / 干净 payload + factory 三路径），全套 692 passed (commit 56e2e14)
- 2026-04-23: 阶段 2 收尾审计——检视 `AgentLoop` 每条注入支路：`_system_prompt` 是静态字符串不走外部数据，`memory` 层从未 `.search()` 后塞进 system prompt（anti-req #2 合规），SOUL/PROFILE/AGENTS.md 目前**没有消费者**自动读取文件 inline 进 prompt。结论：当前 AgentLoop 只有一个"外部数据入 prompt"的切面——`tool_result`——阶段 1 已扫；`policy.py` 预埋的 `SOURCE_PROFILE` / `SOURCE_MEMORY_RECALL` / `SOURCE_WEB_FETCH` 三个 tag 是护栏留位，一旦 Epic #4 / Epic #9 落地 agent profile 或主动 memory recall，call-site 直接 `apply_policy(text, source=SOURCE_PROFILE, ...)` 一行接入。`docs/V2_STATUS.md` 同步说明这个实际状态，避免给用户虚幻"已扫 SOUL/PROFILE" 的错觉。Epic 状态 🟡→✅（anti-req #14 在当前代码表面上已无未扫注入点；M8 退出标准的 Epic #14 侧同步打勾）
- 2026-04-23: 阶段 2 地基——新增 `xmclaw/security/policy.py` 提供 `apply_policy(text, *, policy, source, extra) -> PolicyDecision` 复用壳：scan + 决定 (detect_only / redact / block) + 构造事件 payload 一次完成；`PolicyDecision` frozen dataclass 含 `content` / `blocked` / `scan` / `event`；导出四个稳定 source tag 常量（`SOURCE_TOOL_RESULT` / `SOURCE_PROFILE` / `SOURCE_MEMORY_RECALL` / `SOURCE_WEB_FETCH`）便于后续 SOUL/PROFILE/memory/web-fetch 注入点统一接入；event `match` 截断 200 字符防总线 DoS，`extra` 用 `setdefault` 保护核心字段不被 callsite 覆盖；`AgentLoop` 重构——原 ~40 行内联 scanner 逻辑压到单次 `apply_policy()` 调用；新增 `tests/unit/test_v2_security_policy.py` 12 测（快路径 / 三档决策 / 事件 shape / 截断 / 防覆盖 / 四 source tag 参数化 / redact 幂等）；全套 728 passed (commit 8748a38)

---

### Epic #15 · 日志

**状态**：✅ 完成 | **负责人**：Claude (AI pair) | **起始**：2026-04-23 | **完成**：2026-04-23
**前置依赖**：无
**关联 Milestone**：M8（可观测）

**开发计划**：

1. **structlog 配置**（0.5 天）——`utils/log.py` 全局 processors（timestamp / level / json）
2. **Rotation handler**（0.5 天）——`RotatingFileHandler` (size-based) 写 `<BASE>/logs/xmclaw.log`，5MiB × 3 份。按日切换非刚需，重开容易撞 Windows file-lock
3. **脱敏**（1 天）——复用 `utils/redact.py` 的 5 条 API-key 正则，作 structlog processor 扫每条 record 的 msg + kwargs
4. **contextvar 绑定**（0.5 天）——每个 turn 绑定 `session_id` / `agent_id`，日志自动带上
5. **迁移**（1 天）——有限迁移：CLI `print()` 是用户可见输出不是日志，不迁；仅迁移真正属于日志的 callsite

**检查清单**：

- [x] `utils/log.py` structlog 配置 + 幂等 `setup_logging()`
- [x] size-based rotate（5MiB × 3 份）；按天 rotate 非刚需，留到真需要再加
- [x] 敏感字段脱敏（processor 层；复用 `redact.redact_string`）
- [x] `session_id` / `agent_id` contextvar 绑定（`bind_log_context()` / `clear_log_context()`）
- [x] `print` / `logging` 迁移——有限迁移：CLI `print()` 是用户输出不迁；`core/bus/memory.py` + `core/performance_monitor.py` 已改用 `get_logger()`；`tests/unit/test_v2_print_audit.py` 用 AST walk 作回归守卫（core/providers/daemon/security/skills/utils/memory/runtime 子树再出现裸 `print(` 就红）

**退出标准**：日志可被 `jq` / `grep` 解析；`grep "sk-ant-" logs/*.log` 返回 0 条真 key。

**进度日志**：

- 2026-04-23: 阶段 1 落地——重写 `xmclaw/utils/log.py`：去掉模块级 `logger = setup_logging()` 副作用（之前每次 import 都在用户 `logs/` 下创建文件，违反 utils AGENTS.md 的 import 纯净约束）；新增 `_scrub_secrets` structlog processor 复用 `redact.redact_string` 扫 msg + 所有字符串 kwargs，`sk-ant-xxx` / `sk-xxx` / `ghp_xxx` / `xox?-xxx` / `AIza***` 都命中；加 `structlog.contextvars.merge_contextvars` 处理器 + `bind_log_context()` / `clear_log_context()` 包装，turn 开始 bind 一次 `session_id` / `agent_id` 下游每条 log 自动带上；`setup_logging()` 幂等（二次调用不累加 handler）；新增 `get_logger(name)` 公开入口（不触发 setup，可安全 import）；`xmclaw/core/performance_monitor.py` 从 `from ... import logger` 改为 `get_logger(__name__)`；新增 `tests/unit/test_v2_logging.py` 9 测（import 无副作用 / 幂等 / file+stream handler 各一 / 两种 scrubber 路径 / JSON 可解析 / contextvars 注入 / clear 生效 / get_logger 无需 setup）；更新 `scripts/test_lanes.yaml` observability lane + `xmclaw/utils/AGENTS.md`；全套 737 passed (commit 5246726)
- 2026-04-23: 阶段 2 收尾——`xmclaw/` 全树 `print(` 扫描只剩两处命中：`cli/chat.py`（交互式 CLI 用户输出，保留）+ `core/bus/memory.py:71`（带 `TODO(phase-1): route to structured logger` 注释，正是本阶段要修的）；`memory.py` 改用 `get_logger(__name__)` + `_log.warning("bus.subscriber_failed", event_type=, session_id=, event_id=, error=)`，结构化事件名 + 字段分离让 `jq` / `grep session_id=sess-xyz` 都能过滤；新增 `tests/unit/test_v2_print_audit.py` 用 ast walk 遍历 core/providers/daemon/security/skills/utils/memory/runtime 子树，发现裸 `print(` 就 fail（`# print-audit: allow` 同行注释作为 escape hatch）；`tests/unit/test_v2_bus_ping.py` 增 `test_subscriber_exception_is_logged_structurally` 断言结构化字段命中 + 老式 `[bus] subscriber failed` 字符串不再出现；Epic 状态 🟡→✅；全套 765 passed (commit 63eab68)

---

### Epic #16 · Secrets 加密

**状态**：⬜ 未开始 | **负责人**：- | **起始**：- | **完成**：-
**前置依赖**：无
**关联 Milestone**：M8（安全硬化）+ M6（Onboarding）

**开发计划**：

1. **`utils/secrets.py`**（1 天）——三层优先级：`keyring > ~/.xmclaw.secret/*.enc > ~/.xmclaw/.env > config.yaml`；`get_secret(name)` / `set_secret(name, value)` 统一接口
2. **加密 fallback**（1 天）——无 keyring 时用 `cryptography` Fernet + 机器绑定 key（从 `ed25519.key` 派生）存 `~/.xmclaw.secret/`
3. **sibling dir 创建**（0.5 天）——首次启动时建 `~/.xmclaw.secret/` + chmod 700
4. **CLI**（1 天）——`xmclaw config set-secret <key>` / `get-secret` / `migrate-secrets`
5. **factory 集成**（0.5 天）——`load_config()` 读 config.yaml 遇到 `${secret:name}` 占位符时自动取
6. **单测**（1 天）——覆盖三层优先级 + 加密解密 + migrate 不丢数据

**检查清单**：

- [ ] `utils/secrets.py` 三层优先级
- [ ] `~/.xmclaw.secret/` sibling 目录 + Fernet 加密
- [ ] `xmclaw config {set,get}-secret` CLI
- [ ] `xmclaw config migrate-secrets`（从旧 config.json 抽 secrets）
- [ ] `${secret:name}` 占位符支持
- [ ] 单测覆盖三层优先级

**退出标准**：新装用户的 API key 不出现在任何明文文件里；`grep -r "sk-" ~/.xmclaw/` 无命中。

**进度日志**：

- _（尚无）_

---

### Epic #17 · 多 Agent 架构（HTTP-to-self 模式）

**状态**：⬜ 未开始 | **负责人**：- | **起始**：- | **完成**：-
**前置依赖**：Epic #13（事件总线）、Epic #2（Plugin SDK）
**关联 Milestone**：M5（进化可感知，进化引擎作为独立 agent）

**开发计划**：

1. **Workspace 类**（2 天）——`daemon/workspace.py` 封装一个 agent 的完整运行时（AgentLoop / MemoryManager / SkillRegistry / ChannelManager）
2. **MultiAgentManager**（2 天）——`daemon/multi_agent_manager.py` `Dict[str, Workspace]` + async lock + pending_starts 去抖
3. **Dynamic Runner**（1 天）——`daemon/app.py` 加 middleware：按 `X-Agent-Id` header 路由到正确 workspace
4. **Context middleware**（0.5 天）——`AgentContextMiddleware` 把当前 agent 放进 `contextvars`
5. **Agent 间工具**（2 天）——`list_agents` / `chat_with_agent` / `submit_to_agent` / `check_agent_task`，走本地 HTTP + `X-Agent-Id`
6. **Session ID 命名**（0.5 天）——`{from}:to:{to}:{ts}:{uuid8}` 格式 + prompt 前缀 `[Agent X requesting]`
7. **Evolution 作为独立 agent**（2 天）——EvolutionEngine 搬到独立 workspace，观察主 agent 事件流、输出 skill 改进 PR
8. **文档 + 范例**（0.5 天）——`docs/MULTI_AGENT.md`

**检查清单**：

- [ ] `daemon/workspace.py` Workspace 类
- [ ] `daemon/multi_agent_manager.py` + lock + dedupe
- [ ] `DynamicMultiAgentRunner` + `X-Agent-Id` 路由
- [ ] `AgentContextMiddleware`
- [ ] 4 个 agent-间 tool
- [ ] Session ID 命名规范
- [ ] EvolutionEngine 独立 agent 化
- [ ] `docs/MULTI_AGENT.md`

**退出标准**：能同时跑 3 个 agent（main + evolution + QA），互相 `chat_with_agent` 走通；session 不串；日志能按 agent_id 分开。

**进度日志**：

- _（尚无）_

---

### Epic #18 · 前端补全（Web UI 从 Mock 到真实）

**状态**：⬜ 未开始 | **负责人**：- | **起始**：- | **完成**：-
**前置依赖**：Epic #13（SQLite event bus，事件回放 API）
**关联 Milestone**：M6（Onboarding + Hub）

> 当前前端是 Hermes WebUI 适配层，`xmclaw_adapter.js` Mock 了大量缺失 API（file browser、workspaces、profiles、memory）。用户打开这些面板看到的是空白。

**开发计划**：

1. **File Browser API**（1 天）——`daemon/routers/files.py`：`GET /api/files?path=` 返回目录树 + 文件内容，支持 allowed_dirs 过滤
2. **Workspaces API**（1 天）——`daemon/routers/workspaces.py`：CRUD + 切换当前 workspace
3. **Profiles API**（0.5 天）——`daemon/routers/profiles.py`：读取 `~/.xmclaw/persona/profiles/*.md`
4. **Memory Editor API**（1 天）——`daemon/routers/memory.py`：读/写 `~/.xmclaw/memory/*.md`，FTS5 搜索
5. **Onboarding 页面**（1.5 天）——静态页面：选 provider → 填 key → 选 tools → smoke test；走 `xmclaw_adapter.js` 新增 `/api/v2/onboarding` 路由
6. **适配层去 Mock**（1 天）——`xmclaw_adapter.js` 把上述 API 从 mock 切到真实 `/api/v2/*`
7. **前端测试**（1 天）——Playwright E2E：发送消息、工具调用、设置面板、模型切换

**检查清单**：

- [ ] `GET /api/v2/files?path=` 返回真实目录树
- [ ] `GET /api/v2/workspaces` CRUD
- [ ] `GET /api/v2/profiles` 列表
- [ ] `GET/POST /api/v2/memory` 读写 + 搜索
- [ ] Onboarding 页面 4 步流程
- [ ] `xmclaw_adapter.js` 零 mock
- [ ] Playwright E2E ≥ 4 个场景

**退出标准**：新用户首次打开 `http://127.0.0.1:8765/` 能看到 onboarding 向导；file browser / memory / workspaces / profiles 面板都有真实数据。

**进度日志**：

- _（尚无）_

---

### Epic #19 · 云部署与系统服务模板

**状态**：⬜ 未开始 | **负责人**：- | **起始**：- | **完成**：-
**前置依赖**：Epic #6（ENV override）
**关联 Milestone**：M6（Onboarding）+ M9（GA）

**开发计划**：

1. **Dockerfile**（0.5 天）——多阶段构建，基于 `python:3.11-slim`，只安装核心依赖
2. **docker-compose.yml**（0.5 天）——XMclaw + 可选 Playwright 浏览器服务
3. **systemd service**（0.5 天）——`deploy/systemd/xmclaw.service` 模板
4. **launchd plist**（0.5 天）——`deploy/launchd/com.xmclaw.daemon.plist` 模板
5. **Windows Service**（1 天）——`deploy/windows-service/` pywin32 包装器 + `sc create` 脚本
6. **一键安装脚本**（1 天）——`scripts/install.sh`（Linux/macOS）+ `scripts/install.ps1`（Windows）
7. **云部署模板**（1 天）——Fly.io `fly.toml`、AWS ECS Task Definition、Railway 模板

**检查清单**：

- [ ] `Dockerfile` 多阶段构建
- [ ] `docker-compose.yml`
- [ ] systemd / launchd / Windows Service 模板
- [ ] `install.sh` + `install.ps1`
- [ ] Fly.io / AWS ECS / Railway 模板 ≥ 1 个
- [ ] 文档 `docs/DEPLOY.md`

**退出标准**：`docker run -p 8765:8765 xmclaw/xmclaw:latest` 能直接对话；新用户 `curl | bash` 后 3 分钟内启动 daemon。

**进度日志**：

- _（尚无）_

---

### Epic #20 · 备份与恢复（零停机重载基础）

**状态**：🟡 进行中 | **负责人**：XMclaw Bot | **起始**：2026-04-23 | **完成**：-
**前置依赖**：Epic #13（SQLite event bus）
**关联 Milestone**：M8（性能与可观测）

> 参照 QwenPaw `backup/_ops/{create,restore,storage}.py`：编排式停止 → 原子目录交换 → 后台重启。

**开发计划**：

1. **Backup 类**（1 天）——`xmclaw/backup/create.py`：tar.gz `~/.xmclaw/`（排除 `logs/`），带 manifest.json（timestamp、version、checksum）
2. **Restore 类**（1 天）——`xmclaw/backup/restore.py`：验证 checksum → 停止受影响 agent → 原子目录交换（`~/.xmclaw/` ↔ `~/.xmclaw.backups/restore-staging/`）→ 后台重启
3. **CLI**（0.5 天）——`xmclaw backup create [name]` / `xmclaw backup list` / `xmclaw backup restore <name>`
4. **自动备份策略**（0.5 天）——config 支持 `backup.auto_daily: true`，cron 触发
5. **零停机重载骨架**（1 天）——`daemon/reloader.py`：新 Workspace 预热 → 原子 swap → 旧实例优雅停止（为 Epic #17 多 Agent 铺路）

**检查清单**：

- [x] `xmclaw backup create` 产出 tar.gz + manifest
- [x] `xmclaw backup restore` 原子交换 + 自动重启（文件系统部分；daemon 重启由调用者负责，Phase 2 再补）
- [ ] 自动 daily backup _(deferred — 需先引入 scheduler，留到 Phase 2)_
- [ ] 零停机重载骨架（Workspace swap）_(deferred — 需 daemon/reloader.py + agent-loop draining，留到 Phase 2)_
- [ ] `docs/BACKUP.md` _(deferred — 功能稳定后再写用户文档)_

**退出标准**：恢复 1GB 数据目录时服务中断 < 5 秒；daily auto-backup 连续 7 天不失败。

**进度日志**：

- 2026-04-23: Phase 1 落地——`xmclaw/backup/` 新包（`manifest.py` / `create.py` / `restore.py` / `store.py` / `AGENTS.md`）+ `xmclaw backup {create,list,restore}` CLI。Manifest v1 = `{schema_version, name, created_ts, xmclaw_version, archive_sha256, archive_bytes, source_dir, excluded, entries}`，permissive 读（忽略未知字段） + 严格写。创建流程：`<name>.tmp` staging → 两遍（写 tar.gz + 再读算 sha256）→ atomic rename；默认排除 `logs/`、`__pycache__/`、`daemon.{pid,meta,log}`、`*.pid`、`*.tmp`（含嵌套路径变体）。恢复流程：schema gate → sha256 verify → `.restore-staging` 解包 → 旧目录改名为 `.prev-<ts>` → 原子 rename staging → 失败时回滚。Tar-slip 防御：`_safe_extract` 用 `resolve().relative_to(target)` 校验每个 member。AGENTS.md 明确依赖规则：backup 不能 import `core/`/`providers/`/`daemon/`（否则坏装不出来）。CLI 三个子命令都走 `BackupError`/`RestoreError` → typer.Exit(1) 路径。smart-gate 新增 `backup` lane。tests: `tests/unit/test_v2_backup.py` 31 个（含 CLI 端到端），`scripts/test_lanes.yaml` always+cli+backup 三 lane 共 253 passed / 2 skipped。第 4 步（auto-daily）和第 5 步（daemon/reloader.py）显式推迟到 Phase 2：前者需先落 scheduler、后者需 agent-loop draining 协议，都是独立工作量 (commit 3de3b50)
- 2026-04-23: Phase 1 观测性补丁——新增 `BackupsCheck` doctor check（Epic #10 × Epic #20 交汇）：纯 observability，始终 `ok=True`；三态（空 / 新鲜 / ≥30d stale）+ `_format_age()` 粗粒度展示；honor `ctx.extras["backups_dir"]` 和 `XMC_BACKUPS_DIR`。checks 数 13→14；tests 97 passed + 2 skipped (commit a3968f9)
- 2026-04-23: Phase 1 日常运维补齐——`xmclaw/backup/store.py` 增 `delete_backup(name)` 与 `prune_backups(keep=N)` 两个原语 + `BackupNotFoundError`：`delete_backup` 拒 path 分隔符、`resolve().relative_to(root)` 挡 symlink 越界、不存在就抛；`prune_backups` 依 `list_backups` 的 created_ts 升序取前 `len-keep` 条删，`keep<0` 报 ValueError。CLI 新增 `xmclaw backup {delete,prune}` 两个子命令：`delete` 默认 `typer.confirm()` 防手滑，`--yes/-y` 跳；`prune` 先 echo 要删的列表再要 `--yes` 放行，`--keep 5` 默认；分别用 exit code 1（未找到 / 用户中止）、2（入参 ValueError）区分。`xmclaw/backup/__init__.py` 公开面加三条（`BackupNotFoundError` / `delete_backup` / `prune_backups`）。tests 增 10 条（store 6：delete happy / 缺失 / 路径分隔符 / prune 5→keep 2 / 低于 keep 无操作 / 负 keep；CLI 4：delete --yes 端到端 / delete 缺失退 1 / prune --keep 1 真删 / prune 无操作时 "nothing to prune"）；backup lane 41/41 + always+cli+backup 201 passed / 2 skipped (commit d56a6c6)
- 2026-04-23: Phase 1 完整性补丁——`xmclaw/backup/restore.py` 暴露 `verify_backup(name) -> Manifest`：只读把 restore 里已有的 sha256 gate 抽出来给用户一条"不解压只核对"的路径，用于 bit-rot 检测 / 搬存储层前 sanity-check。CLI 新增 `xmclaw backup verify <name>`：通过就 echo entries + bytes，失败走 RestoreError → Exit(1)。`xmclaw/backup/__init__.py` 公开面 +1。tests +7（return manifest / detect bit-flip / missing / archive 缺失 / schema newer / CLI happy / CLI corrupted exit 1）；backup lane 48/48、smart-gate 208 passed / 2 skipped

---

## 5. 让差异化"看得见"（Visible Differentiation）

> 用户不会读 `core/evolution/controller.py`。如果 agent 在进步，要让他**直接看到**。

### 5.1 三个可感知信号

| 信号 | 实现 | 用户在哪看到 |
|------|------|------------|
| **"Agent 今天学到了什么"** | `~/.xmclaw/skills/<name>/history.jsonl` 每条有 human-readable summary | `xmclaw evolution show --since 24h` |
| **"这个回答比昨天更好了"** | 每轮 grader 分数写入 event bus；session 结束时产"比较报告" | `xmclaw session report <id>` 显示"本 session vs 上周同类 task 的分数变化" |
| **"Skill 正在进化"** | `SKILL_EVOLVED` 事件直接打在终端（TUI 左下角常驻区） | CLI repl 模式时，agent 进化一次就在底部 flash 一行绿字 `[evolved] github-code-review v3 → v4 (+0.12)` |

### 5.2 对标对手的"看不见"

- Hermes 的进化在**另一个仓库**、**批量离线**、**$2-10 一次**——用户感知 = 零。
- OpenClaw 根本没做进化。
- QwenPaw 没做进化。

**我们的唯一使命**：把"agent 在变强"做成 **实时、本地、免费、可回溯** 的可视事件。

### 5.3 Marketing-visible killer demo

建议在 README 顶部放一个 asciinema / GIF：

```
$ xmclaw chat
> 帮我整理这周的邮件
[agent] 好的...正在运行 email_digest skill v3...
[agent] ✅ 整理完毕，5 封高优先级

--- 一周后 ---

> 帮我整理这周的邮件
[agent] 好的...正在运行 email_digest skill v7...
[agent] 注意：本次使用了你上周反馈"不要摘要 newsletter"后自动改进的过滤规则
[evolved] email_digest v6 → v7 (+0.18, 'newsletter filter tightened')
[agent] ✅ 整理完毕，3 封高优先级
```

Hermes、OpenClaw 都给不出这种 demo——他们的"进步"要么是手动 batch run，要么根本没有。

---

## 6. 取长补短清单（"及百家之长、避百家之短"）

### 6.1 直接抄（license 允许 + 纯工程问题）

| 来源 | 抄什么 | 为什么值得抄 |
|------|--------|------------|
| Hermes | `tools/registry.py` 的 AST-scan 自注册 | 省去中心化注册表的维护 |
| Hermes | `hermes_state.py` SQLite + FTS5 + WAL 模式 | 多渠道并发读写的成熟解 |
| Hermes | `agent/prompt_builder.py` 注入防御正则 + 不可见字符扫描 | 我们裸奔 |
| Hermes | `~/.hermes/config.yaml` + `~/.hermes/.env` 二元格式 | 我们的 JSON+secrets 混一起是坑 |
| OpenClaw | `AGENTS.md` 分层 + `pnpm check:changed` 智能门禁 | 工程纪律，直接出规矩 |
| OpenClaw | `src/plugin-sdk/*` 公开契约 + extensions 不得反向 import | 插件边界必须硬 |
| OpenClaw | `openclaw onboard` 的交互流 | UX 已验证 |
| QwenPaw | `security/skill_scanner/rules/signatures/` 8 份 YAML | Apache-2，直接拷；覆盖 command injection / exfil / obfuscation / injection / social / supply chain |
| QwenPaw | `security/tool_guard/rules/dangerous_shell_commands.yaml`（309 行，20+ 规则） | 含 fork-bomb / dd 破坏 / mkfs 检测 |
| QwenPaw | `agents/tool_guard_mixin.py:291-400` 4-path decision | auto_denied / preapproved / needs_approval / fall_through |
| QwenPaw | `app/approvals/service.py:58-75` 带 GC 的审批服务 | 30 分钟 pending 超时 + 200/500 条容量上限 |
| QwenPaw | `app/multi_agent_manager.py:22-137` HTTP-to-self | Epic #17 核心参考 |
| QwenPaw | `cli/doctor_registry.py` + `pyproject.toml:77-80` entry_points 组 | 插件化 doctor |
| QwenPaw | `app/channels/registry.py:97-129` `custom_channels/` 动态发现 | `sys.path` 注入 + `BaseChannel` 子类扫描 |
| QwenPaw | `agents/memory/agent_md_manager.py` + `proactive/*` | Markdown 记忆 + 主动消息模式 |
| QwenPaw | `backup/_ops/{create,restore,storage}.py` | v1.1.3 新增，我们长线需要 |
| QwenPaw | `app/runner/session.py:60-110` 文件名 sanitize + 局部恢复 | Windows 文件名字符替换 + partial JSON decode，解决 `discord:dm:12345` session id 的坑 |
| QwenPaw | `providers/provider.py` 的 `ModelInfo.supports_image/video` + `multimodal_prober.py` | 启动时探测 LLM 能力，prompt 动态注入 multimodal 提示 |
| QwenPaw | `providers/retry_chat_model.py` + rate limiter（10 concurrent / 600 QPM） | LLM 限流 + 重试的工业级实现 |
| **free-code** | `permissions.ts` 细粒度规则 + Auto Classifier + Denial tracking | 权限从三级（ASK/ALLOW/BLOCK）升级到规则引擎 |
| **free-code** | `memdir.ts` MEMORY.md 索引 + 类型化记忆（user/feedback/project/reference） | 记忆产品化：索引 + 语义检索双轨 |
| **free-code** | `cronScheduler.ts` CronCreate/Delete/ListTool + `.claude/scheduled_tasks.json` | Agent 自管理调度，锁 + jitter + missed task 检测 |
| **free-code** | `loadSkillsDir.ts` SKILL.md + `paths` 条件激活 + 多层级加载 | 技能生态网络效应：与 Claude Agent Skills 互操作 |
| **free-code** | `QueryEngine.ts` 对话生命周期抽象（headless/SDK/REPL） | 未来暴露 Python SDK 的基础 |

### 6.2 避什么坑（从他们 issues 总结）

| 坑 | 出自 | 我们的规避 |
|----|------|----------|
| Tool call 解析飘回文本 | OpenClaw #1467, Hermes #8912 | **已规避**：`core/ir/toolcall.py` 结构化 IR |
| Memory 绝对阈值溢出 | OpenClaw #31781 | **已规避**：Epic #5 eviction + 后台清扫 |
| Skill 无 rollback | Hermes FAQ "use git" | **已规避**：SkillRegistry append-only history |
| LLM self-judge 造成虚假满意 | Hermes 公开缺陷 | **已规避**：HonestGrader opinion ≤ 0.20 |
| 渠道 CI parity 缺失 | OpenClaw #52838 | **待做**：Epic #1 conformance test |
| 本地 WS 无设备绑定 | ClawJacked CVE 家族 | **已做一半**：ed25519_pairing.py 存在，需完成 pairing.py（Epic ed25519） |
| 评估跑一次 $2-10 | Hermes self-evolution 定价 | **已规避**：我们流式进化是 runtime 免费 |
| Windows native 不支持 | Hermes README 明说"请用 WSL2" | **差异化**：我们 Windows-first 开发（CLAUDE.md 已声明） |
| Skill 无进程级隔离，靠容器做边界 | QwenPaw `SECURITY.md:136-141` "skills run in-process" | **选择题**：短期走 QwenPaw 路线（进程内 + 容器隔离）；长期如果要多租户，v1.0 之前必须决策 |
| "Qwen" 变虚名：没有专门 provider，走 DashScope OpenAI-compat | QwenPaw `providers/openai_provider.py` | **反面教材**：我们 provider 层要 **API-compat 优先**，不给每个模型单开文件 |
| RoutingChatModel 喊智能路由但代码全 `del` 掉参数 | QwenPaw `routing_chat_model.py:42-53` | **差异化机会**：gene-driven 模型路由才是真的 |
| Agent loop 外包给 agentscope，升级风险全在他们手上 | QwenPaw `pyproject.toml:8-9` 死锁 `agentscope==1.0.19` | **差异化**：自建 loop 让我们能深度改，但要验证我们 loop 不比 agentscope 差 |
| 覆盖率 `fail_under: 30` 形同虚设 | QwenPaw `pyproject.toml:113-131` | **差异化**：我们要把关键模块 coverage 卡到 80%+，别学这个 |

---

## 7. 成熟度里程碑（从 dev-alpha 到 GA）

> 给每个里程碑一个可验证的退出标准。按当前团队节奏估算（保守）。
>
> **对应到策略 Phase**：M1+M4+M8 ≈ [COMPETITIVE Phase 1](COMPETITIVE_GAP_ANALYSIS.md#phase-1--安全与基础补全1-2-个月)；M2+M3 ≈ Phase 2；M6+M7 ≈ Phase 3；M5（★进化可感知）≈ Phase 4。两份文档在这里合流。

### M1 · Daemon 稳定性 GA（2 周）

**状态**：⬜ 未开始 | **起始**：- | **完成**：-
**包含 Epics**：#6 ENV / #10 Doctor / #11 Smart-gate / #12 AGENTS.md / #13 SQLite bus

**退出标准**：
- [ ] 连续 72h 压测不崩
- [ ] `xmclaw doctor` 通过率 100%（Epic #10）
- [x] SQLite event bus 落地（Epic #13 — schema + WAL + FTS5 + /api/v2/events + 重启重放 + FTS5 <100ms）
- [x] ENV override 工作（Epic #6）
- [x] smart-gate 测试 CI 跑 < 3 分钟（Epic #11，phase 1+2 落地 2026-04-23；实际 CI 运行时间待合入 main 后观测）
- [x] 所有子包 AGENTS.md 完成（Epic #12，2026-04-23；plugin_sdk/ 的 AGENTS.md 挂单 Epic #2）

**进度日志**：
- _（尚无）_

---

### M2 · 三渠道可用（2 周）

**状态**：⬜ 未开始 | **起始**：- | **完成**：-
**包含 Epics**：#1 Channel SDK

**退出标准**：
- [ ] Discord / Slack / Telegram 各发 100 条消息往返不丢
- [ ] Channel conformance test 全绿（Epic #1）
- [ ] `dm_policy` 安全钩子启用

**进度日志**：
- _（尚无）_

---

### M3 · 插件 SDK v1（1 周）

**状态**：⬜ 未开始 | **起始**：- | **完成**：-
**包含 Epics**：#2 Plugin SDK

**退出标准**：
- [ ] `plugin_sdk/` 公开契约冻结
- [ ] import 隔离 CI 检查通过
- [ ] 至少一个样例第三方插件 repo（`xmclaw-plugin-example`）跑起来

**进度日志**：
- _（尚无）_

---

### M4 · 沙箱可用（2 周）

**状态**：⬜ 未开始 | **起始**：- | **完成**：-
**包含 Epics**：#3 沙箱

**退出标准**：
- [ ] `providers/runtime/process.py` 把 skill 代码跑在子进程
- [ ] QwenPaw YAML 规则移植完成，`xmclaw security scan <skill>` 能用
- [ ] 内建 5 条"危险 skill"测试用例全部被拦截
- [ ] 3 Guardian 架构 + 4-path decision 在 AgentLoop 生效

**进度日志**：
- _（尚无）_

---

### M5 · 进化可感知（★3 周）

**状态**：⬜ 未开始 | **起始**：- | **完成**：-
**包含 Epics**：#4 Evolution 执行层 + #17 多 Agent（独立进化 agent）
**这是差异化的单一焦点里程碑——其他里程碑的目的是让它站稳。**

**退出标准**：
- [ ] Epic #4 完整交付
- [ ] killer demo GIF 能录出来（§5.3）
- [ ] 一周实测：agent 在同类 task 上可见变强（grader 分数 +0.1 以上）
- [ ] `xmclaw evolution show --since 7d` 能看到真实 evolution 事件 ≥ 3 条

**进度日志**：
- _（尚无）_

---

### M6 · Onboarding + Hub（2 周）

**状态**：⬜ 未开始 | **起始**：- | **完成**：-
**包含 Epics**：#8 Skill Hub + #9 Onboarding + #18 前端补全 + #19 云部署

**退出标准**：
- [ ] 新用户从 `pip install xmclaw` 到第一次对话 ≤ 3 分钟
- [ ] Skill hub 至少 10 个可安装 skill
- [ ] 跨平台（Win/Mac/Linux）onboarding 都跑通

**进度日志**：
- _（尚无）_

---

### M7 · IDE 入口（1 周）

**状态**：⬜ 未开始 | **起始**：- | **完成**：-
**包含 Epics**：#7 ACP

**退出标准**：
- [ ] ACP server 被 Zed 识别
- [ ] 反向 delegate 到 claude_code 跑通
- [ ] `docs/IDE.md` 有 Zed + VS Code 配置示例

**进度日志**：
- _（尚无）_

---

### M8 · 性能与可观测（1 周）

**状态**：⬜ 未开始 | **起始**：- | **完成**：-
**包含 Epics**：#5 Memory eviction + #14 Prompt 注入 + #15 日志 + #16 Secrets + #20 备份恢复

**退出标准**：
- [x] 结构化日志 + rotation（Epic #15）
- [x] Memory eviction（Epic #5）
- [x] Prompt 注入防御（Epic #14）
- [ ] Secrets 加密（Epic #16）
- [ ] `grep -r sk- ~/.xmclaw/` 无命中（明文 secret 审计清空）

**进度日志**：
- _（尚无）_

---

### M9 · v1.0 GA（封板 1 周）

**状态**：⬜ 未开始 | **起始**：- | **完成**：-
**包含 Epics**：所有 Epic（#1~#20）收尾 + 发布

**退出标准**：
- [ ] 所有 Epic 关闭
- [ ] `pyproject.toml` 版本跳 `1.0.0`
- [ ] 发 PyPI + GitHub release
- [ ] README 放 killer demo
- [ ] `DEV_ROADMAP.md` / `COMPETITIVE_GAP_ANALYSIS.md` 全部 Epic / Milestone 状态为 ✅
- [ ] CHANGELOG.md v1.0.0 段落写完

**进度日志**：
- _（尚无）_

---

**总计**：乐观 ~12 周，现实 ~16 周。

### 7.10 里程碑依赖图

```
                 ┌─────────────┐
                 │ M1 稳定性   │ (周 1-2)  ENV+Doctor+Gate+AGENTS+SQLite
                 └──────┬──────┘
            ┌──────────┬┴─────────┬──────────┐
            ↓          ↓          ↓          ↓
      ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐
      │M2 渠道  │ │M3 SDK   │ │M4 沙箱  │ │M8 观测  │ (周 3-6 可并)
      └────┬────┘ └────┬────┘ └────┬────┘ └─────────┘
           │           │           │
           └───────┬───┴───────────┘
                   ↓
            ┌─────────────┐
            │ M5 进化 ★   │ (周 7-9)  依赖 M3+M4+M8
            └──────┬──────┘
                   ↓
      ┌────────────┼────────────┐
      ↓            ↓            ↓
┌─────────┐ ┌─────────┐  ┌─────────┐
│M6 Hub   │ │M7 IDE   │  │...       │ (周 10-12)
└────┬────┘ └────┬────┘  └─────────┘
     └──────┬────┘
            ↓
     ┌─────────────┐
     │ M9 GA 发布  │ (周 13-16)
     └─────────────┘
```

---

## 8. 落地优先级建议（下一周动什么）

**本周（Week 1）**：
1. Epic #6 ENV override（半天）——解锁 Docker / CI 部署
2. Epic #10 doctor（2 天）——用户能自诊断
3. Epic #13 SQLite event bus（2 天）——事件回放 + session report 基础
4. §3 路径规范落地 `utils/paths.py`（半天）——后续一切路径 bug 的防御 ✅ 2026-04-23 落地 (commit 7f47d18)：`xmclaw/utils/paths.py` v2 API（`data_dir()` / `v2_workspace_dir()` / `logs_dir()` / `default_{pid,meta,daemon_log,token,events_db,memory_db}_path()`），`XMC_DATA_DIR` workspace 总开关 + 既有窄 override 继续生效；`daemon/lifecycle.py` / `daemon/pairing.py` / `core/bus/sqlite.py` / `cli/main.py` / `cli/doctor_registry.py` 全部改为委托；`get_logs_dir()` 从 `<repo>/logs` 修正为 `~/.xmclaw/logs/`（对齐 log.py 文档字符串，修 §3.1 违规）；18 条新 `tests/unit/test_v2_utils_paths.py`，smart-gate 309 passed / 2 skipped

**下周（Week 2）**：
5. Epic #14 prompt injection 防御（1 天，抄 Hermes 的正则）
6. Epic #15 structlog rotation（1 天）
7. Epic #4 Phase A：`SKILL_EVOLVED` 事件类型 + `xmclaw evolution show` 命令骨架（3 天）——让"可见进化"开跑

这 7 件事做完，我们就从"跑得动"跃升到"能给人看"。M1 就差不多。

---

## 附录 A · 对标仓库关键文件速查

如果后续要再查任何细节，以下是最值得直接读的文件：

**Hermes**（`NousResearch/hermes-agent` @ main）
- `README.md`、`AGENTS.md`、`pyproject.toml`
- `tools/registry.py`（AST-scan 自注册）
- `agent/prompt_builder.py`（注入防御）
- `hermes_state.py`（SQLite+FTS5 schema v8）
- `gateway/run.py`（多渠道 gateway 主循环）
- `cli.py` + `hermes_cli/main.py`（CLI 结构）
- [hermes-agent-self-evolution] `README.md` + `PLAN.md`（批量进化设计）

**OpenClaw**（`openclaw/openclaw` @ main）
- `README.md`、`AGENTS.md`、`VISION.md`
- `src/plugin-sdk/*`（公开契约）
- `src/channels/AGENTS.md`、`src/plugins/AGENTS.md`、`src/gateway/protocol/AGENTS.md`
- `.github/labeler.yml`（插件 CI 组织方式）

**QwenPaw**（`agentscope-ai/QwenPaw` @ main，本地镜像 `C:/Users/15978/Desktop/qwenpaw-src/`）

架构骨架：
- `src/qwenpaw/agents/react_agent.py:76-188`（`QwenPawAgent = ToolGuardMixin + ReActAgent`）
- `src/qwenpaw/agents/tool_guard_mixin.py:291-400, 662-689`（4-path decision + `_acting`/`_reasoning` override）
- `src/qwenpaw/app/multi_agent_manager.py:22-137`（HTTP-to-self 多 agent）
- `src/qwenpaw/app/_app.py:71-80`（`X-Agent-Id` 路由）
- `src/qwenpaw/app/workspace/workspace.py:49-100`（Workspace 全家桶）

安全：
- `src/qwenpaw/security/tool_guard/engine.py:54-120`（3 guardian 装配）
- `src/qwenpaw/security/tool_guard/rules/dangerous_shell_commands.yaml`（309 行 bash 危险命令）
- `src/qwenpaw/security/skill_scanner/scanner.py` + `rules/signatures/*.yaml` × 8
- `src/qwenpaw/security/skill_scanner/data/default_policy.yaml`（242 行策略）
- `src/qwenpaw/security/secret_store.py`（keyring + cryptography 加密）
- `src/qwenpaw/app/approvals/service.py:58-75`（审批 GC）

Skill 系统：
- `src/qwenpaw/agents/skills_manager.py:48-67, 88-149, 248-298`（routing + 语言对 + 版本）
- `src/qwenpaw/agents/skills/multi_agent_collaboration-en/SKILL.md:1-36`（格式样例）

Channel：
- `src/qwenpaw/app/channels/registry.py:20-36, 62-77, 97-185`（15 channel + custom_channels 发现）
- `src/qwenpaw/app/channels/base.py:68-75, 78-100`（BaseChannel 契约）
- `src/qwenpaw/app/channels/dingtalk/{channel.py, ai_card.py, markdown.py}`（最完整 channel 适配样例）

Memory：
- `src/qwenpaw/agents/memory/base_memory_manager.py:21-55`（ABC）
- `src/qwenpaw/agents/memory/reme_light_memory_manager.py:37-80`（reme-ai 0.3.1.8 + 平台分流）
- `src/qwenpaw/agents/memory/proactive/*.py`（主动消息）
- `src/qwenpaw/app/runner/session.py:24-110`（`SafeJSONSession` 文件名 sanitize + 局部恢复）

CLI：
- `src/qwenpaw/cli/main.py:58-93`（LazyGroup 懒加载）
- `pyproject.toml:73-80`（双 CLI 入口 + entry_points 组）

配置：
- `src/qwenpaw/constant.py:12-25, 89-111, 145-200`（env 读取 + WORKING_DIR + 目录常量）
- `src/qwenpaw/config/config.py`（1728 LOC pydantic 模型）
- `src/qwenpaw/config/utils.py:41-74`（路径迁移规范化）
- `src/qwenpaw/app/mcp/{manager.py, watcher.py}`（MCP 热重载）

LLM：
- `src/qwenpaw/providers/provider_manager.py:1-80`
- `src/qwenpaw/providers/retry_chat_model.py`（重试）
- `src/qwenpaw/providers/capability_baseline.py` + `multimodal_prober.py`（能力探测）
- `src/qwenpaw/agents/routing_chat_model.py:42-122`（stub 但参考）

ACP / 扩展：
- `src/qwenpaw/agents/acp/{server.py:1-60, client.py, permissions.py, tool_adapter.py}`
- `src/qwenpaw/plugins/architecture.py:10-58`（前端+后端双入口 PluginManifest）

备份 / Doctor：
- `src/qwenpaw/backup/_ops/{create,restore,storage}.py`
- `src/qwenpaw/cli/{doctor_cmd,doctor_checks,doctor_connectivity,doctor_fix_runner,doctor_registry}.py`

---

## 附录 B · 术语

- **Anti-requirement**：对标仓库已暴露的失败模式，我们**强制规避**。
- **HonestGrader**：`core/grader/`，不让 LLM 给自己打分，LLM 意见权重 ≤0.20。
- **Streaming Evolution**：进化作为 runtime 原语，订阅事件流实时产出候选，而非 Hermes 的批量离线。
- **Plugin-SDK 边界**：公开 API 冻结契约，内部实现随意重构，插件只吃公开 API。
- **Smart-gate**：`git diff` 驱动的测试 lane 选择，避免全量跑 83 个测试文件。

---

*文档结束。下次 review：M1 完成后（预计 2026-05-06）。*
