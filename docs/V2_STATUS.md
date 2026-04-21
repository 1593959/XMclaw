# XMclaw v2 — Status snapshot

**Date:** 2026-04-21
**Branch:** `v2-rewrite`
**State:** Phase 3.5 complete. Autonomous self-evolution is live-validated.

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
293 v2 tests passing (unit + integration + conformance + offline bench)
 3 live benches passing when XMC_ANTHROPIC_API_KEY set
 2 v1 tests failing (pre-existing in v1, unrelated to v2 work)
CI hard gates: import-direction check, v2 ping smoke — all green
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
| 8 | WS device-bound auth | `auth_check` callback hook present; impl deferred to Phase 4 |
| 9 | Session lifecycle explicit | Cross-session memory integration tests pass |
| 10 | Multi-backend runtime parity | Runtime conformance matrix (LocalSkillRuntime, N=1) |
| **11** | **Same-model bench gate** | **14-test conformance: v2 sends byte-identical API body to a naked SDK** |
| **12** | **Promotion requires evidence** | **Defense in depth: controller + registry both refuse empty evidence** |
| 13 | Open + plugin-able | 7 provider ABCs + import-direction CI gate |
| 14 | Cross-protocol + cross-OS | Anthropic + OpenAI paths; Windows dev tested; macOS/Linux in CI TODO |

**11 / 14 fully encoded in code with tests. 3 deferred to Phase 4.**

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

- **Phase 3.4 subprocess sandbox:** `LocalSkillRuntime` only enforces CPU timeout in-process; a rogue skill can still `Path(...).read_text()` anywhere. Needs subprocess/docker isolation. Not blocking — Phase 3.5 doesn't depend on it.
- **Phase 4 daemon integration + web UI + release:** none of `xmclaw/daemon/*` / `web/*` rebuild has happened yet. v1 daemon is still the shipping version.
- **v1 strangler-fig cleanup:** v1 modules (`xmclaw/core/agent_loop.py`, `evolution/*`, `genes/*`, `task_classifier.py`) still live alongside v2. Intentional; let Phase 4 delete them together with v1 release deprecation.

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
