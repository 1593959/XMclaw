# AGENTS.md — `xmclaw/providers/memory/`

## 1. 职责

Persistent memory stores. `base.py` defines `MemoryProvider` ABC;
`sqlite_vec.py` is the shipped implementation — SQLite + `sqlite-vec`
extension for cosine-similarity recall on embedding vectors.

The AgentLoop feeds the store with chat turns and queries it for
top-k relevant memories before the next prompt build.

## 2. 依赖规则

- ✅ MAY import: `xmclaw.core.*` (events, IR), `xmclaw.utils.*`,
  stdlib, `sqlite3`, `sqlite_vec`, embedding client SDKs.
- ❌ MUST NOT import: sibling `providers/*` packages,
  `xmclaw.daemon.*`, `xmclaw.cli.*`.

## 3. 测试入口

- Unit: `tests/unit/test_v2_memory_sqlite_vec.py`,
  `tests/unit/test_v2_agent_memory.py`.
- Integration: `tests/integration/test_v2_daemon_memory.py`,
  `tests/integration/test_v2_cross_session_memory.py`.
- Smart-gate lane: `memory`.

## 4. 禁止事项

- ❌ Don't open the sqlite connection at module scope. One
  connection per `MemoryProvider` instance — connection sharing
  across threads breaks sqlite's checkthread.
- ❌ Don't store raw PII in the embedding-input text. The scanner
  in `security/prompt_scanner.py` runs against *tool output*; user
  memories pass through untouched, so caller-side redaction is on
  the caller.
- ❌ Don't add migration logic without a version bump in the
  `schema_version` table. Silent schema mutation breaks replay.

## 5. 关键文件

- `base.py` — `MemoryProvider` ABC: `add(record)`, `search(query,
  k)`, `delete(id)`.
- `sqlite_vec.py` — schema + WAL + vec0 virtual table wiring.
