# XMclaw CLI 使用手册

XMclaw 提供完整的命令行界面，基于 **Typer** 和 **Rich** 构建。

---

## 安装与入口

安装项目后，CLI 命令 `xmclaw` 即可使用：

```bash
pip install -e .
xmclaw --help
```

---

## 核心命令

### 服务管理

```bash
# 启动 Daemon
xmclaw start

# 停止 Daemon
xmclaw stop

# 查看 Daemon 状态
xmclaw status
```

### 聊天交互

```bash
# 进入交互式聊天
xmclaw chat

# 指定 Agent
xmclaw chat --agent myagent

# 开启计划模式
xmclaw chat --plan
```

聊天中支持的特殊输入：
- `/quit` 或 `/exit` — 退出聊天
- 普通消息 — 发送给 Agent
- 当 Agent 弹出 `ask_user` 时，直接输入回复即可

### 任务管理

```bash
# 列出所有任务
xmclaw task-list

# 创建新任务
xmclaw task-create "实现用户登录" --description "添加 JWT 认证和登录页面"
```

### 进化状态

```bash
# 查看 Gene 和 Skill 数量
xmclaw evolution-status
```

### 记忆搜索

```bash
# 搜索记忆文件
xmclaw memory-search "数据库配置"

# 指定 Agent
xmclaw memory-search "项目进度" --agent default
```

### 配置查看

```bash
# 显示当前 Agent 配置
xmclaw config-show
```

---

## 消息类型展示

CLI 支持完整的 WebSocket 协议，不同类型的消息会有不同的 Rich 渲染效果：

| 消息类型 | CLI 展示 |
|---------|---------|
| `chunk` | 流式文本输出 |
| `state` | 灰色斜体状态提示 |
| `tool_result` | 黄色边框 Panel |
| `ask_user` | 洋红色边框确认 Panel |
| `reflection` | 青色边框 Reflection Panel |
| `error` | 红色错误文本 |

---

## 计划模式使用示例

```bash
$ xmclaw chat --plan
You: 帮我重构用户模块，添加缓存和日志
[State: PLANNING | 计划模式已开启，正在构建执行计划...]
[Agent 输出计划...]
[ask_user] XMclaw 询问: 是否按以上计划执行？
You: 可以，但第3步先跳过
[Agent 开始执行...]
```

---

## 开发调试

```bash
# 直接运行 Python 模块
python -m xmclaw.cli.main start
python -m xmclaw.cli.main chat
```
