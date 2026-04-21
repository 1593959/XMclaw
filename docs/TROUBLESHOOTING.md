---
summary: "Common problems and solutions"
title: "故障排查"
---

# 故障排查

运行 XMclaw 时遇到问题？先运行 `xmclaw doctor` 诊断，常见问题及解决方案如下。

---

## 快速诊断

```bash
# 先运行诊断命令
xmclaw doctor

# 查看详细日志
ls xmclaw/logs/
tail -100 xmclaw/logs/xmclaw.log
```

---

## 常见问题

### 启动问题

#### 端口已被占用（Port 8765 already in use）

```
Error: Permission denied or port already in use
```

**原因**：8765 端口已被其他进程占用。

**解决**：
```bash
# Windows: 查找占用端口的进程
netstat -ano | findstr 8765
taskkill /PID <进程ID> /F

# Linux/macOS
lsof -i :8765
kill -9 <进程ID>
```

或者修改端口：
```bash
xmclaw config set gateway.port 8765
xmclaw start
```

#### 配置文件不存在（config.json not found）

```
Config file not found
```

**原因**：首次运行没有配置文件。

**解决**：
```bash
xmclaw config init
# 按提示输入 API Key
xmclaw start
```

#### Python 版本不兼容

```
Python 3.x required, found x.x
```

**原因**：Python 版本低于 3.10。

**解决**：
```bash
# 安装 Python 3.11+
# Windows: https://www.python.org/downloads/
# macOS: brew install python@3.11
# Linux: apt install python3.11
python3.11 -m venv .venv
```

---

### LLM 连接问题

#### Anthropic API Key 无效

```
Error: 401 Unauthorized
```

**原因**：API Key 填写错误或已失效。

**解决**：
1. 访问 https://console.anthropic.com/settings/keys
2. 复制新 Key
3. 运行 `xmclaw config init` 重新配置

#### Anthropic API Key 为空

```
[错误：未配置 Anthropic API Key]
请运行 xmclaw config init 或编辑 daemon/config.json → llm.anthropic.api_key
```

**原因**：未配置 API Key。

**解决**：
```bash
xmclaw config init
```

或直接编辑 `daemon/config.json`：
```json
"anthropic": {
  "api_key": "sk-ant-your-key-here",
  "default_model": "claude-sonnet-4-20250514"
}
```

#### OpenAI API Key 无效

```
Error: 401 Incorrect API key provided
```

**解决**：同上，从 https://platform.openai.com/api-keys 获取新 Key。

#### 网络请求超时

```
TimeoutError: Connection timed out
```

**原因**：网络问题或代理配置错误。

**解决**：
```bash
# 设置代理（根据你的网络环境）
export HTTP_PROXY=http://127.0.0.1:7890
export HTTPS_PROXY=http://127.0.0.1:7890

# 或在 config.json 中配置 base_url
xmclaw config set llm.openai.base_url https://api.openai.com/v1
```

---

### 工具执行问题

#### Playwright 浏览器未安装

```
playwright._impl错了.D.Error: Executable doesn't exist
```

**解决**：
```bash
pip install playwright
playwright install chromium
```

#### Bash 命令执行失败

```
Bash tool execution error: command not found
```

**原因**：命令路径未配置或权限不足。

**解决**：
```bash
# Windows: 确保命令在 PATH 中
# WSL: 确保 .bashrc 中有 export PATH=...
```

#### 文件操作权限错误

```
Permission denied: /path/to/file
```

**解决**：
```bash
# Windows (PowerShell)
icacls "C:\path\to\file" /grant Everyone:F

# Linux/macOS
chmod 644 /path/to/file
```

---

### 进化系统问题

#### 进化循环无洞察数据

```
evolution_no_insights insights: 0
```

**原因**：会话历史中工具调用数据不足（需要至少 2 次相同工具调用）。

**解决**：
1. 进行一些实际对话（使用工具），让系统积累数据
2. 进化每 30 分钟运行一次，无需干预
3. 查看历史会话：`xmclaw memory-search "tool usage"`

#### 生成的技能语法错误

```
SyntaxError: unexpected indent
```

**原因**：自动生成的代码有缺陷，VFM 验证未能拦截。

**解决**：
```bash
# 查看失败技能
ls shared/skills/skill_*.py

# 删除有问题的技能
rm shared/skills/skill_abc123.py

# 手动验证语法
python3 -m py_compile shared/skills/skill_abc123.py
```

#### VFM 评分阈值过高

生成的技能/基因全部被拒绝。

**解决**：
```bash
# 降低阈值
xmclaw config set evolution.vfm_threshold 4.0

# 暂时禁用评分
xmclaw config set evolution.vfm_threshold 0
```

---

### Web UI 问题

#### WebSocket 连接失败

```
WebSocket error
```

**原因**：守护进程未启动或端口不对。

**解决**：
```bash
xmclaw status   # 确认守护进程运行中
xmclaw start   # 如果未运行，启动
```

浏览器访问 http://127.0.0.1:8765

#### 页面空白

**解决**：
1. 清除浏览器缓存
2. 强制刷新：Ctrl+Shift+R
3. 确认无广告拦截插件干扰

---

### 集成问题

#### Slack Bot 无法连接

```
slack_tokens_missing
```

**原因**：未配置 Slack Bot Token。

**解决**：
1. 在 https://api.slack.com/apps 创建 App
2. 启用 Socket Mode，获取 `xapp-...` App Token
3. 添加 Bot Token Scopes，Install to Workspace
4. 配置：
```bash
xmclaw config init
# 或直接编辑 daemon/config.json
```

#### Telegram Bot 无响应

**原因**：Bot Token 未配置或未设置 Webhook。

**解决**：
1. @BotFather 创建 Bot，获取 Token
2. 编辑 `daemon/config.json`：
```json
"telegram": {
  "enabled": true,
  "bot_token": "123456:ABC-DEF...",
  "chat_id": ""
}
```
3. 重启守护进程

---

### 数据问题

#### 会话历史丢失

**原因**：`session_retention_days` 配置过短。

**解决**：
```bash
xmclaw config set memory.session_retention_days 30
```

#### 向量数据库损坏

```
sqlite3.OperationalError: database is locked
```

**解决**：
```bash
# 删除损坏的数据库，重新初始化
rm -rf shared/vector_db/
rm shared/memory.db
xmclaw start
```

---

## 环境变量

常用环境变量：

| 变量 | 说明 | 示例 |
|------|------|------|
| `XMC__llm__anthropic__api_key` | Anthropic Key | `sk-ant-...` |
| `XMC__llm__openai__api_key` | OpenAI Key | `sk-...` |
| `XMC__evolution__enabled` | 禁用进化 | `false` |
| `XMC__gateway__port` | 改端口 | `8765` |
| `XMC_SECRET_KEY` | 加密密钥（可选） | 任意字符串 |
| `HTTP_PROXY` / `HTTPS_PROXY` | 网络代理 | `http://...` |

---

## 获取帮助

1. 运行 `xmclaw doctor` 查看诊断信息
2. 查看日志：`tail xmclaw/logs/xmclaw.log`
3. 查看 Evolution 状态：`xmclaw evolution-status`
4. 提交 Issue：https://github.com/1593959/XMclaw/issues

