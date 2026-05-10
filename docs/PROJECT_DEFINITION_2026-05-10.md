---
title: XMclaw 项目定位 (基于代码实际能力，2026-05-10)
status: draft
audience: 项目作者 + 自己
---

# XMclaw 项目定位

> 不看 README，只看代码实际定义。本文目的：把"项目到底是什么"写
> 死，避免后续工作飘。

## TL;DR — 一句话

**XMclaw 是一个本地常驻、跨会话有持续记忆 + 持续认知 + 自主目标分解
+ 多模态感知 + 自我进化的"个人贾维斯"runtime。** 用户原话：
"我希望他像贾维斯那样，拥有强大的记忆系统，任务处理能力和进化
能力" + "我的目的是给他自己用，不是光给我用"。

不是聊天机器人，不是 SaaS 产品，不是给别人卖的工具。是**个人助理
runtime**，作者本人是唯一一类用户。

## 谁在用 / 怎么用

* **唯一用户类**：项目作者本人（"自己用的"）
* **接触面 3 类**：
  * **Web UI** (`http://127.0.0.1:8765/ui/`) — 22 个页面
  * **CLI** (`xmclaw start/stop/chat/doctor/...`) — 多个 typer 子命
    令树 (approvals/backup/channels/config/cron/evolution/memory/
    skill...)
  * **Channel** (Telegram/Discord/Slack/Feishu/DingTalk/WeCom/Email)
    — agent 反向接到 IM
* **跨设备**：daemon 在本地跑，用户从任何 channel 戳它都能
  得到带历史的回答

## 实际代码能力（grep 出来的事实）

### 1. 持续认知 (`xmclaw/cognition/`，23 模块)

* `CognitiveDaemon` 1Hz 后台心跳，**默认 enabled**
* `PerceptionBus` 接 4 个多模态 source：FileWatcher / ProcessWatcher
  / ScreenWatcher / ActiveWindowWatcher / ClipboardWatcher /
  CalendarWatcher（**默认 on，依赖 dep 自动 fall-back**）
* `AttentionFilter` 7±2 工作记忆容量 + 显著性打分
* `ReasoningEngine` 4 模式 (causal / analogical / counterfactual / meta)
* `Planner` + `HTNPlanner` —— 高层 goal 递归分解为 Task DAG
* `TaskScheduler` 拓扑序并行执行 + SQLite 持久化
* `ReflectionCycle` 4 档周期 (5min reflect / 1h consolidate / 1d
  groom / 1min metacognize)
* `AutonomyPolicy` 3 tier (observe/suggest/execute) + risk-gated
  双重确认（**默认 50 = suggest**）
* `SuggestionInbox` SQLite — 主动建议待操作员审批

### 2. 记忆系统 (`xmclaw/memory/` + `xmclaw/providers/memory/`)

* `UnifiedMemorySystem` 三轴查询：semantic (向量) × relation (图)
  × temporal (时序) × layer (working/short/long/procedural)
* `MemoryGraph` SQLite 关系图
* `MemoryExtractor` 自动萃取每轮对话的持久事实
* AgentLoop **每 turn 自动 recall + 自动 put**（"agent 自己用
  记忆"飞轮闭合）

### 3. 元认知 / 自我进化 (`xmclaw/core/{evolution,grader,metacognition}/`)

* `DecisionTraceRecorder` 每个 agent 决策（tool_choice /
  skill_choice / answer_style / decline / ask_clarification）
  落 SQLite
* `MetaCognitionPass` 周期 LLM 扫 trace 找行为 pattern
* `Reformer` 把 pattern 映射成提案 (curriculum_edit /
  preference_update / skill_propose)
* `EvolutionController` + `HonestGrader` 多信号 gate — 强独立证据
  才让 skill 升级
* `SkillRegistry` + `SkillProposer` —— 真正的 skill 增量学习

### 4. 工具 / Tool surface

* 内置 tool ~30 个：file_read / write / bash / web_fetch /
  web_search / sqlite / memory_search / journal / todo / persona
  edit / voice / worktree / apply_patch / ask_user_question /
  curriculum_edit / 等
* 工具组织：`builtin*` mixin 拆分（fs/shell/db/memory/persona/
  user/voice/worktree）
* MCP hub — 第三方 tool 通过 MCP server 接入
* Browser tool / LSP tool 可选

### 5. 多 LLM 后端 (`xmclaw/providers/llm/`)

* Anthropic / OpenAI native + OpenRouter
* OpenAILLM 用 `base_url` 适配 DeepSeek / Kimi / Qwen / Gemini /
  本地 Ollama / 其他兼容端点
* `LLMRegistry` + 多 profile 配置 — 一个 daemon 多模型并存
* prompt-cache 自动 marker (Anthropic + 部分国产端点)
* 结构化错误分类 12 种 reason × 重试调度

### 6. 多 channel 接入 (`xmclaw/providers/channel/`)

8 个真实现 + 1 scaffold：feishu / dingtalk / wecom (outbound) /
telegram / discord / slack / email + acp scaffold + weixin scaffold

### 7. 评测 / Benchmark (`xmclaw/eval/`)

LongMemEval / TerminalBench 2.0 / SWE-bench Verified Tier-1+2
(Docker sandboxed)。

### 8. 数据持久化（重要 — 用户痛点）

全部走 `xmclaw/utils/paths.py`，`XMC_DATA_DIR` 一刀切重定向：
* `<v2>/events.db` 行为事件审计
* `<v2>/sessions.db` 会话历史
* `<v2>/memory.db` SQLite-vec 长期记忆
* `<v2>/graph.db` 记忆图
* `<v2>/cognitive_state.json` 认知状态快照
* `<v2>/decisions.db` (R3) 决策痕迹
* `<v2>/suggestions.db` (R5) 建议盒子
* `<v2>/experiments.db` 自我实验
* `<v2>/proposals/` 进化提案
* `<v2>/eval_cache/` HF 数据集
* `<v2>/journal/<YYYY-MM>/` 会话日志
* `<v2>/agents/` 多 agent 注册
* `<data>/skills/` skill 进化历史 (workspace 外，audit 不丢)
* `<data>/skills_user/` 用户写的 skill (workspace 外)
* `<data>/persona/profiles/` 7 文件人格 (workspace 外)
* `<data>/memory/` 用户笔记 (workspace 外)
* `~/.xmclaw.secret/` Fernet 加密 secret store (sibling)

## 不做什么

* **不做**多用户 SaaS — 没认证、没 RBAC、没 audit trail (除了本地 events.db)
* **不做**数据上传 — 默认 nothing leaves the box（除了用户授权的
  channel adapter 主动联网）
* **不做**云训练 / 模型微调 — 全部依赖外部 LLM API 或本地 Ollama
* **不做**其他人友好的 onboarding — 是作者自己的工具，权衡偏向 power user

## 与"产品定位"的差距 (2026-05-10 现状)

| 项 | 现状 | 期望 |
|---|---|---|
| 单用户性 | ✅ 默认 127.0.0.1 only，pairing token | 不变 |
| 数据本地 | ✅ 全部 sqlite + 本地 fs | 不变 |
| 持续认知 | ✅ R1-R6 完整闭环（飞轮齿全到位）| 已达成 |
| 多模态感知 | 🟡 4 个 watcher 已实现，依赖 optional pip extras | 装 dep 后开箱用 |
| 记忆 / 进化 | ✅ 三轴 + 多档反思 + grader-gated 进化 | 已达成 |
| 任务编排 | ✅ HTN 拆解 → DAG 并行 → sub-agent 执行 | 已达成 |
| 跨设备连续性 | 🟡 IM channel 接进来，但 user-state 没显式沉淀 (今后) | R5 follow-up |
| 心智可视化 | ✅ Cognition 页 3 tab (state/monologue/suggestions) | 已达成 |
| **路径统一** | ✅ Patch A 950abe5 已闭 | 已达成 |

## 工程姿态

* **Windows-first** — 作者主机
* **Python 3.10+** — 不用 3.11+ async features
* **零 build step** — 前端 Preact + htm via ESM 直接服务
* **TestClient 跨前后端测试**强制 (CLAUDE.md, 2026-05-09 standing rule)
* **import 方向 lint** + **路径硬编码 lint** (Patch A 950abe5) — 把
  反需求变成 CI 守门员

## 当前焦点（基于近期 commits）

最近 4 周（git log 可见）：
* Sprint 1-4 完成
* Jarvisification Phase 1-6 (R1-R6 框架级重构) 完成
* 默认隐私 flip (autonomy_level → 50 / metacognize → 60s / R4
  watcher 默认 on)
* Patch A 路径统一收尾
* **正在进行**：作者并行 background agent 在做大代码拆分（builtin.py
  → 8 mixin / app.py → app_lifespan / providers/llm → streaming_utils
  / providers/channel → _common），还在 untracked 状态

## 一句话再 reframe

XMclaw 不是产品，是**作者本人的"贾维斯"runtime — 在自己机器上常
驻、跨会话有记忆、能自动拆任务并行做、能感知环境主动建议、能从
自己行为里学习并改自己 prompt 的 personal agent**。其他人能不能
用是次要问题。
