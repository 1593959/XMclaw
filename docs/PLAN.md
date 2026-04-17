---
summary: "Development roadmap: prioritize, track, and execute"
read_when:
- Starting a new development session
- Triaging which bug to fix next
- Onboarding to the project
title: "Development Plan"
---

# Development Plan

## Project Overview

XMclaw is a local-first, self-evolving AI Agent runtime. The daemon (`xmclaw start`) runs a FastAPI + WebSocket server on `127.0.0.1:8765`. Clients (web UI, CLI, desktop) connect over WebSocket and exchange typed JSON events.

```
Clients (web/CLI/desktop)
    ↕ WebSocket
Daemon (FastAPI + Uvicorn)
    ├── AgentOrchestrator
    │   ├── AgentLoop          ← think→act→observe loop
    │   ├── ToolRegistry      ← built-in + plugin tools
    │   ├── LLMRouter        ← Anthropic + OpenAI + plugins
    │   └── MemoryManager     ← SQLite + vector store
    ├── EvolutionEngine       ← VFM scorer → GeneForge/SkillForge
    ├── EventBus              ← pub/sub (all components publish)
    └── IntegrationManager    ← Slack/Discord/Telegram/GitHub/Notion
```

---

## Priority Backlog

### P0 — Critical (blocks fundamental operation)

#### P0-1: ✅ DONE — Fix ask_user tool re-execution on resume

---

### P1 — Major (degrades quality or correctness)

#### P1-1: ✅ DONE — Add missing columns to genes table
#### P1-2: ✅ DONE — Gene trigger type system (keyword/regex/intent/event)
#### P1-3: ✅ DONE — CLI multi-turn with ask_user support

---

### P2 — Enhancement (improves robustness)

#### P2-1: LLM fallback chain with error recovery
**File**: `xmclaw/llm/router.py`  
**Current**: If primary provider fails, it doesn't fall back to secondary.

**Fix**: Implement a fallback chain: if `anthropic` fails → try `openai` → try plugin providers.

```python
async def stream(self, messages, tools=None):
    for provider in self._provider_chain:
        try:
            async for chunk in provider.stream(messages, tools):
                yield chunk
            return
        except Exception as e:
            logger.warning("provider_failed", provider=provider.name, error=str(e))
            continue
    raise RuntimeError("All LLM providers failed")
```

#### P2-2: Vector store graceful degradation
**File**: `xmclaw/memory/vector_store.py`  
**Impact**: If `sqlite-vec` or embedding API is unavailable, memory search silently returns empty.

Add a `VectorStoreHealthCheck` that runs on startup and logs warnings if vector store is unavailable.

#### P2-3: Evolution cycle dry-run mode
**File**: `xmclaw/evolution/engine.py`, `xmclaw/evolution/scheduler.py`  
**Impact**: Cannot test evolution without actually modifying files.

Add `xmclaw config set evolution.dry_run true` — when enabled, VFM scores are computed and logged but no Gene/Skill files are written.

---

### P3 — Polish (good-to-have)

#### P3-1: Gene/Skill version history
Track version history for auto-generated genes and skills. When a gene is re-generated, archive the old version with a timestamp. This makes rollbacks safe.

#### P3-2: MCP server health check
Before connecting to an MCP server, verify it's reachable (`curl` or `ping` depending on transport type). If unreachable, log a warning and skip rather than hanging.

#### P3-3: Web UI — full migration to modules
`main_new.js` (2294 lines) still contains all DOM rendering logic. A gradual migration to Vue 3 or Preact would reduce complexity and enable proper component testing.

Target: Extract chat rendering (`addMessage`, tool cards, reflection) into `src/modules/chat.js`.

#### P3-4: `xmclaw --help` rich output
**File**: `xmclaw/cli/main.py`  
Typer auto-generates help, but the rich markup (`[bold]`, `[green]`) may not render in all terminals. Add `--help` formatting checks.

---

## Completed Items (for reference)

These were fixed in the previous sessions:

- ✅ Evolution idle cycling (session window 20→200, insight dedup)
- ✅ Skills syntax errors (AST parsing for `FunctionDef` vs `Call`)
- ✅ Session disconnect (WS offline queue + 25s heartbeat)
- ✅ Tool repeated init (`ToolRegistry` singleton `set_shared`/`get_shared`)
- ✅ VFM too simplistic (novelty/clarity/actionability/relevance scoring)
- ✅ Web UI crude (URL routing, offline queue, shortcuts, draft persistence)
- ✅ API key plaintext (Fernet encryption + PBKDF2)
- ✅ Tool plugin architecture (`plugins/tools/` discovery)
- ✅ LLM multi-provider (`LLMProviderPlugin` base class)
- ✅ EventBus unused (audit log, tool analytics, new event types)
- ✅ Multi-agent collaboration (`orchestrator.run_team()`)
- ✅ MCP single transport (stdio + SSE + WebSocket)
- ✅ Config not flexible (`xmclaw config set` dot notation + env overrides)
- ✅ Reflection never called (now `_schedule_reflection()` with `asyncio.create_task`)
- ✅ Reflection not utilized (now `load_context()` → `PromptBuilder` insights injection)
- ✅ `prompt_builder.py` hardcoded Windows path (now dynamic `BASE_DIR`)
- ✅ P0-1: ask_user re-execution fix (`.asend()` pattern, server.py + agent_loop.py)
- ✅ P1-1: genes table schema fix (added priority/enabled/intents/regex_pattern/trigger_type columns + migration)
- ✅ P1-2: Gene trigger type system (explicit `trigger_type`: keyword/regex/intent/event in DB + GeneManager.match())
- ✅ P1-3: CLI multi-turn with ask_user support (recursive `_handle_stream()`, gateway.receive_stream() no longer breaks on done)
- ✅ Frontend: Dev Panel (Plan/File/Diff tabs, devpanel.js module, wired to WS events)
- ✅ Frontend: Floating shortcuts bar (Plan/LLM/Evolution/Skills/Channel/Dev/View toggles + LLM quick config tooltip)
- ✅ Frontend: Message card inline edit overlay (Ctrl+E, replaces browser prompt)
- ✅ Frontend: Scroll-to-bottom button (auto-shows when not at bottom, disappears when scrolled)
- ✅ Frontend: WebSocket.js missing `handleReflectionComplete` import (reflection events now work)
- ✅ Frontend: Plan mode title + `[PLAN APPROVE]` injection in ask_user dialog

---

## File Health Summary

| Category | Count | Status |
|----------|-------|--------|
| Python source files (xmclaw/) | 93 | ✅ All syntax OK |
| Test files | 11 | ✅ All syntax OK |
| Generated genes (shared/genes/) | ~200 | ✅ Syntax OK |
| Generated skills (shared/skills/) | ~100 | ✅ Syntax OK |
| JS modules (web/src/modules/) | 6 | ✅ Brace balanced |
| main_new.js | ~2400 lines | ✅ Brace balanced |
| Relative import resolution | — | ✅ All resolve |
| pyproject.toml deps declared | 20 | ✅ Complete |
| Documentation files | 8 | ✅ |

---

## Release Readiness Checklist

Before a 1.0 release, the following must pass:

- [ ] `xmclaw start` → daemon starts without errors
- [ ] `xmclaw doctor` → all checks pass
- [ ] Web UI loads at `http://127.0.0.1:8080`
- [ ] WebSocket connects to `ws://127.0.0.1:8765/agent/default`
- [ ] Sending a message gets a streaming response
- [ ] `xmclaw chat` CLI works end-to-end
- [ ] `xmclaw config set` persists correctly
- [ ] Evolution cycle runs without generating empty genes
- [ ] Tool plugins in `plugins/tools/` are auto-loaded
- [ ] All pytest tests pass (`python -m pytest tests/ -v`)
- [ ] No syntax errors in any Python file (`find xmclaw -name "*.py" | xargs python3 -m py_compile`)
