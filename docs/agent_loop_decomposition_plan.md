# agent_loop.py / hop_loop.py decomposition plan

**Status:** proposal, 2026-05-26 audit G1.
**Files in scope:** `xmclaw/daemon/agent_loop.py` (2358 lines), `xmclaw/daemon/hop_loop.py` (1533 lines), `xmclaw/daemon/turn_types.py`, `xmclaw/daemon/history_compression.py`.

## Why decompose

Every batch of bug fixes in the past month has had to thread changes through one of:

- a 700-line `_run_turn_inner`
- a 600-line hop body
- helpers scattered across `agent_loop` / `hop_loop` / `history_compression` with no clear boundary

Concrete impact:
- 2026-05-25 image-upload fix: had to add user-image plumbing across `app.py` → `orchestrator.handle` → `run_turn` → `_run_turn_inner` → `_invoke_extractor` → Message construction. One missing edit at any layer = silent drop.
- 2026-05-26 toxic-fact filter: required edits in `_run_turn_inner` near line 1745 (50 lines down from the system-prompt assembly) for read-time injection of the autobio block, separately from the write-time hook at `post_sampling_hooks.py:176`. The two ends of the same feature live 600 lines apart in different files.
- correction_detector hook landed at line 2014 of agent_loop.py just to splice one line of text into the user message; the closest sibling (`memory_ctx_block`) is at line 2022 and any new such hook will repeat the pattern.

Trail of evidence: agent_loop has 14 distinct `try / except Exception: # noqa: BLE001` blocks in `_run_turn_inner` alone. Each is a candidate for `xmclaw.utils.swallowed_exceptions.record` (audit A3), but auditing them by hand in a 700-line function is the kind of work that explains why nobody has done it.

## What NOT to do

A clean-slate rewrite. The hop loop's state (cancel_event, stuck-loop deque, no-progress counter, goal anchor, narration silent-hops counter, B-227 retry, B-230 max_tokens continue, B-397 anti-loop guard, B-Vision attachments, speculation cache, post-sampling hook context) is genuinely interrelated. A big-bang rewrite would lose a handful of edge cases that took years to find.

## What TO do — phased extraction

Each phase is independent, testable, mergeable on its own.

### Phase 1 — extract `turn_setup.py` (~600 lines moved)

Everything from `_run_turn_inner` line 750 (signature) through the end of the system-prompt assembly + history-compression preroll + memory recall + plan-first decomposition + correction_detector splice. Produces a `TurnSetupResult` dataclass:

```python
@dataclass
class TurnSetupResult:
    messages: list[Message]
    system_content: str
    user_correlation_id: str | None
    correction_hint: str
    prep_timings: dict[str, float]
    plan_steps: list[str] | None
    run_mode: str | None
```

`_run_turn_inner` becomes ~80 lines: call `prepare_turn(...)` → call `hop_loop._run_hop_loop(...)` → finalize.

Risk: medium. Tests for the existing path are not great (most coverage is integration-level). Mitigation: extract behind a façade, keep `_run_turn_inner` calling the new function with the SAME signature initially; only inline the result construction once the new path is green.

### Phase 2 — extract `narration_enforcer.py` (~100 lines)

The silent-hops counter + nudge injection added in audit batch 1. Owns:
- `_silent_hops` counter
- soft / hard thresholds
- nudge text
- `INNER_MONOLOGUE` publish

Drop-in: hop_loop calls `narration_enforcer.observe_hop(response, messages)` once per hop and gets back a `(messages, should_publish_progress)` tuple.

Risk: low. Self-contained.

### Phase 3 — extract `anti_loop_guard.py` (~150 lines)

The stuck-loop deque + no-progress counter + B-397 anti-loop guard. Same shape as Phase 2 — observer that hop_loop calls once per hop and gets back a `LoopVerdict`.

Risk: low.

### Phase 4 — extract `vision_attachment_aggregator.py` (~80 lines)

The `_vision_attachments` collection + post-batch synthetic-user-message injection (B-Vision). Easy lift; isolated.

Risk: low.

### Phase 5 — extract `goal_anchor_injector.py` (~120 lines)

Already mostly lives in `xmclaw/cognition/goal_anchor.py`. The hop-loop side just decides when to inject. Move the "should this hop inject?" + "build the anchor message" logic out of `hop_loop._run_hop_loop` into a small helper.

Risk: low.

### Phase 6 — final pass: agent_loop is just the orchestrator

After phases 1-5, `agent_loop.py` is:
- `__init__`
- `register_session` / `cancel_session`
- `run_turn` (~50 lines): call turn_setup → call hop_loop → finalize
- a small handful of helpers that genuinely span turns (history mirror, cost tracker glue)

Expected line count: ~700 from 2358.

`hop_loop.py` shrinks similarly via the observer pattern from phases 2-5; expected ~600 from 1533.

## Acceptance criteria

- Every phase keeps `tests/unit/test_v2_agent_loop.py` + `tests/integration/test_v2_tool_loop.py` green.
- Each phase commits separately. No batched merge — if phase 3 breaks, only phase 3 reverts.
- Each extracted module gets its own focused unit tests (~10-15 each). Total new test count: ~70.

## When to do this

Not now. The audit batches 1-3 just landed; the codebase is in a known-good state. Wait for the next "I keep having to thread this through 3 files" pain signal before pulling the trigger — that signal is the forcing function the proposal exists to serve.

Estimated total effort: 4-6 working days across phases.

## Open questions

- Phase 1 boundary: where exactly does turn_setup end and hop_loop begin? Some helpers (skill prefilter, browse hint) live in the gray zone. Probably they belong to turn_setup; revisit at extraction time.
- Should the post-turn `_persist_history` / journal flush stay in agent_loop or move into a `turn_teardown` helper? Phase 1 leaves it; phase 6 may revisit.

## Conclusion

Do it as 5 small extractions, not one rewrite. Wait for the next visible pain. Document the plan now so the next person who hits the wall doesn't re-derive the analysis.
