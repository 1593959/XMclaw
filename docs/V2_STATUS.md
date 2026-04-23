# XMclaw v2 — Status snapshot

**Date:** 2026-04-23
**Branch:** `main` (v2-rewrite merged; v1 strangler-fig sweep complete)
**State:** Phase 1–4 complete. Autonomous self-evolution is live-validated; the v2 daemon is end-to-end usable via CLI + Web UI (`xmclaw/daemon/static/`).

---

## The one-paragraph summary

XMclaw v2 is a rewrite of v1 around a differentiated claim: **evolution
is the runtime, not a plugin.** A streaming observer bus feeds every
behavioral event (tool call, LLM response, skill exec) to an Honest
Grader and an Online Scheduler; the Evolution Controller promotes
winning skill versions based on evidence, never LLM self-judgment; the
Skill Registry stores versions with a forever audit trail. On real
MiniMax, the full autonomous cycle (bandit → grader → promote → HEAD →
next session) lifts session-level mean reward by **18%** — no human in
the loop.

---

## Numbers (all reproducible)

### Live bench results on `minimax-portal/MiniMax-M2.7-highspeed`

| Bench | What it measures | Result | Gate |
|---|---|---|---|
| Phase 1.3 learning curve | Streaming evolution lift vs uniform-random baseline | **1.119×** | ≥ 1.05× |
| Phase 2.6 tool-aware | Agent actually invokes tools (anti-req #1 live) | **100% tool-firing** | ≥ 80% |
| Phase 3.5 autonomous evolution | Session 2 (HEAD) vs Session 1 (bandit) | **1.183×** | ≥ 1.05× |

### Hermetic test suite

```
875 v2 tests collected (unit + integration + conformance + offline bench)
    on Windows, macOS, Linux matrix
 3 live benches passing when XMC_ANTHROPIC_API_KEY set
CI hard gates: import-direction check, v2 ping smoke, smart-gate — all green
```

---

## Anti-requirement scorecard (14 items)

| # | Name | Code evidence |
|---|---|---|
| 1 | Don't trust text-claimed tool calls | Proven at translator + grader + live agent-loop layers |
| 2 | Memory is semantic + layered + never auto-injected | `SqliteVecMemory` + surface-scan test for injection methods |
| 3 | Per-provider translator is strict | 29 rejection tests + cross-provider parametric matrix |
| 4 | LLM self-judgment capped at 0.20 | `HonestGrader` weight assertion at import + unit tests |
| 5 | Skills are versioned and rollback-able | `SkillRegistry` + append-only history + integration test |
| 6 | Hard budget circuit-breaker | v1 `cost_tracker` migrated; full hard-cap test in Phase 4 |
| 7 | Channel CI parity (matrix) | Parametric channel conformance (WS, matrix N=1) |
| 8 | WS device-bound auth | Pairing-token shared-secret auth (Phase 4.4): 0600 file perms, query + header accept, constant-time compare, `close(4401)` reject, crash-safe. ed25519 pairing ceremony deferred to Phase 4.7 |
| 9 | Session lifecycle explicit | Cross-session memory integration tests pass |
| 10 | Multi-backend runtime parity | Runtime conformance matrix (LocalSkillRuntime, N=1) |
| **11** | **Same-model bench gate** | **14-test conformance: v2 sends byte-identical API body to a naked SDK** |
| **12** | **Promotion requires evidence** | **Defense in depth: controller + registry both refuse empty evidence** |
| 13 | Open + plugin-able | 7 provider ABCs + import-direction CI gate |
| 14 | Cross-protocol + cross-OS | Anthropic + OpenAI paths; CI matrix runs Windows / macOS / Linux on every push to main |

**12 / 14 fully encoded in code with tests.** The remaining two
partial items are: #6 hard-budget circuit-breaker (v1 cost-tracker
migrated; full e2e cap test lands with daemon integration bench) and
#14 cross-OS (matrix CI is wired; the first few green runs live on
the Actions tab).

---

## Files a reviewer should open first

**Architecture and status:**
- [REWRITE_PLAN.md §11](REWRITE_PLAN.md#11-交付现状2026-04-21) — delivery matrix
- [V2_DEVELOPMENT.md §4](V2_DEVELOPMENT.md#4-事件-schemabehavioralevent) — event schema (v2's data contract)
- [V2_DEVELOPMENT.md §5](V2_DEVELOPMENT.md#5-数据流从用户消息到-agent-响应) — data flow

**Proof that things actually run:**
- [tests/bench/phase1_live_learning_curve.py](../tests/bench/phase1_live_learning_curve.py) — run with `XMC_ANTHROPIC_API_KEY` set
- [tests/bench/phase2_tool_aware_live.py](../tests/bench/phase2_tool_aware_live.py) — tool-aware live bench
- [tests/bench/phase3_autonomous_evolution_live.py](../tests/bench/phase3_autonomous_evolution_live.py) — the 1.18× run

**Core differentiators:**
- [xmclaw/core/grader/verdict.py](../xmclaw/core/grader/verdict.py) — LLM opinion capped at 0.20 (anti-req #4)
- [xmclaw/core/evolution/controller.py](../xmclaw/core/evolution/controller.py) — four-gate promotion engine
- [xmclaw/skills/registry.py](../xmclaw/skills/registry.py) — versioning + append-only history

---

## What's NOT done

- **Sandboxed skill runtime (Epic #3):** `LocalSkillRuntime` enforces a CPU/wall-clock timeout in-process, but a rogue skill can still `Path(...).read_text()` anywhere. `providers/runtime/process.py` is the isolation path; subprocess/docker hard-isolation is what graduates it from "dev default" to "production default". Neither runtime is wired into the AgentLoop yet — skills execute via the scheduler's in-process path.
- **Second-stage anti-req #14 coverage:** `policy.apply_policy()` is wired at tool-output boundary and exposes four stable source tags (`tool_result` / `agent_profile` / `memory_recall` / `web_fetch`). **SOUL / PROFILE / AGENTS.md auto-injection is not currently implemented in the AgentLoop** — the only system-prompt source is the static `_DEFAULT_SYSTEM`. Memory-recall likewise is not auto-injected (consistent with anti-req #2). The `agent_profile` / `memory_recall` source tags are placeholders waiting for the first consumer; the guard will activate automatically the moment one appears.
- **72h daemon stress test (M1 exit):** not session-runnable; needs a real multi-day window on a dedicated host.

---

## How to reproduce the live bench

```bash
git checkout v2-rewrite
pip install -e .

# 1. Phase 1 learning curve
export XMC_ANTHROPIC_API_KEY=sk-ant-...  # or a compat key
python -m pytest tests/bench/phase1_live_learning_curve.py -v -s

# 2. Tool-aware
python -m pytest tests/bench/phase2_tool_aware_live.py -v -s

# 3. Autonomous evolution
python -m pytest tests/bench/phase3_autonomous_evolution_live.py -v -s
```

Each run takes 5–10 minutes and costs <$0.10 on Haiku-class models.
Per-turn logs land at `tests/bench/_phase{1,2,3}_live_log.jsonl`
(gitignored) for post-hoc inspection.
