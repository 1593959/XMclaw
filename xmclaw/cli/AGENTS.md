# AGENTS.md — `xmclaw/cli/`

## 1. 职责

Typer-based CLI entry points: `xmclaw start|stop|status|chat|
doctor|config`. `main.py` holds the root app; `chat.py` runs the
interactive client; `doctor.py` + `doctor_registry.py` implement the
diagnostics framework (see `docs/DOCTOR.md`).

CLI is **downstream of the daemon** — it talks over HTTP/WS like
any other client. It does not share Python state with the running
daemon process.

## 2. 依赖规则

- ✅ MAY import: `xmclaw.daemon.*` (for lifecycle + factory calls),
  `xmclaw.core.*`, `xmclaw.utils.*`, `typer`, `rich`, `httpx`,
  stdlib.
- ❌ MUST NOT import: `xmclaw.providers.*` directly (go through
  `daemon.factory.build_agent_from_config`), `xmclaw.skills.*`
  (skills run inside the daemon, not the CLI).

## 3. 测试入口

- Unit: `tests/unit/test_v2_doctor.py`,
  `tests/unit/test_v2_chat_formatter.py`.
- Smart-gate lane: `cli`.
- Manual smoke: `xmclaw doctor`, `xmclaw doctor --fix`,
  `xmclaw chat` against a running daemon.

## 4. 禁止事项

- ❌ Don't put business logic in the CLI command body. Commands
  should parse args, call the daemon over HTTP/WS (or the factory
  for `xmclaw start`), and render the result. Everything else is
  a daemon-side concern.
- ❌ Don't swallow typer.Exit; let the framework translate exit
  codes. Custom `sys.exit` calls bypass the test harness.
- ❌ Don't print secrets. `xmclaw doctor --json` sanitizes config
  via `_sanitize_config`; any new diagnostic that prints config
  must reuse that path.

## 5. 关键文件

- `main.py` — root Typer app; `xmclaw` entry point.
- `doctor.py` + `doctor_registry.py` — diagnostics framework
  (`DoctorCheck` ABC, `DoctorRegistry`, `--fix` runner).
- `chat.py` — interactive chat client (WebSocket → stdout).
