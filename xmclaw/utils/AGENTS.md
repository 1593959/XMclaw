# AGENTS.md — `xmclaw/utils/`

## 1. 职责

Low-level helpers shared across the tree: path resolution
(`paths.py`), structured logging setup (`log.py`), redaction
(`redact.py`, `security.py`), and cost accounting (`cost.py`).

Nothing here knows about the runtime graph — these are primitive
building blocks safe to import from anywhere.

## 2. 依赖规则

- ✅ MAY import: Python stdlib, pinned third-party libs (`structlog`,
  `platformdirs`).
- ❌ MUST NOT import: ANY `xmclaw.*` package. utils is the bottom
  of the DAG; an upward import immediately creates a cycle because
  core + providers + daemon all import from here.

**Why**: the only place in the tree that genuinely has no
dependencies. Every reverse edge kills that property.

## 3. 测试入口

- Unit: `tests/unit/test_v2_cost_tracker.py`,
  `tests/unit/test_v2_redact.py`,
  `tests/unit/test_v2_logging.py` (9 tests: import purity,
  idempotence, secret scrubber, contextvars merge, JSON shape).
- Smart-gate lane: `observability` (cost + log + performance monitor).

## 4. 禁止事项

- ❌ Don't import from another `xmclaw.*` module. Enforceable via
  an extension to `scripts/check_import_direction.py` (TODO).
- ❌ Don't add runtime-dependent behaviour (env reads, config
  lookups) at module scope. Utils must be functionally pure so
  tests can import them without side effects.
- ❌ Don't grow `security.py` into a grab-bag. Credential-level
  sanitization stays here; anything about prompt injection goes to
  `xmclaw/security/` (different package).

## 5. 关键文件

- `paths.py` — `xmclaw_data_dir()`, `xmclaw_config_dir()`, etc.
  The single source of truth for where the daemon reads/writes.
- `redact.py` — field-level redaction of `api_key` / `token` /
  `password` in nested dicts; used by `doctor --json` and log
  middleware.
- `cost.py` — token-based cost estimation used by the budget
  gate.
- `log.py` — structlog configuration.
  - `setup_logging()` — idempotent; call once at daemon start.
    Does NOT run at module import time (utils import purity).
  - `get_logger(name=None)` — public logger handle; safe to call
    before setup (emits to default handler).
  - `bind_log_context(**kw)` / `clear_log_context()` — contextvar
    wrappers; pin `session_id` / `agent_id` at turn start and every
    downstream log line carries them automatically.
  - Processor chain scrubs secrets via `redact.redact_string` before
    JSON render — no caller-side effort needed for the 5 patterns
    `redact.py` knows.
