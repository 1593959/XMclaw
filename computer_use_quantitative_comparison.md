# XMclaw vs Hermes vs OpenClaw — 量化对比报告

## 1. 工具面大小（Schema Token 占用）

| 项目 | 工具数 | Action 参数 | Schema Tokens (估算) | vs XMclaw |
|------|--------|-------------|---------------------|-----------|
| XMclaw | 23 | 0 | ~8,050 | 1.0x |
| Hermes | 1 | 13 | ~800 | 0.10x |
| OpenClaw (Codex CUA) | 1 | 10 | ~800 | 0.10x |

**关键差距**：XMclaw 的 schema 占用是 Hermes/OpenClaw 的 **4.4x**。在 128K 上下文窗口中，这直接减少了可用历史记录空间，更容易触发压缩/截断。

**根因**：XMclaw 的 23 个独立工具每个都有完整的 `description` + `parameters_schema` JSON，而 Hermes/OpenClaw 只有 1 个 schema，内部通过 `action` enum 区分。模型的 prompt 中，每个 ToolSpec 都是独立的 JSON 对象，token 线性增长。

---

## 2. 执行轮次（点击一个按钮）

| 场景 | XMclaw 轮次 | Hermes 轮次 | OpenClaw 轮次 | 加速比 |
|------|------------|-------------|---------------|--------|
| 点击按钮 | ~4 轮 | 2 轮 | 2 轮 | **2.0x** |
| 输入文本 | ~4 轮 | 2.5 轮 | 2.5 轮 | **1.6x** |

**XMclaw 标准流程**（点击按钮）：
1. `screen_capture` → 模型看到截图
2. `mouse_click(x, y)` → 返回 "ok"
3. `screen_capture`（验证） → 模型确认效果
4. [可选] `verify_text` 或重试

**Hermes/OpenClaw 标准流程**（点击按钮）：
1. `capture(mode="som")` → 模型看到截图 + 元素编号列表
2. `click(element=3, capture_after=True)` → 返回 ok + 新截图（自动验证）

**关键差距**：`capture_after=True` 是架构级优化。它把"动作 + 验证"合并为一次工具调用，XMclaw 没有这个功能，所以每步 mutating 操作后必须额外调用 `screen_capture` 确认。

**时间估算**（假设 3.5s/轮 = 2s LLM 推理 + 1s 工具执行 + 0.5s 网络往返）：
- XMclaw 点击按钮：4 × 3.5s = **14s**
- Hermes 点击按钮：2 × 3.5s = **7s**
- OpenClaw 点击按钮：2 × 3.5s = **7s**

**更复杂的场景**（如 gui_send_chat 的 8 步操作）：
- XMclaw：1 个 atomic tool 内部硬编码 8 步，模型看不到中间状态，任何一步失败整个 tool 失败
- Hermes：拆成 4 个独立的 `computer_use` 调用，每步都有截图验证，逐步推进
- 在 8 步场景中，Hermes 的逐步推进比 XMclaw 的 atomic 失败-重试更快，因为模型能精确定位失败点

---

## 3. 坐标系可靠性（模型认知负担）

| 维度 | XMclaw | Hermes | OpenClaw |
|------|--------|--------|----------|
| 模型计算坐标 | 是 — 模型必须从截图中计算 (x, y) | 否 — 后端计算元素索引 | 否 — 后端计算元素索引 |
| DPI 感知 | 手动 — 模型必须理解 `click_scale` 提示 | 自动 — cua-driver 处理 | 自动 — CUA 驱动处理 |
| OCR 依赖 | 高 — `click_on_text` 内部用 OCR 查找坐标 | 无 — UIA 直接枚举元素 | 无 — SOM 覆盖层直接标注 |
| 滚动漂移 | 有 — 窗口滚动后坐标变化，模型需重新计算 | 无 — 元素索引随滚动保持 | 无 — 元素索引稳定 |
| 窗口粘滞 | 无 — 每轮调用独立，无 `_active_pid` | 有 — `_active_pid` / `_active_window_id` | 有 — CUA 维护窗口状态 |

**根因**：XMclaw 的 `mouse_click` 接受像素坐标 `(x, y)`，模型需要做坐标推理。这受以下因素影响：
- DPI 缩放（Windows 125%/150% 下物理像素 vs 逻辑坐标不匹配）
- OCR 误读（RapidOCR 对中文简写、按钮、图标的误读率是常态）
- 窗口滚动（滚动后坐标系变化，模型需重新截图重新计算）
- 焦点竞争（没有粘滞状态，其他窗口弹窗干扰导致点击错窗口）

Hermes/OpenClaw 的 SOM（Set-of-Mark）模式：
- 后端通过 UIA / Accessibility API 枚举可交互元素
- 计算每个元素在截图中的位置
- 在截图上叠加编号（1, 2, 3...）
- 模型只需输出 `element=3`，无需任何坐标计算

---

## 4. 安全架构对比

| 维度 | XMclaw | Hermes | OpenClaw |
|------|--------|--------|----------|
| 快捷键硬阻断 | 无 — 依赖 guardian 配置 | 有 — `_BLOCKED_KEY_COMBOS` frozenset 常数时间查找 | 有 — CUA 原生安全层 |
| 文本危险模式 | 无 — guardian 按动作分级 | 有 — `_BLOCKED_TYPE_PATTERNS` 正则预过滤 | 有 — CUA 原生过滤 |
| 动作分级 | Guardian 按动作类型分级 | `_SAFE_ACTIONS` / `_DESTRUCTIVE_ACTIONS` 集合 | 依赖 CUA 安全模型 |
| 坐标漂移容错 | 5 点重试（事后补救） | 元素索引（事前预防，零漂移） | 元素索引（事前预防） |

**差距**：XMclaw 的 guardian 是事后审批机制（操作前问用户是否允许），而 Hermes 是事前硬阻断（危险操作在 tool 层直接拒绝）。

---

## 5. 实现策略对比

| 项目 | XMclaw | Hermes | OpenClaw |
|------|--------|--------|----------|
| 实现方式 | 全部自研（3381 行） | 全部自研（cua-driver） | **代理给专业工具**（MCP/CLI） |
| 核心依赖 | pyautogui + OCR + 坐标计算 | cua-driver + UIA + SOM | Codex CUA MCP / Peekaboo CLI |
| 维护负担 | 高 — 维护 23 工具 + 坐标系 + OCR + 重试 | 中 — 维护 cua-driver + schema | **低** — 安装配置即可 |
| 扩展方式 | 新增工具（增加 schema 负担） | 新增 action 参数 | 换 MCP 服务器或 CLI |
| 平台 | Windows 原生 | macOS 原生 | macOS（通过代理） |

**关键洞察**：OpenClaw 的架构选择是"专业领域交给专业实现"。它的 `extensions/codex` 只有 695 行代码，只负责插件安装和 MCP 状态检查，实际桌面控制由 Codex 的 `computer-use` MCP 服务器处理。这降低了维护复杂度，同时享受了 CUA 驱动的专业实现（元素索引、粘滞状态、安全阻断）。

---

## 6. 非 Vision 模型回退

| 项目 | XMclaw | Hermes | OpenClaw |
|------|--------|--------|----------|
| 非 vision 模型支持 | 基本无法使用（无图片描述） | 自动路由到 `auxiliary.vision` | 通过 CUA 的文本描述模式 |
| 回退机制 | 无 | `mode="ax"` 文本描述 + 元素列表 | `modelHasVision` 控制图片内联 |

**差距**：XMclaw 没有为非 vision 模型提供回退路径。如果主模型没有 vision 能力，截图对模型完全无用。Hermes 检测到主模型非 vision 时，自动路由到辅助 vision 管道，把截图转为文本描述返回。

---

## 7. 可借鉴的具体改进点（按收益排序）

### 从 Hermes 借鉴（已识别 6 点）：

1. **单一 `computer_use` 工具 + `action` 参数** — 已于 2026-06-18 完成 ✅
   - 文件：`xmclaw/providers/tool/computer_use.py`
   - 效果：schema token 从 ~8,050 降到 ~800

2. **SOM 元素索引** — 待完成
   - 复用现有 `uiautomation` 遍历代码（`_ui_inspect` 已枚举元素）
   - 在 `capture(mode="som")` 时，用 PIL 在截图上叠加编号圆圈
   - 返回 `elements` 列表：`[{index, name, control_type, bbox}]`
   - 模型用 `element=3` 替代坐标 `(x, y)`

3. **`capture_after` 合并动作+验证** — 已于 2026-06-18 完成 ✅
   - mutating action 默认 `capture_after=True`
   - 动作后自动截图并写入 `metadata.attach_image`
   - 每步节省 1 轮往返

4. **`_active_pid` 粘滞状态** — 已于 2026-06-18 完成 ✅
   - `capture()` 后自动记录 frontmost 窗口
   - 后续 `click()`/`type()` 自动命中同一窗口

5. **快捷键硬阻断** — 已部分实现，需对齐 Hermes 的完整列表
   - Hermes 的 `_BLOCKED_KEY_COMBOS` 包含：`cmd+shift+backspace`（清空废纸篓）、`cmd+ctrl+q`（锁屏）、`cmd+shift+q`（注销）
   - XMclaw 当前只拦截了 `alt+f4`、`win+l`、`ctrl+alt+del`

6. **文本危险模式预过滤** — 待完成
   - Hermes 的 `_BLOCKED_TYPE_PATTERNS` 包含：`curl ... | bash`、`sudo rm -rf`、`rm -rf /`
   - 在 tool 层直接阻断，不依赖 guardian 配置

### 从 OpenClaw 借鉴（已识别 3 点）：

1. **代理架构** — 长期方向
   - Windows 上寻找等价 CUA 驱动或 UI 自动化框架（如 `Playwright` + `uiautomation` 桥接）
   - 通过 MCP 或类似机制代理，XMclaw 只负责配置和 schema 定义
   - 当前 Windows 缺乏成熟等价物，全量自研更现实

2. **工具面预过滤** — 待完成
   - OpenClaw 的 `nodes` 工具已采用 `action` 参数模式（单一工具）
   - XMclaw 的 `browser`（30 工具）、`builtin`（40+ 工具）需要同样处理

3. **MCP 注册机制** — 长期方向
   - 通过标准化接口注册外部工具，降低维护成本
   - 复用 OpenClaw 的 `mcpServerStatus/list` 和 `plugin/install` 模式

---

## 8. 尚未修复的差距清单

- [ ] SOM 截图覆盖（Phase 1：uiautomation 依赖 Windows，非 Windows 环境优雅降级）
- [ ] 非 vision 回退（需要模型层在调用参数中传入 `vision=False`）
- [ ] 危险动作硬阻断（快捷键和文本模式需扩展完整列表）
- [ ] Browser 30 工具合并（同 computer_use 问题，参照重构）
- [ ] Builtin 40+ 工具合并（memory 系列 8 个 → 1 个，canvas 系列 3 个 → 1 个）
- [ ] Guardian 按 tool_name 索引（O(n) → O(1)，每次工具调用都遍历所有 guardian）
- [ ] Memory recall 批量 graph 查询（10x LanceDB 往返 → 1 次批量查询）
- [ ] Token count 增量缓存（每 turn 20 万次字符遍历 → 增量更新）
- [ ] Skill 预过滤缓存（每 turn 遍历 400+ skills → per-session LRU 缓存）

---

## 9. 总结：为什么 Hermes/OpenClaw 更丝滑更快

**根因不是"功能更多"，而是"模型执行路径更短"**：

1. **Schema 小了 4.4x** → 更多上下文留给历史记录，更少压缩
2. **轮次少了 2x** → `capture_after` 合并动作+验证，每步省 1 轮
3. **坐标零漂移** → 元素索引替代像素坐标，消除 OCR 误读和 DPI 问题
4. **上下文粘滞** → 窗口状态记住，减少重新定位操作
5. **事前阻断** → 危险操作在 tool 层直接拒绝，不需要 guardian 审批往返
6. **代理架构** → OpenClaw 把专业工作交给专业实现，自己只负责配置

**XMclaw 的 3381 行 `computer_use.py` 试图用 pyautogui + OCR 自己实现全套桌面控制，这是问题的根源。Hermes 和 OpenClaw 都选择了"元素索引 + 单一工具 + 后端粘滞状态"的架构，这才是正确的方向。**
