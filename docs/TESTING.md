---
summary: "Integration test plan, test matrix, and known issues"
read_when:
- Running the test suite
- Verifying a module works end-to-end
- Reporting or triaging a bug
title: "Testing"
---

# Testing Guide

## Test Structure

```
tests/
├── test_config.py        # Config loading, env overrides, encryption, masking
├── test_evolution.py    # VFM scoring, insight extraction, scheduler
├── test_registry.py      # ToolRegistry singleton, plugin discovery
├── test_memory.py        # SQLite store, session manager
├── test_integration.py   # Cross-module flows (needs live daemon)
├── test_security.py      # Fernet encryption, API key masking
├── test_tools.py         # Tool execution (sandboxed)
├── test_bash.py          # Bash tool timeout, error handling
├── test_file_write.py    # File write safety, path traversal guard
└── test_tool.py          # Base tool interface contract
```

## Running Tests

```bash
# All tests
python -m pytest tests/ -v

# Single module
python -m pytest tests/test_config.py -v

# With coverage
python -m pytest tests/ --cov=xmclaw --cov-report=term-missing

# Fast smoke test (no network)
python -m pytest tests/test_config.py tests/test_registry.py tests/test_evolution.py -v
```

## Test Matrix

### Config & Security

| Feature | Test | Status |
|---------|------|--------|
| Config load from `daemon/config.json` | `test_config.py` | ✅ |
| Env var override (`XMC__section__key`) | `test_config.py::test_env_override` | ✅ |
| Type inference (bool/int/float/str) | `test_config.py::test_type_inference` | ✅ |
| Secret masking in `config show` | `test_config.py::test_mask_secrets` | ✅ |
| Fernet encrypt/decrypt roundtrip | `test_security.py::test_fernet_roundtrip` | ✅ |
| PBKDF2 key derivation deterministic | `test_security.py::test_pbkdf2_key` | ✅ |
| `ENC:` prefix detection | `test_security.py::test_enc_prefix` | ✅ |

### Core Modules

| Feature | Test | Status |
|---------|------|--------|
| `AgentLoop` tokenization (tool_call_start/input/end) | `test_tool.py` | ✅ |
| `PromptBuilder` system prompt formatting | inline in `test_tool.py` | ✅ |
| `PromptBuilder` insights injection | `test_evolution.py::test_insights_in_context` | ✅ |
| `GeneManager.match()` keyword trigger | `test_evolution.py::test_gene_keyword_match` | ✅ |
| `GeneManager.match()` regex pattern | `test_evolution.py::test_gene_regex_match` | ✅ |
| `AgentOrchestrator.run_team()` parallel | `test_integration.py` | ✅ |
| EventBus publish/subscribe roundtrip | `test_integration.py` | ✅ |
| `EventBus` rate limiting (200/60s) | `test_integration.py::test_rate_limit` | ✅ |

### Evolution

| Feature | Test | Status |
|---------|------|--------|
| VFM novelty score (solid vs generic keywords) | `test_evolution.py::test_vfm_novelty` | ✅ |
| VFM clarity score (name pattern) | `test_evolution.py::test_vfm_clarity` | ✅ |
| VFM actionability (trigger verb analysis) | `test_evolution.py::test_vfm_actionability` | ✅ |
| VFM relevance (feedback signal priority) | `test_evolution.py::test_vfm_relevance` | ✅ |
| Insight extraction (tool threshold ≥ 2) | `test_evolution.py::test_insight_extraction` | ✅ |
| Insight deduplication (content-keyed) | `test_evolution.py::test_insight_dedup` | ✅ |
| `_schedule_reflection()` fire-and-forget | integration only | ⚠️ needs daemon |
| `EVOLUTION_NOTIFY` → WS broadcast | integration only | ⚠️ needs daemon |
| `REFLECTION_COMPLETE` → WS broadcast | integration only | ⚠️ needs daemon |

### Tools & Plugins

| Feature | Test | Status |
|---------|------|--------|
| `ToolRegistry` singleton (`set_shared`/`get_shared`) | `test_registry.py::test_singleton` | ✅ |
| Plugin auto-discovery from `plugins/tools/` | `test_registry.py::test_plugin_discovery` | ✅ |
| `BaseTool` + `Tool` multi-inheritance | `test_registry.py::test_multi_inherit` | ✅ |
| Tool hot-reload without restart | integration only | ⚠️ |
| MCP stdio transport | integration only | ⚠️ |
| MCP SSE transport | integration only | ⚠️ |
| MCP WebSocket transport | integration only | ⚠️ |

### Memory

| Feature | Test | Status |
|---------|------|--------|
| `SQLiteStore` CRUD (insights/genes/skills) | `test_memory.py` | ✅ |
| Session append and retrieval | `test_memory.py::test_session` | ✅ |
| `load_context()` returns insights | implicit via `test_evolution.py` | ✅ |

## Known Issues

### 🔴 ask_user / plan mode — tool re-execution on resume

**Severity**: Critical  
**File**: `xmclaw/daemon/server.py` + `xmclaw/core/agent_loop.py`

When `ask_user` pauses the agent loop:

1. `agent_loop.run()` yields `{"type": "ask_user", ...}` and returns
2. `server.py` receives `ask_user_answer` and calls `run_agent()` **again** with `"[RESUME]{answer}"`
3. The second `run_agent()` call re-executes all previous tool calls because:
   - `_turn_history` accumulates across calls
   - `turn_count` resets to 0 each call
   - No state is shared between the two `run_agent()` invocations

**Fix direction**: Persist `pending_question` in `AgentOrchestrator.agents[agent_id]` between calls. In the second call, detect `[RESUME]` and skip re-execution by checking `agent.pending_question`.

### 🟡 genes table missing priority column

**Severity**: Major  
**File**: `xmclaw/memory/sqlite_store.py`

```sql
-- Current schema (missing columns)
CREATE TABLE genes (id, agent_id, name, description, trigger, action);

-- GeneManager.get_all() tries to sort by priority:
sorted(genes, key=lambda g: g.get("priority", 0), reverse=True)
-- Always returns 0 → sort is a no-op
```

Fix: add `priority INTEGER DEFAULT 0` to the schema and `INSERT OR REPLACE`.

### 🟡 gene trigger JSON collision

**Severity**: Minor  
**File**: `xmclaw/evolution/gene_forge.py`

Auto-generated genes sometimes store a JSON object as the trigger string:
```python
trigger = "{'type': 'event', 'source': 'issue_tracker', ...}"
```
`GeneManager.match()` then checks if this long JSON string appears in the user input — it never does. These genes are effectively dead weight.

**Fix direction**: Add a `gene_type` field (`keyword` | `event` | `regex`). Only apply keyword/in/regex matching to `keyword` type genes. Event genes should be triggered by `EventBus` subscriptions instead.

### 🟡 CLI doesn't support multi-turn with ask_user

**Severity**: Minor  
**File**: `xmclaw/cli/client.py`

The CLI sends a single message and waits for the response stream. When `ask_user` is received, it prints the question but the program exits — the user cannot type an answer.

**Fix direction**: Wrap the message loop in `while True`, collect user input after `ask_user` events, and send `ask_user_answer` before the next prompt.

## Integration Test (requires live daemon)

```bash
# Start daemon first
xmclaw start

# In another terminal
curl -s http://127.0.0.1:8765/api/config | jq .llm
curl -s http://127.0.0.1:8765/api/events/stats | jq .
xmclaw status
xmclaw evolution-status
xmclaw doctor
```

### WebSocket smoke test

```javascript
// Open browser DevTools console on http://127.0.0.1:8080
const ws = new WebSocket('ws://127.0.0.1:8765/agent/default');
ws.onopen = () => ws.send(JSON.stringify({role:'user', content:'hello'}));
ws.onmessage = (e) => console.log(JSON.parse(e.data));
```

Expected events: `state:THINKING` → `chunk` (streaming) → `done`.

## Syntax Check (no deps needed)

```bash
find xmclaw -name "*.py" | xargs python3 -c "
import sys, ast
for f in sys.stdin.read().splitlines():
    try: ast.parse(open(f).read())
    except SyntaxError as e:
        print(f'SYNTAX ERROR: {f}:{e.lineno} {e.msg}')
"
```
