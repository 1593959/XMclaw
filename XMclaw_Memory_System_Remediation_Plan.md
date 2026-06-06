# XMclaw Memory System Remediation Plan

> **Version**: 1.0  
> **Date**: 2026-06-06  
> **Author**: Senior Software Architect — AI Agent Memory Systems  
> **Sources**: Internal Audit (`XMclaw_记忆系统调研报告_2026-06-06.md`) + External Research (`主流 AI Agent 记忆系统：实现架构、应用场景与前沿趋势调研报告`)

---

## Executive Summary

This plan remediates **10 identified issues (M-1..M-10)** in XMclaw's V2 memory system, mapping each to industry best practices from the external research report. The fixes are organized into **4 waves** of increasing scope and architectural depth, from critical hot-fixes to advanced infrastructure features.

**Key external research insights driving this plan:**

| Research Insight | Justifies Fix For |
|-----------------|-------------------|
| Hybrid retrieval (Vector+BM25+Graph) is table stakes; pure vector Recall@1 < 60% [^madial] | M-2 (enable hybrid by default) |
| RRF (k=60) is the fusion standard; weighted fusion is fragile [^rrf] | M-2 (fusion strategy) |
| Temporal reasoning is the differentiator; bi-temporal modeling enables point-in-time queries [^zep-paper] | M-4 (task continuity), Wave 4 (bi-temporal engine) |
| Security boundary underestimated: Sleeper Memory Poisoning 99.8% write rate; RPR 160-194x leakage amplification [^sleeper][^rpr] | Wave 3 (provenance, per-hop auth, input sanitization) |
| Evaluation expanded to 5D: accuracy + hallucination + latency + cost + safety [^halumem] | All waves (acceptance criteria) |
| CJK is a structural deficit: standard BM25 fails on CJK; need jieba/bigram/PGroonga fixes [^cjk-bm25-fail] | M-2, M-7 (CJK tokenization) |
| Provenance is essential: every fact needs source tracing for audit and defense [^provenance_b] | M-3 (provenance field) |
| Incremental curation: full-scan O(N²) dedup doesn't scale; need watermark-based incremental scanning [^wgrow] | M-5 (curator cost), M-9 (scan limit) |
| Per-hop authorization: graph traversal must re-check permissions at each hop [^rpr_b] | Wave 3 (RPR defense) |

---

## Wave 1: Critical Fixes

> **Theme**: Stop user-facing pain immediately. Zero schema migrations. Config-only + code tuning.
> **ETA**: 1-2 days  
> **Issues**: M-1, M-10, M-3 (partial — schema prep)

---

### W1-1: M-1 — Recall Timeout 1.0s → 3.0s (with metrics)

**External research justification**: 
- Mem0 achieves p50 0.148s / p95 0.200s search latency [^mem0-paper_b]; XMclaw's 1.0s cap is below the p95 of healthy systems, causing false-positive timeouts on large stores.
- LongMemEval shows latency is a first-class dimension; premature timeout creates "memory suddenly fails" UX [^1_e].

**Root cause**: `xmclaw/daemon/auto_recall.py:83` hard-codes `_DEFAULT_TIMEOUT_S = 1.0` as an emergency cap after incident `chat-b09a3ad4`. The cap protects against the hybrid-BM25 stall but also kills legitimate large-store recalls.

**Implementation**:

```python
# xmclaw/daemon/auto_recall.py
# BEFORE:
_DEFAULT_TIMEOUT_S = 1.0

# AFTER:
_DEFAULT_TIMEOUT_S = 3.0   # Wave-1 fix: 1.0s → 3.0s
# Sub-budgets: embedding ≤ 2.0s, search ≤ 2.0s, total ≤ 3.0s
```

Add metrics emission:

```python
# xmclaw/daemon/auto_recall.py:186-195
except _asyncio.TimeoutError:
    try:
        from xmclaw.utils.log import get_logger
        from xmclaw.core.bus.events import emit_event  # if available
        get_logger(__name__).info(
            "auto_recall.timeout after=%.1fs (turn proceeds without recall)",
            timeout_s,
        )
        # NEW: emit metric for monitoring dashboard
        emit_event("metric", {
            "name": "recall_timeout_count",
            "value": 1,
            "tags": {"timeout_s": str(timeout_s)},
        })
    except Exception:
        pass
    return []
```

**Configuration change**: None required (default change). Operators who need the old behavior can pass `timeout_s=1.0` to `recall_for_message()`.

**Test additions**:
- `tests/test_v3_auto_recall.py`: add `test_recall_timeout_3s_allows_large_store()` — mock a 2.5s recall and assert it returns hits, not `[]`.
- Add `test_recall_timeout_metric_emitted()` — assert `recall_timeout_count` event fires.

**Effort**: S (1-2 hours)

---

### W1-2: M-10 — Context Pollution Threshold 0.40 → Configurable

**External research justification**:
- MADial-Bench shows optimal embedding model Recall@1 < 60% [^madial]; distance thresholds are highly model-dependent and must be tunable per deployment.
- Mem0's token-efficient algorithm uses adaptive thresholds learned from retrieval distribution [^mem0-blog_b]; fixed empirical values decay over time.

**Root cause**: `xmclaw/memory/v2/service.py:2618` hard-codes `distance <= 0.40` as an emergency fix for context pollution ("需要" queries injecting irrelevant neighbors). The value is untested across embedding models and store sizes.

**Implementation**:

1. Add config key to `daemon/config.json` schema:

```python
# xmclaw/daemon/config_schema.py (append to validation rules)
# In the memory section validator:
if not isinstance(cfg.get("memory_recall_distance_threshold"), (int, float)):
    errors.append("memory.memory_recall_distance_threshold must be a float")
elif not (0.0 <= cfg["memory_recall_distance_threshold"] <= 1.0):
    errors.append("memory.memory_recall_distance_threshold must be in [0, 1]")
```

2. Wire config into `MemoryService` construction:

```python
# xmclaw/memory/v2/service.py:2615-2618
# BEFORE:
new_hits = _rank([
    h for h in relevant_hits
    if h.fact.id not in seen_ids
    and float(getattr(h, "distance", 0.0) or 0.0) <= 0.40
])

# AFTER:
_recall_distance_threshold = getattr(
    self, "_recall_distance_threshold", 0.40
)
new_hits = _rank([
    h for h in relevant_hits
    if h.fact.id not in seen_ids
    and float(getattr(h, "distance", 0.0) or 0.0) <= _recall_distance_threshold
])
```

3. Inject from factory:

```python
# xmclaw/daemon/factory.py (where MemoryService is instantiated)
# Add:
memory_service = MemoryService(
    ...,
    recall_distance_threshold=config.get("memory_recall_distance_threshold", 0.40),
)
```

4. Add metrics to track threshold hit rate:

```python
# Inside render_for_prompt, after filtering:
_total = len(relevant_hits)
_filtered = len(new_hits)
if _total:
    _log.debug("render.threshold_hit_rate %.2f (%d/%d)", _filtered/_total, _filtered, _total)
```

**Configuration change**:
```json
{
  "memory": {
    "recall_distance_threshold": 0.40
  }
}
```

**Test additions**:
- `tests/test_v2_memory_v2_service.py`: `test_render_distance_threshold_configurable()` — set threshold to 0.10 vs 0.80 and assert different hit counts.

**Effort**: S (2-3 hours)

---

### W1-3: M-3 (Partial) — Provenance Field Schema Preparation

**External research justification**:
- OpenBrain's four-label provenance system (`observed_from_source`, `inferred_by_model`, `confirmed_by_user`, `imported_from_transcript`) is the industry reference [^17_c].
- MIF (Memory Interchange Format) uses W3C PROV vocabulary with five confidence intervals [^18_b].
- Without provenance, curation cannot prioritize user-confirmed facts, and defense cannot trace Sleeper Memory Poisoning injections [^sleeper].

**Root cause**: `xmclaw/memory/v2/models.py:Fact` lacks a `provenance` field. Only `source_event_id` exists (points to events.db), but it doesn't distinguish write channel (regex vs LLM vs tool vs UI vs persona).

**Implementation (Wave 1: schema only, no write-path changes yet)**:

```python
# xmclaw/memory/v2/models.py:166-212
@dataclass(slots=True)
class Fact:
    # ... existing fields ...
    # Wave-1 schema prep: provenance field (backwards-compatible default)
    provenance: str = "unknown"   # "user_confirmed" | "auto_extract_regex" | "auto_extract_llm" | "tool_invoked" | "manual_ui" | "persona_file" | "unknown"
```

Update `to_dict()` and `from_dict()`:

```python
# to_dict:
"provenance": self.provenance,

# from_dict:
provenance=str(d.get("provenance") or "unknown"),
```

Update LanceDB schema:

```python
# xmclaw/memory/v2/backend_lancedb.py:52-83
class FactRecord(LanceModel):
    # ... existing fields ...
    provenance: str  # "" when absent (but we default to "unknown" in Python layer)
```

**Migration**: See [Migration Plan](#migration-plan) for LanceDB schema migration.

**Test additions**:
- `tests/test_v2_memory_v2_models.py`: `test_fact_provenance_roundtrip()` — serialize/deserialize with provenance set.

**Effort**: S (2-3 hours)

---

## Wave 2: Architecture Improvements

> **Theme**: Fix structural deficits in recall, curation, and task continuity. Requires schema migrations and new modules.
> **ETA**: 1-2 weeks  
> **Issues**: M-2, M-5, M-9, M-4

---

### W2-1: M-2 — Hybrid Recall (BM25) Default Enabled + CJK Bigram Fix

**External research justification**:
- Hindsight's TEMPR pipeline (semantic + BM25 + graph + temporal) achieves 91.4% LongMemEval vs pure-vector 39% [^hindsight-paper_b].
- Standard BM25 fails on CJK: query "机器学习算法" is treated as a single token, BM25 score ≈ 0 [^cjk-bm25-fail_b].
- RRF (k=60) is the fusion standard; weighted 0.6/0.4 fusion is fragile and requires per-domain tuning [^rrf].

**Root cause**: `xmclaw/daemon/auto_recall.py:113` defaults `use_hybrid: bool = False`. The BM25 path (`xmclaw/memory/v2/bm25.py`) already has CJK bigram tokenization, but the hybrid fusion in `MemoryService.recall_hybrid()` uses weighted sum (0.6 vector / 0.4 BM25) rather than RRF.

**Implementation**:

1. Flip default in auto_recall:

```python
# xmclaw/daemon/auto_recall.py:113
# BEFORE:
use_hybrid: bool = False,

# AFTER:
use_hybrid: bool = True,   # Wave-2: default ON when rank_bm25 available
```

2. Add config gating:

```python
# xmclaw/daemon/auto_recall.py:42 area
_DEFAULT_USE_HYBRID = True   # new constant
```

```json
// daemon/config.json
{
  "memory": {
    "auto_recall_hybrid": true
  }
}
```

3. Replace weighted fusion with RRF in `MemoryService.recall_hybrid()`:

```python
# xmclaw/memory/v2/service.py:2039-2180 (recall_hybrid)
# CURRENT (weighted, fragile):
# fused_score = 0.6 * vec_score + 0.4 * bm25_score

# NEW (RRF k=60):
_RRF_K = 60

def _rrf_score(rank_vec: int | None, rank_bm25: int | None) -> float:
    score = 0.0
    if rank_vec is not None:
        score += 1.0 / (_RRF_K + rank_vec)
    if rank_bm25 is not None:
        score += 1.0 / (_RRF_K + rank_bm25)
    return score

# Build rank lookups
vec_ranks = {h.fact.id: i for i, h in enumerate(vec_hits)}
bm25_ranks = {fid: i for i, (fid, _) in enumerate(bm25_results)}

all_ids = set(vec_ranks) | set(bm25_ranks)
fused = sorted(
    all_ids,
    key=lambda fid: _rrf_score(
        vec_ranks.get(fid),
        bm25_ranks.get(fid),
    ),
    reverse=True,
)
```

4. CJK tokenization is already partially implemented in `bm25.py:49-78` (bigrams over Chinese chars). Verify it covers mixed CJK-Latin text. Add jieba as optional enhancement:

```python
# xmclaw/memory/v2/bm25.py:34-80
# Add optional jieba path:
def tokenize_for_bm25(text: str) -> list[str]:
    if not text:
        return []
    tokens: list[str] = []
    # 1) Latin words / digits (existing)
    ...
    # 2) CJK: try jieba first, fallback to bigram
    try:
        import jieba
        jieba_tokens = list(jieba.cut(text))
        for t in jieba_tokens:
            t = t.strip()
            if not t:
                continue
            if _is_chinese_char(t[0]):
                tokens.append(t)
            elif t.isalnum():
                tokens.append(t.lower())
    except ImportError:
        # fallback: char unigrams + bigrams (existing)
        chinese_chars = [c for c in text if _is_chinese_char(c)]
        tokens.extend(chinese_chars)
        for i in range(len(chinese_chars) - 1):
            tokens.append(chinese_chars[i] + chinese_chars[i + 1])
    return tokens
```

Add `jieba` to optional deps:
```
# pyproject.toml [memory-full] extra
"jieba>=0.42; platform_system!='Emscripten'",
```

**Test additions**:
- `tests/test_v3_recall_hybrid.py`: `test_rrf_fusion_order()` — mock vec=[A,B,C], bm25=[B,A,D] and assert RRF order is B>A>C>D.
- `test_cjk_bigram_tokenization()` — assert "机器学习" produces ["机", "器", "学", "机器", "器学"].
- `test_cjk_jieba_tokenization()` — if jieba installed, assert "机器学习算法" produces multi-char tokens.

**Effort**: M (1-2 days)

---

### W2-2: M-5 — Curator Incremental Scan (Watermark-Based)

**External research justification**:
- Mem0's 2026-04 algorithm shifted from full-scan AUDN to Single-pass ADD-only, eliminating O(N²) curation [^mem0-blog_b].
- Incremental materialized view maintenance (IVM) principle: record last scan watermark, only process new/changed rows [^narrative-ivm].
- TeleMem uses semantic clustering for batch dedup, but real-time systems need incremental [^telemem_b].

**Root cause**: `xmclaw/memory/v2/curator.py:219-256` runs `contradict` and `crystallize` as full-scope scans on every curation pass, regardless of whether facts have changed since last run. On 500 facts, crystallize needs ~8 LLM calls per pass.

**Implementation**:

1. Add watermark persistence to `CurationReport` and `MemoryCurator`:

```python
# xmclaw/memory/v2/curator.py:135-148
class MemoryCurator:
    def __init__(self, service: Any, *, llm: Any | None = None) -> None:
        self._svc = service
        self._llm = llm or getattr(service, "_llm", None)
        # NEW: incremental watermark (ts_last of newest fact processed)
        self._last_curate_ts: float = 0.0
        self._load_watermark()

    def _load_watermark(self) -> None:
        """Load last_curate_ts from disk (survives daemon restarts)."""
        import json, os
        from xmclaw.utils.paths import get_data_dir
        path = os.path.join(get_data_dir(), "memory_curator_watermark.json")
        if os.path.exists(path):
            try:
                with open(path) as f:
                    data = json.load(f)
                    self._last_curate_ts = float(data.get("last_curate_ts", 0.0))
            except Exception:
                self._last_curate_ts = 0.0

    def _save_watermark(self, ts: float) -> None:
        import json, os
        from xmclaw.utils.paths import get_data_dir
        path = os.path.join(get_data_dir(), "memory_curator_watermark.json")
        with open(path, "w") as f:
            json.dump({"last_curate_ts": ts}, f)
```

2. Modify `curate()` to skip LLM phases when change volume is low:

```python
# xmclaw/memory/v2/curator.py:149-269
async def curate(self, ..., min_changes_for_llm: int = 10) -> CurationReport:
    # ... existing setup ...
    
    # NEW: count changed facts since watermark
    changed_facts = await self._count_changed_since(self._last_curate_ts)
    _log.info("curator.incremental changed_facts=%d watermark=%.0f", changed_facts, self._last_curate_ts)
    
    # Pass 3 & 4: skip if insufficient changes
    if do_contradict and changed_facts < min_changes_for_llm:
        report.passes_skipped.append("contradict")
        report.passes_skipped.append("crystallize")
        _log.info("curator.skip_llm_passes insufficient_changes=%d (< %d)", changed_facts, min_changes_for_llm)
    else:
        # run contradict + crystallize as before
        ...
    
    # After successful curation, bump watermark to now
    if not dry_run:
        self._save_watermark(time.time())
    return report
```

3. Add `_count_changed_since()` helper:

```python
# xmclaw/memory/v2/curator.py
async def _count_changed_since(self, watermark: float) -> int:
    """Count facts with ts_last > watermark."""
    # Use VectorBackend search with where clause if supported, else scan
    try:
        all_recent = await self._svc.recall(
            None, k=_MAINTENANCE_SCAN_LIMIT,
            min_confidence=0.0, include_relations=False,
        )
        return sum(1 for h in all_recent if h.fact.ts_last > watermark)
    except Exception:
        return _MAINTENANCE_SCAN_LIMIT  # conservative: assume many changes
```

**Configuration change**:
```json
{
  "memory": {
    "curator_min_changes_for_llm": 10,
    "curator_time_budget_s": 20.0
  }
}
```

**Test additions**:
- `tests/test_v2_memory_curator.py`: `test_curator_skips_llm_when_no_changes()` — mock 0 changed facts, assert contradict/crystallize skipped.
- `test_curator_watermark_persistence()` — run curate, restart curator, assert watermark loaded.

**Effort**: M (2-3 days)

---

### W2-3: M-9 — Maintenance Scan Cursor-Based Pagination

**External research justification**:
- XMclaw's `_MAINTENANCE_SCAN_LIMIT = 5000` means stores > 5000 facts silently truncate curation [^wgrow].
- Database IVM principle: cursor-based pagination by `ts_last` or `id` maintains completeness [^narrative-ivm].

**Root cause**: `xmclaw/memory/v2/service.py:79` defines `_MAINTENANCE_SCAN_LIMIT = 5000`. `sweep()`, `dedup`, and backfill paths all use this cap with `_maybe_warn_scan_truncated()` but no automatic pagination.

**Implementation**:

1. Add cursor-based scan helper to `MemoryService`:

```python
# xmclaw/memory/v2/service.py:82-95 area
async def _scan_all(
    self,
    *,
    where: str | None = None,
    order_by: str = "ts_last DESC",
    batch_size: int = _MAINTENANCE_SCAN_LIMIT,
) -> list[Fact]:
    """Cursor-based pagination scan that yields ALL facts, not just first N."""
    all_facts: list[Fact] = []
    last_ts: float | None = None
    while True:
        batch_where = where or ""
        if last_ts is not None:
            ts_clause = f"ts_last < {last_ts}"
            batch_where = f"{batch_where} AND {ts_clause}" if batch_where else ts_clause
        
        batch = await self._vec.search(
            None, where=batch_where, limit=batch_size,
        )
        if not batch:
            break
        all_facts.extend(batch)
        last_ts = min(f.ts_last for f in batch)
        if len(batch) < batch_size:
            break
    return all_facts
```

2. Replace `_MAINTENANCE_SCAN_LIMIT` usage in `sweep()`:

```python
# xmclaw/memory/v2/service.py:2289-2298
# BEFORE:
listing = await self._vec.search(
    None,
    where=f"layer = '{layer}'",
    limit=_MAINTENANCE_SCAN_LIMIT,
)
_maybe_warn_scan_truncated(...)

# AFTER:
listing = await self._scan_all(
    where=f"layer = '{layer}'",
    batch_size=_MAINTENANCE_SCAN_LIMIT,
)
# No truncation warning needed — _scan_all is exhaustive
```

3. Update `MemoryCurator._dedup_scope_budgeted()` to use cursor scan:

```python
# xmclaw/memory/v2/curator.py:273-298
# Replace the single recall(None, k=max_facts) with _scan_all or paginated recall
```

**Note**: `_scan_all` requires `VectorBackend.search()` to support `where` + `limit` + ordering. Verify `backend_lancedb.py` and `backend_inmemory.py` both support this.

**Test additions**:
- `tests/test_v2_memory_v2_service.py`: `test_sweep_no_truncation_at_6000_facts()` — insert 6000 facts, run sweep, assert all considered.
- `tests/test_v2_memory_curator.py`: `test_dedup_6000_facts()` — assert dedup processes all 6000.

**Effort**: M (2-3 days)

---

### W2-4: M-4 — Cross-Session Task Continuity

**External research justification**:
- MemGPT/Letta's Core/Recall/Archival paging enables Agent to retain task state across sessions [^memgpt-paper].
- OpenClaw's `session_store` extends `task_context` field for automatic inheritance [^2_d].
- LangGraph Checkpointer manages single-thread history, but BaseStore manages cross-thread facts [^5_b]; XMclaw conflates the two.

**Root cause**: `xmclaw/daemon/session_store.py` stores `Message` lists but no task state machine. `render_for_prompt` only recalls query-relevant facts, not "ongoing task context".

**Implementation**:

1. Add `TaskContext` model:

```python
# xmclaw/memory/v2/models.py (append)
from dataclasses import dataclass, field
from typing import Any

@dataclass(slots=True)
class TaskContext:
    """Structured cross-session task state."""
    task_id: str
    goal: str
    completed_steps: list[str] = field(default_factory=list)
    pending_steps: list[str] = field(default_factory=list)
    last_session_id: str = ""
    ts_created: float = field(default_factory=time.time)
    ts_updated: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "goal": self.goal,
            "completed_steps": list(self.completed_steps),
            "pending_steps": list(self.pending_steps),
            "last_session_id": self.last_session_id,
            "ts_created": self.ts_created,
            "ts_updated": self.ts_updated,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TaskContext":
        return cls(
            task_id=str(d["task_id"]),
            goal=str(d["goal"]),
            completed_steps=list(d.get("completed_steps") or []),
            pending_steps=list(d.get("pending_steps") or []),
            last_session_id=str(d.get("last_session_id") or ""),
            ts_created=float(d.get("ts_created", 0.0) or time.time()),
            ts_updated=float(d.get("ts_updated", 0.0) or time.time()),
        )
```

2. Store `TaskContext` as a special `Fact` kind (`kind="project", scope="project", bucket="task_context"`) or add a dedicated table. **Recommended**: reuse Fact store with `kind="task_context"` (new enum value) to avoid new backend complexity.

```python
# xmclaw/memory/v2/models.py:36-85
class FactKind(str, Enum):
    # ... existing ...
    TASK_CONTEXT = "task_context"   # Wave-2: cross-session task continuity
```

3. Add task context injection to `render_for_prompt()`:

```python
# xmclaw/memory/v2/service.py:2483-2654
# In render_for_prompt, after "决定记录" section:
task_t = self.recall(
    None, kinds=["task_context"],
    scopes=["project"], k=3, include_relations=False,
)
# gather with existing 4 recalls
user_facts, project_facts, decision_facts, task_facts, relevant_hits = (
    await _asyncio.gather(user_t, project_t, decision_t, task_t, relevant_t)
)
# Render task context section
if task_facts:
    sections.append("### 进行中任务")
    for h in task_facts:
        f = h.fact
        # Parse task context from fact text (JSON blob)
        try:
            ctx = TaskContext.from_dict(json.loads(f.text))
            sections.append(f"- **{ctx.goal}** 已完成: {len(ctx.completed_steps)} 步, 待办: {len(ctx.pending_steps)} 步")
        except Exception:
            sections.append(f"- {f.text}")
```

4. Add task lifecycle hooks in `hop_loop.py` / `session_store.py`:
- On new session start: recall active `task_context` facts and prepend to system prompt.
- On task completion: mark `task_context` as `invalid_at` (bi-temporal) or delete.

**Test additions**:
- `tests/test_v2_cross_session_memory_e2e.py`: `test_task_context_survives_session_restart()` — create task in session A, open session B, assert task context injected.

**Effort**: L (3-5 days)

---

## Wave 3: Security Hardening

> **Theme**: Close the Sleeper Memory Poisoning and RPR attack surfaces. Complete M-3 provenance wiring.
> **ETA**: 1-2 weeks  
> **Issues**: M-3 (completion), per-hop auth, input sanitization

---

### W3-1: M-3 (Completion) — Wire Provenance Through All 5 Write Paths

**External research justification**:
- W3C PROV + MIF five-level trust model requires every fact to carry source channel [^18_b].
- MemGuard's type-aware isolation requires provenance to assign functional roles at write time [^memguard].

**Implementation**:

Update all 5 write paths to set `provenance`:

| Write Path | File | Provenance Value |
|-----------|------|-----------------|
| ① KeyInfoExtractor regex | `xmclaw/memory/v2/key_info_extractor.py` | `"auto_extract_regex"` |
| ② LLMFactExtractor async | `xmclaw/memory/v2/llm_extractor.py` | `"auto_extract_llm"` |
| ③ Agent tool call | `xmclaw/providers/tool/builtin_memory.py` | `"tool_invoked"` |
| ④ UI manual | `xmclaw/daemon/routers/memory_v2.py` | `"manual_ui"` |
| ⑤ Persona file | `xmclaw/memory/v2/service.py` (upsert_persona_manual) | `"persona_file"` |

Example for KeyInfoExtractor:

```python
# xmclaw/memory/v2/key_info_extractor.py:300-330 (where ExtractedKey → remember)
# In the hook caller:
for key in extracted:
    await memory_service.remember(
        text=key.text,
        kind=key.kind,
        scope=key.scope,
        confidence=key.confidence,
        provenance="auto_extract_regex",   # Wave-3
        source_event_id=event_id,
    )
```

Example for LLMExtractor:

```python
# xmclaw/memory/v2/llm_extractor.py:350-400
# In the extraction loop:
for item in extracted_items:
    await memory_service.remember(
        ...,
        provenance="auto_extract_llm",
    )
```

Update `MemoryService.remember()` signature:

```python
# xmclaw/memory/v2/service.py:860-880
async def remember(
    self,
    text: str,
    kind: FactKind | FactKindStr,
    scope: FactScope | FactScopeStr,
    ...,
    provenance: str = "unknown",   # NEW
) -> dict[str, Any]:
    ...
    fact = Fact(
        id=fid,
        kind=kind_str,
        ...,
        provenance=provenance,
    )
```

**Test additions**:
- `tests/test_v2_memory_v2_service.py`: `test_remember_provenance_preserved()` — remember with each provenance, recall, assert provenance intact.

**Effort**: M (2-3 days)

---

### W3-2: Per-Hop Authorization in Graph Traversal (RPR Defense)

**External research justification**:
- RPR (Retrieval Pivot Risk) in hybrid RAG produces 160-194x leakage amplification because vector-stage permissions don't propagate to graph traversal [^rpr_b].
- Per-hop authorization eliminates all measured leakage (RPR→0.0) with <1ms overhead [^rpr_b].

**Root cause**: `xmclaw/cognition/memory_graph.py` and `xmclaw/memory/v2/backend_lancedb.py:642-684` (`neighbors()`) traverse graph edges without re-checking access control at each hop.

**Implementation**:

1. Add `access_tags` field to `Fact` (schema prep in Wave 1, wiring here):

```python
# xmclaw/memory/v2/models.py
@dataclass(slots=True)
class Fact:
    # ... existing ...
    access_tags: tuple[str, ...] = ()   # e.g., ("user:alice", "tenant:acme")
```

2. Implement per-hop auth in `GraphBackend.neighbors()`:

```python
# xmclaw/memory/v2/backend_lancedb.py:642-684
async def neighbors(
    self,
    fact_id: str,
    ...,
    source_access_tags: tuple[str, ...] = (),   # NEW
) -> list[tuple[Relation, str]]:
    ...
    # After fetching target fact, check access
    for rel in relations:
        target = await self.get(rel.target_fact_id)
        if target is None:
            continue
        # Per-hop authorization: target must share at least one access tag with source
        if source_access_tags and not any(tag in target.access_tags for tag in source_access_tags):
            _log.warning("graph.perhop_auth_denied target=%s source_tags=%s", rel.target_fact_id, source_access_tags)
            continue
        out.append((rel, rel.target_fact_id))
    return out
```

3. Wire `source_access_tags` through `MemoryService.recall()`:

```python
# xmclaw/memory/v2/service.py:1900-2030 (recall)
# When enriching with neighbors, pass the source fact's access_tags:
for h in hits:
    related = await self._graph.neighbors(
        h.fact.id,
        source_access_tags=h.fact.access_tags,
    )
```

**Note**: For single-tenant XMclaw deployments, `access_tags` can default to `("user:{user_id}",)`. The defense becomes critical when multi-tenancy or shared memory pools are introduced.

**Test additions**:
- `tests/test_v2_memory_graph.py`: `test_perhop_auth_blocks_unauthorized_neighbor()` — create fact A (tag "user:alice") linked to fact B (tag "user:bob"); neighbors from A must exclude B.

**Effort**: M (2-3 days)

---

### W3-3: Input Sanitization Layer (Memory Poisoning Defense)

**External research justification**:
- Sleeper Memory Poisoning achieves 99.8% write rate on GPT-5.5 by injecting seemingly benign facts that activate later [^sleeper_b].
- MemGuard's type isolation + input sanitization reduces memory reliability risk by 28.27% [^memguard].
- Enterprise security requires "cognitive architecture protection" at the memory layer [^enterprise-security].

**Implementation**:

1. Add `MemorySanitizer` module:

```python
# xmclaw/memory/v2/sanitizer.py
"""Pre-write input sanitization for memory poisoning defense."""

_SUSPICIOUS_PATTERNS = [
    # Explicit command injection attempts
    r"ignore\s+(previous|above|all)\s+instructions",
    r"system\s*prompt\s*:\s*",
    r"you\s+are\s+now\s+",
    r"new\s+role\s*:\s*",
    # Sleeper-style delayed activation
    r"when\s+user\s+says\s+.*\s+then\s+",
    r"if\s+.*\s+activate\s+",
]

class MemorySanitizer:
    def __init__(self) -> None:
        self._patterns = [re.compile(p, re.IGNORECASE) for p in _SUSPICIOUS_PATTERNS]

    def check(self, text: str, provenance: str) -> tuple[bool, str]:
        """Returns (is_safe, reason). Blocks if suspicious."""
        # High-trust provenances bypass heuristic
        if provenance in ("user_confirmed", "manual_ui", "persona_file"):
            return True, "high_trust"
        for p in self._patterns:
            if p.search(text):
                return False, f"suspicious_pattern:{p.pattern[:30]}"
        return True, "clean"
```

2. Integrate into `MemoryService.remember()`:

```python
# xmclaw/memory/v2/service.py:860-950
async def remember(self, text: str, ..., provenance: str = "unknown") -> dict[str, Any]:
    # NEW: sanitize before embedding
    sanitizer = getattr(self, "_sanitizer", None)
    if sanitizer:
        is_safe, reason = sanitizer.check(text, provenance)
        if not is_safe:
            _log.warning("memory.remember.sanitizer_blocked text=%r reason=%s", text[:80], reason)
            raise MemoryPutError(f"Sanitizer blocked: {reason}")
    # ... rest of remember ...
```

3. Add config:

```json
{
  "memory": {
    "enable_input_sanitizer": true,
    "sanitizer_block_provenances": ["auto_extract_llm", "auto_extract_regex", "tool_invoked"]
  }
}
```

**Test additions**:
- `tests/test_v2_memory_sanitizer.py`: `test_blocks_sleeper_pattern()`, `test_allows_high_trust_provenance()`.

**Effort**: M (2-3 days)

---

## Wave 4: Advanced Features

> **Theme**: Infrastructure-level capabilities for scale, compliance, and differentiation.
> **ETA**: 2-4 weeks  
> **Issues**: M-6, M-7, M-8, bi-temporal query engine

---

### W4-1: M-8 — LanceDB Corruption Auto-Repair (Transient Error Recovery)

**External research justification**:
- Production vector stores must distinguish recoverable errors (disk full, file lock) from permanent corruption [^lancedb].
- Zep's Graphiti handles backend errors with retry + cleanup, not permanent disable [^zep-pricing].

**Root cause**: `xmclaw/memory/v2/backend_lancedb.py:229` sets `self._corrupted = True` on ANY `RuntimeError` containing "lance error". Transient errors (disk full, file lock, antivirus scan) permanently disable V2 until manual `memory.db` deletion.

**Implementation**:

1. Replace permanent `_corrupted` with transient retry + permanent corruption detection:

```python
# xmclaw/memory/v2/backend_lancedb.py:226-230
# BEFORE:
self._corrupted: bool = False

# AFTER:
self._corrupted: bool = False
self._transient_failures: int = 0
_MAX_TRANSIENT_RETRIES = 3
_TRANSIENT_RETRY_DELAY_S = 2.0
```

2. Add error classification:

```python
# xmclaw/memory/v2/backend_lancedb.py (new helper)
def _is_transient_lance_error(exc: RuntimeError) -> bool:
    msg = str(exc).lower()
    transient_signatures = [
        "resource temporarily unavailable",
        "file is locked",
        "no space left",
        "permission denied",
        "device or resource busy",
    ]
    return any(sig in msg for sig in transient_signatures)
```

3. Update all exception handlers (upsert, search, delete, count, get, graph ops):

```python
# Pattern for each method:
except RuntimeError as exc:
    if "lance error" in str(exc).lower():
        if _is_transient_lance_error(exc):
            self._transient_failures += 1
            if self._transient_failures < _MAX_TRANSIENT_RETRIES:
                _log.warning("lancedb.transient_retry attempt=%d/%d err=%s", 
                             self._transient_failures, _MAX_TRANSIENT_RETRIES, exc)
                await asyncio.sleep(_TRANSIENT_RETRY_DELAY_S)
                # retry logic or return empty (caller retries)
                return []  # or re-raise for caller retry
            else:
                _log.error("lancedb.transient_exhausted — marking corrupted")
                self._corrupted = True
        else:
            self._corrupted = True
            _log.error("lancedb.permanent_corruption err=%s", exc)
    raise
```

4. Add auto-repair attempt:

```python
# xmclaw/memory/v2/backend_lancedb.py
async def attempt_repair(self) -> bool:
    """Try lance.dataset.cleanup() or table recovery. Returns success."""
    if not self._corrupted:
        return True
    try:
        import lancedb
        db = await lancedb.connect_async(self._db_path)
        # Attempt cleanup / compaction
        for name in await db.table_names():
            tbl = await db.open_table(name)
            await tbl.cleanup_old_versions()
        self._corrupted = False
        self._transient_failures = 0
        _log.info("lancedb.repair_success")
        return True
    except Exception as exc:
        _log.error("lancedb.repair_failed err=%s", exc)
        return False
```

**Test additions**:
- `tests/test_v2_backend_lancedb.py`: `test_transient_error_retries()`, `test_permanent_error_marks_corrupted()`, `test_attempt_repair()`.

**Effort**: M (2-3 days)

---

### W4-2: M-7 — Native FTS5 / CJK Tokenization (Long-Term)

**External research justification**:
- Hindsight 0.7.0 introduced PGroonga polyglot backend to fix CJK BM25 failure [^pgroonga].
- CJK bigram is the standard Elasticsearch/OpenSearch fix [^cjk-bigram].
- LanceDB has announced native FTS (Phase 5) but timeline uncertain.

**Root cause**: `xmclaw/memory/v2/bm25.py` builds a Python-side BM25 index per query (O(N) corpus scan). No persistent FTS index exists. CJK tokenization is char-level bigram, which is better than whitespace but still suboptimal vs jieba/PGroonga.

**Implementation**:

**Short-term** (Wave 4a): Pre-built BM25 index with background refresh.

```python
# xmclaw/memory/v2/bm25.py
class BM25Index:
    """Wave-4: persistent in-process index with background refresh."""
    def __init__(self, service: Any, refresh_interval_s: float = 300.0):
        self._svc = service
        self._index: Any | None = None
        self._last_refresh: float = 0.0
        self._refresh_interval_s = refresh_interval_s

    async def search(self, query: str, k: int = 10) -> list[tuple[str, float]]:
        if self._index is None or time.time() - self._last_refresh > self._refresh_interval_s:
            await self._rebuild()
        ...

    async def _rebuild(self) -> None:
        all_facts = await self._svc._scan_all(batch_size=5000)
        self._index = _BM25Index(all_facts)
        self._last_refresh = time.time()
```

**Long-term** (Wave 4b): Migrate to LanceDB native FTS when available, or integrate PGroonga via SQLite bridge.

**Configuration**:
```json
{
  "memory": {
    "bm25_refresh_interval_s": 300,
    "cjk_tokenizer": "jieba"   // "jieba" | "bigram"
  }
}
```

**Test additions**:
- `tests/test_v3_recall_hybrid.py`: `test_prebuilt_bm25_faster_than_per_query()`.

**Effort**: L (3-5 days for pre-built index; XL for native FTS migration)

---

### W4-3: M-6 — Cross-Instance Memory Sync

**External research justification**:
- OpenClaw provides `memory-lancedb` cloud sync [^2_f].
- Mem0 Cloud / Zep hosted offer sync as a core value prop [^mem0-pricing][^zep-pricing].
- For self-hosted XMclaw, sync enables multi-device continuity.

**Root cause**: LanceDB is a local file (`~/.local/share/xmclaw/memory/`). No replication or export/import pipeline exists.

**Implementation**:

1. Add sync abstraction:

```python
# xmclaw/memory/v2/sync.py
"""Optional cross-instance sync backend."""
from typing import Protocol

class SyncBackend(Protocol):
    async def push(self, facts: list[Fact], relations: list[Relation]) -> bool: ...
    async def pull(self, since: float) -> tuple[list[Fact], list[Relation]]: ...

class FileExportSync:
    """Simplest sync: export/import JSONL files."""
    def __init__(self, path: str): ...

class S3Sync:
    """S3-compatible sync for cloud backup."""
    def __init__(self, bucket: str, prefix: str): ...
```

2. Add CLI commands:

```bash
xmclaw memory export --format jsonl --output memory_backup_2026-06-06.jsonl
xmclaw memory import --input memory_backup.jsonl --merge-strategy upsert
xmclaw memory sync --backend s3 --bucket my-xmclaw-backups
```

3. Integrate into daemon lifespan:

```python
# xmclaw/daemon/app_lifespan.py
# On graceful shutdown: push delta to sync backend
# On startup: pull delta from sync backend
```

**Effort**: L (4-6 days)

---

### W4-4: Bi-Temporal Query Engine

**External research justification**:
- Zep/Graphiti's four-timestamp model (t_valid, t_invalid, t'_created, t'_expired) enables point-in-time queries [^2_c].
- TSM improves Temporal accuracy from 36.5% → 69.9% by using semantic timeline [^tsm].
- XTDB and BiTRDF provide formal bitemporal foundations [^3_c][^4_c].

**Root cause**: `xmclaw/memory/v2/models.py:210-211` already has `valid_at` / `invalid_at` fields (Phase 8 ⑩, 2026-05-30), but `MemoryService.recall()` does NOT filter by validity window. The fields are write-only.

**Implementation**:

1. Add validity filtering to `recall()`:

```python
# xmclaw/memory/v2/service.py:1900-2030
async def recall(
    self,
    query: str | list[float] | None,
    ...,
    valid_at: float | None = None,   # NEW: point-in-time query
) -> list[RecallHit]:
    ...
    # After fetching hits, filter by validity window
    now = valid_at or time.time()
    hits = [
        h for h in hits
        if (h.fact.valid_at is None or h.fact.valid_at <= now)
        and (h.fact.invalid_at is None or h.fact.invalid_at > now)
    ]
    return hits
```

2. Add temporal query syntax to `render_for_prompt()`:

```python
# Support "what was true on 2025-12-01?" via natural language parsing
# (Future: integrate spaCy temporal parser like TSM)
```

3. Update `remember()` to auto-set `invalid_at` on superseded facts:

```python
# When a contradicting fact is written, mark old fact invalid_at = now
old_fact.invalid_at = time.time()
await self._vec.upsert([old_fact])
```

**Test additions**:
- `tests/test_v2_memory_v2_service.py`: `test_recall_point_in_time()` — insert fact A valid 2025-01→2025-06, fact B valid 2025-06→∞; query at 2025-03 returns A, at 2025-09 returns B.

**Effort**: L (4-6 days)

---

## Testing Strategy

### Unit Tests (per wave)

| Wave | New Test Files | Coverage Target |
|------|---------------|-----------------|
| W1 | `test_v3_auto_recall.py` (timeout + metric), `test_v2_memory_v2_service.py` (threshold config) | 90% of modified lines |
| W2 | `test_v3_recall_hybrid.py` (RRF + CJK), `test_v2_memory_curator.py` (watermark), `test_v2_cross_session_memory_e2e.py` (task context) | 85% of new modules |
| W3 | `test_v2_memory_sanitizer.py`, `test_v2_memory_graph.py` (per-hop auth) | 90% of security paths |
| W4 | `test_v2_backend_lancedb.py` (repair), `test_v2_sync.py` | 80% of new modules |

### Integration Tests

- **Large store simulation**: `tests/perf/test_memory_10k_facts.py` — insert 10,000 facts, assert sweep/curate complete without truncation, recall p95 < 3s.
- **CJK end-to-end**: `tests/e2e/test_memory_cjk_recall.py` — Chinese user messages, assert BM25 contributes non-zero score.
- **Security regression**: `tests/conformance/test_memory_poisoning_defense.py` — attempt sleeper memory injection, assert sanitizer blocks.

### Benchmarks

- Establish internal LongMemEval-S subset (500 questions, Chinese translations) as regression anchor.
- Track 5D metrics: accuracy, hallucination rate (HaluMem-style), latency p50/p95, cost (tokens/query), safety (injection block rate).

---

## Migration Plan

### Schema Migration: Fact.provenance + access_tags

**Step 1**: Add columns to LanceDB schema (Wave 1).

LanceDB supports schema evolution via `add_columns`:

```python
# Migration script: scripts/migrate_memory_v2_add_provenance.py
import asyncio
import lancedb

async def migrate():
    db = await lancedb.connect_async("~/.local/share/xmclaw/memory/")
    table = await db.open_table("facts")
    # Add provenance column with default "unknown"
    await table.add_columns({"provenance": "string"})
    # Add access_tags column with default ""
    await table.add_columns({"access_tags": "string"})
    # Backfill: set provenance="unknown" for existing rows
    # (LanceDB add_columns fills with empty string; update if needed)
    print("Migration complete")

if __name__ == "__main__":
    asyncio.run(migrate())
```

**Step 2**: Backfill provenance from `source_event_id` (best-effort heuristic):
- If `source_event_id` starts with `event:regex_` → `provenance="auto_extract_regex"`
- If `source_event_id` starts with `event:llm_` → `provenance="auto_extract_llm"`
- Else → `provenance="unknown"`

**Step 3**: InMemory backend automatically handles new fields (dataclass defaults). No migration needed for test stores.

**Rollback**: If migration fails, delete `memory.db` and let XMclaw rebuild from events.db (lossy but safe).

### Config Migration

New config keys are optional with safe defaults. Existing `daemon/config.json` files work without modification.

| New Key | Default | Wave |
|---------|---------|------|
| `memory.recall_distance_threshold` | 0.40 | W1 |
| `memory.auto_recall_hybrid` | true | W2 |
| `memory.curator_min_changes_for_llm` | 10 | W2 |
| `memory.enable_input_sanitizer` | true | W3 |
| `memory.bm25_refresh_interval_s` | 300 | W4 |

---

## Acceptance Criteria

### Wave 1: Critical Fixes

- [ ] `recall_for_message()` with 2.5s recall latency returns hits (not `[]`) on stores with 5,000+ facts.
- [ ] `recall_timeout_count` metric is emitted and visible in daemon logs.
- [ ] `memory_recall_distance_threshold` config changes the filtering behavior in `render_for_prompt()`.
- [ ] `Fact.provenance` field exists in schema, round-trips through `to_dict()` / `from_dict()`, and LanceDB stores it.
- [ ] All existing tests pass; no regression in `test_v3_auto_recall.py`.

### Wave 2: Architecture Improvements

- [ ] Hybrid recall is default ON; `use_hybrid=False` can still disable it.
- [ ] RRF fusion produces different (and more stable) ranking than old weighted fusion on test corpus.
- [ ] CJK query "机器学习" produces non-zero BM25 scores.
- [ ] Curator skips `contradict` + `crystallize` when < 10 facts changed since last watermark.
- [ ] Watermark persists across daemon restarts.
- [ ] `sweep()` processes all facts in a 6,000-fact store without truncation warning.
- [ ] Cross-session task context survives session restart and appears in prompt rendering.

### Wave 3: Security Hardening

- [ ] All 5 write paths populate `provenance` with correct value.
- [ ] `test_perhop_auth_blocks_unauthorized_neighbor()` passes.
- [ ] Memory sanitizer blocks at least 3 known sleeper memory patterns in tests.
- [ ] High-trust provenances (`manual_ui`, `persona_file`) bypass sanitizer.
- [ ] No regression in recall accuracy on existing test suite.

### Wave 4: Advanced Features

- [ ] LanceDB transient errors (simulated) retry 3 times before marking corrupted.
- [ ] `attempt_repair()` successfully recovers from a simulated transient error.
- [ ] Pre-built BM25 index refreshes automatically and reduces per-query latency by >50% on 5K-fact store.
- [ ] `memory export` CLI produces valid JSONL with all facts + relations.
- [ ] `memory import` CLI upserts without duplication.
- [ ] Point-in-time recall (`valid_at` filter) returns correct historical fact versions.

---

## Appendix: Issue-to-Research Mapping

| Issue | External Research Chapter | Key Insight |
|-------|--------------------------|-------------|
| M-1 | §4.1.4, §8.2.2 | Mem0 p95 0.200s; 1.0s cap is below healthy p95 |
| M-2 | §4.3.1-4.3.4, §12.1.5 | Hybrid is table stakes; CJK BM25 fails without bigram/jieba; RRF is fusion standard |
| M-3 | §6.3.1, §10.3.2, §12.2.2 | OpenBrain 4-label provenance; W3C PROV; MIF 5-level trust |
| M-4 | §3.3.2, §5.2.2, §9.1.1 | MemGPT paging; TSM semantic timeline; Mem0 cross-session accuracy +26% |
| M-5 | §7.1.2, §7.3.3, §12.1.8 | Incremental IVM; watermark scanning; Mem0 ADD-only reduces curation pressure |
| M-6 | §9.1.2, §11.1.1 | OpenClaw sync; Mem0 Cloud / Zep hosted sync as core value |
| M-7 | §4.3.4, §12.1.5 | jieba/bigram/PGroonga are required for CJK; LanceDB FTS pending |
| M-8 | §4.1.2 | LanceDB error recovery; Zep's resilient backend handling |
| M-9 | §7.3.3, §12.1.8 | Cursor-based pagination; incremental watermark scanning |
| M-10 | §4.1.4, §8.3.1 | Thresholds are model-dependent; must be configurable and monitored |

---

*End of Remediation Plan*
