# XMclaw 全面修复计划

## 已确认的 Bug（含测试暴露 + 截图指向）

### P0 — 生产级 Bug（影响 daemon 运行）

1. **B-227 retry 在 instant 模式完全缺失**
   - `_run_instant_single_shot` 直接调用 `llm.complete_streaming`，无任何 classify-and-retry 保护
   - 导致 trivial greeting（"hello"/"hi"）触发 rate_limit / 503 时直接崩溃，不 retry
   - 文件：`xmclaw/daemon/agent_loop.py:1586`
   - 修复：将 B-227 retry 逻辑从 `_run_hop_loop` 提取为可复用函数，在 instant 模式复用

2. **`pkg_resources` 缺失导致 feishu adapter 启动崩溃**
   - `feishu/adapter.py:506` 直接 `import pkg_resources`，在 `uv` 或精简环境无 setuptools 时崩溃
   - 文件：`xmclaw/providers/channel/feishu/adapter.py`
   - 修复：try/except 包裹，缺失时优雅降级

3. **`_is_placeholder_api_key` 过度拒绝合法测试 key**
   - `"sk-test"` / `"sk-ant-test"` 被当作 placeholder 拒绝，导致 `build_llm_from_config` 返回 None
   - 文件：`xmclaw/daemon/factory.py:411`
   - 修复：缩小拒绝范围，仅拒绝空字符串和全零/占位符

### P1 — 架构重构导致的不一致

4. **Browser 工具统一化为 `browser` 后，测试未同步更新**
   - `list_tools()` 只返回 `browser` 一个工具，但多个测试仍期望 `browser_open` 等旧名
   - 文件：`tests/unit/test_v2_browser_tools.py`、`test_v2_default_tools_full_set.py`、`test_user_browser.py`
   - 修复：更新测试断言匹配新 unified spec；或让 `list_tools()` 同时返回 legacy 别名（零回归策略）

5. **instant mode 绕过 hop loop，导致带 tool 的 trivial 测试失败**
   - `test_b332_run_turn_no_allowlist_unaffected` 等测试发 "hi" 被路由到 instant，tool 不执行
   - 文件：`tests/unit/test_v2_filtered_tool_provider.py`、retry 测试等
   - 修复：测试中强制 `forced_mode="agent"` 或发非 trivial 消息

### P2 — 数据层逻辑漂移

6. **curator / dedup 测试期望与实际行为不匹配**
   - `test_curate_marks_contradiction_and_downweights_weaker`：confidence 阈值从 0.4 变为 0.5
   - `test_llm_dedup_commits_merges_when_not_dry_run`：surviving_ids 选择逻辑变化
   - `test_auto_extract_calls_remember_per_candidate`：extractor 产出 2 个而非 1 个 fact
   - 文件：`tests/unit/test_v3_curator.py`、`test_v3_llm_dedup.py`、`test_v2_phase7_agent_loop_memory.py`
   - 修复：确认是预期行为变更后更新测试断言

### P3 — 环境/配置

7. **Playwright chromium 版本不匹配**（截图）
   - 系统安装 `chromium-1228` 但配置/缓存指向 `chromium-1223`
   - 非代码 bug，属环境维护：建议 `playwright install chromium` 刷新

8. **daemon 未响应**（截图）
   - 需确认：daemon 进程是否崩溃？端口是否被占？
   - 可能由上述 P0 bug（instant retry 崩溃 / feishu 启动崩溃）连锁导致

## 执行顺序

Stage 1: P0 修复（instant retry + pkg_resources + placeholder key）
Stage 2: P1 修复（browser 工具兼容 + 测试强制 mode）
Stage 3: P2 修复（curator/dedup/memory 测试期望同步）
Stage 4: 运行测试验证，确认 16 个失败 → 0 失败
Stage 5: git commit
