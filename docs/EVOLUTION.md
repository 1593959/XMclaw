# Evolution — how XMclaw's skills improve themselves

> "XMclaw 会进化" is the core differentiator. This doc is the **user-facing
> contract**: what triggers an evolution, what gates it, how you see it, and
> how to tune / disable it. If you want the *why* (strategy / competitive
> positioning), read [ARCHITECTURE.md](ARCHITECTURE.md) §Skill evolution
> and the archived [COMPETITIVE_GAP_ANALYSIS](archive/COMPETITIVE_GAP_ANALYSIS.archived.md).

---

## 1. The short version

A **skill** is a named piece of agent behavior (`email_digest`,
`github_code_review`). XMclaw evolves skills along two independent axes:

* **Vertical (mutation):** an existing skill drifts to a worse score → the
  daemon synthesises a `v(N+1)` from grader history, registers it
  un-promoted, and the controller decides whether the new version beats
  HEAD.
* **Horizontal (proposing):** the daemon notices a recurring pattern in
  the journal that no installed skill covers → drafts a brand-new skill
  and (when the proposer's confidence clears the floor) materialises it
  as a runnable v1 on disk under `~/.xmclaw/skills_user/`.

Both paths funnel through the same final stage:

1. The **HonestGrader** emits `GRADER_VERDICT` with a 0–1 score from
   *objective* checks (anti-req #4 — never an LLM judge).
2. The **EvolutionAgent** EWMA-aggregates verdicts per
   `(skill_id, version)`.
3. The **EvolutionEvaluationTrigger** (B-294) — debounced + cooldown'd —
   calls `EvolutionAgent.evaluate()` once a burst of verdicts settles.
4. The **EvolutionController** decides on four objective gates whether
   the leading candidate beats HEAD; if yes it emits
   `SKILL_PROMOTION_RECOMMENDED` (B-318 alias of the legacy
   `SKILL_CANDIDATE_PROPOSED decision="promote"`).
5. The **EvolutionOrchestrator** (when `auto_apply=True`) calls
   `SkillRegistry.promote(evidence=[...])` — HEAD moves, history line
   appended to `~/.xmclaw/skills/<skill_id>.jsonl`, `SKILL_PROMOTED`
   fires.
6. Every connected REPL flashes `[evolved] <skill> v3→v4` in green.

No human in the loop *for promotion* (when auto_apply is on); no silent
mutation either — HEAD moves only when objective evidence clears every
gate, and you see it the moment it happens. New-skill drafts always
materialise; whether their v1 wins HEAD against an alternative still
depends on the grader.

---

## 2. The pipeline in one picture

```
                         tool calls land verdicts on the bus
                                          │
                                          ▼
                                   HonestGrader
                                  (objective only)
                                          │
                                          ▼
                                   GRADER_VERDICT  ───────────┐
                                          │                   │
                                          ▼                   │
                                   EvolutionAgent             │
                                  (EWMA per arm)              │
                                          │                   │
            ┌─────────────────────────────┴────────────┐      │
            ▼                                          ▼      ▼
  EvolutionEvaluationTrigger               MutationOrchestrator (B-172)
       (B-294)                              triggers on EWMA drop —
   debounce 30s + cooldown 300s             builds DSPy/GEPA dataset
   + min_new_verdicts 10                    from events.db, runs
            │                               SkillMutator → writes
            ▼                               versions/v(N+1).md, registers
   EvolutionAgent.evaluate()                un-promoted, emits propose
            │
            ▼
   EvolutionController (4 gates)
            │
            ▼
   SKILL_PROMOTION_RECOMMENDED      ────────────┐
   (decision="promote" payload —                │
    legacy alias: SKILL_CANDIDATE_PROPOSED)     │
                                                ▼
                                      EvolutionOrchestrator
                                       (auto_apply=True only)
                                                │
                                                ▼
                                  SkillRegistry.promote(evidence=[...])
                                                │
                                                ▼
                                         SKILL_PROMOTED
                                                │
                                                ▼
                                  every REPL flashes [evolved]
                                  `xmclaw evolution show` logs it
                                  session report picks it up


  ── Net-new skill axis (independent of the verdict pipeline) ─────────
                                          │
   ┌──────────────────────────────────────┼──────────────────────────┐
   ▼                                      ▼                          ▼
  RealtimeEvolutionTrigger (B-164)   SkillDreamCycle (Epic #24    `xmclaw skills propose`
   after every turn                  Phase 3.2)                   (manual, CLI)
   debounce 15s + cooldown 60s        every 30 min
                  │                          │
                  └────────────┬─────────────┘
                               ▼
                        SkillProposer
                  (LLM-backed extractor reads
                   recent journal, emits a
                   ProposedSkill with body +
                   evidence + triggers)
                               │
                               ▼
              SKILL_DRAFTED  (B-318)        ◄── audit-only event
                               │
                               ▼
                    ProposalMaterializer (B-167)
                  writes ~/.xmclaw/skills_user/<id>/SKILL.md,
                  registers v1, set_head=True
                               │
                               ▼
                       agent picks up next turn
                       (verdicts start landing → vertical pipeline)
```

Key files:

- [xmclaw/core/grader/verdict.py](../xmclaw/core/grader/verdict.py) — scoring
- [xmclaw/core/evolution/controller.py](../xmclaw/core/evolution/controller.py) — four-gate decision
- [xmclaw/daemon/evolution_agent.py](../xmclaw/daemon/evolution_agent.py) — EWMA aggregation; `EvolutionAggregator` is the B-317 forward-compat alias
- [xmclaw/daemon/evolution_evaluation_trigger.py](../xmclaw/daemon/evolution_evaluation_trigger.py) — B-294 hop-3 wiring
- [xmclaw/daemon/mutation_orchestrator.py](../xmclaw/daemon/mutation_orchestrator.py) — B-172 vertical mutation via DSPy/GEPA
- [xmclaw/daemon/skill_dream.py](../xmclaw/daemon/skill_dream.py) — periodic + realtime SkillProposer drivers
- [xmclaw/daemon/proposal_materializer.py](../xmclaw/daemon/proposal_materializer.py) — B-167 draft→disk→registered
- [xmclaw/skills/orchestrator.py](../xmclaw/skills/orchestrator.py) — bus bridge to registry
- [xmclaw/skills/registry.py](../xmclaw/skills/registry.py) — HEAD + history + anti-req #12
- [xmclaw/skills/variant_selector.py](../xmclaw/skills/variant_selector.py) — which skills the mutation orchestrator targets

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
[controller.py](../xmclaw/core/evolution/controller.py) as
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
[verdict.py](../xmclaw/core/grader/verdict.py).

---

## 5. The two evolution paths in detail

### 5a. Vertical — "this existing skill should be better"

Lives in [`MutationOrchestrator`](../xmclaw/daemon/mutation_orchestrator.py)
(B-172). Watches per-skill EWMA from `GRADER_VERDICT` events. When the
EWMA dips past a configurable floor and a per-skill cooldown has elapsed,
it:

1. Pulls the skill's last N grader rows from `events.db`.
2. Builds a DSPy/GEPA training dataset (positive examples = high-score
   plays, negatives = low-score plays).
3. Runs `SkillMutator.mutate(...)` to synthesise a `v(N+1)` markdown
   body.
4. If the synthesis succeeds, writes `versions/v(N+1).md` next to the
   existing skill and `register(set_head=False)` so HEAD doesn't move
   yet — the new version starts collecting its own verdicts.
5. Emits `SKILL_PROMOTION_RECOMMENDED` with `decision="promote"`,
   `winner_version=N+1`, evidence = the score deltas observed.

After 10+ plays of the new version, the same controller / orchestrator
flow from §1 decides if HEAD finally moves.

`SkillMutator.mutate()` returns `ok=False, reason="dspy_not_installed"`
gracefully when the optional `dspy` extra isn't installed, so the
orchestrator no-ops at the cost of one cheap function call per
trigger — better than failing closed.

### 5b. Horizontal — "we should have a skill for this"

Lives in two drivers around the same [`SkillProposer`](../xmclaw/core/evolution/skill_proposer.py):

* [`RealtimeEvolutionTrigger`](../xmclaw/daemon/skill_dream.py) (B-164) —
  subscribes to turn-end events, debounces 15 s, cooldowns 60 s, fires
  the proposer on each settled burst. Catches the "I just used `bash`
  five times this turn to grep, wc, and rg the same dir" pattern within
  minutes.
* [`SkillDreamCycle`](../xmclaw/daemon/skill_dream.py) — periodic
  driver, default 30 min interval. Catches drift the realtime trigger
  missed (idle gaps, daemon restarts).

Both produce `ProposedSkill` objects → `SKILL_DRAFTED` events (B-318) →
[`ProposalMaterializer`](../xmclaw/daemon/proposal_materializer.py)
(B-167) writes the rendered SKILL.md to
`~/.xmclaw/skills_user/<skill_id>/SKILL.md`, wraps it in
`MarkdownProcedureSkill`, and `register(set_head=True)` so the agent
picks it up on the next turn.

The materialiser is **idempotent**: if `skill_id` already exists in the
registry it skips silently. Re-emitted drafts on the next dream tick
are a no-op, not a clobber.

---

## 6. How you see evolution

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

Picks up `skill_promoted` / `skill_rolled_back` /
`skill_promotion_recommended` / `skill_drafted` events from the session's
SQLite log and places them in an `evolution_events` section of the
markdown / JSON report. Useful for retros on a specific session.

### c. REPL flash (live)

`xmclaw chat` subscribes to the bus via WebSocket. When HEAD moves —
regardless of which session triggered the promotion, even the internal
`_system` session the orchestrator uses — every connected REPL prints:

```
  [evolved] email_digest v3→v4
```

Green ANSI on a terminal. Rollbacks print yellow `[rolled back] ... :
<reason>`; new drafts print dim `[drafted] <skill> v0 — <pattern>`;
candidate-promotion recommendations print dim
`[recommend] promote <skill> v<v>`.

The cross-session broadcast is deliberate: a promotion is global state, so
every observer sees it even if their own session didn't drive the evidence.

---

## 7. Configuration

`daemon/config.json` (template: `daemon/config.example.json`):

```json
{
  "evolution": {
    "enabled": true,
    "interval_minutes": 30,
    "daily_review_hour": 22,
    "vfm_threshold": 5.0,
    "max_genes_per_day": 10,
    "auto_rollback": true,
    "auto_apply": false,
    "evaluation": {
      "debounce_s": 30.0,
      "cooldown_s": 300.0,
      "min_new_verdicts": 10
    }
  }
}
```

| Field | Purpose |
|-------|---------|
| `enabled` | Master switch. `false` = no `EvolutionAgent`, no proposals, HEAD never moves automatically. |
| `interval_minutes` | `SkillDreamCycle` interval. 30 min default; shorter bands for workloads with many plays per hour. |
| `daily_review_hour` | Local-time hour for a heavier review pass (looks for rollback candidates based on regression signals). |
| `vfm_threshold` | Value-for-minute floor — a candidate below this is pruned from scheduler rotation regardless of gates. |
| `max_genes_per_day` | Cap on how many *new* candidate variants the mutation orchestrator may spawn in one day. Hard ceiling against runaway exploration. |
| `auto_rollback` | When `true`, a HEAD that regresses below `min_mean` over a sliding window triggers a `SKILL_ROLLED_BACK`. Keeps the registry honest after a bad promotion. |
| `auto_apply` | When `true`, `EvolutionOrchestrator` automatically calls `registry.promote(...)` on `SKILL_PROMOTION_RECOMMENDED`. Default `false` so first-install daemons don't silently rewrite themselves. |
| `evaluation.debounce_s` | B-294 trigger: wait this many seconds of grader-verdict quiet before calling `evaluate()`. Default 30 s — RealtimeEvolutionTrigger uses 15 s for proposal generation; evaluation is less time-sensitive. |
| `evaluation.cooldown_s` | B-294 trigger: at most one `evaluate()` call per this many seconds. Default 300 s — the controller's decision is monotonic given the same snapshot. |
| `evaluation.min_new_verdicts` | B-294 trigger: only fire when at least this many new verdicts have arrived since the last `evaluate()`. Default 10 — below the controller's `min_plays=5` there's nothing to decide. |

### Disabling evolution

Set `evolution.enabled: false`. The `EvolutionAgent` still subscribes to the
bus for `xmclaw evolution show` to work, but the evaluation trigger / dream
cycle / mutation orchestrator stay dark and no proposals are emitted. HEAD
only moves if *you* call `orch.promote(skill_id, v, evidence=[...])`
manually (e.g. a migration script).

### Observing without applying

Default: `EvolutionOrchestrator(auto_apply=False)`. First-install experience
is always opt-in — a freshly-deployed daemon does not silently rewrite
itself. You still see candidate proposals in the REPL flash and in
`session report`, but HEAD stays put until you either (a) flip
`evolution.auto_apply: true` in `daemon/config.json` (b) run `xmclaw
evolution promote <skill> <v>` (Phase C, TBD) or (c) call it from your
own wiring code.

---

## 8. FAQ

**Q: My skill ran ten times but nothing promoted.** Three things to check:

1. *All four gates passed?* `xmclaw evolution show --since 24h` prints
   evidence for every promotion attempt that *would have* fired.
2. *Was `EvolutionEvaluationTrigger` blocking on debounce / cooldown?*
   With `debounce_s=30` and a steady stream of one verdict every 15 s,
   the trigger keeps deferring `evaluate()`. Daemon logs at
   `XMC_LOG_LEVEL=debug` show `evaluation_trigger.deferred` /
   `evaluation_trigger.fired` lines.
3. *Did the controller refuse?* Look for `controller.no_promotion` log
   lines — they include the failing gate and the snapshot.

**Q: Can a bad LLM judge tank my HEAD?** No. The LLM opinion is capped at
20% of the score. If you disabled the structural checks (`ran` /
`returned` / `type_matched` / `side_effect_observable`) everything else
would still have to clear `min_mean=0.65` for a promotion.

**Q: What if the controller promotes a broken skill?** `auto_rollback=true`
watches HEAD's running mean. A regression past `min_mean` over the
configured window publishes `SKILL_ROLLED_BACK` and moves HEAD back. The
REPL flashes a yellow warning; `xmclaw evolution show` records both the
promote and the rollback.

**Q: Where do skills live on disk?** Skill *implementations* live in two
places: built-ins in `xmclaw/skills/` and user / evolved skills in
`~/.xmclaw/skills_user/<skill_id>/`. The **version history** (every
promote / rollback with its evidence) is persisted to
`~/.xmclaw/skills/<skill_id>.jsonl` — the append-only audit log; it
survives workspace wipes (`~/.xmclaw/skills/` is a peer of
`~/.xmclaw/v2/`, not a child).

**Q: Is there a `rollback` CLI command?** Yes, but intentionally only at
the library layer right now: `EvolutionOrchestrator.rollback(skill_id, v,
reason=...)`. A user-facing `xmclaw evolution rollback` command is
Phase C — see [DEV_ROADMAP.md](DEV_ROADMAP.md) Epic #4.

**Q: Does evolution work offline?** Yes. Every piece of the pipeline
(grader, controller, scheduler, evaluation trigger, mutation
orchestrator, dream cycle, proposer, registry) is local. The bus is
in-process. The JSONL history is on-disk. The only external dependency
is the LLM provider that the *skill itself* uses at runtime — and the
mutation orchestrator's optional `dspy` package, which degrades to
no-op when missing.

**Q: Multiple daemons on the same machine?** They each have their own
`~/.xmclaw/skills/` directory by default. Point them at a shared location
via `XMC_V2_SKILLS_DIR=/path/to/shared/skills` if you want a fleet to share
evolution evidence. Concurrent writes to the same JSONL are safe — every
line is an atomic append.

---

## 9. Events emitted by the pipeline

Full schema in [EVENTS.md](EVENTS.md). Summary:

| Event | Source | Payload highlights |
|-------|--------|---------------------|
| `GRADER_VERDICT` | HonestGrader | `skill_id`, `version`, `score`, per-check evidence |
| `SKILL_DRAFTED` (B-318) | SkillProposer (via SkillDreamCycle / RealtimeEvolutionTrigger) | `winner_candidate_id`, `draft={title, description, body, triggers, confidence}`, `reason=source_pattern` |
| `SKILL_PROMOTION_RECOMMENDED` (B-318) | EvolutionAgent.evaluate() | `winner_candidate_id`, `winner_version`, `evidence[]`, `reason` |
| `SKILL_ROLLBACK_RECOMMENDED` (B-318) | EvolutionAgent.evaluate() | as above with `decision="rollback"` |
| `SKILL_PROMOTED` | EvolutionOrchestrator → Registry | `skill_id`, `from_version`, `to_version`, `evidence[]`, `ts` |
| `SKILL_ROLLED_BACK` | EvolutionOrchestrator → Registry | `skill_id`, `from_version`, `to_version`, `reason`, `ts` |

Legacy: `SKILL_CANDIDATE_PROPOSED` is the catch-all event the three B-318
event names refine. New code should emit/handle the specific names; the
legacy alias is preserved so older subscribers and exported audit logs
still parse.

Global-scope (session_id = `_system` by default): all evolution events
are broadcast to every connected REPL — see
[xmclaw/daemon/app.py](../xmclaw/daemon/app.py) `_GLOBAL_EVENT_TYPES`.
