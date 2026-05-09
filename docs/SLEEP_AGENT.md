# Sleep-Time Agent — Sprint 3 #3 (Letta pattern)

> Status: implemented 2026-05-09. See
> [`docs/EVOLUTION_HONEST_STATE.md`](EVOLUTION_HONEST_STATE.md) for
> the broader Sprint 3 honest-disclosure context (default
> `evolution.enabled=false`; benchmarks deferred to Sprint 4).

## Problem

Pre-Sprint 3, every "dream / evolution" task in XMclaw fired on a
fixed cron interval — 30 minutes for `SkillDreamCycle`, 1 hour for
`MemorySweepTask`, daily for the `DreamCompactor`. The interval
policy was independent of foreground activity. Two failure modes
followed:

1. **WAL contention.** A heavy `dream_compactor` LLM round-trip and
   the user's active turn raced on the same SQLite WAL.
   `MemorySweepTask` ran `prune` + `evict` while the agent was reading
   `memory_search` results. Latency users felt as "the model is slow"
   was actually "the daemon is fighting itself for the memory store".
2. **Half-compacted state.** A SIGINT during a dream-cycle's mid-run
   compaction left memory in a partially-rewritten state — some
   layers compacted, others not. Recovery required deleting
   `memory.db`.

Letta's research (TerminalBench 2.0 +36.8% relative) showed splitting
foreground / sleep-time agents — only the sleep agent writes memory,
only the foreground reads — both improves user-felt latency AND
raises evolution quality. Sprint 3 #3 ports that pattern.

## Architecture

```
┌─────────────────────┐  poll every 30s   ┌─────────────────┐
│ IdleDetector        │ ─────────────────▶│ SleepWorker     │
│   Windows / macOS / │                    │  - 2 thresholds │
│   Linux / fallback  │                    │  - registry     │
└─────────────────────┘                    │  - lifecycle    │
                                            └────────┬────────┘
                                                     │
                                                     │  fire on idle ≥ threshold
                                                     ▼
                                            ┌─────────────────┐
                                            │ Registered task │
                                            │  + SleepWorkspace│
                                            │    (read or RW)  │
                                            └────────┬────────┘
                                                     │
                                            success: │ apply()
                                            cancel:  │ rollback() + SLEEP_INTERRUPTED
                                                     ▼
                                            ┌─────────────────┐
                                            │ Bus events:     │
                                            │  SLEEP_TASK_*   │
                                            │  SLEEP_INTERRUPT│
                                            │  SLEEP_IDLE_*   │
                                            └─────────────────┘
```

### IdleDetector (`xmclaw/daemon/_idle_detector.py`)

Cross-platform OS idle interface. Each impl returns idle seconds as a
`float`; negative = "unmeasurable" sentinel.

| Platform | Source | Notes |
|---|---|---|
| Windows | `GetLastInputInfo` via stdlib ctypes | Wraps the 32-bit tick counter cleanly. No new pip dep. |
| macOS | `IOHIDIdleTime` via `objc.loadBundle` | **Soft-dep** on `pyobjc`. When pyobjc is missing → fallback (logged). Install with `pip install 'xmclaw[idle-macos]'`. |
| Linux | `xprintidle` then `loginctl IdleHint` | First match wins. Either tool absent → fallback. |
| Anything else | `_AlwaysIdleDetector` | Reports `86400.0` on every poll, so behaviour is **identical to today's cron firing** — every tick crosses both thresholds. Logged at boot so the user knows. |

The fallback is the safety net: a daemon on an unsupported platform
keeps working exactly as it did before this feature landed.

### SleepWorker (`xmclaw/daemon/sleep_worker.py`)

Async polling loop. Two thresholds (Letta's spacing):

| Level | Default | Purpose |
|---|---|---|
| `idle_short_s` | 300s (5 min) | Light tasks: memory dedup, journal-summary, recent-trace strategy distillation. |
| `idle_long_s` | 1800s (30 min) | Heavy tasks: skill mutation evaluation, cross-skill EWMA recompute, full memory.md compact. |

Each tick (every `poll_interval_s`, default 30s):

1. Read `detector.idle_seconds()`.
2. For each level (`short` first, then `long`): if observed idle ≥
   threshold AND the level is "armed", fire every registered task at
   that level in registration order, then disarm.
3. A level re-arms only when observed idle drops back below its
   threshold — i.e. each crossing fires **once**, not repeatedly.
4. While a task is running, the loop keeps polling. If the user
   resumes (idle drops below the threshold), the in-flight task is
   cancelled at its next `await` checkpoint and any buffered writes
   are rolled back.

### SleepWorkspace — permission separation

The actual Letta value-add. Sleep tasks get a `SleepWorkspace`
handle whose semantics depend on registration:

- **`writable=False` (default)** — read-only. `buffer_set()` is a
  silent no-op. `apply()` is a no-op. Use this for purely
  observational tasks (e.g. "summarise the last 10 turns into a
  Journal row" — the writer is JournalWriter, the sleep task only
  triggers it).
- **`writable=True`** — buffered writes. The task calls
  `buffer_set(key, value)` and registers `apply(callback)` callbacks.
  Buffered writes are applied **atomically** at the end of a
  successful run; if the task raises or the worker cancels mid-run,
  the buffer is discarded and the foreground sees the pre-task
  state. SLEEP_INTERRUPTED includes whatever `checkpoint(**kwargs)`
  the task last set so reviewers can see "I got 33% of the way
  through" instead of an empty payload.

This stops the failure mode where a dream-cycle's mid-run crash
leaves memory in a half-compacted state.

### Bus events

Four new event types (defined in `xmclaw/core/bus/events.py`):

| Type | When | Payload |
|---|---|---|
| `SLEEP_IDLE_DETECTED` | Threshold crossing (once per crossing) | `{"level", "idle_seconds"}` |
| `SLEEP_TASK_STARTED` | Right before the registered fn runs | `{"task_name", "level"}` |
| `SLEEP_TASK_FINISHED` | After the fn returns or raises | `{"task_name", "level", "ok", "duration_ms", "result"}` |
| `SLEEP_INTERRUPTED` | User resumes mid-run; cancel-with-rollback | `{"task_name", "level", "partial_progress"}` |

All emit on `session_id="_system"`, `agent_id="sleep-worker"` so they
don't pollute user session transcripts. The Trace page filters them
into a "Background work" tab.

## Configuration

Daemon config (`daemon/config.json`):

```jsonc
{
  "evolution": {
    "scheduler": {
      "idle_aware": true,        // master switch — false reverts to cron-only
      "idle_short_s": 300,
      "idle_long_s": 1800,
      "poll_interval_s": 30
    }
  }
}
```

`idle_aware=false` reverts to the legacy cron-only behaviour (each
task runs on its own interval, regardless of idle state). The
default is `true` because the fallback `_AlwaysIdleDetector` already
makes the worst-case behaviour identical to cron.

## Migration

Existing periodic tasks register as sleep tasks via two helpers in
`xmclaw.daemon.sleep_worker`:

```python
from xmclaw.daemon.sleep_worker import (
    SleepWorker,
    build_idle_detector,
    make_dream_cycle_task,
    make_memory_sweep_task,
)

worker = SleepWorker(build_idle_detector(), bus,
                     idle_short_s=300, idle_long_s=1800)

# Existing SkillDreamCycle / MemorySweepTask keep their cron loops;
# SleepWorker layers an idle-aware trigger on top, whichever crosses
# first runs the cycle.
worker.register_task("skill_dream_cycle", "long",
                      make_dream_cycle_task(skill_dream))
worker.register_task("memory_sweep", "short",
                      make_memory_sweep_task(memory_sweep_task))

await worker.start()
```

This wiring already lives in
[`xmclaw/daemon/app.py`](../xmclaw/daemon/app.py) lifespan; the
config flag `evolution.scheduler.idle_aware` gates it.

## How to register a new sleep task

Three steps:

1. **Write an async fn** that takes a `SleepWorkspace` and returns
   `dict[str, Any]` (the result lands in `SLEEP_TASK_FINISHED`'s
   payload).

   ```python
   async def my_task(ws: SleepWorkspace) -> dict[str, Any]:
       # Pure observation: just read.
       count = await some_observation()
       return {"count": count}

   # Or with buffered writes:
   async def my_writer(ws: SleepWorkspace) -> dict[str, Any]:
       ws.register_apply(my_atomic_disk_writer)
       ws.buffer_set("draft", new_draft)
       ws.checkpoint(phase="drafted", percent=50)
       # ... more work ...
       ws.checkpoint(phase="reviewed", percent=100)
       return {"ok": True}
   ```

2. **Register against the worker** in the daemon lifespan:

   ```python
   worker.register_task("my_task", "short", my_task)
   # Or for a writable task:
   worker.register_task("my_writer", "long", my_writer, writable=True)
   ```

3. **(Optional) subscribe to the bus events** in the UI / observers
   if you want richer reporting. The Trace page's Background-work
   tab already routes `SLEEP_*` events.

### Pick a level

- **short** if the task is small (≤ 1s typical, no LLM round-trip):
  memory dedup, TTL prune, log-row aggregation, USER.md delta flush.
- **long** if the task involves an LLM call, full-table scan, or
  cross-skill recompute: skill proposal, mutation evaluation, full
  memory.md compaction.

If unsure, start at `short` and escalate after observing wallclock.

## Iron Rules alignment

From `docs/EVOLUTION_HONEST_STATE.md` §"Three Iron Rules":

> 1. **Two independent signals minimum** for any promotion.
> 2. **Staging → gate → explicit promote.** The orchestrator never
>    mutates `SkillRegistry` HEAD inline.
> 3. **Per-model capability profile.**

Sleep-time agent helps Iron Rule #2 directly: every memory write
(and every future skill mutation) goes through the buffered
`SleepWorkspace.apply()` path, so a half-run task never half-applies.
Sleep-time agent helps Iron Rule #1 indirectly: by separating the
write phase from the user's foreground reads, the grader signal
(foreground) and the sleep task's mutation signal stay independent
in time.

## Testing

`tests/unit/test_v2_sleep_worker.py` (38+ tests) covers:

- Each platform's IdleDetector with the OS calls mocked.
- `_AlwaysIdleDetector` fallback returns the sentinel on every poll.
- `parse_sleep_config` per-field bad-value fallback (never raises).
- Threshold-crossing fires the level's bus event exactly once per
  crossing.
- Multiple registered tasks at same level run in registration order.
- Long-level task isn't fired before short-level at the same
  crossing.
- `SleepWorkspace` buffered writes apply on success and discard on
  cancel.
- `stop()` cancels in-flight task with rollback (SLEEP_INTERRUPTED
  fires with `partial_progress` from the task's last `checkpoint()`).

## Limits / known unknowns

- **Pyobjc soft-dep**: macOS users without `pip install 'xmclaw[idle-macos]'`
  fall through to the always-idle path. Idle-aware behaviour
  degrades to cron-equivalent there. Documented at boot via a one-
  line WARN log.
- **Headless Linux / containers** without `xprintidle` and without
  systemd-logind will also fall through. Same WARN.
- **Sleep tasks that bypass `SleepWorkspace`** (e.g. directly
  mutating disk in the task body) bypass the rollback guarantee.
  This is intentional — most legacy paths still rely on the cron
  cycle's own atomicity (e.g. `MemorySweepTask` uses SQLite
  transactions internally). New tasks SHOULD route writes through
  the workspace; legacy tasks keep their existing safety story.
- **Cron triggers still fire**. The migration is additive: the
  cron loop in each existing task keeps running. SleepWorker layers
  a second trigger on top. If both fire near-simultaneously the
  `is_running()` guard inside each existing task is what serializes
  them; sleep tasks themselves run sequentially within one
  SleepWorker tick.

## File layout

```
xmclaw/daemon/sleep_worker.py     — SleepWorker + SleepWorkspace + config + helpers
xmclaw/daemon/_idle_detector.py   — IdleDetector ABC + 3 native impls + fallback
xmclaw/core/bus/events.py         — SLEEP_* event types
xmclaw/daemon/app.py              — lifespan wiring (gated by idle_aware)
daemon/config.example.json        — evolution.scheduler block
tests/unit/test_v2_sleep_worker.py — full coverage
docs/SLEEP_AGENT.md               — this doc
```

## References

- Letta TerminalBench 2.0 results — original foreground/sleep agent
  paper.
- EvoMap `idleScheduler.js` — MIT-licensed JS reference for the
  cross-platform idle detection we ported here.
- `docs/EVOLUTION_HONEST_STATE.md` — Sprint 3 honest-state including
  the Iron Rules.
