# M4 协同任务样例 — Android Companion 端到端场景

> **Status**: Draft — 2026-06-18  
> **Scope**: 展示手机端 Companion 与 PC 端 Agent 的跨设备协同能力，覆盖验证码流转、图文协作、记忆沉淀、跨屏查询、安全审批五条主线。  
> **前置**: 需完成 M4 全部工具接入（`phone_open_app` / `phone_click` / `phone_tap` / `phone_input` / `phone_swipe` / `phone_key` / `phone_screenshot` / `phone_ui_tree` / `phone_notification` / `phone_wait` / `phone_clipboard_get` / `phone_clipboard_set`）。

---

## 示例 1：跨设备复制验证码

**场景**：用户在电脑端登录网银，需要短信验证码；手机刚收到银行短信。

**目标**：Agent 自动从手机通知提取验证码，填入电脑端表单。

### 时序图

```
┌─────────┐    ┌──────────┐    ┌──────────┐    ┌─────────┐
│  PC端    │    │  Agent   │    │  Daemon  │    │ 手机    │
│ 浏览器   │    │          │    │          │    │ Companion│
└────┬────┘    └────┬─────┘    └────┬─────┘    └────┬────┘
     │              │               │               │
     │ ① 用户请求"帮我填验证码"   │               │               │
     │──────────────>│               │               │
     │              │               │               │
     │              │ ② phone_notification          │
     │              │──────────────>│               │
     │              │               │────③ 下发通知栏指令──>│
     │              │               │               │
     │              │               │<────④ obs.tree（通知内容）──│
     │              │               │               │
     │              │ ⑤ redact_tree（脱敏）          │               │
     │              │ [DeviceRedactor]              │               │
     │              │               │               │
     │              │ ⑥ LLM OCR 提取验证码 123456   │               │
     │              │               │               │
     │ ⑦ 自动填入验证码  │               │               │
     │<──────────────│               │               │
     │              │               │               │
     │ ⑧ 提交表单成功 │               │               │
     │──────────────>│               │               │
     │              │               │               │
```

### 涉及的 phone_* 工具

| 工具 | 作用 |
|---|---|
| `phone_notification` | 拉下通知栏，让通知内容进入 UI 树 |
| `phone_ui_tree` | 读取通知栏中每条通知的 `text` 字段（含验证码） |

### 记忆/审批接入点

- **记忆**：无需写入长期记忆（验证码是一次性、短时效的）。
- **审批**：`phone_notification` 为 `read_only=False`（会改变手机屏幕状态），但属于低风险动作，通常无需 `tool_guard` 拦截；若策略要求，可走 `user.approval` 确认。

---

## 示例 2：手机发图 + 电脑写报告

**场景**：用户在外用手机拍了现场照片，想让 Agent 在电脑端 workspace 生成一份分析报告。

**目标**：Agent 读取手机截图，理解图像内容，在电脑端写入 Markdown 报告。

### 时序图

```
┌─────────┐    ┌──────────┐    ┌──────────┐    ┌─────────┐
│  用户   │    │  Agent   │    │  Daemon  │    │ 手机    │
│ (手机)  │    │          │    │          │    │ Companion│
└────┬────┘    └────┬─────┘    └────┬─────┘    └────┬────┘
     │              │               │               │
     │ ① "帮我写份现场报告"         │               │               │
     │ (从电脑端发话)               │               │               │
     │──────────────>│               │               │
     │              │               │               │
     │              │ ② phone_screenshot              │
     │              │──────────────>│               │
     │              │               │────③ 截图上传──>│
     │              │               │               │
     │              │               │<────④ obs.screenshot{url}──│
     │              │               │               │
     │              │ ⑤ attach_image_url → LLM 视觉分析 │               │
     │              │               │               │
     │              │ ⑥ 在电脑 workspace 写入 report.md │               │
     │              │               │               │
     │ ⑦ 返回报告路径 │               │               │
     │<──────────────│               │               │
     │              │               │               │
```

### 涉及的 phone_* 工具

| 工具 | 作用 |
|---|---|
| `phone_screenshot` | 捕获手机屏幕，通过 `attach_image_url` 让 LLM 看到图像 |

### 记忆/审批接入点

- **记忆**：
  - 写入 `UnifiedMemorySystem`：事件 `phone_screenshot` + 用户意图 "现场照片分析" → 下次用户说"把上次的现场报告发给我"可 `recall`。
- **审批**：
  - `phone_screenshot` 为 `read_only=True`，无需审批。

---

## 示例 3：手机操作进记忆

**场景**：用户让 Agent 在手机上设置 WLAN（连 Wi-Fi、输密码），下次忘了操作路径时，Agent 能从记忆 recall。

**目标**：操作序列自动沉淀为结构化记忆，支持跨会话回忆。

### 时序图

```
┌─────────┐    ┌──────────┐    ┌──────────┐    ┌─────────┐    ┌──────────────┐
│  用户   │    │  Agent   │    │  Daemon  │    │ 手机    │    │ UnifiedMemory│
│ (语音)  │    │          │    │          │    │ Companion│    │   System     │
└────┬────┘    └────┬─────┘    └────┬─────┘    └────┬────┘    └──────┬───────┘
     │              │               │               │               │
     │ ① "帮我连公司WiFi"          │               │               │
     │──────────────>│               │               │               │
     │              │               │               │               │
     │              │ ② phone_open_app("com.android.settings")     │               │
     │              │──────────────>│               │               │
     │              │               │────③ 打开设置──>│               │
     │              │               │               │               │
     │              │ ④ phone_ui_tree → 找到 WLAN 节点 │               │
     │              │──────────────>│               │               │
     │              │               │<────⑤ obs.tree──│               │
     │              │               │               │               │
     │              │ ⑥ phone_click(target:{text:"WLAN"})            │               │
     │              │──────────────>│               │               │
     │              │               │               │               │
     │              │ ⑦ phone_input(text:"CorpWiFiPassword")         │               │
     │              │──────────────>│               │               │
     │              │               │               │               │
     │              │ ⑧ phone_click(target:{text:"连接"})             │               │
     │              │──────────────>│               │               │
     │              │               │               │               │
     │ ⑨ 设置完成   │               │               │               │
     │<──────────────│               │               │               │
     │              │               │               │               │
     │              │ ⑩ 提取关键决策 → 写入记忆       │               │               │
     │              │ 序列: [open_app→click WLAN→input password→click 连接]│
     │              │ 标签:  ["wlan", "公司网络", "设置"]              │               │
     │              │──────────────────────────────────────────────────────>│
     │              │               │               │               │
     │              │               │               │               │
     │ (两天后) "上次帮我设置的WLAN在哪？"                             │               │
     │──────────────>│               │               │               │
     │              │ ⑪ recall("公司网络 WLAN")       │               │               │
     │              │──────────────────────────────────────────────────────>│
     │              │<──────────────────────────────────────────────────────│
     │              │ 返回: 设置 → WLAN → 输入密码 → 连接             │               │
     │              │               │               │               │
     │ ⑫ 回复用户   │               │               │               │
     │<──────────────│               │               │               │
     │              │               │               │               │
```

### 涉及的 phone_* 工具

| 工具 | 作用 |
|---|---|
| `phone_open_app` | 打开系统设置 |
| `phone_ui_tree` | 定位 "WLAN" 可点击节点 |
| `phone_click` | 点击 WLAN 条目、点击"连接"按钮 |
| `phone_input` | 输入 Wi-Fi 密码 |
| `phone_screenshot` | （可选）截图确认连接成功 |

### 记忆/审批接入点

- **记忆**：
  - 操作完成后，Agent 提取 `decision_chain`（决策链）并写入 `UnifiedMemorySystem`：
    - `type`: `action_sequence`
    - `content`: `[{tool:"phone_open_app", args:{...}}, {tool:"phone_click", ...}, ...]`
    - `tags`: `["wlan", "公司网络", "设置", "android"]`
  - 回忆时通过 `recall` 按标签/语义检索，返回完整操作路径。
- **审批**：
  - `phone_input` 输入密码时可能触发 `tool_guard` HIGH 级别（涉及敏感信息输入），需 `user.approval` 弹窗确认；通过后继续执行。

---

## 示例 4：电脑+手机协同查询

**场景**：用户在电脑端工作，想快速查看手机微信有没有新消息，不想拿起手机。

**目标**：Agent 打开手机微信，截图，通过 LLM OCR 读取消息，在电脑端返回摘要。

### 时序图

```
┌─────────┐    ┌──────────┐    ┌──────────┐    ┌─────────┐
│  PC端    │    │  Agent   │    │  Daemon  │    │ 手机    │
│  用户   │    │          │    │          │    │ Companion│
└────┬────┘    └────┬─────┘    └────┬─────┘    └────┬────┘
     │              │               │               │
     │ ① "帮我看看手机微信有新消息吗"                │               │               │
     │──────────────>│               │               │
     │              │               │               │
     │              │ ② phone_open_app("com.tencent.mm")              │               │
     │              │──────────────>│               │
     │              │               │────③ 启动微信──>│
     │              │               │               │
     │              │ ④ phone_screenshot              │               │
     │              │──────────────>│               │
     │              │               │────⑤ 截图上传──>│
     │              │               │               │
     │              │               │<────⑥ obs.screenshot{url}──│
     │              │               │               │
     │              │ ⑦ attach_image_url → LLM OCR 读取消息列表     │               │
     │              │               │               │
     │              │ ⑧ 生成消息摘要（文字回复）       │               │
     │              │               │               │
     │ ⑨ "微信有3条新消息：① 工作群：项目延期通知；② 家人群：聚餐时间确认；③ 快递：已到菜鸟驿站" │               │               │
     │<──────────────│               │               │
     │              │               │               │
```

### 涉及的 phone_* 工具

| 工具 | 作用 |
|---|---|
| `phone_open_app` | 启动微信（`com.tencent.mm`） |
| `phone_screenshot` | 截取微信消息列表画面 |

### 记忆/审批接入点

- **记忆**：
  - 可选写入 `UnifiedMemorySystem`：用户查询意图 + 微信消息摘要（脱敏后）。
  - 隐私策略：`screenshot` 原图 URL 不上传外部，LLM 仅通过 `attach_image_url` 本地推理；摘要中涉及具体联系人名称可再做一层 `DeviceRedactor`。
- **审批**：
  - `phone_open_app` 启动微信属于中等风险（打开社交 app），可配置 `tool_guard` MEDIUM 级别触发 `user.approval`；若用户此前已授权"允许访问微信"，则跳过。

---

## 示例 5：安全审批流程

**场景**：Agent 检测到手机端正在操作银行 App（如转账），`tool_guard` 判定为 HIGH/Critical 风险。

**目标**：强制中断操作，弹出审批弹窗，用户必须在手机端点击"允许"后才能继续；审批记录写入审计日志。

### 时序图

```
┌─────────┐    ┌──────────┐    ┌──────────┐    ┌─────────┐    ┌──────────┐
│  PC端    │    │  Agent   │    │  tool_guard│    │ 手机    │    │ Auditor  │
│  用户   │    │          │    │  /policy │    │ Companion│    │ (审计日志)│
└────┬────┘    └────┬─────┘    └────┬─────┘    └────┬────┘    └────┬────┘
     │              │               │               │               │
     │ ① "帮我转账5000元"           │               │               │
     │──────────────>│               │               │               │
     │              │               │               │               │
     │              │ ② phone_open_app("com.bank.app")              │               │
     │              │──────────────>│               │               │
     │              │               │               │               │
     │              │ ③ phone_click(target:{text:"转账"})           │               │
     │              │──────────────>│               │               │
     │              │               │               │               │
     │              │ ④ phone_input(text:"5000") → tool_guard 拦截 │               │               │
     │              │────────────────>│               │               │
     │              │               │               │               │
     │              │ ⑤ 判定风险等级: HIGH/Critical  │               │               │
     │              │               │               │               │
     │              │ ⑥ 暂停操作，下发 user.approval 请求           │               │
     │              │──────────────>│               │               │
     │              │               │────⑦ 手机弹窗"允许/拒绝"──>│               │
     │              │               │               │               │
     │              │               │<────⑧ 用户点击"允许"──│               │
     │              │               │               │               │
     │              │<────⑨ user.approval{decision:"allow"}───│               │
     │              │               │               │               │
     │              │ ⑩ 恢复操作: phone_input(text:"5000") 继续执行 │               │               │
     │              │──────────────>│               │               │
     │              │               │               │               │
     │ ⑪ 转账完成   │               │               │               │
     │<──────────────│               │               │               │
     │              │               │               │               │
     │              │ ⑫ 审批记录写入审计日志           │               │               │
     │              │────────────────────────────────────────────────────>│
     │              │               │               │               │
     │              │ 记录内容: {request_id:"uuid", tool:"phone_input",  │               │               │
     │              │            risk:"HIGH", decision:"allow", ts, user} │               │               │
     │              │               │               │               │
```

### 涉及的 phone_* 工具

| 工具 | 作用 |
|---|---|
| `phone_open_app` | 打开银行 App |
| `phone_click` | 点击"转账"按钮 |
| `phone_input` | 输入转账金额（被 `tool_guard` 拦截点） |
| `phone_ui_tree` | （可选）确认当前页面是银行转账页面 |

### 记忆/审批接入点

- **审批**（核心）：
  - `tool_guard` 在 `phone_input` 前触发拦截，原因：
    - 当前 `pkg` 属于银行 App（命中 `policy.py` 敏感应用名单）。
    - 输入金额匹配金融交易模式（`\d+` 元/金额关键字）。
  - 综合评分 ≥ HIGH 阈值 → 强制 `user.approval` 流程。
  - 用户必须在手机端弹窗操作（`user.approval{decision:"allow"}`），PC 端无法绕过。
- **审计**：
  - `Auditor` 模块记录完整审批链：
    - `request_id`：唯一追溯 ID
    - `tool_name`：`phone_input`
    - `risk_level`：`HIGH` / `Critical`
    - `decision`：`allow` / `deny` / `always`
    - `timestamp`：epoch 秒
    - `user_id`：操作人
    - `context`：银行 App `pkg` 名称、当前页面 `activity`、输入金额（脱敏后）
  - 日志写入 `auditor.py` 指定的持久化存储（本地文件或审计后端）。
- **记忆**：
  - 若用户点击"always"，则写入 `UnifiedMemorySystem`：
    - 标签 `["bank_app", "always_approved", "转账"]`
    - 下次同一 App 同类操作可自动通过（依据策略是否信任"always"）。

---

## 附录：工具与接入点速查表

| 示例 | 核心工具 | 记忆接入 | 审批接入 |
|---|---|---|---|
| 1 跨设备验证码 | `phone_notification` + `phone_ui_tree` | — | 可选（`read_only=False`） |
| 2 手机发图 | `phone_screenshot` | ✅ 写入报告路径 | — |
| 3 操作进记忆 | `phone_open_app` + `phone_click` + `phone_input` | ✅ 写入决策链 | `phone_input` 可能触发 HIGH |
| 4 跨屏查询 | `phone_open_app` + `phone_screenshot` | 可选写入摘要 | `phone_open_app` 可选 MEDIUM |
| 5 安全审批 | `phone_open_app` + `phone_click` + `phone_input` | ✅ "always" 权限 | ✅ `tool_guard` + `user.approval` + `Auditor` |

---

## 附录：安全与隐私要点

1. **截图脱敏**：`phone_screenshot` 返回的 `url` 指向本地/内网上传端点，**图片本身不做 OCR 后文本脱敏**（由 LLM 视觉直接处理）；若需要文本级脱敏，可先 `phone_ui_tree` 提取文字再过 `DeviceRedactor`。
2. **UI 树脱敏**：`phone_ui_tree` 的 `obs.tree` 返回前自动经过 `DeviceRedactor`，`text` / `desc` 中的手机号、身份证号、银行卡号被替换为 `[REDACTED]`，避免 LLM 上下文泄露。
3. **审批不可绕过**：`user.approval` 弹窗必须在手机端物理点击，PC 侧 Agent 无法伪造；`tool_guard` 判定 HIGH 时拒绝任何"自动允许"回退。
4. **审计不可篡改**：审批记录写入 `Auditor` 的只追加日志；`always` 权限的授予本身也是一条审计记录。
