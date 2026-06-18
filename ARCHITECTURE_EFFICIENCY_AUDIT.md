# XMclaw 架构效率审计报告

## 审计范围
- 工具面大小：xmclaw/providers/tool/ 下全部 20+ 个 provider
- Security guardian：xmclaw/security/tool_guard/ + rule_loader + llm_classifier
- Memory 系统：xmclaw/memory/v2/（service, gateway, backend, embedding, recall）
- 事件总线：xmclaw/core/bus/（InProcessEventBus + SqliteEventBus）
- Provider 注册：CompositeToolProvider + ChannelRegistry + MCPHub
- Context 组装：AgentLoop.run_turn + ContextCompressor

---

## 1. 工具面过大（Tool Surface Bloat）

### 问题 1.1：BrowserTools 暴露 30 个独立工具
- **文件路径/行号**：`xmclaw/providers/tool/browser.py:1105-1131`
- **问题类型**：工具面过大
- **量化影响**：30 个 ToolSpec × 每个平均 200-500 token 的 description + schema = 约 6,000-15,000 token 的上下文占用。每轮 LLM 调用都携带这些 schema，即使 agent 只需要 `browser_open` 和 `browser_screenshot`。
- **根因**：与已修复的 computer_use 同样的问题——每个 browser 操作（click、fill、scroll、tabs、dialog、network_log 等）都是独立工具，而非 `browser(action="...")` 的单一工具 + action 参数模式。
- **修复建议**：参照 computer_use 2026-06-18 的重构，将 30 个 browser_* 工具合并为 1 个 `browser` 工具 + `action` 参数（保留 backward-compat deprecation layer）。
- **优先级**：P1

### 问题 1.2：BuiltinTools 动态暴露 40+ 工具
- **文件路径/行号**：`xmclaw/providers/tool/builtin.py:392-532`
- **问题类型**：工具面过大
- **量化影响**：list_tools() 动态组装，典型生产环境返回 40+ 个 ToolSpec（文件操作 6 个 + bash + web 4 个 + todo 2 个 + memory 10+ 个 + persona 4 个 + curriculum 2 个 + schedule + sqlite + note + journal + agent_status + ask_user + send_media + worktree 2 个 + plan_mode 2 个 + canvas 3 个 + think + voice 2 个 + output_style + ...）。总 token 占用约 15,000-25,000。
- **根因**：BuiltinTools 是 Mixin 组合模式，每个 Mixin 追加自己的工具，没有合并机制。大量工具（如 memory_forget / memory_correct / memory_dedup / memory_inspect / memory / memory_get / memory_graph_neighbors）可以合并为 `memory(action="...")` 的单一工具。
- **修复建议**：
  1. 将 memory 系列 8 个工具合并为 1 个 `memory` 工具 + action 参数（保留 backward-compat）
  2. 将 canvas 系列 3 个工具合并为 1 个 `canvas` 工具
  3. 将 worktree / plan_mode 的 enter/exit 对合并为单一工具
- **优先级**：P1

### 问题 1.3：AndroidRemote 暴露 12 个工具
- **文件路径/行号**：`xmclaw/providers/tool/android_remote.py:230-244`
- **问题类型**：工具面过大
- **量化影响**：12 个独立工具（phone_open_app, phone_click, phone_tap, phone_input, phone_swipe, phone_key, phone_screenshot, phone_ui_tree, phone_notification, phone_wait, phone_clip_get, phone_clip_set）。
- **根因**：与 computer_use/browser 同样的问题——每个 GUI 操作都是独立工具。
- **修复建议**：合并为 1 个 `phone` 工具 + action 参数（如 `phone(action="tap", x=..., y=...)`）。
- **优先级**：P2

### 问题 1.4：Integrations 暴露 12+ 个发送工具
- **文件路径/行号**：`xmclaw/providers/tool/integrations.py:391-430`
- **问题类型**：工具面过大
- **量化影响**：webhook_send + rss_fetch + 各平台 send 工具（email, slack, telegram, discord, github, notion, feishu, wecom, dingtalk, qq）共 12+ 个。
- **根因**：每个集成平台都是独立工具，可以合并为 `send_message(platform="...", ...)` 的单一工具 + 平台选择器。
- **修复建议**：合并为 1 个 `send_message` 工具 + `platform` 参数（保留 webhook 和 rss_fetch 作为独立工具，因为它们语义不同）。
- **优先级**：P2

### 问题 1.5：MCPHub 工具数量无上限
- **文件路径/行号**：`xmclaw/providers/tool/mcp_hub.py:456-470`
- **问题类型**：工具面过大
- **量化影响**：每个 MCP 服务器可以注册任意数量工具，没有上限。一个 busy MCP server 可能注册 50+ 工具，全部通过 `list_tools()` 暴露给 LLM。
- **根因**：MCPHub 没有工具数量限制或分页机制，也没有类似 skill prefilter 的 relevance filtering。
- **修复建议**：
  1. 对每个 MCP server 设置工具数量上限（如 max 20 per server）
  2. 或者引入 per-user-query 的 MCP 工具 relevance prefilter（复用 skill prefilter 的逻辑）
- **优先级**：P1

---

## 2. Security Guardian 性能

### 问题 2.1：ToolGuardEngine 每次调用线性遍历所有 guardians
- **文件路径/行号**：`xmclaw/security/tool_guard/engine.py:80-127`
- **问题类型**：同步阻塞 / O(n) 遍历
- **量化影响**：guard() 方法 `for g in self._guardians` 遍历所有 guardian。当前 guardian 数量通常 3-5 个，看似不大，但每个 guardian 内部可能做更多工作。当 guardian 数量增长到 10+ 时（如未来增加网络 guardian、LLM guardian），每次工具调用都线性遍历。
- **根因**：没有按 tool_name 索引 guardian。engine 知道 `is_guarded()` 可以快速判断，但 guard() 仍然遍历所有。
- **修复建议**：在 `__init__` 中按 tool_name 构建 `guardian_index: dict[str, list[BaseToolGuardian]]`，guard() 时只查询相关 guardian。`_ALWAYS_RUN_GUARDIANS` 可以单独存储。
- **优先级**：P1

### 问题 2.2：RuleBasedToolGuardian 每次调用遍历所有规则
- **文件路径/行号**：`xmclaw/security/tool_guard/rule_guardian.py:28-81`
- **问题类型**：同步阻塞 / O(n×m) 遍历
- **量化影响**：对每个字符串参数，调用 `scan_with_rules()` 遍历所有 YAML 规则（通常 20-50 个正则模式），对每个规则做 `pat.search(text)`。如果一次工具调用有 5 个字符串参数，就是 5 × 50 = 250 次正则搜索。
- **根因**：没有规则索引或 Aho-Corasick 类多模式匹配。每次都是独立的 regex.search() 调用。
- **修复建议**：
  1. 使用 `pyahocorasick` 或 `regex` 库的复合模式做批量匹配
  2. 或者预编译所有规则为一个大的 `(pattern1|pattern2|...)` 复合正则（注意捕获组以区分规则）
- **优先级**：P2

### 问题 2.3：scan_with_rules 没有规则索引
- **文件路径/行号**：`xmclaw/security/rule_loader.py:124-197`
- **问题类型**：同步阻塞 / 重复计算
- **量化影响**：每次 `scan_with_rules()` 调用都遍历所有 `Rule` 对象，检查 `rule.tools` 和 `rule.params` 过滤，然后对每个规则的每个 `exclude_patterns` 和 `patterns` 做正则搜索。没有预过滤或索引。
- **根因**：规则过滤是线性扫描，没有按 tool_name 或 param_name 索引规则。
- **修复建议**：在 `load_rules()` 返回时，构建 `dict[(tool_name, param_name)] -> list[Rule]` 索引，scan_with_rules 直接查索引。
- **优先级**：P2

### 问题 2.4：FilePathToolGuardian 敏感路径检查 O(n)
- **文件路径/行号**：`xmclaw/security/tool_guard/file_guardian.py:94-104`
- **问题类型**：同步阻塞 / O(n) 遍历
- **量化影响**：`_is_sensitive()` 对每个路径都遍历 `self._sensitive_dirs` 做 `relative_to()` 检查。如果敏感目录列表增长到 50+（如配置了大量目录），每次检查都是 O(n)。
- **根因**：使用 list/set 遍历而不是前缀树（trie）或路径哈希索引。
- **修复建议**：将敏感目录按路径前缀构建 trie 或字典树，检查降为 O(路径深度)。
- **优先级**：P2

### 问题 2.5：LLMInjectionClassifier 是同步阻塞的 LLM 调用
- **文件路径/行号**：`xmclaw/security/llm_classifier.py:127-178`
- **问题类型**：同步阻塞 / 外部 API 阻塞
- **量化影响**：当 `security.prompt_injection_llm_classifier.enabled=true` 时，每次扫描（tool_result / web_fetch / file_read）都触发一次 `llm.complete()` 调用，timeout=5s。这意味着一个包含 3 个 web_fetch 的 turn 可能额外增加 15s 的 LLM 调用延迟。
- **根因**：默认关闭（`enabled=false`），但开启后完全阻塞主线程。没有 batching、没有异步 pipeline、没有与其他 LLM 调用合并。
- **修复建议**：
  1. 保持默认关闭
  2. 如果必须开启，改为 batching 模式：收集 N 秒内所有待扫描文本，做一次 batch LLM 调用
  3. 或者移到后台线程，不阻塞主 agent loop
- **优先级**：P0（因为开启后影响严重，但默认关闭）

---

## 3. Memory 系统查询效率

### 问题 3.1：MemoryService.remember() 每次写入触发多次独立查询
- **文件路径/行号**：`xmclaw/memory/v2/service.py:684-1078`
- **问题类型**：重复计算 / 多次独立查询
- **量化影响**：单次 remember() 调用链：
  1. `embedder.embed(text)` — 1 次 embedding API 调用
  2. `_find_near_duplicate()` — 1 次 `_vec.search()`（KNN 查询）
  3. 如果 existing 为 None 且不做 skip：relation scan — 第 2 次 `_vec.search()`（带 where 条件）
  4. `_auto_link_relations()` — 1 次 `_graph.add_relations()`（可能批量）
  5. `entity.register_fact_text()` — 1 次 entity store 写入
  6. `upsert()` — 1 次 LanceDB merge_insert
  单次写入 = 2 次向量搜索 + 1 次 embedding + 1 次 graph 写入 + 1 次 entity 写入 + 1 次 LanceDB upsert。
  在并发写入场景下，LanceDB 连接可能成为瓶颈。
- **根因**：near-dup 检测和 relation scan 是独立的两次 vector search，没有合并。
- **修复建议**：
  1. 合并 near-dup 和 relation scan 为一次向量搜索（扩大 limit，然后分别检查距离阈值）
  2. 将 embedding 调用批量化（如果 caller 是批量写入，先 embed_batch 再逐个处理）
- **优先级**：P1

### 问题 3.2：MemoryService.recall() 对每个 hit 单独做 graph 查询
- **文件路径/行号**：`xmclaw/memory/v2/service.py`（recall 相关逻辑）
- **问题类型**：重复计算 / 多次独立查询
- **量化影响**：recall() 中 `for each hit, fetch its 1-hop neighbours` — 对每个 hit 调用 `_graph.neighbors()` 和 `_graph.reverse_neighbors()`。如果 k=10，就是 10 次独立的 graph 查询。LanceDB 的 graph 表是单独的 table，每次查询都是一次 SQL WHERE + 网络往返。
- **根因**：没有批量 graph 查询 API。`neighbors()` 只接受单个 fact_id。
- **修复建议**：添加 `neighbors_batch(fact_ids: list[str])` API，一次查询所有 hit 的邻居（`WHERE source_fact_id IN (id1, id2, ...)`）。
- **优先级**：P1

### 问题 3.3：CognitiveMemoryGateway._think() 是同步 LLM 调用
- **文件路径/行号**：`xmclaw/memory/v2/gateway.py:361-448`
- **问题类型**：同步阻塞 / 外部 API 阻塞
- **量化影响**：每个需要 THINK 的 observation 都触发一次 `llm.complete()` 调用（complete，不是 streaming）。如果一次 turn 提取了 5 个 observations，就是 5 次独立的 LLM 调用。每次调用可能 1-3s（fast tier），总共 5-15s 的阻塞延迟。
- **根因**：THINK 层是顺序执行的，没有 batching。
- **修复建议**：
  1. 对 Tier-1 信号（identity/preference/环境配置）已经做了 fast-path bypass（好）
  2. 对非 Tier-1 的批量 observations，改为 batch THINK：一次 LLM 调用处理多个 observations
  3. 或者将 THINK 移到后台任务（fire-and-forget），当前 turn 使用 passthrough digest
- **优先级**：P1

### 问题 3.4：Gateway.targeted_recall 的 hybrid 模式 rebuild BM25 每查询
- **文件路径/行号**：`xmclaw/memory/v2/gateway.py:252-331` + 注释引用
- **问题类型**：重复计算 / 同步阻塞
- **量化影响**：注释明确说明："the first version put this on the critical path with no timeout and called `recall_hybrid` (which rebuilds a Python BM25 index per query over the full corpus). A 5K-fact store took 6245s per turn." 虽然当前默认 `use_hybrid=False`，但如果用户开启，就是灾难。
- **根因**：Python BM25 索引每次查询重建，没有预建索引。
- **修复建议**：
  1. 预建 BM25 索引（如 `whoosh` 或 `rank-bm25` 的持久化索引）
  2. 或者使用 LanceDB 的 native FTS 索引（C++ 实现，不重建）
  3. 保持 `use_hybrid=False` 作为默认，直到预建索引落地
- **优先级**：P0

### 问题 3.5：AgentLoop V1 legacy recall 阻塞 60-195s
- **文件路径/行号**：`xmclaw/daemon/agent_loop.py:2186-2385`
- **问题类型**：同步阻塞 / 重复计算
- **量化影响**：虽然默认 `legacy_recall_enabled=False`，但注释明确记录："on a fat events.db (200MB+) it takes 60-195s (real trace: turn_prep memory_recall=195046ms)". 如果用户开启，每次 turn 都触发这个阻塞查询。
- **根因**：V1 MemoryManager.query() 的 hybrid RRF 实现是同步阻塞的，且 SQLite 查询可能全表扫描。
- **修复建议**：
  1. 已经在代码中默认关闭（好）
  2. 添加 deprecation warning 当用户开启时
  3. 物理移除 V1 路径（既然 V2 已经完整替代）
- **优先级**：P1

### 问题 3.6：MemoryService.bootstrap_centrality() 全图遍历 O(n) 次查询
- **文件路径/行号**：`xmclaw/memory/v2/service.py:535-573`
- **问题类型**：同步阻塞 / 多次独立查询
- **量化影响**：启动时对每个节点调用 `neighbors()` + `reverse_neighbors()`。如果图有 5000 个节点，就是 10000 次独立的 LanceDB 查询。虽然只在启动时运行，但可能延迟 daemon 启动时间。
- **根因**：没有批量图度查询 API。
- **修复建议**：使用 LanceDB 的聚合查询或 SQL 直接计算度数：`SELECT source_fact_id, COUNT(*) FROM relations GROUP BY source_fact_id`。
- **优先级**：P2

---

## 4. 事件总线/消息队列

### 问题 4.1：SqliteEventBus 每次 publish 都触发 SQLite 写入 + 线程调度
- **文件路径/行号**：`xmclaw/core/bus/sqlite.py:295-322`
- **问题类型**：同步阻塞 / 重复计算
- **量化影响**：`publish()` 使用 `asyncio.to_thread(self._conn.execute, _INSERT_SQL, _event_to_row(event))` 将每个事件写入 SQLite。在高频场景（如 LLM streaming，每 token 一个 LLM_CHUNK 事件），每秒可能产生 10-50 个事件，每个都触发线程调度 + SQLite WAL 写入。虽然 WAL 模式是并发的，但线程调度开销不可忽视。
- **根因**：没有事件 batching 或 write-behind 机制。每个事件都是独立的线程调用。
- **修复建议**：
  1. 对高频事件类型（LLM_CHUNK, LLM_THINKING_CHUNK）启用 write-behind batching：收集 100ms 内的事件，一次性 executemany
  2. 或者对这些高频事件提供可选的内存缓冲（不持久化到 SQLite）
- **优先级**：P1

### 问题 4.2：InProcessEventBus 同步遍历所有 subscribers
- **文件路径/行号**：`xmclaw/core/bus/memory.py:59-70`
- **问题类型**：同步阻塞 / O(n) 遍历
- **量化影响**：`publish()` 中 `for sub in list(self._subs)` 遍历所有 subscribers。每个 subscriber 做 predicate 检查，然后 `asyncio.create_task()`。subscribers 数量随着功能增加而增长（WS forwarder、grader、cache metrics、memory、evolution、web UI 等）。
- **根因**：没有按 EventType 索引 subscribers。如果大部分 subscribers 只关心特定事件类型，遍历所有是浪费。
- **修复建议**：按 EventType 维护 subscriber 索引，publish 时只遍历关心该事件类型的 subscribers。
- **优先级**：P2

---

## 5. Provider 注册/发现

### 问题 5.1：CompositeToolProvider.invoke() miss 时重新扫描所有 children
- **文件路径/行号**：`xmclaw/providers/tool/composite.py:51-65`
- **问题类型**：重复计算 / O(n²) 匹配
- **量化影响**：router miss 时（如 skill 动态注册后），遍历所有 children 并对每个 child 调用 `list_tools()` 重新扫描。如果 children 数量多（Builtin + Browser + MCP + Android + ...），就是 O(n×m) 的匹配。
- **根因**：静态 router 在 construction 时构建，但动态工具（如 SkillToolProvider、MCPHub）的工具变化后 router 不更新。
- **修复建议**：
  1. 对动态 provider 提供 `invalidate_router()` 回调
  2. 或者定期（而非每次 miss）重新构建 router
  3. 对 SkillToolProvider 和 MCPHub 做特殊处理：它们注册时主动通知 CompositeToolProvider 更新 router
- **优先级**：P1

### 问题 5.2：Channel registry.discover() 每次调用都重新 import
- **文件路径/行号**：`xmclaw/providers/channel/registry.py:51-91`
- **问题类型**：重复计算 / 同步阻塞
- **量化影响**：`discover()` 遍历 `CHANNEL_IDS` 并对每个调用 `importlib.import_module()`。虽然 Python import 有缓存，但 `getattr(mod, "MANIFEST", None)` 等反射操作仍然有开销。每次 UI 刷新 Channels 列表都触发完整扫描。
- **根因**：没有缓存 manifest 结果。`discover()` 是 pure function 但做了大量 IO。
- **修复建议**：在模块级别缓存 `discover()` 结果，提供 `invalidate_cache()` 在插件安装/卸载时调用。或者使用 `functools.lru_cache` 包装 `discover()`。
- **优先级**：P2

---

## 6. Context 组装/Token 管理

### 问题 6.1：ContextCompressor 每次 turn 重新计算 token count，无缓存
- **文件路径/行号**：`xmclaw/context/compressor.py:153-201`
- **问题类型**：重复计算 / 上下文膨胀
- **量化影响**：`estimate_messages_tokens_rough()` 每次调用都遍历所有消息，对每个消息的 content 调用 `_count_tokens_in_text()`，后者逐个字符检查 Unicode 范围。对于 100 条消息、每条平均 2000 字符的 session，每次 token 估计需要 100 × 2000 = 20 万次字符检查。在 compression 触发的 turn 中，这个函数可能被调用 3-5 次（pre-compress estimate、post-prune、post-compress estimate、threshold check）。
- **根因**：没有 per-message token count 缓存。消息内容在 turn 之间大部分不变，只有新消息追加。
- **修复建议**：
  1. 在 `Message` 对象上缓存 `_token_count`（惰性计算，内容不变时复用）
  2. 在 `AgentLoop` 层面维护 `session_token_count` 增量更新：新 turn 只计算新增消息的 token，加到上次总和中
- **优先级**：P1

### 问题 6.2：ContextCompressor._serialize_for_summary() 每次压缩都全量 redact
- **文件路径/行号**：`xmclaw/context/compressor.py:810-874`
- **问题类型**：重复计算
- **量化影响**：每次 compression 都对每个消息的 content 调用 `redact_string()`，后者扫描文本中的敏感信息（正则匹配）。对于 50 条消息的压缩窗口，就是 50 次 redact 扫描。如果 compression 每 5-10 turn 触发一次，累积开销可观。
- **根因**：`redact_string` 在序列化阶段被调用，而大部分消息内容在之前的 turn 已经被 redact 过。
- **修复建议**：在 `Message` 对象上标记 `already_redacted`，或者在 `AgentLoop` 存储历史时预 redact，避免重复扫描。
- **优先级**：P2

### 问题 6.3：AgentLoop.run_turn() 系统提示词每次重新 hash 计算
- **文件路径/行号**：`xmclaw/daemon/agent_loop.py:2987-2995`
- **问题类型**：重复计算
- **量化影响**：每次 turn 都计算 `hashlib.sha256("\n\n".join(_parts).encode("utf-8")).hexdigest()[:12]` 用于 cache fingerprint debug logging。虽然 `hashlib.sha256` 很快，但 `_parts` 可能包含数千字符的 system prompt，每次 turn 都重新计算是不必要的。
- **根因**：cache fingerprint 计算在每次 turn 都执行，即使 debug logging 级别可能不输出。
- **修复建议**：
  1. 只在 debug logging 启用时计算 fingerprint
  2. 或者缓存 fingerprint，只在 `_parts` 变化时重新计算
- **优先级**：P2

### 问题 6.4：AgentLoop 每次 turn 都遍历 tool_specs 做 skill 预过滤
- **文件路径/行号**：`xmclaw/daemon/agent_loop.py:3002-3124`
- **问题类型**：重复计算 / O(n) 遍历
- **量化影响**：每次 turn 都：
  1. 调用 `effective_tools.list_tools()` 获取全部工具
  2. 遍历所有 tool specs 数 skill 数量（`sum(1 for s in tool_specs if s.name.startswith("skill_"))`）
  3. 如果需要语义发现，对所有 skill 描述做 embedding（后台任务，但首次是冷的）
  4. 调用 `select_relevant_skills()` 做 token 重叠过滤
  5. 如果 trigger engine 启用，再遍历所有 skill 做 trigger 匹配
  对于 400+ skills（注释提到 "404 skills installed"），每次 turn 的 skill 预过滤遍历 400+ 个工具。
- **根因**：没有 per-session 或 per-turn 的 skill 预过滤结果缓存。用户消息在相同 session 的连续 turn 中可能相似，但 skill 预过滤每次都重新计算。
- **修复建议**：
  1. 对 skill 预过滤结果做 per-session LRU 缓存（keyed by user_message hash 或前 10 个词）
  2. 对 semantic index 做持久化缓存，daemon 重启后复用
- **优先级**：P1

---

## 总结表

| 优先级 | 问题数 | 核心主题 |
|--------|--------|----------|
| P0 | 2 | LLM classifier 阻塞、hybrid BM25 重建 |
| P1 | 10 | 工具面合并、guardian 索引、memory 批量查询、event batching、token count 缓存、skill 预过滤缓存 |
| P2 | 8 | 工具面合并（次要）、graph 启动优化、redact 重复、registry 缓存、subscriber 索引 |

### 按方向的问题密度
- **工具面过大**：6 个问题（browser 30 个、builtin 40+ 个、android 12 个、integrations 12 个、MCP 无上限、skill 400+ 无上限）
- **Security guardian**：5 个问题（guardian 线性遍历、规则线性扫描、路径 O(n)、LLM 阻塞）
- **Memory 查询**：6 个问题（remember 多次查询、recall 逐 hit 查询、THINK LLM 阻塞、hybrid BM25、V1 legacy 阻塞、centrality 启动）
- **事件总线**：2 个问题（SQLite 单事件写入、subscriber 线性遍历）
- **Provider 注册**：2 个问题（router miss 重扫描、registry 重复 import）
- **Context 组装**：4 个问题（token count 无缓存、redact 重复、hash 重复计算、skill 预过滤重复）

### 最关键的 3 个修复（如果只能做 3 个）
1. **P0-1**：将 browser 30 个工具合并为 1 个（参照 computer_use 重构），释放 6,000-15,000 token/turn 的上下文预算
2. **P0-2**：将 memory recall 的逐 hit graph 查询改为批量查询（`neighbors_batch`），减少 10× LanceDB 往返
3. **P0-3**：为 ContextCompressor 的 token count 添加增量缓存，避免每 turn 20 万次字符遍历

### 已存在的良好实践（值得保持）
- `computer_use` 已合并为 1 个工具（2026-06-18）
- `EmbeddingService` 有 LRU cache（embedding.py）
- `CognitiveMemoryGateway` 的 Tier-1 fast-path 绕过 LLM（gateway.py）
- `AgentLoop` 的 `_frozen_prompts` 缓存系统提示词基础部分
- `SqliteEventBus` 使用 WAL 模式 + asyncio.to_thread（已避免同步阻塞）
- V1 legacy recall 已默认关闭
- 技能预过滤已存在 `SkillSemanticIndex`（但缺少缓存预热）
