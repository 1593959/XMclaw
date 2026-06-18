# Computer Use 能力对比报告：XMclaw vs Hermes vs OpenClaw

> 基于 2026-06-18 对三个项目 GitHub 仓库源码的直接分析。
> - Hermes: `github.com/NousResearch/hermes-agent` (main)
> - OpenClaw: `github.com/openclaw/openclaw` (main)
> - XMclaw: 本地工作目录 `C:\Users\15978\Desktop\XMclaw`

---

## 一、架构定位差异

| 维度 | XMclaw | Hermes | OpenClaw |
|------|--------|--------|----------|
| **执行层** | 本地 `pyautogui` 直接驱动 OS | 本地 `cua-driver` MCP 后端驱动 | 远程 `nodes` 网关代理驱动 |
| **目标平台** | Windows（主）+ macOS/Linux（部分） | **仅 macOS** | 任何可配对为 Node 的设备 |
| **核心依赖** | `pyautogui` + `mss` + `Pillow` + `cv2` + `uiautomation` | `cua-driver`（需独立安装） | OpenClaw Gateway + 远程 Node Agent |
| **模型接口** | 多独立工具（23个） | 单一工具 + `action` 参数区分 | 多独立工具 + 远程 invoke |
| **视觉输入** | 截图 → 直接 vision 管道 | SOM 截图 / 纯截图 / AX 树 | 远程截图/相机 → 媒体管道 |

**关键洞察**：
- **Hermes** 走的是「本地 macOS 原生驱动」路线，用 cua-driver 的 SkyLight 私有 API 做**后台不抢焦点**操作。
- **OpenClaw** 走的是「远程设备代理」路线，screen/camera 操作发生在**另一台设备**上，通过 Gateway 回传。
- **XMclaw** 走的是「本地跨平台驱动」路线，直接用 `pyautogui` 在 daemon 所在机器上操作，覆盖 Windows 生产力场景。

---

## 二、工具面对比

### 2.1 XMclaw — 23 个工具，分层递进

```
基础层（Phase 1）
  screen_capture, screen_size, cursor_position
  mouse_move, mouse_click, mouse_drag, mouse_scroll
  keyboard_type, keyboard_press
  window_list, window_focus

视觉定位层（2026-05-12 r1）
  screen_ocr, find_on_screen, click_on_text, wait_for_text
  screen_region_capture

图像+原生 UI 层（2026-05-12 r2）
  find_image_on_screen, click_on_image, scroll_to_text
  ui_inspect, ui_click          ← Windows UIA 无障碍树

原子操作层（2026-05-12 r3）
  gui_send_chat                 ← 一键导航+发送，防错 chat
```

**特点**：
- 每个功能一个独立工具，LLM 调用意图清晰，token 描述完整。
- 支持 **5 点候选重试**（像素点击未触发时，中心+四角自动重试）。
- 支持 **verify_text** 动作后验证：点击后 OCR 轮询确认目标文本出现，失败显式回传 `verified: false`。
- 支持 **DPI 坐标对齐**：回报 `click_scale` 让模型在 DPI 缩放场景下正确换算坐标。
- 中文友好：OCR 引擎优先 `rapidocr-onnxruntime`（~50MB，中文优化）。

### 2.2 Hermes — 单一 `computer_use` 工具，action 派生

```python
computer_use(action="capture", mode="som|vision|ax")
computer_use(action="click", element=N)          # 优先元素索引
computer_use(action="double_click|right_click|middle_click")
computer_use(action="drag", from_element=N, to_element=M)
computer_use(action="scroll", direction="up|down|left|right")
computer_use(action="type", text="...")
computer_use(action="key", keys="cmd+s")
computer_use(action="set_value", value="...")   # AXPopUpButton 直接设值
computer_use(action="wait", seconds=...)
computer_use(action="list_apps")
computer_use(action="focus_app", app="Safari")
```

**特点**：
- **Schema 紧凑**：单一工具，token 成本低。
- **SOM 模式**（Set-of-Mark）：截图上叠加可交互元素的编号，LLM 直接说「点击 #3」即可，无需像素坐标。
- **背景不抢焦点**：`focus_app` 默认不 `raise_window`，输入路由到目标应用但不打断用户当前操作。
- **元素索引优先**：`element` 参数比 `coordinate` 更可靠，模型无需做像素级定位。
- **max_elements 截断**：默认 100/最大 1000，防止 Electron 密集 UI 一次捕获 500+ 节点撑爆上下文。
- **辅助视觉路由**：当主模型非 vision 时，自动把截图发给 `auxiliary.vision` 管道做文本化描述，再回传主模型。

### 2.3 OpenClaw — Nodes 远程媒体工具

```
nodes:screen_snapshot    → 远程节点屏幕截图
nodes:screen_record      → 远程节点屏幕录屏
nodes:camera_snap        → 远程节点相机拍照
nodes:camera_clip        → 远程节点相机录像
nodes:photos_latest      → 远程节点相册
nodes:invoke             → 任意远程命令透传
```

**特点**：
- **没有本地鼠标/键盘**：OpenClaw 本身不直接控制桌面鼠标键盘；控制发生在远程 Node 上。
- 面向**手机伴侣**、**远程服务器**、**另一台电脑**场景。
- 通过 `gateway` 的 `node.invoke` 协议走网络，有 idempotencyKey、timeout、媒体安全校验。
- 支持 `modelHasVision` 开关：有 vision 时把 base64 图直接塞进 content 块；无 vision 时只传路径。

---

## 三、安全模型对比

| 维度 | XMclaw | Hermes | OpenClaw |
|------|--------|--------|----------|
| **默认状态** | `tools.computer_use.enabled=false`（完全关闭） | 工具注册后可用，但 action 需审批 | 通过 Node 配对授权 |
| **审批粒度** | 按动作类型（读取/操作）+ Guardian 模式 | 按 action 类型（读取直接过，操作需审批） | Gateway 层面操作符审批 |
| **危险动作硬阻断** | pyautogui FAILSAFE（鼠标拉角落终止） | 硬阻断快捷键（cmd+shift+q 注销等） | 节点命令白名单/黑名单 |
| **危险文本过滤** | 无内置 | 阻断 `curl ... \| bash`、`sudo rm -rf` 等 | Gateway 路径策略 |
| ** Guardian 实现** | `ComputerUseActionGuardian`：按动作分级 | 内嵌 `_SAFE_ACTIONS` / `_DESTRUCTIVE_ACTIONS` 集合 | Node 命令级策略 |

**XMclaw 可以借鉴的**：
1. **Hermes 的快捷键硬阻断列表**：`cmd+shift+q`（注销）、`cmd+ctrl+q`（锁屏）等。XMclaw 目前靠 FAILSAFE 和 Guardian，但没有对 `keyboard_press` 的按键组合做语义级阻断。
2. **Hermes 的 `type` 文本危险模式过滤**：`curl ... \| bash` 等。XMclaw 的 `keyboard_type` 可以注入任意文本，没有内容级过滤。
3. **Hermes 的 `max_elements` 截断机制**：XMclaw 的 `ui_inspect` 也做了 100 元素上限，但 `screen_ocr` 的 blocks 没有上限截断，密集文本场景可能撑爆 prompt。

**OpenClaw 可以借鉴的**：
1. **Node 远程执行模型**：XMclaw 目前只控制 daemon 所在机器的桌面。如果引入「远程节点」抽象，可以让手机/另一台电脑成为 XMclaw 的 screen/camera 延伸。
2. **媒体安全校验**：`writeScreenSnapshotToFile` 时的 expectedHost 校验、无效 payload 拒绝，XMclaw 的截图保存可以借鉴这种防篡改/防伪造设计。

---

## 四、视觉定位与可靠性

| 维度 | XMclaw | Hermes | OpenClaw |
|------|--------|--------|----------|
| **截图方式** | `mss` 全屏/区域 PNG | `cua-driver` JPEG/PNG + SOM 覆盖 | 远程 Node 回传 base64 |
| **元素定位** | OCR + 图像模板 + UIA 树 | **SOM 编号覆盖** + AX 树 | 无（只有原始截图） |
| **坐标系** | 物理像素，需处理 DPI 缩放 | 逻辑像素（由 cua-driver 统一） | 远程节点自行处理 |
| **重试机制** | 5 点候选重试 + verify_text 轮询 | 无自动重试，依赖模型重新 capture | 无 |
| **多屏支持** | `monitor` 参数（mss index） | 通过 `app=` 过滤，z-index 排序 | `screenIndex` 参数 |
| **中文 UI** | rapidocr 优先，子串+模糊匹配 | cua-driver 的 AX 树读 label | 无 OCR |

**XMclaw 可以借鉴的**：
1. **SOM 编号覆盖**：这是 Hermes 最大的差异化能力。XMclaw 的 `ui_inspect` 已经能读出 UIA 树，但 LLM 仍需做「坐标换算」。如果能在截图上叠加数字编号，LLM 直接输出 `click_on_text(element=3)` 级别的指令，可彻底规避 OCR 误差和坐标漂移。
2. **Hermes 的 `capture_after=True`**：动作后自动截图验证。XMclaw 的 `verify_text` 是 OCR 轮询，但 Hermes 的方式是「动作+截图」一个原子返回，减少一轮往返。

---

## 五、多模态与模型适配

| 维度 | XMclaw | Hermes | OpenClaw |
|------|--------|--------|----------|
| **vision 管道** | `metadata.attach_image` → hop_loop 自动注入 | `_multimodal` 字典 + 各 provider 适配器 | `content: [{type: "image", data: base64}]` |
| **非 vision 模型** | 只能 OCR 后文本化 | 自动路由到 `auxiliary.vision` 管道描述截图 | 只传路径，不强制 vision |
| **base64 处理** | 默认不内联，走文件+vision 管道 | 内联 data URI，但检测尺寸防 provider 拒绝 | 内联或写文件 |
| **provider 适配** | 统一 `attach_image` | 专门处理 Anthropic/OpenAI/Gemini 各自的 tool_result 格式 | 统一 AgentToolResult |

**XMclaw 可以借鉴的**：
1. **Hermes 的 `auxiliary.vision` 回退**：当主模型没有 vision 能力时，Hermes 自动把截图发给 vision 子模型做文本描述，再把描述文本回传主模型。XMclaw 目前没有这个回退路径——如果 LLM 不是 vision 模型，截图等于白传。
2. **Hermes 的 provider 级图片尺寸检测**：`_image_dimensions_from_b64` 检测 8x8 以下的截图，自动 fallback 到 AX 树文本，避免 provider 侧 400 错误。

---

## 六、工程实现质量

| 维度 | XMclaw | Hermes | OpenClaw |
|------|--------|--------|----------|
| **测试覆盖** | 3 个单测文件（~800 行），mock OCR/pyautogui/mss | 3 个测试文件（capture/vision/skills 路由） | 测试文件分散在 `tests/tools/` |
| **backend 抽象** | 无（直接 pyautogui） | `ComputerUseBackend` ABC + `CuaDriverBackend` | 无抽象（Gateway 透传） |
| **降级策略** | 逐个工具缺失依赖 → 独立报错 | backend 可切换 `noop`（CI 测试） | 远程节点离线 → 网络错误 |
| **会话状态** | 无（每调用独立） | `_active_pid` / `_active_window_id` 粘滞 | 无状态（Node 有状态） |
| **并发** | `asyncio.to_thread` 包装 pyautogui | 后台 asyncio loop + 线程桥接 | Gateway 异步 |

---

## 七、可借鉴清单（按优先级排序）

### 高优先级（建议近期纳入 XMclaw）

1. **「动作后自动截图」参数** (`capture_after=True`)
   - 在 `mouse_click`、`keyboard_press` 等工具上加 `capture_after` bool 参数，动作成功后自动截图，合并到同一 ToolResult。
   - 减少 LLM 往返一轮，且让模型直接看到动作效果。

2. **危险键盘快捷键硬阻断**
   - 在 `keyboard_press` 中维护一个 `_BLOCKED_KEY_COMBOS` 集合，阻断 `alt+f4`、`win+l`、`ctrl+alt+del` 等系统级快捷键。
   - 参考 Hermes：`cmd+shift+q`（注销）、`cmd+ctrl+q`（锁屏）等。

3. **危险文本输入过滤**
   - 在 `keyboard_type` 中加 `_BLOCKED_TYPE_PATTERNS`：阻断 `curl ... \| bash`、`rm -rf /`、`sudo rm -rf` 等。
   - 特别是防止 agent 在 chat 窗口或浏览器地址栏输入危险命令。

4. **非 vision 模型的截图 fallback**
   - 当主模型没有 vision 能力时，把截图发给一个配置的 vision 子模型（或本地 OCR）做文本描述，替代 vision 管道。
   - 参考 Hermes 的 `_route_capture_through_aux_vision`。

### 中优先级（中期改进）

5. **SOM 编号覆盖层**
   - 在 `screen_capture` 的输出图上叠加 `ui_inspect` 读取到的元素编号，让 LLM 可以直接用 `click(element=3)` 而不是像素坐标。
   - 这比 OCR 更可靠，尤其对图标、按钮、无文字元素。

6. **元素索引优先的点击工具**
   - 新增 `ui_click_by_index` 工具，接收 `element_index`（由 `ui_inspect` 或 SOM 截图返回），通过 UIA 直接 invoke 元素。
   - 完全绕过坐标，解决 DPI、缩放、窗口滚动导致的坐标漂移。

7. **截图尺寸保护**
   - 在 `screen_capture` 和 `screen_region_capture` 中加 `_image_dimensions_from_b64` 检测，过滤掉 8x8 以下的无效截图，避免 provider 400。

8. **max_elements 截断**
   - `ui_inspect` 已有 100 元素上限，但 `screen_ocr` 的 blocks 没有上限。密集文本场景（如代码编辑器）可能一次返回 200+ blocks，需截断并提示。

### 低优先级（长期架构）

9. **Backend 抽象层**
   - 参考 Hermes 的 `ComputerUseBackend` ABC，把 pyautogui 封装成 `PyAutoGUIBackend`，预留 `UiaBackend`、`AndroidBackend` 等扩展位。
   - 为后续多平台（macOS cua-driver、Android ADB）做准备。

10. **远程 Node 代理模型**
    - 参考 OpenClaw 的 `nodes` 系统，让 XMclaw 可以控制远程设备（手机、另一台电脑）的屏幕/相机。
    - 与现有的 Android Companion（Phase 12）结合，形成统一的「设备控制」抽象。

---

## 八、结论

| 场景 | 最优方案 |
|------|----------|
| 本地 Windows 桌面自动化 | **XMclaw**（pyautogui + UIA + OCR 组合最全面） |
| 本地 macOS 后台不干扰操作 | **Hermes**（cua-driver + SOM 最优雅） |
| 远程手机/设备控制 | **OpenClaw**（Nodes 网关架构最成熟） |
| 纯浏览器/Web 操作 | 三者都通过 Playwright/浏览器工具覆盖，非 computer use 核心差异 |

XMclaw 的当前 computer use 面在**功能广度**（23 工具、OCR、UIA、图像匹配、原子发送）上领先，但在**安全深度**（快捷键阻断、文本过滤）和**模型友好度**（SOM 编号、自动动作后截图）上落后于 Hermes。建议按上述优先级清单分阶段补强，同时保持跨平台（Windows 为主）的差异化优势。
