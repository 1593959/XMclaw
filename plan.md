# XMclaw 三线修复计划

> 锚定日期：2026-06-16  
> 工作空间：`C:\Users\15978\Desktop\XMclaw`

---

## 问题清单

1. **PyAutoGUI 精准度**（`xmclaw/providers/tool/computer_use.py`）
   - 坐标定位：click_on_text / click_on_image 单次执行，无重试；DPI 缩放导致坐标偏移时无补偿。
   - 截图匹配：find_image_on_screen 仅使用单尺度 cv2.matchTemplate，无法应对 UI 缩放/高分屏。
   - 重试策略：mouse_click / click_on_text / click_on_image / click_on_image 均没有 retry loop，一次失败即返回 ok=False，agent 需重新截图→OCR→点击，浪费 hop。

2. **记忆写入超时**（`xmclaw/memory/v2/backend_lancedb.py`）
   - 现状：LanceDB 的 async Python API 底层是 Rust I/O，可能不 yield 回 asyncio loop，导致外层 30s 超时形同虚设（实际是“傻等”）。
   - 用户倾向：不做“把 30s 调长”的表面配置，而是做根因加固——给 LanceDB query/put 加内层 timeout + 失败重试（指数退避），避免事件循环被挂住。

3. **Anthropic cache_breakpoints_over_budget**（`xmclaw/providers/llm/anthropic.py`）
   - 现状：每轮 system breakpoints 可能达到 5（frozen + autobio + 其它），加上 tools=1，总预算 4。代码只在超限后打 warning log，继续把超额的 cache_control 塞进请求 → API 静默丢弃 → 全量 re-bill → 拖慢+增成本。
   - 修复：在构造请求时强制截断到 ≤4 个 breakpoint，优先保留 system 前缀和 messages 尾部（命中最高），必要时丢弃中间断点，而不是依赖 API 静默丢弃。

---

## 阶段划分

### Stage 1 — PyAutoGUI 精准度加固（investigate + fix）

**子任务 1a：坐标定位重试**
- 在 `_mouse_click` 中增加 `retry=1` 机制：若提供了 `verify_text` 且验证失败，自动在 (x±3, y±3) 像素邻域内尝试 1 次偏移点击。
- 在 `_click_on_text` 和 `_click_on_image` 中同样增加 fallback：如果 verify_text 失败，在目标 bbox 中心、左上角、右下角各试一次（共 3 次），成功后返回 `retried: true` 和实际命中坐标。

**子任务 1b：图像模板多尺度匹配**
- `_find_image_on_screen` 增加 `multi_scale=True`：在 0.75x–1.25x 之间做 3 层金字塔 matchTemplate，取最佳 match。
- 返回 `scale_used` 字段，方便下游调试。

**子任务 1c：mouse_click 像素级确认**
- 增加 `_verify_pixel_after_click` 辅助：在点击前记录目标坐标像素颜色，点击后检测颜色是否变化（简单但有效），避免“点击了但 UI 没响应”的假阳性。
- 仅当 `verify_pixel=true` 时启用，默认关闭（避免额外截图开销）。

**子任务 1d：click_on_text 的 OCR 漂移容错**
- 当前 `_match_text_in_blocks` 是 exact/substring 匹配。增加 `fuzzy_match` 策略：当 exact 失败时，用 difflib.SequenceMatcher 找 best ratio > 0.7 的候选，防止 OCR 漏字导致点击失败。

### Stage 2 — LanceDB 内层超时 + 重试（investigate + fix）

**子任务 2a：LanceDB 操作封装**
- 新建 `xmclaw/memory/v2/lancedb_utils.py`：提供 `async def _with_timeout_and_retry(coro, timeout_s=10, max_retries=2, context="")`。
- 使用 `asyncio.wait_for(asyncio.shield(coro), timeout=timeout_s)` 确保即使 Rust 不 yield，Python 侧也能在 timeout_s 后抛 `TimeoutError`。
- 捕获 `TimeoutError`、`RuntimeError(lance error)`、`ConnectionError` 做指数退避重试（delay=1s, 2s）。
- 若连续失败，再标记 `_corrupted=True`，避免永远 retry。

**子任务 2b：后端接入**
- 在 `LanceDBVectorBackend` 的 `upsert`、`search`、`delete`、`count`、`get` 中，把所有直接 `await lance_op` 替换为 `await _with_timeout_and_retry(lance_op, ...)`。
- 在 `LanceDBGraphBackend` 的 `add_relations`、`remove_relation`、`neighbors`、`reverse_neighbors`、`all_nodes` 中同样接入。
- 保持 `_handle_lance_error` 作为最后一道防线，但 `_with_timeout_and_retry` 先吃掉了超时和瞬态错误，减少进入 corruption 的概率。

### Stage 3 — Anthropic cache breakpoint 预算硬截断（investigate + fix）

**子任务 3a：预算硬截断**
- 在 `_messages_to_anthropic` 中，把 `_MAX_BREAKPOINTS = 4` 的 enforcement 从“只 warning”改为“实际截断”。
- 策略：
  - system blocks：保留前 `min(_sys_bp_count, _MAX_BREAKPOINTS - 2)` 个断点（确保至少留 1 个给 tools，1 个给 messages）。
  - tools：保留 1 个（在 `_tools_to_anthropic` 中不变）。
  - messages：`_mark_history_cache_breakpoint` 只在总预算还有余量时才打标记。
- 实现方式：在 `_messages_to_anthropic` 末尾，统计已放置的 `cache_control` 数量；若 ≥4，则不再调用 `_mark_history_cache_breakpoint`，或者如果已经调用了，则 strip 掉最后一个 message 的 cache_control。

**子任务 3b：audit 字段**
- 在 `LLMResponse` 或内部日志中增加 `cache_breakpoints_used` / `cache_breakpoints_dropped` 字段，让运维能看到每轮实际用了几个断点， dropped 几个。

---

## 执行顺序

| 顺序 | 阶段 | 技能/代理 | 说明 |
|------|------|-----------|------|
| 1 | Stage 1 | `coder` 子代理 | 修改 `computer_use.py`，增加重试+多尺度+fuzzy |
| 2 | Stage 2 | `coder` 子代理 | 新建 `lancedb_utils.py` + 修改 `backend_lancedb.py` |
| 3 | Stage 3 | `coder` 子代理 | 修改 `anthropic.py`，硬截断 cache breakpoints |
| 4 | 集成验证 | 主代理 | 运行相关单测，确认无回归；出 patch 总结 |

---

## 依赖文件

- `xmclaw/providers/tool/computer_use.py`（Stage 1）
- `xmclaw/memory/v2/backend_lancedb.py`（Stage 2）
- `xmclaw/memory/v2/backend.py`（Stage 2，看协议接口）
- `xmclaw/providers/llm/anthropic.py`（Stage 3）
- `xmclaw/providers/llm/base.py`（Stage 3，LLMResponse 结构）
