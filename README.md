# XMclaw

**敬宇专属 Agent 运行时** — 对标 OpenClaw / Hermes Agent / Claude Code，完全独立开发。

XMclaw 是一个本地优先、自主进化、具备人格化交互的 AI Agent 操作系统。它不只是聊天机器人，而是一个能思考、能行动、能学习、能自我改进的智能体运行时。

---

## 核心特性

| 特性 | 说明 |
|------|------|
| **本地优先** | 所有数据、记忆、配置都保存在本地，隐私完全可控 |
| **双 LLM 端口** | 同时支持 OpenAI 兼容 API 和 Anthropic Claude API |
| **原生桌面应用** | PySide6 构建的 Agent OS 仪表盘，非浏览器页面 |
| **自主进化** | 对话分析 → Gene/Skill 自动生成 → 验证 → 注册 → 热重载 完整闭环 |
| **Plan 模式** | 复杂任务先制定计划，再逐步执行，用户可随时确认 |
| **任务系统** | 支持多任务并行追踪，Task 面板可视化 |
| **ask_user 暂停** | 关键操作自动暂停等待用户确认，安全可控 |
| **向量记忆** | SQLite-vec + LLM Embedding，长期记忆可检索 |
| **Reflection 反思** | 每次对话结束后自动反思，提取教训和改进建议 |
| **Computer Use** | 桌面截屏、鼠标点击、键盘输入，操控本地应用 |
| **自动测试** | 自动生成 pytest 单元测试并执行，验证代码修改 |
| **MCP 集成** | 支持 Model Context Protocol，连接外部 MCP Servers |
| **Git 集成** | 代码修改后一键 commit/push，版本可控 |
| **流式对话** | WebSocket 实时流式输出，体验丝滑 |

---

## 快速开始

### 安装

```bash
# 克隆项目
cd XMclaw

# 安装依赖
pip install -e .

# 可选：安装 Computer Use 依赖
pip install pyautogui mss Pillow

# 可选：安装 MCP 依赖
pip install mcp
```

### 启动

```bash
# 启动 Daemon（后台服务）
xmclaw start

# 打开桌面应用
python -m xmclaw.desktop.app

# 或使用 CLI 聊天
xmclaw chat

# 计划模式聊天
xmclaw chat --plan

# 停止 Daemon
xmclaw stop
```

### 配置 LLM

编辑 `agents/default/agent.json`：

```json
{
  "llm": {
    "default_provider": "anthropic",
    "anthropic": {
      "default_model": "claude-3-5-sonnet-20241022",
      "api_key": "sk-...",
      "base_url": "https://api.anthropic.com/v1"
    }
  }
}
```

---

## 项目结构

```
XMclaw/
├── xmclaw/
│   ├── daemon/          # FastAPI + WebSocket 服务端
│   ├── gateway/         # WebSocket 客户端抽象
│   ├── core/            # AgentLoop、Orchestrator、PromptBuilder、Reflection
│   ├── llm/             # LLM 路由（OpenAI / Anthropic）
│   ├── tools/           # 工具注册表与内置工具
│   ├── memory/          # 记忆管理（SQLite + JSONL + 向量）
│   ├── evolution/       # 自主进化引擎（Gene/Skill 生成）
│   ├── genes/           # Gene 管理系统
│   ├── desktop/         # PySide6 桌面应用
│   ├── cli/             # 命令行工具
│   └── web/             # Agent OS Web UI
├── agents/              # 各 Agent 的数据目录
├── shared/              # 共享数据（genes、skills、memory.db）
├── tests/               # 单元测试
├── README.md
└── docs/
    ├── ARCHITECTURE.md
    ├── TOOLS.md
    ├── EVOLUTION.md
    ├── DESKTOP.md
    └── CLI.md
```

---

## 文档索引

- [架构设计](docs/ARCHITECTURE.md) — 系统架构、数据流、控制流
- [工具系统](docs/TOOLS.md) — 所有内置工具说明和扩展方式
- [自主进化](docs/EVOLUTION.md) — Gene/Skill 进化机制详解
- [桌面应用](docs/DESKTOP.md) — Agent OS 仪表盘使用指南
- [CLI 工具](docs/CLI.md) — 命令行完整使用手册

---

## 开发原则

1. **不能慢、不能记不住事** — 性能与记忆是底线
2. **每次修改立即 git commit** — 版本可控，随时回滚
3. **先 CLI 后 GUI 后语音** — 渐进式交互扩展
4. **模块之间连接必须明确** — 数据流/控制流清晰可追踪
5. **自动验证、自动反思、自动进化** — 闭环自增强

---

## License

MIT — 敬宇专属，自由使用。
