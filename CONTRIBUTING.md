# 贡献指南

感谢你考虑为 XMclaw 做出贡献！本文档说明了如何设置开发环境、提交代码和规范。

---

## 开发环境

### 前置依赖

- Python 3.10+（见 `pyproject.toml`）
- Git
- Windows 10+/11 是主要开发平台；macOS / Linux 由 CI matrix 跟进
- 可选：`playwright install chromium`（browser tools）、`pyautogui` + `mss`（computer-use）
- 不需要 Node.js。Web UI 在 `xmclaw/daemon/static/`（vanilla HTML/CSS/JS），改完刷浏览器即可

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
xmclaw doctor
```

### 运行测试

```bash
# 全量测试套件（较慢）
python -m pytest tests/ -v

# 只跑受本次改动影响的 lane（Epic #11 smart-gate）
python scripts/test_changed.py --dry-run   # 先看会跑什么
python scripts/test_changed.py             # 实际执行

# 强制全量
python scripts/test_changed.py --all

# 带覆盖率报告
python -m pytest tests/ --cov=xmclaw --cov-report=term-missing
```

Smart-gate lane 映射表在 [`scripts/test_lanes.yaml`](scripts/test_lanes.yaml)；改动某个子包 → 对应 lane 的测试自动跑。

---

## 项目结构

```
xmclaw/
├── core/            # Bus / IR / grader / scheduler / evolution       → core/AGENTS.md
├── daemon/          # FastAPI app + AgentLoop + factory + lifecycle   → daemon/AGENTS.md
├── providers/       # LLM / tool / memory / runtime / channel 适配器  → providers/AGENTS.md
├── security/        # Prompt-injection scanner + policy gate          → security/AGENTS.md
├── skills/          # SkillBase + registry + demo skills              → skills/AGENTS.md
├── cli/             # xmclaw 入口 + doctor                            → cli/AGENTS.md
├── utils/           # paths / log / redact / cost（DAG 最底层）        → utils/AGENTS.md
└── plugins/         # 第三方 plugin 加载（Epic #2 WIP）
```

每个子目录都有自己的 `AGENTS.md`，说明职责、依赖规则、测试入口、禁止项。**动代码前先读**。

runtime 数据（events.db / memory.db / daemon.pid / pairing_token.txt）在 `~/.xmclaw/v2/`——**不在仓库里**，见 [docs/WORKSPACE.md](docs/WORKSPACE.md)。

---

## 分支策略

| 分支             | 用途                                   |
| ---------------- | -------------------------------------- |
| `main`           | 发布版本；**不允许**直接 push          |
| `feat/*`, `fix/*`, `docs/*`, `chore/*` | 功能 / 修复 / 文档 / 杂项分支 |

**工作流程：**
1. 从 `main` 切出 `feat/<topic>` 或 `fix/<bug>` 分支
2. 编写代码 + 测试；遵守对应 AGENTS.md 的依赖规则
3. 本地跑 `python scripts/test_changed.py` 让 smart-gate 确认改动不破 lane
4. 提交时引用 Epic 号：`Epic #<n>: ...` / `Epic #<n> partial: ...`（见 [CLAUDE.md § 开发纪律](CLAUDE.md)）
5. `gh pr create` 开 PR；PR CI 跑 smart-gate，merge 到 main 后跑全量

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

1. **事件是契约** — 客户端只消费 [docs/EVENTS.md](docs/EVENTS.md) 里声明的字段；新增事件类型要同步改 `xmclaw/core/bus/events.py` + 文档
2. **BehavioralEvent schema 破坏性变更走 major bump** —— 规则在 [docs/V2_DEVELOPMENT.md §4.3](docs/V2_DEVELOPMENT.md#43-schema-管理)
3. **错误 surface 成结构化结果** — `ToolResult(error=...)` / `GraderVerdict(ran=False)` / `ANTI_REQ_VIOLATION` 事件；不要吞异常然后返成功
4. **配置不硬编码** — secrets 走 `daemon/config.json`（gitignored）或 `XMC__*` 环境变量

---

## 安全规则

- **API Key 绝对不能提交** — `daemon/config.json` 是 gitignored；用 `daemon/config.example.json` 作模板
- **敏感数据** — Cookie、Token、密码必须通过环境变量（`XMC__llm__anthropic__api_key` 形式）或外部 secret store
- **外部网络请求** — 所有 HTTP 调用必须设置合理的 timeout
- **prompt-injection 扫描** — 工具返回值、agent profile、memory recall 都要过 `xmclaw.security.prompt_scanner`，绕过会让 anti-req #14 失效

---

## 新增工具 / Skill

### 添加新 ToolProvider

完整协议见 [docs/TOOLS.md § 4](docs/TOOLS.md#4-写一个-toolprovider)。最短路径：

1. 在 `xmclaw/providers/tool/` 新增 `my_provider.py`，实现 `ToolProvider` ABC（`list_tools()` + `async invoke()`）
2. 在 `xmclaw/daemon/factory.py` 的 `build_tool_provider` 里按 config 条件 append 到 composite
3. 加测试到 `tests/unit/test_v2_<name>_tools.py`
4. 把测试文件登录到 `scripts/test_lanes.yaml` 的 `tools` lane
5. 更新 [docs/TOOLS.md](docs/TOOLS.md) 的工具清单

```python
from xmclaw.core.ir import ToolCall, ToolResult, ToolSpec
from xmclaw.providers.tool.base import ToolProvider


class EchoProvider(ToolProvider):
    def list_tools(self) -> list[ToolSpec]:
        return [ToolSpec(
            name="echo",
            description="Return the input text unchanged.",
            parameters_schema={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        )]

    async def invoke(self, call: ToolCall) -> ToolResult:
        return ToolResult(
            call_id=call.id, ok=True,
            content=call.args.get("text", ""),
        )
```

### 添加新 Skill

实现 `xmclaw.skills.base.SkillBase` 子类 + `skill.yaml` manifest，注册到 `SkillRegistry`。Promotion / rollback 由 `SkillScheduler` + `EvolutionController` 基于 grader 证据自动决策——**不要**自己写 "skill 永远上线" 的 shortcut。协议见 [`xmclaw/skills/AGENTS.md`](xmclaw/skills/AGENTS.md) 和 [docs/V2_DEVELOPMENT.md §3](docs/V2_DEVELOPMENT.md)。

### 添加新 CLI 命令

在 `xmclaw/cli/main.py` 中添加 typer 命令；遵守 [`xmclaw/cli/AGENTS.md`](xmclaw/cli/AGENTS.md)。

### 添加新 doctor check

完整扩展协议见 [docs/DOCTOR.md § 扩展：自己写一个 check](docs/DOCTOR.md#扩展自己写一个-check)——走 `entry_points` 的 `xmclaw.doctor` 组。

---

## 测试策略

| 类型       | 位置                             | 说明                          |
| ---------- | -------------------------------- | ----------------------------- |
| 单元测试   | `tests/unit/test_v2_*.py`        | 单文件 / 单模块               |
| 集成测试   | `tests/integration/test_v2_*.py` | 多模块协作、端到端            |
| Conformance | `tests/conformance/*.py`         | IR 翻译器双向 fuzz            |
| Smart-gate | 由 `scripts/test_lanes.yaml` 映射 | 改动文件 → 对应 lane 测试自动跑 |

核心模块覆盖率目标 ≥ 80%；bus / IR / grader / scheduler 无覆盖下降即为 PR 阻塞项。

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
