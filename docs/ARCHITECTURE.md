# XMclaw 架构设计

## 整体架构

XMclaw 采用 **分层架构 + 事件驱动** 的设计：

```
┌─────────────────────────────────────────────────────────────┐
│                      交互层 (Presentation)                   │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐  │
│  │  Desktop    │  │   Web UI    │  │       CLI           │  │
│  │  (PySide6)  │  │  (Agent OS) │  │   (Rich + Typer)    │  │
│  └──────┬──────┘  └──────┬──────┘  └──────────┬──────────┘  │
└─────────┼────────────────┼────────────────────┼─────────────┘
          │                │                    │
          └────────────────┴────────────────────┘
                             │
                    WebSocket (ws://localhost:8765)
                             │
┌────────────────────────────┼─────────────────────────────────┐
│                      网关层 (Gateway)                        │
│              WebSocketGateway / FastAPI Server               │
└────────────────────────────┼─────────────────────────────────┘
                             │
┌────────────────────────────┼─────────────────────────────────┐
│                      核心层 (Core)                           │
│  ┌─────────────────────────────────────────────────────────┐ │
│  │              AgentOrchestrator                          │ │
│  │   ┌─────────┐  ┌─────────────┐  ┌─────────────────┐    │ │
│  │   │ AgentLoop│← │ LLMRouter   │  │ ToolRegistry    │    │ │
│  │   └────┬────┘  └─────────────┘  └─────────────────┘    │ │
│  │        │                                                 │ │
│  │   ┌────┴────┐  ┌─────────────┐  ┌─────────────────┐    │ │
│  │   │PromptBuilder│ │MemoryManager│  │ ReflectionEngine│    │ │
│  │   └─────────┘  └─────────────┘  └─────────────────┘    │ │
│  └─────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────┘
                             │
┌────────────────────────────┼─────────────────────────────────┐
│                      扩展层 (Extensions)                     │
│  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌────────┐ │
│  │  Tools  │ │ Memory  │ │Evolution│ │  Genes  │ │  MCP   │ │
│  └─────────┘ └─────────┘ └─────────┘ └─────────┘ └────────┘ │
└──────────────────────────────────────────────────────────────┘
```

---

## 数据流

### 1. 用户请求流

```
用户输入
    │
    ▼
Desktop/Web/CLI ──WebSocket──► Daemon Server
    │                              │
    │                              ▼
    │                      AgentOrchestrator
    │                              │
    │                              ▼
    │                         AgentLoop.run()
    │                              │
    │              ┌───────────────┼───────────────┐
    │              ▼               ▼               ▼
    │         PromptBuilder    LLMRouter      ToolRegistry
    │              │               │               │
    │              ▼               ▼               ▼
    │         System Prompt    Stream Chunks   Tool Execution
    │                              │               │
    │                              └───────┬───────┘
    │                                      │
    │                              MemoryManager.save_turn()
    │                                      │
    │                              ReflectionEngine.reflect()
    │                                      │
    ◄──────────────────────────────────────┘
                              WebSocket Response
```

### 2. 自主进化流

```
对话历史 (JSONL)
    │
    ▼
EvolutionEngine.observe()
    │
    ├──► PatternDetector → 意图/模式统计
    │
    ├──► TrendAnalyzer → 高频需求/痛点
    │
    ├──► InsightExtractor → 结构化洞察
    │
    ▼
EvolutionEngine.evolve()
    │
    ├──► GeneForge.generate() → Gene 代码
    │       │
    │       ▼
    │   EvolutionValidator.run() → 验证
    │       │
    │       ▼
    │   GeneManager.register() → 注册
    │
    ├──► SkillForge.generate() → Skill 代码
    │       │
    │       ▼
    │   EvolutionValidator.run() → 验证
    │       │
    │       ▼
    │   ToolRegistry._load_generated_skills() → 热重载
    │
    ▼
MemoryManager.save_insight() → 长期记忆
```

---

## 控制流

### AgentLoop 单次迭代

```python
while turn < max_turns:
    1. LLM stream → 接收思考过程文本
    2. 解析 tool calls (<function>...</function>)
    3. 如果没有 tool calls:
         - 保存对话回合
         - break
    4. 执行每个 tool:
         - 调用 ToolRegistry.execute()
         - 发送 tool_result 事件到客户端
         - 检测自修改 (文件/代码变更)
         - 如果是 ask_user → 暂停，等待用户回复
    5. 将观察结果追加到 messages
    6. 进入下一轮

# 循环结束后
7. CostTracker 统计 token 消耗
8. ReflectionEngine.reflect() 自动反思
9. 发送 done 事件
```

### WebSocket 消息协议

| 消息类型 | 方向 | 说明 |
|---------|------|------|
| `chunk` | Server → Client | 流式文本片段 |
| `state` | Server → Client | 状态更新 (THINKING, TOOL_CALL, WAITING 等) |
| `tool_call` | Server → Client | 工具调用通知 |
| `tool_result` | Server → Client | 工具执行结果 |
| `ask_user` | Server → Client | 暂停等待用户确认 |
| `reflection` | Server → Client | 反思结果 |
| `cost` | Server → Client | Token/Cost 统计 |
| `done` | Server → Client | 本轮结束 |
| `error` | Server → Client | 错误信息 |

---

## 关键模块职责

### `daemon/`
- `server.py`: FastAPI + WebSocket 服务端，处理所有客户端连接
- `lifecycle.py`: Daemon 启动/停止/状态管理
- `config.py`: 全局配置加载

### `core/`
- `agent_loop.py`: 核心智能体循环（思考-行动-观察）
- `orchestrator.py`: Agent 实例管理器
- `prompt_builder.py`: 系统提示词构建（含 Gene 注入）
- `reflection.py`: 对话反思引擎
- `cost_tracker.py`: Token 消耗统计

### `llm/`
- `router.py`: 统一路由到 OpenAI 或 Anthropic 客户端
- `clients/`: 各 LLM 提供商的具体实现

### `tools/`
- `registry.py`: 工具注册表，支持热重载生成 Skill
- `base.py`: Tool 抽象基类
- 内置工具: `file_read/write/edit`, `bash`, `browser`, `web_search/fetch`, `todo`, `task`, `ask_user`, `agent`, `skill`, `memory_search`, `glob`, `grep`, `git`, `computer_use`, `test`, `mcp`

### `memory/`
- `manager.py`: 统一记忆管理入口
- `sqlite_store.py`: 结构化数据存储
- `session_manager.py`: 对话会话 JSONL 存储
- `vector_store.py`: SQLite-vec + LLM Embedding 向量检索

### `evolution/`
- `engine.py`: 进化引擎主控
- `gene_forge.py`: Gene 代码生成
- `skill_forge.py`: Skill 代码生成
- `validator.py`: 进化产物验证器
- `vfm.py`: 价值函数模型评分

### `genes/`
- `manager.py`: Gene 匹配与注入
- `base.py`: Gene 抽象基类

### `desktop/`
- `app.py`: PySide6 应用入口
- `main_window.py`: Agent OS 主窗口（6 个视图）
- `ws_client.py`: WebSocket QThread 客户端

### `cli/`
- `main.py`: Typer CLI 入口
- `client.py`: WebSocket CLI 客户端
- `rich_ui.py`: Rich 渲染组件

### `web/`
- `index.html`: Agent OS Web UI
- `main.js`: 前端逻辑与 WebSocket 处理
