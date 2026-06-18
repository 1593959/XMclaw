# 第二轮架构效率整改方案

> 基于第二轮排查结果（8 个新发现的架构级效率问题），给出整改方案。

---

## 问题 1：LLM 图片重复处理（P1）

**文件**：`xmclaw/providers/llm/anthropic.py:1095-1150`, `openai.py:1019-1063`

**根因**：每次 LLM 调用都会把用户消息里的图片走一遍 `_img_to_anthropic_block`（磁盘读取 → PIL resize → base64）。多 hop turn 中同一张截图被反复处理 5 次。

**整改方案**：
1. 在 AnthropicProvider / OpenAIProvider 中新增 `_image_block_cache: dict`（按 `(file_path, width, height)` 做 key）
2. `_img_to_anthropic_block` 先查缓存，命中则直接返回已 base64 编码的 block
3. 缓存 TTL 为 5 分钟（防止文件修改后 stale）
4. 测试：`test_llm_image_cache.py` — 验证同一张图片第二次调用命中缓存

**预期收益**：5-hop turn + 1 张 4MB 截图 → 从 5 次磁盘读取+resize+base64 降到 1 次

---

## 问题 2：AgentLoop 每次 turn 全量消息序列化写入磁盘（P1）

**文件**：`xmclaw/daemon/agent_loop.py:3432-3448`

**根因**：`run_turn` 每次把完整 `messages` 列表通过 `_message_to_dict` + `json.dumps` 写入 `~/.xmclaw/v2/inflight/{session_id}.json`。

**整改方案**：
1. 改为**增量 checkpoint**：只写入自上次 checkpoint 以来的新消息
2. 去掉每次 `mkdir(parents=True, exist_ok=True)`，改为首次创建后复用
3. 或者改为后台线程写入（`asyncio.to_thread`），不阻塞主 agent loop
4. 测试：`test_agent_loop_checkpoint.py` — 验证增量写入、后台不阻塞

**预期收益**：50 条消息/turn → 从写入数 MB JSON 降到只写增量（通常 1-3 条新消息）

---

## 问题 3：每次 hop 都全量遍历消息转换（P2）

**文件**：`xmclaw/providers/llm/anthropic.py:136`, `openai.py:223`

**根因**：`_messages_to_anthropic` 在每次 LLM 调用（每个 hop）都从头遍历整个 `messages` 并构建新的 dict 列表。5-hop turn 里同一批历史消息被转换 5 次。

**整改方案**：
1. 在 `Message` 对象（或 provider 层）缓存 provider 专用块（`_openai_dict` / `_anthropic_block`）
2. 如果 `Message` 是 frozen dataclass，缓存是安全的（内容不变）
3. 新增 `Message.to_provider_dict(provider_name)` 方法，内部查缓存
4. 测试：`test_message_provider_cache.py` — 验证同一消息第二次转换命中缓存

**预期收益**：50 消息 × 5 hop = 250 次全量转换 → 降到 50 次（首次）+ 缓存命中（后续）

---

## 问题 4：LanceDB 连接未共享（P2）

**文件**：`xmclaw/memory/v2/backend_lancedb.py:265, 317, 769`

**根因**：`LanceDBVectorBackend` 与 `LanceDBGraphBackend` 各自独立调用 `lancedb.connect_async()`，启动时两次 connect + 两次 open_table。

**整改方案**：
1. 引入按 `db_path` 的单例 `AsyncConnection` 管理器
2. 在 `__init__` 中注入共享 connection，而不是各自新建
3. 测试：`test_lancedb_connection_shared.py` — 验证两个 backend 使用同一连接

**预期收益**：启动时 connect 次数从 2 降到 1，消除潜在锁竞争

---

## 问题 5：同一 turn 内两次 `list_tools()`（P2）

**文件**：`xmclaw/daemon/agent_loop.py:2763-2764`, `3064`

**根因**：同一 turn 内分别在校验 `propose_curriculum_edit` 和主 tool 装配时两次调用 `effective_tools.list_tools()`。

**整改方案**：
1. 在 `run_turn` 开始时缓存 `tool_specs = effective_tools.list_tools()`
2. 后续复用同一列表
3. 仅在 provider 信号变更时（如 skill 动态注册）重新构建
4. 测试：`test_agent_loop_tool_spec_cache.py` — 验证同一 turn 内只调用一次

**预期收益**：2 × 100+ 规格构建 → 降到 1 次

---

## 问题 6：output_schema 每次 turn 重新 json.dumps（P2）

**文件**：`xmclaw/daemon/agent_loop.py:3358-3360`

**根因**：若调用方传入 `output_schema`，每次 turn 都重新 `json.dumps(output_schema, indent=2)`。

**整改方案**：
1. 在 `AgentLoop` 中缓存 `_schema_block_cache: dict[hash, str]`
2. 按 `id(schema)` 或 `hash(json.dumps(schema))` 做 key
3. 测试：`test_schema_cache.py` — 验证相同 schema 第二次命中缓存

**预期收益**：大 JSON Schema 每次 turn 重新 dumps → 降到 1 次

---

## 问题 7：Feature Flags 每次 set 都全量写盘（P2）

**文件**：`xmclaw/core/feature_flags/engine.py:282-299`

**根因**：`_save_disk` 在每次 `set()` 后都走 `tmp.write_text + os.replace` 写完整 `features.json`，无防抖。

**整改方案**：
1. 引入 debounce：收集 500ms 内的所有变更，一次性写入
2. 或改为批量写入：每 5 次 set 后统一 flush
3. 测试：`test_feature_flags_debounce.py` — 验证高频 set 只写 1 次

**预期收益**：高频变更时从 N 次全量写盘 → 降到 1 次

---

## 问题 8：CompositeToolProvider router miss 时实时重扫（P2）

**文件**：`xmclaw/providers/tool/composite.py:60-64`

**根因**：当静态 router 找不到工具名时，遍历每个 child 并调用 `c.list_tools()` 做实时重扫。

**整改方案**：
1. 在 `SkillRegistry` 增删技能时主动通知 `CompositeToolProvider` 更新 `_router`
2. 消除 invoke 热路径中的 fallback 重扫
3. 测试：`test_composite_router_no_rescan.py` — 验证动态注册后 router 自动更新

**预期收益**：消除热路径中的 O(n²) 重扫

---

## 执行优先级

| 优先级 | 修复项 | 原因 |
|--------|--------|------|
| **P1** | 图片重复处理（问题1） | 多 hop 中同一张图被反复处理，磁盘+CPU开销大 |
| **P1** | 全量消息序列化写入（问题2） | 每次 turn 数 MB 写入，磁盘压力 |
| **P2** | 消息转换缓存（问题3） | 250次全量转换 → 缓存命中 |
| **P2** | LanceDB 连接共享（问题4） | 启动时 2x 连接 |
| **P2** | list_tools 缓存（问题5） | 同一 turn 2x 调用 |
| **P2** | output_schema 缓存（问题6） | 每次 turn 重新 dumps |
| **P2** | Feature Flags debounce（问题7） | 高频 set 全量写盘 |
| **P2** | Composite router 实时更新（问题8） | 热路径 O(n²) 重扫 |

---

## 无问题的方向（第二轮确认）

- 传输层（WebSocket）：单次 json.dumps 后广播，无逐条重复
- 加密/哈希：无每请求 JWT 完整验签
- 启动时间：无重量级顶层 import
- 内存/GC：无 deepcopy 滥用
- SQLite：单连接 + 每操作 cursor，符合最佳实践
