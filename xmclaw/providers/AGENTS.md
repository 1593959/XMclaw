# AGENTS.md — `xmclaw/providers/`

## 1. 职责

Adapters that let the pure `core/` runtime talk to the messy outside
world: LLM APIs (`llm/`), tool backends (`tool/`), persistent memory
stores (`memory/`), skill-execution runtimes (`runtime/`), and real-
time transport channels (`channel/`).

Each subdirectory holds a `base.py` ABC + one or more concrete
implementations. The ABC is the contract; swap implementations
behind the same ABC without touching `core/` or `daemon/`.

## 2. 依赖规则

- ✅ MAY import: `xmclaw.core.*`, `xmclaw.utils.*`,
  `xmclaw.security.*`, Python stdlib, third-party SDKs pinned in
  `pyproject.toml` (openai, anthropic, sqlite-vec, playwright,
  websockets, …).
- ❌ MUST NOT import: `xmclaw.daemon.*`, `xmclaw.cli.*`,
  `xmclaw.skills.*`, sibling provider packages unless they're
  specifically named in that provider's own AGENTS.md.

**Why**: providers are leaves of the DI graph; sibling-cross imports
turn what's supposed to be a flat set of adapters into a tangled
web. The daemon's factory is the only place that wires providers
together.

## 3. 测试入口

- Unit lanes in `scripts/test_lanes.yaml`: `llm` (anthropic/openai
  providers + translators), `tools` (all tool subdirs),
  `memory` (sqlite_vec), `runtime` (local + process).
- Integration: `tests/integration/test_v2_mcp_bridge.py`,
  `test_v2_tool_loop.py`, `test_v2_live_pipeline.py`.
- Each provider subdir has its own AGENTS.md listing its lanes.

## 4. 禁止事项

- ❌ Don't skip the `base.py` ABC — every new provider must
  implement the contract fully, including the error types. Partial
  implementations slip through into factory wiring and crash at
  runtime.
- ❌ Don't do module-import-time network or disk I/O. Import the
  SDK; do the auth / connection check inside the constructor or
  first use.
- ❌ Don't leak raw credentials through `__repr__` / structured
  logs. Use `utils.security.redact` on any field named `api_key`,
  `token`, or `password`.
- ❌ Don't cross-import between `llm/` and `tool/` (or any other
  pair). If two providers legitimately need to share a helper, it
  belongs in `core/` or `utils/`.

## 5. 关键文件

- `llm/base.py`, `tool/base.py`, `memory/base.py`, `runtime/base.py`,
  `channel/base.py` — the five ABCs every adapter implements.
- Per-subdir AGENTS.md files for the concrete contract details.
