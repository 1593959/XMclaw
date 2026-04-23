# `xmclaw doctor` — 插件化诊断

`xmclaw doctor` 在不启 daemon 的前提下逐项检查本地 setup，把「为什么 serve / chat 起不来」这种四层远的错误摊回到第一层。

## 快速用法

```bash
xmclaw doctor                       # 标准诊断（11 项内置 check）
xmclaw doctor --json                # 输出机器可读 JSON
xmclaw doctor --no-daemon-probe     # 跳过 HTTP health probe（完全离线）
xmclaw doctor --network             # 额外探测 LLM endpoint 可达性（默认关）
xmclaw doctor --discover-plugins    # 附带加载第三方 check
xmclaw doctor --fix                 # 对可自动修复的红项调用 DoctorCheck.fix()
```

退出码：全绿 → 0；任意一项 `ok=False` → 1。CI / shell 脚本可以直接 `xmclaw doctor && xmclaw serve`。

## 内置 check

按 registry 顺序列出（`ConfigCheck` 必须先跑，它把解析好的 dict 挂到 `ctx.cfg` 上供后续 check 共享）：

| id             | 含义                                                      | 失败时的 advisory                                               | fixable |
| -------------- | --------------------------------------------------------- | --------------------------------------------------------------- | ------- |
| `config`       | `daemon/config.json` 存在、能被 JSON 解析、root 是 dict   | 提示 `xmclaw config init`；缺文件 → `--fix` 写骨架              | ✅      |
| `llm`          | 至少一家 LLM 有 `api_key`                                 | 加 api_key 或接受 echo 模式                                     | -       |
| `tools`        | `tools.allowed_dirs` 有效（空 list 也算警告）             | 补路径或删掉 `tools` 段                                         | -       |
| `workspace`    | `~/.xmclaw/v2/` 目录存在且可写                            | 缺失 → `--fix` 自动 `mkdir -p`                                  | ✅      |
| `pairing`      | `~/.xmclaw/v2/pairing_token.txt` 存在且权限 0600（POSIX） | 空文件 → `unlink`；POSIX 松权限 → `chmod 600`；两者皆 `--fix`   | ✅      |
| `port`         | 目标 `host:port` 可以 bind                                | 已占用 → 提示换端口或停掉占用进程                               | -       |
| `events_db`    | `~/.xmclaw/v2/events.db` 可读且 schema 版本不超前          | 损坏 → 备份并让 daemon 重建；schema 超前 → 升级 xmclaw          | -       |
| `connectivity` | `--network` 开启时，HEAD-probe 每家配置了 api_key 的 LLM  | 不可达 → 检查网络 / 代理 / `base_url`                           | -       |
| `roadmap_lint` | 运行 `scripts/lint_roadmap.py`，catching §3.6.5 drift     | 违规列表前 3 条回显；完整列表跑 `python scripts/lint_roadmap.py` | -       |
| `pid_lock`     | `~/.xmclaw/v2/daemon.pid` 指向的进程要么在跑，要么干净缺席 | 僵 pid 文件 → `--fix` 清理 `daemon.pid` + `daemon.meta`         | ✅      |
| `daemon`       | 可选：探测 `GET /health`；跑不通不视为错                  | 未启动不报红                                                    | -       |

## `--fix` 运行器（Epic #10 阶段 2）

`xmclaw doctor --fix` 在常规诊断结束后，对所有 `ok=False AND fix_available=True` 的 check 调用它们自己的 `DoctorCheck.fix(ctx)` 并原地重跑。输出多出一段：

```
fix attempts:
  - workspace: resolved
  - redis: still failing (fix raised: TimeoutError: connection refused)
```

JSON 模式下 `fix_attempts` 是独立字段，每项 `{check_id, before_ok, after_ok, fix_raised}`，便于 CI / 外壳脚本判断是否自动修好。

`fix()` 抛异常由 registry 捕获并记到 `fix_raised` 字段——broken fix 不会把整体流程带走；后续 check 照常再跑一次。

## 扩展：自己写一个 check

从 Epic #10 开始，`xmclaw doctor --discover-plugins` 会走 [`entry_points`](https://packaging.python.org/en/latest/specifications/entry-points/) 的 `xmclaw.doctor` 组，允许任意第三方 wheel 把自己的 check 接进诊断流程。

### 1. 实现一个 `DoctorCheck` 子类

```python
# my_pkg/doctor.py
from pathlib import Path

from xmclaw.cli.doctor_registry import (
    CheckResult,
    DoctorCheck,
    DoctorContext,
)


class MyRedisCheck(DoctorCheck):
    id = "redis"
    name = "redis"

    def run(self, ctx: DoctorContext) -> CheckResult:
        try:
            import redis  # noqa: F401
        except ImportError:
            return CheckResult(
                name=self.name, ok=False,
                detail="redis package not installed",
                advisory="pip install redis",
                fix_available=False,
            )
        # ...ping, version check, etc.
        return CheckResult(name=self.name, ok=True, detail="ping ok")

    def fix(self, ctx: DoctorContext) -> bool:
        # 可选；默认返回 False（无自动修复）。
        return False
```

### 2. 在自己的 `pyproject.toml` 声明 entry point

```toml
[project.entry-points."xmclaw.doctor"]
redis = "my_pkg.doctor:MyRedisCheck"
```

右侧可以是 `DoctorCheck` 子类，也可以是返回 `DoctorCheck` 实例的零参工厂。

### 3. 安装后用 `--discover-plugins` 跑

```bash
pip install my-xmclaw-plugin
xmclaw doctor --discover-plugins
```

Registry 的顺序是：**built-in 先，plugin 后**。Plugin import 失败不会整体停机——会在输出里多一条 `plugin:<name>` 的红线，其他 check 照常跑。

## 上下文共享：`DoctorContext`

`run(ctx)` 拿到的 `DoctorContext` 带下列字段：

| 字段            | 用途                                                              |
| --------------- | ----------------------------------------------------------------- |
| `config_path`   | `--config` 指向的 JSON 路径                                       |
| `host`, `port`  | `--host` / `--port`                                               |
| `probe_daemon`  | `False` 表示用户加了 `--no-daemon-probe`                          |
| `probe_network` | `True` 表示用户加了 `--network`；默认 `False` 保持离线可跑        |
| `cfg`           | `ConfigCheck` 解析成功后填进来的 dict；前置 check 未跑时是 `None` |
| `token_path`    | 显式覆盖 pairing token 的位置（单测场景用）                       |
| `extras`        | 自由空间；plugin 之间要传数据可以塞这里（`workspace_dir` / `events_db_path` / `pid_path` 等单测 override 也走这里） |

先跑的 check 可以把昂贵结果挂在 `ctx` 上，后跑的 check 只读不算：例如 `ConfigCheck` 把 `cfg` 缓存好之后 `LLMCheck` / `ToolsCheck` 就不会再 parse 一次文件。

## 错误处理

- `DoctorCheck.run()` 抛异常 → registry 捕获，自动产出一条 `ok=False` 的 `CheckResult`（detail 带异常类型和消息）。**一个 check 炸了不会把整个诊断流程带走**。
- `DoctorCheck.fix()` 的默认实现返回 `False`；仅在确认能原地修好时才覆写（例如 `WorkspaceCheck` 调 `mkdir -p`）。`fix_available=True` 是向 `--fix` 运行器声明「我有得可修」的唯一开关——漏了这个字段，即便写了 `fix()` 也不会被调用。

## 相关文件

- [`xmclaw/cli/doctor_registry.py`](../xmclaw/cli/doctor_registry.py) — ABC / 注册表 / 发现逻辑
- [`xmclaw/cli/doctor.py`](../xmclaw/cli/doctor.py) — 内置 check 的纯函数实现（registry 调用它们）
- [`xmclaw/cli/main.py`](../xmclaw/cli/main.py) — CLI 入口（`--json` / `--discover-plugins`）
- [`tests/unit/test_v2_doctor.py`](../tests/unit/test_v2_doctor.py) — 覆盖 check 行为 + registry 插件加载
- [Epic #10 · Doctor 诊断](DEV_ROADMAP.md#epic-10--doctor-诊断可插拔)
