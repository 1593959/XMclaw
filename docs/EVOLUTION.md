# Evolution — how XMclaw's skills improve themselves

> "XMclaw 会进化" is the core differentiator. This doc is the **user-facing
> contract**: what triggers an evolution, what gates it, how you see it, and
> how to tune / disable it. If you want the *why* (strategy / competitive
> positioning), read [ARCHITECTURE.md](ARCHITECTURE.md) §Skill evolution
> and the archived [COMPETITIVE_GAP_ANALYSIS](archive/COMPETITIVE_GAP_ANALYSIS.archived.md).

---

## 1. The short version

A **skill** is a named piece of agent behavior (e.g. `email_digest`,
`github_code_review`). Each time a skill runs:

1. The **HonestGrader** emits a `GRADER_VERDICT` with a 0–1 score — NOT from
   an LLM judge, but from objective checks: did the tool run, did it return,
   did the result type match, did the declared side-effect land on disk.
2. The **EvolutionAgent** accumulates `(skill_id, version) → plays, reward`
   over a rolling window.
3. When a candidate version beats HEAD on **all four gates**
   (see §3), the agent emits `SKILL_CANDIDATE_PROPOSED`.
4. The **EvolutionOrchestrator** (if `auto_apply=True`) promotes the
   candidate via `SkillRegistry.promote(evidence=[...])` — HEAD moves,
   history line appended to `~/.xmclaw/skills/<skill_id>.jsonl`, and a
   `SKILL_PROMOTED` event fires on the bus.
5. Every connected REPL flashes a green `[evolved] <skill> v3→v4` line.

No human in the loop, no silent mutation — HEAD moves *only* when objective
evidence clears every gate, and you see it the moment it happens.

---

## 2. The pipeline in one picture

```
  tool runs                                                       bus
      │                                                            │
      ▼                                                            │
  HonestGrader ──► GRADER_VERDICT ──► EvolutionAgent ──► aggregates│
   (objective          (score, evidence)   (plays, reward)         │
    checks)                                     │                   │
                                                ▼                   │
                            EvolutionController (pure decision)     │
                                                │                   │
                                    four gates pass?                │
                                                │                   │
                                                ▼                   │
                              SKILL_CANDIDATE_PROPOSED ──────────► bus
                                                                    │
                                                                    ▼
                                             EvolutionOrchestrator
                                             (auto_apply=True only)
                                                    │
                                                    ▼
                                     SkillRegistry.promote(evidence=…)
                                                    │
                                                    ▼
                                            SKILL_PROMOTED ──► bus
                                                                │
                                                                ▼
                                           every REPL flashes [evolved]
                                           `xmclaw evolution show` logs it
                                           session report picks it up
```

Key files:

- [xmclaw/core/grader/verdict.py](../xmclaw/core/grader/verdict.py) — scoring
- [xmclaw/core/evolution/controller.py](../xmclaw/core/evolution/controller.py) — four-gate decision
- [xmclaw/daemon/evolution_agent.py](../xmclaw/daemon/evolution_agent.py) — aggregation + proposal
- [xmclaw/skills/orchestrator.py](../xmclaw/skills/orchestrator.py) — bus bridge to registry
- [xmclaw/skills/registry.py](../xmclaw/skills/registry.py) — HEAD + history + anti-req #12

---

## 3. The four promotion gates

A candidate version `v` promotes past HEAD only if **all** of these hold:

| Gate | Default | Why |
|------|---------|-----|
| `min_plays` | 10 | A handful of lucky runs isn't evidence. Ten plays buys signal. |
| `min_mean` | 0.65 | The mean grader score must beat a floor, absolute — not just "better than HEAD's bad day". |
| `min_gap_over_head` | 0.05 | 5% improvement over the current HEAD; below that is noise. |
| `min_gap_over_second` | 0.03 | 3% over the second-place candidate — otherwise the "winner" is a tie we flipped with a coin. |

The thresholds live in
[controller.py:49](../xmclaw/core/evolution/controller.py#L49) as
`PromotionThresholds`. You can override them by constructing
`EvolutionAgent(thresholds=PromotionThresholds(min_plays=20, ...))` if you
wire your own daemon factory.

### Anti-req #12 — no promotion without evidence

`SkillRegistry.promote()` raises `ValueError` if `evidence=[]`. The
orchestrator passes the controller's evidence tuple straight through, so
there's no way to reach HEAD movement without a list of reasons. This is
the hard backstop: a malformed proposal, a race, a buggy controller — all
refused at the registry door, no event emitted, nothing to replay as a
phantom promotion.

---

## 4. Scoring — what the grader actually measures

**Not an LLM judge.** The grader's weight is intentionally objective because
LLM-as-judge poisons its own feedback loop (anti-req #4). What it scores:

| Check | Weight | Source |
|-------|--------|--------|
| `ran` | 30% | Did the tool actually dispatch? |
| `returned` | 20% | Did it return a non-error result? |
| `type_matched` | 25% | Result type matches what the skill declared? |
| `side_effect_observable` | 25% | Declared side-effect (file write, network call) is verifiable after the fact. |
| LLM opinion (optional) | ≤20% | If a downstream LLM call grades the *output quality*, it lands here capped — never dominates. |

The LLM cap is deliberate: qualitative judgment can tip a tie, but it cannot
outvote the structural checks. See
[verdict.py:98](../xmclaw/core/grader/verdict.py#L98).

---

## 5. How you see evolution

Three surfaces, in increasing interactivity:

### a. CLI — `xmclaw evolution show`

Offline view of `~/.xmclaw/skills/*.jsonl`. No daemon needed.

```
$ xmclaw evolution show --since 7d
2026-04-19 14:02  email_digest       v3 → v4   [evidence: plays=14 mean=0.78 gap_head=0.09]
2026-04-20 09:11  github_code_review v1 → v2   [evidence: plays=11 mean=0.71 gap_head=0.06]
2026-04-22 18:44  email_digest       v4 → v5   [evidence: plays=22 mean=0.82 gap_head=0.07]
```

Supports `--since 24h`, `--since 7d`, `--since 48`, etc.

### b. `xmclaw session report <session_id>`

Picks up `skill_promoted` / `skill_rolled_back` / `skill_candidate_proposed`
events from the session's SQLite log and places them in an `evolution_events`
section of the markdown / JSON report. Useful for retros on a specific
session.

### c. REPL flash (live)

`xmclaw chat` subscribes to the bus via WebSocket. When HEAD moves —
regardless of which session triggered the promotion, even the internal
`_system` session the orchestrator uses — every connected REPL prints:

```
  [evolved] email_digest v3→v4
```

Green ANSI on a terminal. Rollbacks print yellow `[rolled back] ... :
<reason>`; candidate proposals print dim `[candidate] <skill> v<v> proposed`.

The cross-session broadcast is deliberate: a promotion is global state, so
every observer sees it even if their own session didn't drive the evidence.

---

## 6. Configuration

`daemon/config.json` (template: `daemon/config.example.json`):

```json
{
  "evolution": {
    "enabled": true,
    "interval_minutes": 30,
    "daily_review_hour": 22,
    "vfm_threshold": 5.0,
    "max_genes_per_day": 10,
    "auto_rollback": true
  }
}
```

| Field | Purpose |
|-------|---------|
| `enabled` | Master switch. `false` = no `EvolutionAgent`, no proposals, HEAD never moves automatically. |
| `interval_minutes` | How often the agent evaluates candidates. 30 min is the default; shorter bands for workloads with many plays per hour. |
| `daily_review_hour` | Local-time hour for a heavier review pass (looks for rollback candidates based on regression signals). |
| `vfm_threshold` | Value-for-minute floor — a candidate below this is pruned from scheduler rotation regardless of gates. |
| `max_genes_per_day` | Cap on how many *new* candidate variants a scheduler may spawn in one day. Hard ceiling against runaway exploration. |
| `auto_rollback` | When `true`, a HEAD that regresses below `min_mean` over a sliding window triggers a `SKILL_ROLLED_BACK`. Keeps the registry honest after a bad promotion. |

### Disabling evolution

Set `evolution.enabled: false`. The `EvolutionAgent` still subscribes to the
bus for `xmclaw evolution show` to work, but no proposals are emitted. HEAD
only moves if *you* call `orch.promote(skill_id, v, evidence=[...])`
manually (e.g. a migration script).

### Observing without applying

Default: `EvolutionOrchestrator(auto_apply=False)`. First-install experience
is always opt-in — a freshly-deployed daemon does not silently rewrite
itself. You still see candidate proposals in the REPL flash and in
`session report`, but HEAD stays put until you either (a) flip
`auto_apply=True` on the orchestrator (b) run `xmclaw evolution promote
<skill> <v>` (Phase C, TBD) or (c) call it from your own wiring code.

---

## 7. FAQ

**Q: My skill ran ten times but nothing promoted.** Check all four gates.
`xmclaw evolution show --since 24h` prints evidence for every promotion
attempt that *would have* fired. Use the daemon logs (`XMC_LOG_LEVEL=debug
xmclaw serve`) for attempted-but-refused promotions — the orchestrator logs
a `promote_refused` warning with the reason.

**Q: Can a bad LLM judge tank my HEAD?** No. The LLM opinion is capped at
20% of the score. If you disabled the structural checks (`ran` /
`returned` / `type_matched` / `side_effect_observable`) everything else
would still have to clear `min_mean=0.65` for a promotion.

**Q: What if the controller promotes a broken skill?** `auto_rollback=true`
watches HEAD's running mean. A regression past `min_mean` over the
configured window publishes `SKILL_ROLLED_BACK` and moves HEAD back. The
REPL flashes a yellow warning; `xmclaw evolution show` records both the
promote and the rollback.

**Q: Where do skills live on disk?** Skill implementations live in code —
`xmclaw/skills/` and any plugin packages. Only the **version history**
(every promote / rollback with its evidence) is persisted, to
`~/.xmclaw/skills/<skill_id>.jsonl`. This is the append-only audit log; it
survives workspace wipes (`~/.xmclaw/skills/` is a peer of
`~/.xmclaw/v2/`, not a child).

**Q: Is there a `rollback` CLI command?** Yes, but intentionally only at the
library layer right now: `EvolutionOrchestrator.rollback(skill_id, v,
reason=...)`. A user-facing `xmclaw evolution rollback` command is
Phase C — see [DEV_ROADMAP.md](DEV_ROADMAP.md) Epic #4.

**Q: Does evolution work offline?** Yes. Every piece of the pipeline
(grader, controller, scheduler, orchestrator, registry) is local. The bus
is in-process. The JSONL history is on-disk. The only external dependency
is the LLM provider that the *skill itself* uses at runtime.

**Q: Multiple daemons on the same machine?** They each have their own
`~/.xmclaw/skills/` directory by default. Point them at a shared location
via `XMC_V2_SKILLS_DIR=/path/to/shared/skills` if you want a fleet to share
evolution evidence. Concurrent writes to the same JSONL are safe — every
line is an atomic append.

---

## 8. Events emitted by the pipeline

Full schema in [EVENTS.md](EVENTS.md). Summary:

| Event | Source | Payload highlights |
|-------|--------|---------------------|
| `GRADER_VERDICT` | HonestGrader | `skill_id`, `version`, `score`, per-check evidence |
| `SKILL_CANDIDATE_PROPOSED` | EvolutionAgent | `winner_candidate_id`, `winner_version`, `evidence[]`, `reason` |
| `SKILL_PROMOTED` | EvolutionOrchestrator → Registry | `skill_id`, `from_version`, `to_version`, `evidence[]`, `ts` |
| `SKILL_ROLLED_BACK` | EvolutionOrchestrator → Registry | `skill_id`, `from_version`, `to_version`, `reason`, `ts` |

Global-scope (session_id = `_system` by default): all four evolution events
are broadcast to every connected REPL — see
[xmclaw/daemon/app.py](../xmclaw/daemon/app.py) `_GLOBAL_EVENT_TYPES`.
