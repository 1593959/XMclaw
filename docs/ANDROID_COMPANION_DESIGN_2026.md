# XMclaw 安卓伴侣（Android Companion）— 双向控制设计 2026-06

> **地位**: 新能力「安卓手机控制」的设计规格书。进度与验收的 source of truth 仍是
> [JARVIS_IMPLEMENTATION_PLAN_2026.md](JARVIS_IMPLEMENTATION_PLAN_2026.md)；本文是其设计附件，
> 待用户拍板 §11 决策后落到 Plan 的一个新 Phase。
> **触发**: 2026-06-17 用户："Kimi 出了 kimiclaw Android 能操作安卓手机，给 XMclaw 也开发一个。"
> **关键澄清（用户 2026-06-17）**: **不是独立 App，而是和 XMclaw 双向控制**——手机端不自带大脑，
> 复用 XMclaw 守护进程的 LLM/记忆/技能；一条持久链路，XMclaw↔手机两个方向都能「感知 + 下令」。

---

## 0. kimiclaw 是怎么做的（先对标）

调研结论（platform.minimax / kimi-claw 文档 + openatx/uiautomator2 + droidrun 生态）：

- **kimiclaw Android = 手机本机 App + 系统无障碍服务（AccessibilityService）**：用无障碍 API 读 UI 树、
  派发点击/手势，**独立跑在手机上、不需要电脑/ADB**；监控后台运行权限；**自动屏蔽银行/支付/证券/保险类
  App** 以保护资金。它的"大脑"是云端 OpenClaw（Kimi 把 agent 部署在云）。
- 另一条业界路线是 **宿主 ADB**（droidrun / AppAgent / Mobile-Agent / openatx-uiautomator2）：电脑用 `adb`
  控制连着的手机。纯软件、无需装系统级 App，但要电脑 + USB/无线调试。

**XMclaw 的选择（用户拍板）**: 走 kimiclaw 同款的**手机端无障碍 App** 路线（① 号方案），
**但**大脑不另起炉灶——复用 XMclaw 已有的 daemon/AgentLoop/记忆/技能。即：
**手机 App = XMclaw 的一等客户端 + 远程手眼**，通过一条双向链路和 daemon 联动。

> 已落地的 `xmclaw/providers/tool/android.py`（宿主 ADB 版，9 个工具）作为**开发期/无 App 时的回退通道**
> 保留——它能在没装伴侣 App、只有数据线时驱动手机，方便联调；不是本设计的主线。

---

## 1. 核心理念：双向控制，不是独立 App

```
        ┌───────────────────────────── 一条持久双向链路（WS over 配对令牌）────────────────────────────┐
        │                                                                                              │
 ┌──────┴───────┐   下行 Downlink：动作指令（tap/swipe/输入/全局键/开 App/读树/截图）           ┌───────┴────────┐
 │  XMclaw daemon│  ───────────────────────────────────────────────────────────────────────▶  │  安卓伴侣 App   │
 │  （大脑）     │                                                                              │  （手眼 + 端UI）│
 │  AgentLoop    │  ◀───────────────────────────────────────────────────────────────────────  │  Accessibility  │
 │  LLM/记忆/技能│   上行 Uplink：感知（无障碍树/截图/通知/事件）+ 用户指令（从手机下任务）       │  Service        │
 └──────────────┘                                                                              └────────────────┘
```

两个方向都既能「感知」也能「下令」：

- **XMclaw → 手机（控手机）**: daemon 里跑的 agent 决策出动作 → 经下行通道发给 App → App 用无障碍服务执行。
- **手机 → XMclaw（控 XMclaw）**: 手机既是 daemon 的远程传感器（持续/按需上报屏幕状态），
  也是**控制端**——用户在手机 App 里直接下任务、追加指令、审批、看时间线（等价于 Mission Control 的移动端），
  甚至让 agent 在**电脑侧**干活（"把这截图整理成文档存到我电脑桌面"）。

> 一句话：**手机是 XMclaw 的另一块屏幕 + 另一双手**。大脑只有一个，在 daemon。

---

## 2. 为什么复用 daemon 大脑（而非手机端自带 agent）

| 维度 | 手机端自带大脑（kimiclaw 云 OpenClaw） | 复用 XMclaw daemon（本设计） |
|---|---|---|
| 记忆/技能/进化 | 各端一套，割裂 | **统一**——手机操作进同一记忆库、同一 Honest-Grader 进化 |
| 模型配置 | 端上再配一遍 | 复用 daemon 的多模型 profile（含刚做的视频/图/语音/embedding 接线）|
| 跨设备协同 | 难 | 天然：一个 agent 同时有电脑的 shell/文件 + 手机的手眼 |
| 一致性 | 行为/身份漂移 | 同一 AgentLoop、同一事件总线、同一审批/安全管线 |
| 开发量 | 手机端重写 agent | 手机端只做「手眼 + 端 UI + WS 客户端」，**薄** |

符合 XMclaw 既有架构原则（[CLAUDE.md] "单 daemon 托管 AgentLoop，客户端经 WS 连接"）。手机只是新客户端形态。

---

## 3. 双向协议（WS 消息契约）

复用 daemon 现有的配对令牌鉴权 + `BehavioralEvent` 总线；新增一个**设备控制通道**。建议端点
`/device/v1/{device_id}`（与人类用的 `/agent/v2/{session}` 并列）。消息为 JSON 帧，`type` 区分。

### 3.1 下行（daemon → 手机）：动作指令

| type | 载荷 | 语义 |
|---|---|---|
| `act.tap` | `{x,y}` 或 `{node_id}` | 点击坐标/无障碍节点 |
| `act.long_press` | `{x,y,ms}` | 长按 |
| `act.swipe` | `{x1,y1,x2,y2,ms}` | 滑动/滚动 |
| `act.text` | `{text, node_id?}` | 向焦点/指定节点输入（**无障碍 setText，原生支持中文/Unicode**）|
| `act.global` | `{action: back\|home\|recents\|notifications\|quick_settings\|lock}` | 全局手势（无障碍 GLOBAL_ACTION_*）|
| `act.launch` | `{package}` | 启动 App |
| `perceive.tree` | `{compact?:bool}` | 请求一帧无障碍树（节点 text/id/desc/bounds/clickable）|
| `perceive.screenshot` | `{scale?}` | 请求一帧截图（MediaProjection）|
| `ctl.observe` | `{on:bool, fps?}` | 开/关持续感知流（事件驱动 or 限频）|
| `ack.req` | `{req_id}` | 需要结果回执的请求都带 `req_id` |

### 3.2 上行（手机 → daemon）：感知 + 用户指令

| type | 载荷 | 语义 |
|---|---|---|
| `obs.tree` | `{req_id?, nodes:[{id,text,res_id,desc,cls,bounds,clickable,...}]}` | 无障碍树快照（应答或主动）|
| `obs.screenshot` | `{req_id?, path/url 或 b64, w, h}` | 截图（大图走 HTTP 上传 + URL，不塞 WS）|
| `obs.event` | `{kind: window_changed\|notification\|toast\|app_opened, ...}` | 设备事件（无障碍回调）|
| `act.result` | `{req_id, ok, error?, ...}` | 动作执行回执 |
| `user.message` | `{text, images?}` | **用户从手机下达的指令**（驱动 daemon 的 AgentLoop）|
| `user.approval` | `{request_id, decision}` | 手机上对审批卡的响应 |
| `dev.hello` | `{device_id, model, android, app_ver, perms:{accessibility,projection,...}}` | 配对握手 + 能力/权限自述 |

### 3.3 截图/大图：旁路 HTTP

WS 只走控制 + 小 JSON；截图等二进制走已有的 `/api/v2/...` 上传 → 返 URL → 在 WS 里只带 URL。
（与现有 `_ensure_servable` / uploads 渲染管线一致，省得重造。）

---

## 4. 手机端 App 组件（Kotlin）

最小可用集（M1）：

1. **`XmclawAccessibilityService`**（核心手眼）
   - `onAccessibilityEvent`：监听 `TYPE_WINDOW_STATE_CHANGED` / 通知 / toast → 发 `obs.event`。
   - 读树：`rootInActiveWindow` 递归 → 扁平节点列表（text/viewIdResourceName/contentDescription/
     className/boundsInScreen/isClickable/...）→ `obs.tree`。
   - 执行动作：`dispatchGesture`（点击/长按/滑动）；`performGlobalAction`（BACK/HOME/RECENTS/通知栏/锁屏）；
     节点 `performAction(ACTION_CLICK / ACTION_SET_TEXT / FOCUS)`——**setText 原生支持中文**，
     解决 ADB `input text` 的 ASCII 限制。
2. **`ScreenCaptureService`**（截图）：`MediaProjection` + `ImageReader` 抓帧 → PNG → 旁路上传。
   需用户授权一次投屏。
3. **`CompanionForegroundService`**（常驻）：前台服务 + 常驻通知，维持 WS 长连，防系统杀后台。
4. **`DaemonLink`**（WS 客户端）：连 `/device/v1/{id}`，配对令牌鉴权，断线重连 + 帧队列冲洗
   （对齐现有前端 `lib/api` 的重连/在飞去重思路）。
5. **`CompanionActivity`（端 UI，可后置到 M3）**：扫码/输入 daemon 地址 + 配对码；权限引导
   （无障碍、投屏、后台保活、通知读取）；一个轻量任务/时间线视图（复用 Mission Control 的事件→条目映射，
   消费同一 `BehavioralEvent`）——这就是"手机控制 XMclaw"的入口。

权限清单：`BIND_ACCESSIBILITY_SERVICE`、`FOREGROUND_SERVICE` + 类型、`POST_NOTIFICATIONS`、
投屏（运行时授权）、可选 `QUERY_ALL_PACKAGES`（列 App）。**不申请** root。

---

## 5. XMclaw 守护进程侧（Python）

1. **`DeviceRegistry`**（新）：管理已配对手机（device_id ⇄ 活动 WS）。`/device/v1/{id}` 路由 +
   配对握手 `dev.hello`。
2. **`AndroidRemoteToolProvider`**（新，`providers/tool/`）：把"控手机"暴露成 AgentLoop 工具——
   `phone_screenshot / phone_ui_tree / phone_tap / phone_swipe / phone_text / phone_key /
   phone_launch_app / phone_global`。每个工具 = 向目标手机发一条下行帧 + await `act.result`/`obs.*`。
   截图工具同样用 `metadata.attach_image` 把图喂视觉模型（与 computer_use/generate_image 同通道）。
   - 复用刚做的 `utils/http_errors`、附件渲染管线；与已建的 ADB 版 `android.py` 共享工具命名/语义，
     仅"执行后端"从 adb 换成 WS→App。
3. **上行用户指令接入**：手机 `user.message` → 注入 daemon 的 AgentLoop（等价于一个远程会话来源），
   让手机成为合法的指令通道；`user.approval` 接入现有审批服务。
4. **门控**：`tools.android_companion.enabled`（默认关）+ 仅对已配对设备开放，复用 security guardians
   做动作确认。

---

## 6. Agent 控制回路（谁决策）

决策永远在 daemon：

```
手机 obs.tree + obs.screenshot ──▶ daemon AgentLoop（LLM 看树+图）──▶ 决定动作
        ▲                                                              │
        └────────────────── act.result / 下一帧感知 ◀── act.tap/text 等下行 ┘
```

- LLM 优先用**无障碍树**做元素级定位（按 text/res-id/desc 选节点 → `node_id` 动作），截图做视觉兜底——
  即用户说的"不单单是截图操作"。
- 一个 agent 可同时持有电脑侧工具（shell/文件/浏览器）和手机侧工具 → 跨设备任务自然成立。

---

## 7. 反向：手机控制 XMclaw（"双向"的另一半）

- 手机 App 的端 UI 是 Mission Control 的移动投影：下任务、追加指令、随时打断、审批、看任务/时间线/产物。
- 手机可触发**电脑侧**动作（agent 在 daemon 跑，自然能调电脑的 shell/文件/浏览器）。
- 进一步（可选）：手机的传感/通知作为**触发器**（如收到某通知 → 唤起一个 daemon 自主目标）。

---

## 8. 安全与隐私（重中之重）

无障碍服务=最高权限，必须比功能更早做实：

1. **敏感 App 黑名单**（对标 kimiclaw）：默认拦截银行/支付/证券/保险/相机隐私类包名的**写动作**
   （白名单可读、黑名单禁点禁输）；列表可配 + 内置一份。
2. **动作确认网关**：复用 `security.tool_guard`——高风险动作（输入、支付页、发送、删除）先过确认卡，
   手机端 `user.approval` 或电脑端审批。
3. **配对与加密**：配对令牌（沿用 `pairing_token`）+ 局域网优先；公网需 TLS/反代。device_id 绑定。
4. **数据边界**：截图/树可能含隐私 → 经现有 `security.redactor` 脱敏后再进记忆；截图默认不长期留存。
5. **可见与可停**：常驻通知显示"XMclaw 正在控制本机"，一键断连/停手（类 pyautogui FAILSAFE）。
6. **最小权限**：不 root；投屏/无障碍均用户显式授权且可随时撤销。

---

## 9. 技术栈与项目落位

- **手机端**: Kotlin + Android（minSdk 26+/8.0；AccessibilityService、MediaProjection、Foreground
  Service、OkHttp WebSocket、Jetpack Compose 端 UI）。
- **落位**: 新建仓库 **`xmclaw-companion`**（Android Studio 工程），**不**塞进 Python 包；
  XMclaw 仓库内只加 daemon 侧的 `DeviceRegistry` + `AndroidRemoteToolProvider` + 协议 schema +
  本设计文档。协议帧的 schema 用一份 `docs/` 里的 JSON/markdown 契约两端共享。
- 既有 `xmclaw/providers/tool/android.py`（ADB 版）保留为回退/联调通道。

---

## 10. 里程碑（建议）

- **M0 协议契约**：定 `/device/v1` 帧 schema（§3）+ 配对握手；daemon 侧 `DeviceRegistry` 骨架 +
  一个回显测试（无真机，单元测试 mock WS）。
- **M1 手眼最小闭环**：App 的无障碍服务（读树 + tap/text/global）+ 截图 + WS 客户端；
  daemon `AndroidRemoteToolProvider` 5 个核心工具；端到端"打开设置→点某项"。
- **M2 决策回路 + 中文输入**：AgentLoop 用树+图驱动多步任务；setText 中文验证；动作确认网关 + 敏感 App 黑名单。
- **M3 手机端 UI（反向控制）**：端上下任务/时间线/审批，Mission Control 移动投影。
- **M4 跨设备任务 + 进化**：手机操作进统一记忆/Honest-Grader；电脑+手机协同任务样例。

每个里程碑**先看效果再扩**（沿用 Mission Control 的流程教训：上轮 UI 重写因"效果不好"被 revert）。

---

## 11. 待你拍板的决策

1. **落位**：手机端独立新仓库 `xmclaw-companion`（推荐）还是 XMclaw 仓库下 `android/` 子目录？
2. **连接形态**：先只做**局域网**（同 WiFi 连家里电脑的 daemon）够用吗？还是 M1 就要公网/中转（更复杂）？
3. **截图来源**：MediaProjection（要用户授权投屏、能截任意 App）默认开，还是优先纯无障碍树（更省、更隐私，但看不到画布类内容）？
4. **敏感 App 黑名单**：内置默认列表（银行/支付/证券）即可，还是要做成用户可视化编辑？
5. **M1 范围**：先验证"手眼最小闭环"（读树+点+输入+截图）就够，对吗？端 UI（反向控制）放 M3？

> 你定了这 5 条，我把它落成 JARVIS Plan 的一个新 Phase（含验收标准），再开 M0 的协议契约 + daemon 侧骨架。
