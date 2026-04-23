# AGENTS.md — `xmclaw/daemon/`

## 1. 职责

The **I/O boundary** of the runtime. `daemon/` owns the FastAPI app
(`app.py`), the WebSocket event stream, the AgentLoop that drives
turns (`agent_loop.py`), config loading + factory wiring
(`factory.py`), process lifecycle (`lifecycle.py`), and pairing-token
handling (`pairing.py`).

Everything a client sees — REST, WS, tokens, status files — is
defined here. The rest of the tree stays pure so this layer can be
swapped out for tests without dragging the whole runtime along.

## 2. 依赖规则

- ✅ MAY import: `xmclaw.core.*`, `xmclaw.providers.*`,
  `xmclaw.security.*`, `xmclaw.utils.*`, Python stdlib, FastAPI /
  Starlette / uvicorn.
- ❌ MUST NOT import: `xmclaw.cli.*`, `xmclaw.skills.*`,
  `xmclaw.plugins.*` (CLI is downstream; skills/plugins are wired
  via the factory, not direct imports).

**Why**: the daemon composes the DI graph. Downstream clients (CLI,
desktop tray, web) connect over HTTP/WS, not Python imports — that's
the whole point of the daemon/client split.

## 3. 测试入口

- Unit: `tests/unit/test_v2_agent_loop.py`,
  `test_v2_agent_memory.py`, `test_v2_daemon_factory.py`,
  `test_v2_daemon_lifecycle.py`, `test_v2_pairing.py`.
- Integration: `tests/integration/test_v2_daemon_app.py`,
  `test_v2_daemon_agent.py`, `test_v2_daemon_config.py`,
  `test_v2_daemon_pairing.py`, `test_v2_daemon_replay.py`,
  `test_v2_agent_loop_budget.py`, `test_v2_prompt_injection.py`,
  `test_v2_tool_loop.py`, `test_v2_live_pipeline.py`.
- Smart-gate lanes: `daemon` (for `app.py` / `factory.py` /
  `lifecycle.py` / `pairing.py`), `agent_loop` (for `agent_loop.py`).
- Manual smoke: `xmclaw start` → `curl -H "X-XMC-Token: …"
  http://127.0.0.1:8765/healthz`.

## 4. 禁止事项

- ❌ Don't add new endpoints without pairing-token enforcement
  (`pairing.validate_token`). The daemon binds `127.0.0.1` but the
  token is what protects the local attack surface.
- ❌ Don't put business logic in `app.py` route handlers beyond DI
  + schema adaption. Logic goes into `AgentLoop` or `core/` — the
  handler is the I/O edge, not the brain.
- ❌ Don't read config directly from disk outside `factory.py`.
  `load_config()` is the single funnel where env overrides + JSON
  merging + schema validation happens; bypassing it causes secret
  + policy drift.
- ❌ Don't start background tasks (asyncio.create_task) without
  registering them with the lifespan hook. Orphan tasks survive
  `xmclaw stop` on Windows and leak file handles.
- ❌ Don't log raw `api_key`, `token`, or user message content at
  INFO. Use `utils.security.redact` and keep raw payloads DEBUG-only.

## 5. 关键文件

- `app.py:69` — `create_app()` builds the FastAPI instance;
  every route, middleware, and lifespan hook is wired there.
- `agent_loop.py:122` — `AgentLoop` class. The single place
  per-turn orchestration lives. Read this before adding new
  tool-call flow logic.
- `factory.py:300` — `build_agent_from_config()` is the DI
  entry point; adding a new provider means touching here.
- `lifecycle.py:146` / `:215` — `start_daemon()` / `stop_daemon()`:
  PID files, meta writes, graceful shutdown. Modify here for any
  Windows/POSIX lifecycle work.
- `pairing.py:46` — `load_or_create_token()` and the owner-only
  permission shim. The security boundary for local access.
