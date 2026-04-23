# XMclaw 配置参考

本文档描述 `daemon/config.json` 的结构以及如何通过环境变量覆盖配置字段。

- 配置文件模板：[`daemon/config.example.json`](../daemon/config.example.json)
- 实际使用的配置：`daemon/config.json`（**gitignored**，放 API key 的地方）

## 环境变量覆盖（Epic #6）

任意 `daemon/config.json` 字段都可以通过带前缀 `XMC__` 的环境变量覆盖；命名规则抄自 Pydantic Settings，适合 Docker / systemd / CI 场景——容器不用挂 volume，直接走 `-e` 或 `.env`。

### 命名规则

- 前缀：`XMC__`（**双下划线**，避免与 env var 名字里自带的下划线冲突）
- 剩余部分按 `__` 切成路径段，每段强制转小写
- 值做 JSON 解析；解析失败则按原字符串保留

| 环境变量                              | 等价 config 路径                              |
| ------------------------------------- | --------------------------------------------- |
| `XMC__llm__anthropic__api_key`        | `config["llm"]["anthropic"]["api_key"]`       |
| `XMC__LLM__ANTHROPIC__API_KEY`        | 同上（大小写都支持）                          |
| `XMC__tools__enable_bash=false`       | `config["tools"]["enable_bash"] = False`      |
| `XMC__daemon__port=8765`              | `config["daemon"]["port"] = 8765`             |
| `XMC__tools__allowed_dirs=["/a","/b"]`| `config["tools"]["allowed_dirs"] = ["/a","/b"]` |

### 类型推断

值经过 `json.loads` 试解析，所以：

- `"true"` / `"false"` → `bool`
- `"42"` / `"3.14"` → `int` / `float`
- `"null"` → `None`
- `"[1,2,3]"` / `'{"k":"v"}'` → `list` / `dict`
- `"sk-ant-xxx"` / `"hello"` → 保持为 `str`（JSON 解析失败）

不想让值被解析？在 shell 里加引号并转义成 JSON 字符串：

```bash
export XMC__llm__anthropic__default_model='"claude-haiku-4-5-20251001"'
```

### 优先级

最终生效的 config：

```
默认值  <  daemon/config.json  <  XMC__* 环境变量
```

ENV 永远赢；即使 config 里那个字段被写成别的类型（例如 `"llm": "string-bug"`），ENV 也会把它改成正确的 dict。

### Docker 使用示例

只用 ENV 起一个 daemon，完全不挂载 config 文件：

```bash
docker run --rm \
  -e XMC__llm__anthropic__api_key=sk-ant-xxx \
  -e XMC__llm__anthropic__default_model=claude-haiku-4-5-20251001 \
  -e XMC__tools__enable_bash=true \
  -e XMC__tools__allowed_dirs='["/workspace"]' \
  -p 8765:8765 \
  xmclaw:latest
```

只要 `daemon/config.json` 存在（哪怕是 `{}`），load_config 就会在它上面 overlay ENV 层。

### 推荐做法

- **API key 走 ENV**，不要提交到 git。`daemon/config.json` 里写 `"api_key": ""` 占位。
- **Docker / CI 走全 ENV**。
- **本地开发走文件**，ENV 只在临时测试覆盖时使用。

### 实现

- `xmclaw.daemon.factory._apply_env_overrides` 执行合并
- `xmclaw.daemon.factory.load_config(path, env=...)` 在读文件后自动调用；传 `env={}` 可跳过（单测用）
- 测试见 [tests/unit/test_v2_daemon_factory.py](../tests/unit/test_v2_daemon_factory.py) `test_env_override_*` 用例

## Memory 配置（Epic #5）

`memory` 段控制 v2 sqlite-vec 记忆层：存哪、TTL、保留策略、定期清扫。完整 schema：

```jsonc
{
  "memory": {
    "enabled": true,                    // false 则整个 memory 层关闭，daemon 不起 store
    "db_path": null,                    // null → ~/.xmclaw/v2/memory.db；":memory:" 走纯内存
    "embedding_dim": null,              // null = 惰性探测第一个向量；设整数则强制校验维度
    "ttl": {                            // 每层的秒级 TTL；null 表示该层不按 age prune
      "short":   3600,                  //   short  层：1 小时
      "working": 86400,                 //   working 层：1 天
      "long":    null                   //   long   层：不按 TTL 清理
    },
    "pinned_tags": ["identity", "user-profile"],  // metadata.tags 命中任一即永久保留
    "retention": {
      "sweep_interval_s": 3600,         // 后台清扫间隔（秒）；≤0 或非数 → 回退到 3600
      "prune_by_ttl": true,             // 每次扫描对所有层跑一次 age-prune
      "max_items": {                    // 每层条数上限；超出按 ts ASC LRU 淘汰（pinned 跳过）
        "short":   2000,
        "working": 20000,
        "long":    null
      },
      "max_bytes": {                    // 每层字节上限（text 长度和）；null = 不限
        "short":   10485760,            //   10 MiB
        "working": 104857600,           //  100 MiB
        "long":    null
      }
    }
  }
}
```

### 行为要点

- **后台 task 由 `create_app` lifespan 管理**：daemon 起来时起 `MemorySweepTask`，关闭时先 stop 再 close 记忆。
- **所有层都没有 cap 且 `prune_by_ttl: false` 时，sweep task 不会启动**——没啥可扫的就别白跑。
- **淘汰时发事件**：`prune` / `evict` 真删到行时会 `bus.publish(MEMORY_EVICTED)`，payload：
  - `layer` — `"short" | "working" | "long"`
  - `count` — 本次删除条数
  - `reason` — `"age"` / `"cap"` / `"cap_items"` / `"cap_bytes"`
  - `bytes_removed` — 仅 `max_bytes` 触发时存在
- **降级语义**：配置段里整数字段传了 0、负数或字符串会被静默降为 `null` 并 `memory_sweep.bad_cap` warn——坏配置不应该让 daemon 拒绝启动。

### 实现

- `xmclaw.daemon.factory.build_memory_from_config` 读 `memory` 段构造 `SqliteVecMemory`
- `xmclaw.daemon.memory_sweep.parse_retention_config` + `MemorySweepTask` 管清扫
- `xmclaw.providers.memory.sqlite_vec.SqliteVecMemory` 在 `prune` / `evict` 内部调 `_emit_evicted`
- 测试：[tests/unit/test_v2_memory_retention.py](../tests/unit/test_v2_memory_retention.py)
