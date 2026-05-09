# Evolution Loop — Honest State (2026-05-09)

This is the **non-marketing** assessment of XMclaw's skill-evolution
pipeline. Written so users / contributors / reviewers know exactly
what works, what doesn't, what's a stub, and what's planned. Updated
each Sprint.

## TL;DR

> **`evolution.enabled` defaults to `false`.** The architecture is in
> place; the signals are weak; promotion is human-gated; benchmark
> numbers do not exist yet. Sprint 3 rebuilds the core; Sprint 4
> publishes A/B numbers from LongMemEval / TerminalBench / SWE-bench.
> Until those land, do not enable it for production usage and do not
> claim XMclaw is "self-evolving" in marketing — that's the goal,
> not the current verified property.

## What works today (when `enabled=true`)

| Subsystem | State | Evidence |
|---|---|---|
| **BehavioralEvent bus** | ✅ Solid | SQLite WAL + FTS5; all observers ride it; integration-tested. |
| **JournalWriter** | ✅ Solid | One row per session under `~/.xmclaw/v2/journal/<YYYY-MM>/`. Used by every other observer. |
| **ExtractFactsHook (B-319)** | ✅ Works | Routes 6-bucket facts to AGENTS.md / TOOLS.md / MEMORY.md / SOUL.md / LEARNING.md / USER.md after every turn. Single LLM round-trip. |
| **SkillRegistry version control** | ✅ Solid | `register / promote / rollback` with evidence requirement at the door (anti-req #12). In-memory; in-process is the source of truth. |
| **Manual `xmclaw evolve` CLI** | ✅ Solid | `review / approve / reject` work as advertised. Humans can drive promotion explicitly. |

## What's weak today (the actual gaps)

| Subsystem | Honest state | Plan |
|---|---|---|
| **HonestGrader signal quality** | 70-80% of scores come from "tool didn't crash" (`ran` is trivially true; `type_matched` defaults trivial when no expected type was declared; `side_effect` only checks fs writes). LLM self-rating is capped at 0.20 but still gameable. | Sprint 3: tighten `ran` to non-trivial-output, require expected type for `type_matched` to count, add a 2nd independent signal (user feedback or holdout test). 3 铁律 #1 — never single-signal promotion. |
| **MutationOrchestrator** | DSPy/GEPA-based; **default disabled** because DSPy is not a hard dep (production install rate ≈ 0). When enabled, depends on DSPy major-version stability, which Hermes' postmortem proved is fragile. | Sprint 3: replace with own implementation of GEPA-style reflective mutation prompt + per-context Pareto frontier. ~500-800 LOC. No DSPy hard dep. |
| **Promotion thresholds** | Cooldown 300s / min_plays 10 / min_gap 0.05 → fires 1-3 times/day, promotes ~0. | Sprint 3: rewrite gating with staged candidate + 4 explicit gates (size / growth / structure / holdout). 3 铁律 #2 — staging → gate → explicit promote. |
| **SkillDreamCycle / RealtimeEvolutionTrigger** | Drafts skills from journal patterns; but the patterns are detected by simple keyword frequency, not behavioral analysis. Quality ≈ "statistically common phrases", not "useful tools". | Sprint 3: feed Live-SWE-style step-reflection prompts at end of each turn so the agent itself proposes tools it wishes it had. Live-SWE ships at 79.2% SWE-bench Verified using exactly this pattern. |
| **Per-model capability** | Same prompts to GPT-5 / Claude / 7B models. Live-SWE issue #7 already proved GPT-5 won't synthesize tools regardless of prompt — they ship with Claude-Sonnet-only. | Sprint 3: per-provider capability profile registry. Strong models (Claude / Opus) get self-extension prompts; weak models (GPT-5 / 7B) get template-fill mode. 3 铁律 #3 — never silently downgrade. |
| **Strategy distillation** | Doesn't exist. Today we keep raw traces + facts; we do not distill "remember strategy X for future task Y". | Sprint 3: ReasoningBank-style strategy distiller. Google paper claims +34% / -16% steps from this alone. |
| **Idle-time evaluation** | The dream-cycle runs on cron, not on idle-detection; can collide with active foreground turns. | Sprint 3: split foreground / sleep-time agents (Letta pattern). Idle-detected via OS API; only sleep agent writes memory; foreground reads. |
| **Benchmark numbers** | Zero. We have unit tests for individual components but no end-to-end before/after evolution comparison on a held-out suite. | Sprint 4: A/B on LongMemEval (memory) + TerminalBench 2.0 (skill use) + SWE-bench Verified subset (problem solving). Publish in README. |

## Why peers haven't solved this either

(So you understand we're not just behind — the whole field is.)

- **Hermes** (the leading "self-evolving" peer): project stalled
  2026-04, 19 PRs unmerged, validator silently rejects every evolved
  skill due to a frontmatter parsing bug (issues #11/#34/#38). 107
  Reddit upvotes for "It always thinks it did a good job. ALWAYS"
  (single-signal echo chamber).
- **Live-SWE-agent**: 79.2% SWE-bench Verified — open-source SOTA —
  but the entire "self-evolution" mechanism is one prompt sentence +
  bash heredoc to write `.py` tools. Paper claims "any model"; issue
  #7 proves GPT-5 doesn't actually synthesize tools, only Claude.
- **EvoMap**: most architecturally complete (Gene-Capsule-Event +
  Idle Scheduler + Validator role + 7 strategy presets), but 27/53
  core files are obfuscated (issue #499). `evolver --loop` actually
  just prints prompts; doesn't modify code.
- **Letta**: only peer with verified benchmark (TerminalBench 2.0
  +36.8% relative / +15.7% absolute). Skill learning is **manual**
  — user types `/skill trigger`. Archival memory has a known dedup
  hole (#3116, unresolved).
- **DSPy + GEPA**: ICLR 2026 Oral (academic SOTA; +6-20% vs GRPO at
  35× fewer rollouts). But **batch optimizer** — needs an offline
  dataset. Doesn't fit a streaming-conversation runtime without
  significant adaptation.

Three peer-research consensus findings (multiple ICLR / TACL papers):

1. Self-correction without external ground-truth is an echo chamber
   (Huang ICLR 2024; CorrectBench NeurIPS 2025).
2. LLM-as-judge ≈ random on hard tasks (JudgeBench ICLR 2025).
3. Multi-agent debate ceiling = best single agent; adversarial agent
   drops 10-40% (arxiv 2505.22960).

XMclaw's Sprint 3 plan accepts all three and designs around them.

## Three Iron Rules for Sprint 3 onward

1. **Two independent signals minimum** for any promotion (e.g. tool
   ran + user reaction; or grader + holdout test). Never single
   LLM-judge.
2. **Staging → gate → explicit promote**. The orchestrator never
   mutates `SkillRegistry` HEAD inline. Always: candidate dir →
   4 gates → explicit `promote()` call (auto-policy or human).
3. **Per-model capability profile**. Strong models get self-extension
   prompts; weak models get template-fill. Don't silently downgrade
   the loop because the user picked GPT-5 instead of Claude.

## How to opt in (for early testers)

```jsonc
// daemon/config.json
{
  "evolution": {
    "enabled": true,        // off by default; flip to enable observers
    "auto_apply": false     // STAY false — human approves promotions
  }
}
```

Then watch:
- `xmclaw evolve review` — what's pending
- `~/.xmclaw/v2/journal/<YYYY-MM>/` — what was logged this session
- `~/.xmclaw/v2/logs/xmclaw.log | grep evolution` — what fired

Report back with concrete examples of (a) skills that were drafted
that you'd want, (b) skills that were drafted that are noise, (c)
promotions you'd reject. That feedback IS the dataset Sprint 3/4
needs.

## Updates

- **2026-05-09** — initial honest-state document. Default flipped
  to `enabled=false` in `config.example.json`. README softened from
  "self-evolves its skills" to "experimentally drafts skills, human-
  gated until benchmark numbers". Sprint 3 rebuild plan committed.
