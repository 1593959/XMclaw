# AGENTS.md — `xmclaw/core/`

## 1. 职责

The **causal upstream** of the runtime. `core/` owns the event bus
(`bus/`), the intermediate representations the rest of the system
speaks (`ir/`), the grader verdicts + domain checks (`grader/`), the
evolution scheduler + gene tooling (`evolution/`, `scheduler/`), and
cross-cutting runtime instrumentation (`performance_monitor.py`,
`session/`).

Everything the daemon, providers, and skills *react to* lives here.
Nothing in `core/` may react to anything downstream — that inversion
is the single rule the whole layering depends on.

## 2. 依赖规则

- ✅ MAY import: `xmclaw.utils.*`, `xmclaw.security.*`, Python stdlib,
  pinned third-party libs (`pydantic`, `sqlite-vec` via typed shims).
- ❌ MUST NOT import: `xmclaw.providers.*`, `xmclaw.skills.*`,
  `xmclaw.daemon.*`, `xmclaw.cli.*`, `xmclaw.plugins.*`.

**Why**: the runtime is a DAG — `core → providers → daemon → (cli |
integrations)`. Any upward edge creates an import cycle and forces the
test layering to collapse. The constraint is mechanized by
`scripts/check_import_direction.py` (CI-1 in V2_DEVELOPMENT §6.3).

## 3. 测试入口

- Unit: `tests/unit/test_v2_bus_*.py`, `test_v2_ir_*.py`,
  `test_v2_grader.py`, `test_v2_evolution_controller.py`,
  `test_v2_scheduler*.py`, `test_v2_performance_monitor.py`.
- Integration: `tests/integration/test_v2_events_api.py`,
  `test_v2_daemon_replay.py`.
- Smart-gate lane: `bus` (for `bus/`, `ir/`), `evolution` (for
  `evolution/`, `grader/`, `scheduler/`), `observability` (for
  `performance_monitor.py`). See `scripts/test_lanes.yaml`.
- Manual smoke: `python -m pytest tests/unit/test_v2_bus_ping.py -v`
  for the cheapest end-to-end bus sanity.

## 4. 禁止事项

- ❌ Don't import any provider or skill module. Enforced by
  `scripts/check_import_direction.py`; a violation fails CI.
- ❌ Don't add sync network or disk I/O at module import time.
  `daemon/factory.py` wires `core/` into a live process; import-time
  side effects turn DI into a minefield.
- ❌ Don't rename or delete an `EventType` enum member without
  bumping the schema and updating `docs/EVENTS.md`. The bus is a
  public contract — clients replay historical events.
- ❌ Don't catch `Exception:` and swallow. `core/` errors should
  propagate so the daemon's `ErrorLoop` + `ANTI_REQ_VIOLATION` path
  can log + enforce budget gates.
- ❌ Don't store runtime config on module-level globals. Everything
  that varies per-process must thread through constructor injection
  — otherwise test isolation breaks.

## 5. 关键文件

- `bus/events.py` — `EventType` enum + payload contract. Every new
  event goes here first. Schema is versioned; check EVENTS.md.
- `bus/memory.py`, `bus/sqlite.py` — in-memory vs WAL-backed bus
  implementations. Same ABC in `bus/base.py`.
- `ir/toolcall.py` — provider-neutral tool-call representation;
  every LLM translator converts to/from this.
- `grader/verdict.py` + `grader/checks/` — verdict objects + the
  domain checks that produce them.
- `evolution/controller.py` — the scheduler loop that moves genes
  through propose → validate → promote.
- `performance_monitor.py` — the single source of per-turn timing
  + cost snapshots; read this before adding another timing path.
