# XMclaw 工具系统

XMclaw 的工具系统采用 **注册表 + 热重载** 架构。所有工具都继承自 `Tool` 基类，通过 `ToolRegistry` 统一管理。

---

## 内置工具列表

### 文件操作

| 工具名 | 功能 | 示例 |
|--------|------|------|
| `file_read` | 读取文件内容 | 读取代码、配置、日志 |
| `file_write` | 写入/覆盖文件 | 生成新文件 |
| `file_edit` | 局部修改文件 | 替换特定文本片段 |
| `glob` | 按模式搜索文件 | `**/*.py` |
| `grep` | 按内容搜索文件 | 在项目中查找函数定义 |

### 系统与网络

| 工具名 | 功能 | 示例 |
|--------|------|------|
| `bash` | 执行 shell 命令 | 运行脚本、安装依赖 |
| `browser` | 浏览器自动化 | 打开网页、点击、截图 |
| `web_search` | 网络搜索 | 查文档、查新闻 |
| `web_fetch` | 抓取网页内容 | 获取 API 文档 |

### 任务与协作

| 工具名 | 功能 | 示例 |
|--------|------|------|
| `todo` | 待办事项管理 | 添加、完成、查看待办 |
| `task` | 任务系统 | 创建长期追踪的任务 |
| `ask_user` | 向用户确认 | 关键操作前暂停询问 |
| `agent` | 子 Agent | 委派子任务给专用 Agent |
| `skill` | 动态加载 Skill | 调用生成的高级技能 |

### 记忆与搜索

| 工具名 | 功能 | 示例 |
|--------|------|------|
| `memory_search` | 向量记忆搜索 | 查找历史经验和知识 |

### 开发与版本控制

| 工具名 | 功能 | 示例 |
|--------|------|------|
| `git` | Git 操作 | commit、push、status |
| `computer_use` | 桌面操控 | 截屏、点击、输入 |
| `test` | 测试生成与执行 | 自动生成 pytest |
| `mcp` | MCP Server 调用 | 连接外部工具生态 |

---

## 工具调用格式

LLM 输出工具调用时，使用以下 XML 格式：

```xml
<function>tool_name</function>
<arguments>
{
  "param1": "value1",
  "param2": "value2"
}
</arguments>
```

AgentLoop 会自动解析这段内容，并调用对应的工具。

---

## 扩展工具：自动生成 Skill

XMclaw 的进化引擎可以根据对话模式自动生成新的 Skill（高级工具）。生成的 Skill 以 Python 文件形式保存在 `shared/skills/skill_*.py` 中。

`ToolRegistry` 启动时会自动扫描并热重载这些 Skill，无需重启 Daemon。

### Skill 文件结构示例

```python
from xmclaw.tools.base import Tool

class MyGeneratedSkill(Tool):
    name = "my_skill"
    description = "Does something useful"
    parameters = {
        "input": {"type": "string", "description": "Input text"}
    }

    async def execute(self, input: str) -> str:
        return f"Result: {input}"
```

---

## 工具权限与安全

### BashTool 安全分级

- **危险命令拦截**: `rm -rf /`, `mkfs`, `dd` 等直接阻止
- **可疑命令警告**: `git push --force`, `curl | bash` 等需要确认
- **白名单机制**: 常用开发命令（`python`, `git`, `pip`）直接放行

### ComputerUse 安全

- 默认启用 `pyautogui.FAILSAFE`（鼠标移到屏幕角落可中断）
- 所有坐标操作需要显式指定 x, y

### Browser 安全

- 默认无头模式运行
- 涉及 CDP（远程调试）时不会自动启用，需要用户明确同意
