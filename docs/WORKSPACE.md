# Workspace（`~/.xmclaw/` 数据目录）

XMclaw v2 daemon 把**所有**运行态数据写到一个根目录下 —— 默认
`~/.xmclaw/`，所有 v2 产物落在 `~/.xmclaw/v2/` 子树。只要这个根目录
能换，整个 XMclaw 就能在 Docker 卷、便携盘、test harness 之间
搬家，不用改代码。

> v1 时代还有个 per-agent 的 `agents/<agent_id>/workspace/` 概念
> (SOUL.md / PROFILE.md / tasks.json / plan.md 这些) —— v2 已下线。
> agent 身份文件（SOUL.md / PROFILE.md / AGENTS.md）作为 prompt
> 来源在 [`xmclaw/security/policy.py`](../xmclaw/security/policy.py)
> 的 `SOURCE_PROFILE` 里还有引用，但不再由 daemon 提供文件 CRUD
> HTTP API；todo / plan 这些信息走 [BehavioralEvent](EVENTS.md)
> 事件流，不再写成磁盘 json 文件。

## 默认布局

```
~/.xmclaw/
├── v2/                          ← v2 运行时子树
│   ├── daemon.pid               ← xmclaw start 写入；stop/restart/doctor 都读
│   ├── daemon.meta              ← 与 pid 并列；记 host/port/version
│   ├── daemon.log               ← xmclaw start 的 stdout/stderr 纯文本 tee
│   ├── pairing_token.txt        ← anti-req #8：WS / HTTP 鉴权 token（0600）
│   ├── events.db                ← SqliteEventBus 的事件持久化（WAL + FTS5）
│   └── memory.db                ← sqlite-vec 长期记忆
└── logs/                        ← structlog JSON 行（xmclaw.log + 轮换）
```

`logs/` 刻意和 `v2/` **平级**而不放在 `v2/` 里面——让用户执行
`xmclaw stop && rm -rf ~/.xmclaw/v2` 做一次 "清空运行时状态" 重置
时，事故取证的日志不会被一起抹掉。

## 唯一入口：`xmclaw/utils/paths.py`

§3.1 「仓库不得写入运行态数据」强制所有 daemon 代码走这个模块
定位路径，**不得**任何地方手写 `~/.xmclaw/v2/...` 字符串。模块导出的
函数即为权威：

| 函数                           | 返回                                        |
| ------------------------------ | ------------------------------------------- |
| `data_dir()`                   | 根目录，默认 `~/.xmclaw`（见下文 env 覆盖） |
| `v2_workspace_dir()`           | `<data>/v2`                                 |
| `logs_dir()`                   | `<data>/logs`                               |
| `default_pid_path()`           | `<data>/v2/daemon.pid`                      |
| `default_meta_path()`          | `<data>/v2/daemon.meta`                     |
| `default_daemon_log_path()`    | `<data>/v2/daemon.log`                      |
| `default_token_path()`         | `<data>/v2/pairing_token.txt`               |
| `default_events_db_path()`     | `<data>/v2/events.db`                       |
| `default_memory_db_path()`     | `<data>/v2/memory.db`                       |

导入规约见 [`xmclaw/utils/AGENTS.md`](../xmclaw/utils/AGENTS.md)：
paths.py 是 DAG 底层，**不能**反向 import 其他 `xmclaw.*` 子包；
`scripts/check_import_direction.py` 会在 CI 挡下来。

## 环境变量覆盖

按优先级：

| env var                       | 用途                                                  |
| ----------------------------- | ----------------------------------------------------- |
| `XMC_DATA_DIR`                | 整体搬家——换 `~/.xmclaw` 为任意目录。Docker 卷 / 便携盘首选。|
| `XMC_V2_PID_PATH`             | 只换 `daemon.pid` 位置（pytest 隔离用）               |
| `XMC_V2_PAIRING_TOKEN_PATH`   | 只换 pairing token 路径                               |
| `XMC_V2_EVENTS_DB_PATH`       | 只换 events.db 路径                                   |

「窄覆盖」三条优先于 `XMC_DATA_DIR`——所以单测可以只重定向一个
文件而不搬整个工作区。

示例：

```bash
# 整个 XMclaw 装到外置盘
XMC_DATA_DIR=/mnt/xmclaw xmclaw start

# 只把 events.db 挪到 tmpfs，别的走默认
XMC_V2_EVENTS_DB_PATH=/dev/shm/events.db xmclaw serve
```

## Doctor 如何校验

[`xmclaw doctor`](DOCTOR.md) 里有三条 check 直接针对这个目录：

- `workspace` —— `~/.xmclaw/v2/` 存在且可写；缺失则 `--fix` 自动 `mkdir -p`。
- `pairing`   —— `pairing_token.txt` 存在且 POSIX 权限 `0600`；空文件 / 宽权限两种都可 `--fix`。
- `pid_lock`  —— `daemon.pid` 指向的进程要么活着要么文件干净；僵 pid 可 `--fix`。

这三条是 "能不能把 daemon 跑起来" 的最低要求——任何一条红，
`xmclaw start` 多半会在 stacktrace 四层远的地方炸。

## 相关文件

- [`xmclaw/utils/paths.py`](../xmclaw/utils/paths.py) —— 单一真相源
- [`tests/unit/test_v2_utils_paths.py`](../tests/unit/test_v2_utils_paths.py) —— env 覆盖 / 默认路径回归
- [`xmclaw/cli/doctor_registry.py`](../xmclaw/cli/doctor_registry.py) —— `workspace` / `pairing` / `pid_lock` check 实现
- [docs/DOCTOR.md](DOCTOR.md) —— `xmclaw doctor` 用法与扩展
- [docs/CONFIG.md](CONFIG.md) —— `daemon/config.json` 解析（配置文件位置 vs 数据目录的分工）
