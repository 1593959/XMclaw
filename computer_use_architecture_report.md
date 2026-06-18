# XMclaw vs Hermes vs OpenClaw Computer Use — 架构级根因分析

> 目的：回答"相同模型、相同任务，为什么 Hermes/OpenClaw 更丝滑更快"，不是对比功能多少。
> 结论：XMclaw 的瓶颈不是缺功能，而是**模型执行路径过长、决策负担过重、坐标系不可靠**。OpenClaw 通过代理架构避免了这个问题。

---

## 一、核心差距：模型每轮需要做什么

### 1.1 工具选择负担

| 项目 | 模型每轮需要选的工具 | 负担 |
|------|---------------------|------|
| **XMclaw** | 23 个独立工具中挑选（screen_capture → mouse_click → screen_ocr → verify_text...） | 高：模型要判断"这一步该用哪个工具" |
| **Hermes** | 1 个 `computer_use` 工具，通过 `action` 参数区分 | 低：模型只需要填 `action` 和 `element` |

**量化影响**：
- 23 个 tool schema 每轮都压入 prompt，约 **7000-13000 tokens**
- Hermes 1 个 schema 约 **800 tokens**
- 在相同上下文窗口下，XMclaw 留给 conversation history 的空间更少，更容易触发压缩/截断
- 模型在 23 个选项中挑选错误工具的概率更高（`click_on_text` vs `mouse_click` vs `ui_click`）

### 1.2 坐标推理 vs 元素索引

**XMclaw 模型需要做的**（从 `gui_send_chat` 代码反推）：
1. 看截图 → 估算窗口位置 → 计算 `click_x = wx + (ww * 2) // 3`
2. 理解 DPI 缩放（`click_scale` 提示词）
3. 理解 WeChat 布局："bottom 50 px = icon row, click ~150 px above"
4. 输出像素坐标 `(x, y)` 给 `mouse_click` 或 `click_on_text`

**Hermes 模型需要做的**：
1. `capture(mode="som")` → 看到截图上有编号 `1, 2, 3...`
2. 读返回的 elements 数组：`#1 Button "Open"`, `#2 TextField "Search"`
3. 输出 `computer_use(action="click", element=1)` — **无需任何坐标计算**

**根因**：
- XMclaw 的 `click_on_text` 内部走 `find_on_screen` → OCR → 匹配 → 计算中心坐标 → `pyautogui.click(x, y)`
- OCR 误读（特别是中文简写、图标、按钮）是常态 → 坐标漂移 → 点击失败 → 模型收到 `verified: false` → 重新截图 → 重新推理 → **多轮往返**
- Hermes 的 SOM 编号由**后端**（cua-driver）直接计算，绕过模型，**零漂移**

### 1.3 验证是事后补救，不是原子合并

**XMclaw 标准流程**（以点击按钮为例）：
1. `screen_capture` → 模型看到截图
2. `mouse_click(x, y)` → 返回 "ok"
3. 模型**必须**再调用 `screen_capture` 或 `verify_text` 确认效果
4. 如果 `verify_text` 失败，触发 5 点重试 → 每轮 0.6s 延迟 × n 次

**总往返：3-5 轮**

**Hermes 标准流程**：
1. `capture(mode="som")` → 看到截图 + 元素编号
2. `click(element=3, capture_after=True)` → **一次返回包含动作结果 + 新截图**
3. 模型直接看到新截图，无需再调用 capture

**总往返：2 轮**

**根因**：`capture_after=True` 是 Hermes 的架构级优化，XMclaw 所有 mutating 动作都不支持。

---

## 二、上下文粘性：窗口状态每轮重置

### 2.1 XMclaw 无粘滞状态

从 `computer_use.py` 代码看：
- `window_focus` 后，没有 `_active_pid` 或 `_active_window_id` 记录
- 下一个 `mouse_click` 或 `keyboard_type` 如果其他操作（如弹窗、焦点切换）干扰了，就打错窗口
- 模型需要每轮重新确认窗口位置

### 2.2 Hermes 的 backend 粘滞

```python
class CuaDriverBackend(ComputerUseBackend):
    def __init__(self):
        self._active_pid: Optional[int] = None
        self._active_window_id: Optional[int] = None
        self._last_app: Optional[str] = None
```

- `capture()` 后自动记录 frontmost 窗口
- 后续 `click()`/`type()` 自动命中同一个窗口
- 模型无需关心窗口上下文

**影响**：减少 `focus_app` 调用频率，减少坐标漂移来源。

---

## 三、gui_send_chat 是反面教材

`gui_send_chat` 这个 atomic tool 的代码（2000-2800 行）完美展示了问题：

```python
# 模型调用一次 gui_send_chat，内部发生：
# 1. focus window（pygetwindow + _force_foreground）
# 2. OCR chat header（verify_chat_title）
# 3. OCR chat list（nav_chat_name）
# 4. click chat（pyautogui.click）
# 5. OCR dropdown（群聊 heading）
# 6. click input box（heuristic: bottom 150px）
# 7. type via clipboard（pyperclip + Ctrl+V）
# 8. press Enter
# 9. optional verify screenshot
```

**问题**：
- 8 步操作在一个 tool 里，模型看不到中间状态
- 任何一步失败（OCR 没识别到、坐标偏移、窗口焦点丢失）→ 整个 tool 失败，返回一个复杂的 error payload
- 模型需要理解 WeChat 布局、DPI 缩放、OCR 容错率才能 debug
- 如果失败，模型**无从知道是哪一步失败**（没有 step-by-step 反馈）

**Hermes 会怎么做**：
- `capture(mode="som")` → 看到截图 + 元素编号
- `click(element=5)` → 点击"输入框"
- `type(value="hello")` → 输入文本
- `key(keys=["Return"])` → 发送
- 每步都有独立的 tool call + 独立的截图验证
- 模型能看到每一步的结果，如果某步失败，只需重试那一步

---

## 四、架构级重构建议

不是"加工具"，而是**重新设计工具面**。

### 4.1 统一为单一 `computer_use` 工具

**目标**：从 23 个工具 → 1 个工具 + `action` 参数

```python
COMPUTER_USE_SCHEMA = {
    "name": "computer_use",
    "parameters": {
        "properties": {
            "action": {
                "enum": [
                    "capture",        # 截图（支持 som/vision 模式）
                    "click",            # 左键单击
                    "double_click",     # 双击
                    "right_click",      # 右键
                    "scroll",           # 滚动
                    "type",             # 输入文本
                    "key",              # 按键（Enter, Tab, Escape...）
                    "wait",             # 等待
                    "list_windows",     # 列出窗口
                    "focus_window",     # 聚焦窗口
                ]
            },
            # SOM 模式
            "element": {"type": "integer"},  # 1-based 元素索引
            # 坐标模式（fallback）
            "coordinate": {"type": "array", "items": {"type": "integer"}},
            # 文本输入
            "text": {"type": "string"},
            # 动作后自动截图
            "capture_after": {"type": "boolean", "default": True},
            # 截图模式
            "mode": {"enum": ["som", "vision"], "default": "som"},
        },
        "required": ["action"],
    },
}
```

**收益**：
- schema token 从 ~10000+ → ~800
- 模型决策从"选工具"降级为"填参数"
- 与 Hermes 的 schema 对齐，降低模型迁移成本

### 4.2 引入 SOM（Set-of-Mark）截图覆盖

**目标**：消除模型的坐标推理负担

```python
class SOMBackend:
    def capture(self, mode="som") -> CaptureResult:
        # 1. 截图
        # 2. 通过 UIA 枚举可交互元素
        # 3. 在截图上叠加编号（1, 2, 3...）
        # 4. 返回 {"image": path, "elements": [...]}
```

**实现路径**（Windows）：
- 复用已有的 `uiautomation` 遍历代码（`ui_inspect` 已经做了）
- 把元素的 `BoundingRectangle` 映射到截图坐标
- 用 PIL/OpenCV 在截图上画编号圆圈
- 限制 `max_elements`（默认 50，硬上限 200）防止密集 UI 撑爆上下文

**模型看到的**：
```
#1 Button "发送" @ (bottom-right)
#2 TextField "输入消息" @ (bottom-center)
#3 Button "搜索" @ (top-left)
```

**模型输出的**：`computer_use(action="click", element=2)` — 无需坐标

**收益**：
- 消除 DPI 缩放问题
- 消除 OCR 误读问题
- 消除窗口滚动导致的坐标漂移
- 模型不需要理解像素坐标系

### 4.3 动作后自动截图（capture_after）

**目标**：减少往返轮次

```python
def click(self, element=None, capture_after=True):
    # 执行点击
    if capture_after:
        return {"action": "ok", "screenshot": self.capture()}
    return {"action": "ok"}
```

**标准流程从 3-5 轮 → 2 轮**：
1. `capture` → 看到截图 + 元素编号
2. `click(element=3, capture_after=True)` → 返回 ok + 新截图

**收益**：每步操作节省 1 轮 model → tool → model 往返，这是**最大单一收益**。

### 4.4 上下文粘滞（Backend Sticky State）

```python
class ComputerUseBackend:
    def __init__(self):
        self._active_hwnd: Optional[int] = None
        self._active_pid: Optional[int] = None
        self._last_window_title: Optional[str] = None

    def capture(self):
        # 枚举窗口，选 frontmost
        # 记录 _active_hwnd / _active_pid
        ...

    def click(self, element=None):
        # 使用 _active_hwnd 作为默认目标
        # 无需模型再次指定窗口
        ...
```

**收益**：减少 `focus_window` 调用，减少焦点竞争。

### 4.5 非 Vision 模型回退

**目标**：当主模型没有 vision 时，自动降级为文本描述

```python
def _route_capture(self, mode="som"):
    if not self.model_has_vision:
        # 把截图发给 vision 子模型（或本地 OCR）
        description = self._vision_model.describe(screenshot)
        # 返回文本描述 + 元素列表（无图片）
        return {"description": description, "elements": [...]}
    return self.capture(mode=mode)
```

**收益**：兼容非 vision 模型，扩大可用模型范围。

### 4.6 危险动作硬阻断

**目标**：防止误操作，比 guardian 更前置

```python
_BLOCKED_KEY_COMBOS = {
    frozenset({"alt", "f4"}),          # 关闭窗口
    frozenset({"win", "l"}),            # 锁屏
    frozenset({"ctrl", "alt", "del"}),  # 安全选项
}

_BLOCKED_TYPE_PATTERNS = [
    re.compile(r"curl\s+[^|]*\|\s*bash", re.IGNORECASE),
    re.compile(r"\brm\s+-rf\s+/\s*$", re.IGNORECASE),
]
```

**收益**：在 tool 层直接阻断，不依赖 guardian 配置，更安全。

---

## 七、OpenClaw 对比：代理架构的胜利

OpenClaw 的 computer use 不是单一实现，而是**三条独立路径**。理解它的架构才能看清为什么它同样比 XMclaw 快。

### 7.1 OpenClaw 的三条桌面控制路径

| 路径 | 实现方式 | 工具面 | 执行层 | 平台 |
|------|----------|--------|--------|------|
| **Codex Computer Use** | 代理 Codex app-server 的 MCP 插件 | 单一 `computer_use` (CUA 原生) | Codex/CUA 驱动 | macOS |
| **PeekabooBridge** | 外部 CLI (`peekaboo`) | 多个子命令 (`click`, `type`, `see`...) | `peekaboo` CLI | macOS |
| **Direct cua-driver MCP** | 直接注册上游 MCP | 单一 `computer_use` (CUA 原生) | `cua-driver` | macOS |

**关键发现**：OpenClaw 自身**不执行桌面操作**。它的 `extensions/codex` 代码（695 行）只做插件安装、MCP 状态检查和配置，实际工具调用由 Codex 的 `computer-use` MCP 服务器处理。这和 XMclaw 3381 行原生实现形成鲜明对比。

### 7.2 为什么 OpenClaw 也更快

OpenClaw 的 Codex Computer Use 路径，底层和 Hermes 是**同源实现**（都基于 OpenAI CUA / cua-driver）。因此它享有同样的架构优势：

| 维度 | XMclaw | OpenClaw (Codex CUA) | Hermes |
|------|--------|---------------------|--------|
| **工具面大小** | 23 个独立工具 | 1 个 `computer_use` | 1 个 `computer_use` |
| **Schema tokens** | ~10000+ | ~800 | ~800 |
| **坐标系** | 像素坐标（模型算） | SOM 元素索引（后端算） | SOM 元素索引（后端算） |
| **capture_after** | ❌ 无 | ✅ CUA 原生 | ✅ 原生 |
| **上下文粘性** | ❌ 无 `_active_pid` | ✅ CUA 原生 | ✅ 原生 |
| **非 vision 回退** | ❌ 无 | ✅ CUA 原生 | ✅ 原生 |
| **执行路径** | 本地 pyautogui | MCP → CUA 驱动 | 本地 cua-driver |
| **往返轮次（点击+验证）** | 3-5 轮 | 2 轮 | 2 轮 |

### 7.3 OpenClaw 的 `nodes` 工具已经示范了正确设计

OpenClaw 的 `nodes` 工具（远程设备控制）采用了**单一工具 + `action` 参数**的模式：

```typescript
const NODES_TOOL_ACTIONS = [
  "status", "describe", "camera_snap", "screen_snapshot",
  "screen_record", "location_get", "invoke", ...
] as const;

const NodesToolSchema = Type.Object({
  action: stringEnum(NODES_TOOL_ACTIONS),
  // ... 各 action 对应的参数
});
```

这和 Hermes 的 `computer_use` 设计完全一致。XMclaw 的 23 个独立工具是反模式。

### 7.4 Peekaboo 是另一套哲学

OpenClaw 的 PeekabooBridge 路径值得单独分析：

**Peekaboo 的 CLI 设计**（`skills/peekaboo/SKILL.md`）：
```bash
peekaboo see --annotate --path /tmp/see.png   # 截图 + 标注元素
peekaboo click --on B3 --app Safari            # 点击元素 B3
peekaboo type "Hello" --return                  # 输入文本
```

Peekaboo 也采用了**元素索引**（`B3`, `T2`）而非像素坐标，和 SOM 模式一致。但它是外部 CLI，不是模型直接调用的工具。

### 7.5 架构对比总结

| 项目 | XMclaw | Hermes | OpenClaw |
|------|--------|--------|----------|
| **实现策略** | 全部自己写（3381 行） | 全部自己写（cua-driver） | **代理给专业工具**（MCP/CLI） |
| **复杂度** | 高：维护 23 个工具 + 坐标系 + OCR + 重试 | 中：维护 cua-driver + schema | **低：安装配置 + 代理调用** |
| **正确性** | 依赖 OCR + 坐标漂移 | 依赖 UIA 元素索引 | **依赖 CUA/Peekaboo 专业实现** |
| **扩展性** | 每加功能要新增工具 | 加 action 参数即可 | **换 MCP 服务器或 CLI 即可** |
| **跨平台** | Windows 原生 | macOS 原生 | macOS（通过 Codex/Peekaboo） |

### 7.6 对 XMclaw 的启示

OpenClaw 的架构选择说明了一个道理：**computer use 是专业领域，应该代理给专业实现，而不是自己写全套。**

XMclaw 的 3381 行 `computer_use.py` 试图用 `pyautogui` + `uiautomation` + `OCR` 自己实现完整的桌面控制，结果：
- 坐标系不可靠（DPI 缩放、OCR 误读）
- 工具面爆炸（23 个工具）
- 验证事后补救（5 点重试）
- 每个 atomic 操作内部藏了 8 步（`gui_send_chat`）

Hermes 和 OpenClaw 都选择了**元素索引 + 单一工具 + 后端粘滞状态**的架构，这才是正确的方向。

如果 XMclaw 要重构，有两条路：
1. **全量自研**：学 Hermes，重写为单一 `computer_use` 工具 + SOM 覆盖 + capture_after + 粘滞状态
2. **代理架构**：学 OpenClaw，在 Windows 上找一个等价的 CUA 驱动或 UI 自动化框架（如 `Playwright` + `uiautomation` 桥接），通过 MCP 或类似机制代理，自己只负责配置和 schema 定义

第二条路在 Windows 上目前缺乏成熟等价物（cua-driver 是 macOS 专用的），所以全量自研更现实。但架构设计必须参照 Hermes/OpenClaw 的范式：单一工具 + 元素索引 + 原子验证。

---

## 八、执行优先级

按"模型执行效率收益"排序，不是按实现难度：

| 优先级 | 重构项 | 预期收益 | 实现复杂度 |
|--------|--------|----------|----------|
| **P0** | `capture_after` 合并动作+验证 | 每步节省 1 轮往返 | 低 |
| **P0** | 统一 `computer_use` 单一工具 | 降低 schema token 90% | 中 |
| **P1** | SOM 截图覆盖（UIA 元素索引） | 消除坐标漂移、OCR 误读 | 中 |
| **P1** | Backend 粘滞状态 | 减少窗口聚焦操作 | 低 |
| **P2** | 非 Vision 回退 | 扩大模型兼容性 | 中 |
| **P2** | 危险动作硬阻断 | 安全加固 | 低 |

---

## 九、总结

Hermes 和 OpenClaw 更丝滑更快的根因不是"功能更多"，而是**模型执行路径更短**：

1. **1 个工具 vs 23 个工具** → 模型选择负担降低，schema 从 ~10000+ tokens 降到 ~800
2. **元素索引 vs 像素坐标** → 模型无需坐标推理，零漂移，不依赖 OCR 容错
3. **capture_after vs 事后 verify** → 每步减少 1 轮往返，标准流程从 3-5 轮 → 2 轮
4. **粘滞上下文 vs 每轮重置** → 减少窗口定位操作，消除焦点竞争
5. **代理架构 vs 全部自研** → OpenClaw 示范了把专业工作交给专业实现，自己只负责配置和 schema

XMclaw 的 `gui_send_chat` 就是问题的缩影：一个 tool 里硬编码 8 步操作，模型看不到中间状态，任何一步失败都导致整个 tool 失败。Hermes 把它拆成了 4 个独立的 `computer_use` 调用，每步都有截图验证，模型能逐步推进。

**架构级重构方向**：不是给 23 个工具加功能，而是**把它们重新设计为 1 个工具 + SOM 元素索引 + capture_after + 粘滞状态**。这是三条路中最现实、收益最大的一条。
