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
