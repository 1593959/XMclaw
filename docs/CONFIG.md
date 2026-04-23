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

## Secrets 占位符（Epic #16 Phase 2）

任何字符串字段都可以写成 `"${secret:NAME}"` 占位符，`load_config` 会在启动时走 [`xmclaw.utils.secrets.get_secret`](../xmclaw/utils/secrets.py)（三层：env `XMC_SECRET_<NAME>` > `~/.xmclaw/secrets.json` > 可选 `keyring`）把它替换为真实值。

```jsonc
{
  "llm": {
    "anthropic": {
      "api_key": "${secret:llm.anthropic.api_key}",
      "default_model": "claude-sonnet-4-20250514"
    }
  },
  "channels": {
    "slack": { "bot_token": "${secret:slack_bot}" },
    "discord": { "webhook_url": "${secret:discord_hook}" }
  }
}
```

然后任选一种方式把真实值放进 secrets 层：

```bash
xmclaw config set-secret llm.anthropic.api_key      # 走 ~/.xmclaw/secrets.json
export XMC_SECRET_LLM_ANTHROPIC_API_KEY=sk-ant-xxx  # 或者直接走环境变量
```

### 规则

- **必须整串匹配**：`"${secret:foo}"` 命中，`"prefix-${secret:foo}-suffix"` 按字面保留。部分替换容易引入 escaping bug，尤其在 API key 这种敏感字段，不冒风险。
- **名字字符集**：`[A-Za-z0-9_.\-]+`。推荐用 `llm.anthropic.api_key` 风格（与 env var 和 CLI 命令命名对齐）；横杠和下划线也合法。
- **坏形状报错**：`"${secret:}"`、`"${secret: foo}"`、`"${secret:with space}"` 这类 LOOK-like 但不匹配 charset 的串会抛 `ConfigError("malformed secret placeholder ...")`，不会静默当成字面量穿透——避免打错字的用户以为"放了占位符"但其实根本没生效。
- **查不到报错**：`get_secret(name)` 返回 `None` 时抛 `ConfigError`，错误信息里带 JSON 路径（`$.llm.anthropic.api_key`）+ 名字 + remediation 提示（`xmclaw config set-secret <name>` 或 env）。不静默降级为 `None`/`""`——写了占位符就是"启动时必须解析"的硬约束。
- **递归**：dict / list 会深入遍历；数字、布尔、null、嵌套结构原值返回。空 dict / 空 list 保留（它们结构上有意义）。
- **顺序**：file → ENV overlay → secret resolution。意味着 ENV override 里塞 `${secret:...}` 也会被解析（`XMC__llm__anthropic__api_key='${secret:foo}'` 有效）。

### 与 Epic #16 Phase 1 的关系

Phase 1 在 `build_llm_from_config` 里做了一个**字段级特例**：`api_key: ""` → 回退到 `get_secret("llm.<provider>.api_key")`。Phase 2 这个占位符是**通用面**：任意字段都可以显式引用 secret，不止 LLM。两条路径共存且不互斥：

| 写法 | 触发条件 | 覆盖范围 |
|------|---------|---------|
| `api_key: ""` / missing | 仅 LLM `api_key` 字段、隐式 fallback | 单一特例 |
| `"${secret:NAME}"` | 任意字段、显式 | 整个 config |

建议新代码用显式占位符（意图清晰、支持任意字段）；Phase 1 的 implicit fallback 保持向后兼容，不会被移除。

### 绕过解析（工具链用）

`load_config(path, resolve_secrets=False)` 保留占位符原样，用于：
- 配置导出 / 迁移工具需要往返读写而不泄露真实 credential
- 测试希望断言"我写的确实是占位符"而不是解析后的值

### 实现

- `xmclaw.daemon.factory._SECRET_PLACEHOLDER_RE` + `_resolve_secret_placeholders(value, _resolver=...)` 递归访问器
- `xmclaw.daemon.factory.load_config(path, ..., resolve_secrets=True)` 默认开启
- 测试见 [tests/unit/test_v2_daemon_factory.py](../tests/unit/test_v2_daemon_factory.py) `test_placeholder_resolver_*` / `test_load_config_*_placeholder*` 用例

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

## Runtime 配置（Epic #3 entry）

`runtime` 段选择 skill 执行后端。目前两种：

```jsonc
{
  "runtime": {
    "backend": "local"   // "local" | "process"
  }
}
```

- **`local`**（默认）—— `LocalSkillRuntime`：asyncio task + `wait_for` 的 CPU 超时。启动快，适合 dev / 单测。父进程堆可被 skill 污染（Python 无 in-proc 隔离），`Path('/').read_text()` 不受控。
- **`process`** —— `ProcessSkillRuntime`：`multiprocessing.spawn` 另起 OS 进程。`SIGKILL` 真生效，skill 不共享父进程的 import / heap / event loop；**fs / net / memory 仍是 advisory**，需要真 sandbox 请等 Epic #3 的 docker 后端。

### 注意

- 未知 `backend` 值抛 `ConfigError`，daemon 启动直接失败——坏配置不应该悄悄降级到 `local` 让你以为在跑 process。
- 没有 `enabled: false`。没有 runtime 的 daemon 没法跑 skill，不是 "可选" 字段。
- **当前无 caller**：factory 已就绪但 AgentLoop / scheduler 还没把 skill 执行路径切到 `SkillRuntime`。接线工作排到 Epic #3 后续 phase。
