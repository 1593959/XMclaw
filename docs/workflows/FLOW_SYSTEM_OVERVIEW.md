# XMclaw 系统流程总览

> 本文档详细描述 XMclaw 项目的所有核心流程，包括对话流程、任务处理、进化系统、记忆系统等。

---

## 📋 目录

1. [核心架构概览](#1-核心架构概览)
2. [对话流程 (Dialog Flow)](#2-对话流程)
3. [任务分类流程 (Task Classification)](#3-任务分类流程)
4. [信息收集流程 (Information Gathering)](#4-信息收集流程)
5. [任务规划流程 (Task Planning)](#5-任务规划流程)
6. [技能匹配流程 (Skill Matching)](#6-技能匹配流程)
7. [执行流程 (Execution)](#7-执行流程)
8. [反思流程 (Reflection)](#8-反思流程)
9. [进化流程 (Evolution)](#9-进化流程)
10. [记忆系统流程 (Memory System)](#10-记忆系统流程)
11. [事件总线流程 (Event Bus)](#11-事件总线流程)
12. [多智能体协作流程 (Multi-Agent)](#12-多智能体协作流程)
13. [集成流程 (Integration)](#13-集成流程)
14. [工具系统流程 (Tool System)](#14-工具系统流程)
15. [启动与关闭流程 (Lifecycle)](#15-启动与关闭流程)
16. [WebSocket 通信流程](#16-websocket-通信流程)
17. [流程层级分类](#17-流程层级分类)

---

## 1. 核心架构概览

```
┌─────────────────────────────────────────────────────────────────────┐
│                          前端 (Web UI)                               │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐  │
│  │ 聊天区  │  │侧边栏   │  │工具面板 │  │日志区   │  │设置面板 │  │
└─────────────────────────────────────────────────────────────────────┘
        │ WebSocket  │
        └────────────┘
                    ↕
┌───────────────────────────────────────────────────────────────────────┐
│                        Daemon Server (FastAPI)                         │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐           │
│  │ WS Handler│  │ REST API │  │ ASR/TTS  │  │ Media API│           │
└───────────────────────────────────────────────────────────────────────┘
                              ↕
┌───────────────────────────────────────────────────────────────────────┐
│                      Agent Orchestrator                                 │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐│
│  │ TaskClassifier│  │InfoGatherer│  │TaskPlanner  │  │SkillMatcher ││
│  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘│
│         └────────────────┴────────┬──────┴─────────────────┘       │
│                                      │                              │
│                      ┌───────────────▼─────────────┐                │
│                      │      Agent Loop            │                │
│                      │   (5-Stage Cognition)      │                │
│                      └─────────────┬───────────────┘                │
│          ┌────────────────────────┼────────────────────────┐        │
│          │                        │                        │        │
│  ┌──────▼──────┐  ┌──────────────▼──────────────┐  ┌─────▼─────┐ │
│  │   LLM       │  │      Tool Registry           │  │   Memory  │ │
│  │   Router    │  │  Built-in | Generated |Plugin │  │   Manager │ │
│  └─────────────┘  └─────────────────────────────┘  └───────────┘ │
│                                                                      │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │                    Evolution Engine                          │   │
│  │  ┌────────┐  ┌────────┐  ┌────────┐  ┌────────┐             │   │
│  │  │Observer│  │ Learner│  │ Evolver│  │Solidify│             │   │
│  └──────────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 2. 对话流程 (Dialog Flow)

### 2.1 完整对话生命周期

```
用户输入
    │
    ▼
┌─────────────────────────────┐
│  特殊消息类型检测            │
│  - [PLAN MODE]             │
│  - [RESUME]                │
│  - [PLAN APPROVE]          │
└─────────────┬───────────────┘
              │
              ▼
╔═══════════════════════════════════════════╗
║     5-Stage Cognition Pipeline            ║
║     (五阶段认知管道)                      ║
╠═══════════════════════════════════════════╣
║  ┌─────────┐  ┌─────────┐  ┌─────────┐   ║
║  │Stage 1  │→│Stage 2  │→│Stage 3  │→... ║
║  │任务分类 │  │信息收集 │  │任务规划 │      ║
║  └─────────┘  └─────────┘  └─────────┘      ║
╚═══════════════════════════════════════════╝
              │
              ▼
┌─────────────────────────────┐
│     Main Execution Loop     │
│  Think → Act → Observe →... │
└─────────────┬───────────────┘
              │
              ▼
┌─────────────────────────────┐
│  反思阶段 (Reflection)      │
│  + 触发进化 (Evolution)     │
│  + 记忆保存 (Memory Save)   │
└─────────────────────────────┘
```

### 2.2 状态流转

```
IDLE → THINKING → TOOL_CALL → WAITING → ... → DONE
 │        │           │           │
 │        │           │           └─ ask_user 暂停
 │        │           └─ 工具执行中
 │        └─ LLM 生成中
 └─ 空闲
```

---

## 3. 任务分类流程 (Task Classification)

### 3.1 任务类型

| 类型 | 描述 | 复杂度 |
|------|------|--------|
| `qa` | 问答/解释 | LOW |
| `code` | 代码编写/调试 | MEDIUM |
| `search` | 信息搜索 | LOW |
| `plan` | 规划/分析 | HIGH |
| `creative` | 创意写作 | MEDIUM |
| `learning` | 学习/研究 | MEDIUM |
| `file_op` | 文件操作 | LOW |
| `system` | 系统控制 | LOW |
| `general` | 通用对话 | LOW |

### 3.2 分类决策流程

```
用户输入
    │
    ▼
┌─────────────────────────────┐
│  Fast Path: 关键词匹配       │
│  - 代码关键词 → MEDIUM      │
│  - 规划关键词 → HIGH        │
│  - 搜索关键词 → LOW         │
└─────────────┬───────────────┘
              │ (未匹配)
              ▼
┌─────────────────────────────┐
│  Slow Path: LLM 辅助分类    │
│  - 发送分类提示词           │
│  - 解析 JSON 响应           │
└─────────────┬───────────────┘
              │
              ▼
      返回 TaskProfile
```

---

## 4. 信息收集流程 (Information Gathering)

### 4.1 并行信息收集

```
InfoGatherer.gather()
    │
    ├── [并行] _search_memories() ──→ Vector Store
    ├── [并行] _search_insights() ──→ SQLite
    ├── [条件] _search_web() ──────→ WebSearch
    └── [条件] _search_code_examples()
```

---

## 5. 任务规划流程 (Task Planning)

### 5.1 规划决策

```
复杂度
    │
    ├── LOW ──→ 直接执行
    ├── MEDIUM ──→ LLM 轻量级规划
    └── HIGH ──→ LLM 完整规划 + 用户确认
```

---

## 6. 技能匹配流程 (Skill Matching)

### 6.1 匹配与执行

```
SkillMatcher.match_and_execute()
    │
    ▼
加载所有技能定义 (ToolRegistry)
    │
    ▼
评分计算 (0-1)
    │
    ▼
阈值过滤: score >= 0.4 → 匹配
            score >= 0.75 → 自动执行
    │
    ▼
自动执行高置信度技能
```

---

## 7. 执行流程 (Execution)

### 7.1 主执行循环

```
while turn_count < 50:
    │
    ▼
LLM 流式调用
    │
    ▼
解析事件流 (text/tool_call_*)
    │
    ▼
工具执行 (ToolRegistry)
    │
    ▼
特殊处理: ask_user → 暂停等待
    │
    ▼
消息构建 → 继续循环
    │
    ▼
无 tool_calls → 退出
```

---

## 8. 反思流程 (Reflection)

### 8.1 执行时机

```
对话执行完毕
    │
    ▼
同步执行反思 (INLINE) → 对用户可见
    │
    ▼
发布 REFLECTION_COMPLETE 事件
    │
    ▼
触发即时进化
```

---

## 9. 进化流程 (Evolution)

### 9.1 进化引擎架构

```
┌─────────────────────────────────────┐
│           Evolution Engine          │
│                                     │
│  Observe → Learn → Evolve →Solidify│
│                                     │
│  Sub-Components:                   │
│  - GeneForge (基因锻造)             │
│  - SkillForge (技能锻造)            │
│  - VFMScorer (变异评分)             │
│  - Validator (验证器)                │
└─────────────────────────────────────┘
```

### 9.2 进化循环流程

```
run_cycle()
    │
    ▼
1. 观察 (Observe)
   - 获取最近 200 个会话
   - 提取工具使用模式
   - 检测重复请求
   - 识别问题反馈
    │
    ▼
2. 学习 (Learn)
   - 保存洞察到 SQLite
    │
    ▼
3. 进化决策 (Decide)
   - pattern → skill 生成
   - problem → gene 生成
    │
    ▼
4a/4b. 基因/技能生成
   - LLM 生成
   - VFM 评分
   - 代码锻造
   - 验证测试
    │
    ▼
5. 记录与通知
   - MEMORY.md
   - 每日日志
   - EVOLUTION_NOTIFY 事件
```

### 9.3 实时进化触发

```
触发源:
  1. 对话结束 → AgentLoop._trigger_immediate_evolution()
  2. 工具阈值达到 → PATTERN_THRESHOLD 事件 (count=3)
  3. 定时调度 → EvolutionScheduler (默认30分钟)
```

---

## 10. 记忆系统流程 (Memory System)

### 10.1 记忆分层架构

```
┌─────────────────────────────────────────┐
│           Memory Manager                │
│                                         │
│  Layer 1: SQLite (结构化数据)          │
│  - 洞察、基因、技能、元数据             │
│                                         │
│  Layer 2: JSONL (原始会话)              │
│  - 原始对话日志、会话历史               │
│                                         │
│  Layer 3: Vector Store (语义记忆)       │
│  - ChromaDB / SQLite-Vec                │
│                                         │
│  Layer 4: Markdown (长期记忆)            │
│  - MEMORY.md, PROFILE.md, SOUL.md       │
└─────────────────────────────────────────┘
```

---

## 11. 事件总线流程 (Event Bus)

### 11.1 事件类型总览

| 类别 | 事件类型 | 描述 |
|------|----------|------|
| Agent | `agent:start/stop/error` | Agent 生命周期 |
| Tool | `tool:called/result` | 工具调用 |
| Memory | `memory:updated` | 记忆更新 |
| Evolution | `evolution:cycle/trigger/notify` | 进化相关 |
| Pattern | `pattern:threshold_reached` | 模式阈值 |
| Reflection | `reflection:complete` | 反思完成 |

---

## 12. 多智能体协作流程 (Multi-Agent)

### 12.1 团队协作

```
create_team(team_name, agent_ids)
    │
    ▼
创建多个 Agent 实例
    │
    ▼
run_team(team_name, task, parallel=True)
    │
    ▼
并行执行 → 结果合并 (concat/first/vote)
```

---

## 13. 集成流程 (Integration)

```
支持的集成:
  - Slack
  - Discord
  - Telegram
  - GitHub
  - Notion

消息路由:
  外部消息 → Integration.on_message()
           → Agent 执行
           → 回复路由
```

---

## 14. 工具系统流程 (Tool System)

### 14.1 工具分类

```
ToolRegistry
    │
    ├── 内置工具 (Built-in)
    │     ├── 文件: FileRead, FileWrite, FileEdit
    │     ├── 执行: Bash, CodeExec
    │     ├── 网络: WebSearch, WebFetch, Browser
    │     ├── Git: Git
    │     ├── 任务: Todo, Task
    │     ├── 搜索: Glob, Grep
    │     ├── 记忆: MemorySearch
    │     ├── AI: Vision, ASR, TTS
    │     └── 特殊: ComputerUse, MCPTool, GitHubTool
    │
    ├── 生成技能 (Generated)
    │     └── shared/skills/skill_*.py
    │
    └── 插件工具 (Plugin)
          └── plugins/tools/*.py
```

---

## 15. 启动与关闭流程 (Lifecycle)

```
启动: xmclaw start
    │
    ├── orchestrator.initialize()
    │     ├── tools.load_all()
    │     └── memory.initialize()
    │
    ├── install_event_handlers()
    ├── evo_scheduler.start()
    └── integration_manager.start()
    
关闭:
    │
    ├── integration_manager.stop()
    ├── evo_scheduler.stop()
    └── orchestrator.shutdown()
```

---

## 16. WebSocket 通信流程

```
前端连接 /agent/{agent_id}
    │
    ▼
订阅所有事件 (subscribe_wildcard)
    │
    ▼
消息循环:
    │
    ├── type: message
    │     └── orchestrator.run_agent() → 流式响应
    │
    ├── type: ask_user_answer
    │     └── agen.asend(answer) 恢复生成器
    │
    └── type: file_upload
          └── 保存文件 → 转换为用户消息
```

---

## 17. 流程层级分类

| 层级 | 描述 | 包含流程 |
|------|------|----------|
| L5 | 用户交互层 | Web UI、WebSocket、文件上传 |
| L4 | 对话管理层 | 会话、上下文、状态 |
| L3 | 认知处理层 | 5阶段管道、执行、反思 |
| L2 | 进化学习层 | 进化引擎、调度器 |
| L1 | 基础设施层 | 记忆、事件、工具、LLM路由 |
| L0 | 系统支撑层 | Web服务、持久化、日志 |

---

## 附录: 关键文件清单

| 模块 | 文件路径 |
|------|----------|
| 核心循环 | `xmclaw/core/agent_loop.py` |
| 任务分类 | `xmclaw/core/task_classifier.py` |
| 信息收集 | `xmclaw/core/info_gather.py` |
| 任务规划 | `xmclaw/core/task_planner.py` |
| 技能匹配 | `xmclaw/core/skill_matcher.py` |
| 编排器 | `xmclaw/core/orchestrator.py` |
| 反思 | `xmclaw/core/reflection.py` |
| 事件总线 | `xmclaw/core/event_bus.py` |
| 进化引擎 | `xmclaw/evolution/engine.py` |
| 调度器 | `xmclaw/evolution/scheduler.py` |
| 基因锻造 | `xmclaw/evolution/gene_forge.py` |
| 技能锻造 | `xmclaw/evolution/skill_forge.py` |
| 记忆管理 | `xmclaw/memory/manager.py` |
| 会话管理 | `xmclaw/memory/session_manager.py` |
| 向量存储 | `xmclaw/memory/vector_store.py` |
| 工具注册 | `xmclaw/tools/registry.py` |
| Web服务 | `xmclaw/daemon/server.py` |
| 集成管理 | `xmclaw/integrations/manager.py` |
| LLM路由 | `xmclaw/llm/router.py` |

---

*文档生成日期: 2026-04-18*
*XMclaw 项目流程系统文档 v1.0*
