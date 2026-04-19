# 贡献指南

感谢你考虑为 XMclaw 做出贡献！本文档说明了如何设置开发环境、提交代码和规范。

---

## 开发环境

### 前置依赖

- Python 3.11+
- Node.js 20+ (用于 Web UI 构建)
- Git
- Windows 10+ (当前主要目标平台)

### 快速开始

```bash
# 克隆仓库
git clone https://github.com/YOUR_USERNAME/XMclaw.git
cd XMclaw

# 创建虚拟环境
python -m venv .venv
.venv\Scripts\activate

# 安装依赖
pip install -e .

# 首次运行：daemon/config.json 会自动生成（包含默认配置）
# 编辑 daemon/config.json 填入你的 LLM API Key，例如：
#   llm.anthropic.api_key = "sk-ant-..."
#   llm.openai.api_key = "sk-..."

# 验证安装
xmclaw status
```

### 运行测试

```bash
# 运行所有测试
python -m pytest tests/ -v

# 运行特定模块测试
python -m pytest tests/test_orchestrator.py -v

# 带覆盖率报告
python -m pytest tests/ --cov=xmclaw --cov-report=term-missing
```

---

## 项目结构

```
xmclaw/
├── core/              # 核心运行时
│   ├── agent_loop.py  # 主 Agent 循环
│   ├── orchestrator.py  # 多代理编排
│   └── event_bus.py  # 事件总线
├── llm/               # LLM 路由（OpenAI + Anthropic）
├── tools/             # 工具注册表与实现
├── memory/            # 记忆系统（向量 + SQLite）
├── genes/             # Gene 管理
├── evolution/         # 自主进化引擎
├── daemon/            # 后台守护进程
├── desktop/           # PySide6 桌面应用
├── web/               # Web UI (HTML/CSS/JS)
└── cli/               # CLI 命令行界面
```

---

## 分支策略

| 分支 | 用途 |
|------|------|
| `main` | 稳定发布版本 |
| `develop` | 开发中的下一版本 |
| `feature/*` | 新功能开发 |
| `fix/*` | Bug 修复 |

**工作流程：**
1. 从 `main` 或 `develop` 创建新分支
2. 编写代码和测试
3. 提交时运行 `python -m pytest tests/`
4. 创建 Pull Request

---

## 代码规范

### Python

- 使用 **Ruff** 检查和格式化代码：`ruff check xmclaw/ --fix`（需要 `pip install -e ".[dev]"`）
- 使用 **MyPy** 做类型检查：`mypy xmclaw/`（需要 `pip install -e ".[dev]"`）
- 类型注解：所有公共函数必须标注返回类型
- Docstring：公共函数和方法必须有 docstring

```python
async def run_agent(self, agent_id: str, user_input: str):
    """
    Run a single agent and yield response chunks.

    Args:
        agent_id: Unique identifier for this agent
        user_input: The user's message

    Yields:
        Response chunks from the agent
    """
```

### Git 提交规范

使用 [Conventional Commits](https://www.conventionalcommits.org/)：

```
feat: add multi-agent team creation
fix: correct desktop port 8080 -> 8765
docs: add CONTRIBUTING.md
refactor: simplify tool registry initialization
test: add orchestrator unit tests
chore: cleanup stub directories
```

### API 设计原则

1. **不要破坏向后兼容** — 公共 API 变更需要 major version bump
2. **错误处理** — 所有异步操作必须 try/except，错误记录到日志
3. **不要硬编码** — 配置通过 `agent.json` 或环境变量，不写死在代码里

---

## 安全规则

- **API Key 绝对不能提交** — 使用 `agent.json` 模板（不含真实 Key）
- **敏感数据** — Cookie、Token、密码必须通过环境变量或安全存储
- **外部网络请求** — 所有 HTTP 调用必须设置合理的 timeout

---

## 新增工具/技能

### 添加新工具

1. 在 `xmclaw/tools/` 创建 `my_tool.py`
2. 实现 `BaseTool` 接口
3. 在 `xmclaw/tools/__init__.py` 注册
4. 添加单元测试到 `tests/test_tools.py`

```python
from xmclaw.tools.base import BaseTool

class MyTool(BaseTool):
    name = "my_tool"
    description = "What this tool does"

    async def execute(self, arg1: str, **kwargs) -> str:
        return f"Did: {arg1}"
```

### 添加新 CLI 命令

在 `xmclaw/cli/main.py` 中添加 typer 命令。

---

## 测试策略

| 类型 | 位置 | 说明 |
|------|------|------|
| 单元测试 | `tests/test_*.py` | 每个模块独立测试 |
| 集成测试 | `tests/integration/` | 多模块协作测试 |
| 端到端 | 手动验证 | Desktop UI、CLI |

**覆盖率要求：** 核心模块（core/, tools/）≥ 70%

---

## 提交代码

```bash
# 1. 确保所有测试通过
python -m pytest tests/ -q

# 2. 提交（使用 Conventional Commits）
git add .
git commit -m "feat: add gene list CLI command"

# 3. 推送
git push origin feature/my-feature

# 4. 创建 Pull Request
```

---

## 问题反馈

发现 Bug？请通过以下方式报告：

1. 搜索现有 Issue，看是否已有人报告
2. 如无，创建新 Issue，包含：
   - 复现步骤
   - 环境信息（OS、Python 版本）
   - 错误日志
   - 预期 vs 实际行为

---

*XMclaw — 本地优先，自主进化。*
