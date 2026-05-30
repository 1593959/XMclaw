"""MemoryCurator — periodic, holistic memory management.

2026-05-30. User feedback: "我要的机制，是全方位管理记忆，不只是去重"
("I want a mechanism for COMPREHENSIVE memory management, not just
dedup"). And: the existing vector dedup "以前也是这个，但是没用啊"
("it was the same before but never worked").

Two hard truths from the daemon logs that this module fixes:

1. **The background dedup never ran.** It was scheduled "every 24
   sweeps" (≈ once a day at the 1h sweep interval), but the user's
   daemon restarts every ~30 min during development, so the
   sweep-count counter reset before reaching 24 — the tick fired
   ZERO times. Fix: wall-clock scheduling persisted to disk
   (``last_curate_ts``) so progress survives restarts.

2. **Manual dedup timed out.** ``dedup_scope`` does O(N²) Python
   cosine clustering + N graph writes; on a 1760-fact store it
   blew the 180s tool wall-clock and was aborted mid-way, leaving
   nothing merged. Fix: every pass here is **time-budgeted and
   batched** — it makes incremental progress within a deadline and
   returns cleanly, so a big store converges over several runs
   instead of failing all-or-nothing.

What "comprehensive management" means here — the curator runs these
passes within one time budget and returns a single honest report:

  * **dedup** — merge duplicates (vector now; LLM semantic pass is
    layered in via ``MemoryService.llm_dedup_scope`` in a later
    commit).
  * **prune** — downweight low-value facts (old + single-evidence +
    never-reinforced). We don't hard-delete; we floor confidence so
    they drop out of recall but stay recoverable.
  * **contradiction detection** + **crystallization** — layered in
    by the next commit; the report already carries their fields so
    the wire format is stable.

The report is the basis for an HONEST proactive message: the daemon
surfaces "I just tidied memory: merged X, downweighted Y" ONLY after
the work actually completed — never a "我正在处理…" claim with nothing
behind it (the dishonesty the user flagged separately).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from math import sqrt
from typing import Any

from xmclaw.utils.log import get_logger

_log = get_logger(__name__)


# ─── Report ───────────────────────────────────────────────────────


@dataclass
class CurationReport:
    """Outcome of one curate() run. Honest by construction — every
    number reflects work that actually happened (or, in dry_run, what
    WOULD happen). The honest proactive message is built from this."""

    scanned: int = 0
    merged: int = 0
    pruned: int = 0
    contradictions_found: int = 0
    crystallized: int = 0
    passes_run: list[str] = field(default_factory=list)
    passes_skipped: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0
    budget_exhausted: bool = False
    dry_run: bool = True
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def did_anything(self) -> bool:
        return bool(
            self.merged or self.pruned
            or self.contradictions_found or self.crystallized
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "merged": self.merged,
            "pruned": self.pruned,
            "contradictions_found": self.contradictions_found,
            "crystallized": self.crystallized,
            "passes_run": list(self.passes_run),
            "passes_skipped": list(self.passes_skipped),
            "elapsed_s": round(self.elapsed_s, 2),
            "budget_exhausted": self.budget_exhausted,
            "dry_run": self.dry_run,
            "details": self.details,
        }

    def honest_summary_zh(self) -> str:
        """A truthful one-line Chinese summary for the proactive
        message. Returns empty string when nothing happened (so the
        caller can stay silent rather than announce a no-op)."""
        if not self.did_anything:
            return ""
        parts: list[str] = []
        if self.merged:
            parts.append(f"合并 {self.merged} 条重复")
        if self.contradictions_found:
            parts.append(f"标记 {self.contradictions_found} 处矛盾")
        if self.crystallized:
            parts.append(f"结晶 {self.crystallized} 条")
        if self.pruned:
            parts.append(f"降权 {self.pruned} 条低价值")
        verb = "预计可" if self.dry_run else "已"
        return f"刚整理了记忆：{verb}" + "、".join(parts) + "。"


# ─── Curator ──────────────────────────────────────────────────────


# Default cosine for the dedup pass — same 0.86 the legacy path used.
# (The threshold was never the problem; the scheduling was.)
_DEDUP_COSINE = 0.86

# Prune heuristic defaults. A fact is "low value" when it's older than
# ``prune_age_s``, has only its original single evidence vote, and its
# confidence is at or below the speculative floor. We DON'T delete —
# we floor confidence so it stops competing in recall but a later
# correction / re-mention can revive it.
_PRUNE_AGE_S = 60 * 60 * 24 * 30  # 30 days
_PRUNE_MAX_EVIDENCE = 1
_PRUNE_CONF_CEILING = 0.55
_PRUNE_FLOOR = 0.15


class MemoryCurator:
    """Holistic periodic memory maintenance over a MemoryService."""

    def __init__(
        self,
        service: Any,
        *,
        llm: Any | None = None,
    ) -> None:
        self._svc = service
        # LLM is optional; the dedup/prune passes are pure-Python.
        # Contradiction + crystallization passes (next commit) use it.
        self._llm = llm or getattr(service, "_llm", None)

    async def curate(
        self,
        *,
        scopes: list[str] | None = None,
        time_budget_s: float = 20.0,
        dry_run: bool = False,
        do_dedup: bool = True,
        do_prune: bool = True,
        max_facts_per_scope: int = 2000,
    ) -> CurationReport:
        """Run the maintenance passes within ``time_budget_s``.

        Each pass checks the deadline and stops cleanly, so a large
        store converges over multiple runs instead of timing out.
        Returns a :class:`CurationReport`.

        ``scopes`` defaults to ``["user", "project", "session"]``.
        ``dry_run`` previews without writing.
        """
        t0 = time.perf_counter()
        deadline = t0 + max(1.0, float(time_budget_s))
        report = CurationReport(dry_run=dry_run)
        target_scopes = scopes or ["user", "project", "session"]

        def _over_budget() -> bool:
            return time.perf_counter() >= deadline

        # ── Pass 1: dedup (time-budgeted, batched) ────────────────
        if do_dedup:
            report.passes_run.append("dedup")
            for sc in target_scopes:
                if _over_budget():
                    report.budget_exhausted = True
                    break
                try:
                    n_scanned, n_merged = await self._dedup_scope_budgeted(
                        scope=sc, deadline=deadline, dry_run=dry_run,
                        max_facts=max_facts_per_scope,
                    )
                    report.scanned += n_scanned
                    report.merged += n_merged
                except Exception as exc:  # noqa: BLE001
                    _log.warning(
                        "curator.dedup_failed scope=%s err=%s", sc, exc,
                    )
        else:
            report.passes_skipped.append("dedup")

        # ── Pass 2: prune low-value facts ─────────────────────────
        if do_prune and not _over_budget():
            report.passes_run.append("prune")
            for sc in target_scopes:
                if _over_budget():
                    report.budget_exhausted = True
                    break
                try:
                    n_pruned = await self._prune_scope_budgeted(
                        scope=sc, deadline=deadline, dry_run=dry_run,
                        max_facts=max_facts_per_scope,
                    )
                    report.pruned += n_pruned
                except Exception as exc:  # noqa: BLE001
                    _log.warning(
                        "curator.prune_failed scope=%s err=%s", sc, exc,
                    )
        elif not do_prune:
            report.passes_skipped.append("prune")

        report.elapsed_s = time.perf_counter() - t0
        _log.info(
            "curator.done scopes=%s scanned=%d merged=%d pruned=%d "
            "elapsed_s=%.2f budget_exhausted=%s dry_run=%s",
            target_scopes, report.scanned, report.merged,
            report.pruned, report.elapsed_s, report.budget_exhausted,
            dry_run,
        )
        return report

    # ── Pass implementations ──────────────────────────────────────

    async def _dedup_scope_budgeted(
        self,
        *,
        scope: str,
        deadline: float,
        dry_run: bool,
        max_facts: int,
    ) -> tuple[int, int]:
        """Cosine-cluster + supersede within a deadline. Returns
        ``(scanned, merged)``.

        Unlike the legacy ``dedup_scope`` (which built ALL clusters
        then wrote them, blowing the wall-clock on big stores), this
        interleaves clustering with the deadline check and applies
        merges incrementally — so an abort still leaves the merges
        done so far committed. Single-linkage-ish: each fact joins
        the first cluster whose representative it's near."""
        hits = await self._svc.recall(
            None,
            k=max_facts,
            scopes=[scope],
            min_confidence=0.0,
            include_relations=False,
            include_superseded=False,
        )
        if len(hits) <= 1:
            return len(hits), 0

        clusters: list[list[Any]] = []
        for h in hits:
            if time.perf_counter() >= deadline:
                break
            emb = h.fact.embedding
            if not emb:
                clusters.append([h])
                continue
            placed = False
            for cluster in clusters:
                ref = cluster[0].fact.embedding
                if not ref:
                    continue
                dot = sum(a * b for a, b in zip(emb, ref))
                na = sqrt(sum(a * a for a in emb))
                nb = sqrt(sum(b * b for b in ref))
                cos = dot / (na * nb) if (na and nb) else 0.0
                if cos >= _DEDUP_COSINE:
                    cluster.append(h)
                    placed = True
                    break
            if not placed:
                clusters.append([h])

        merged = 0
        for group in clusters:
            if len(group) < 2:
                continue
            if time.perf_counter() >= deadline:
                break
            group_sorted = sorted(
                group,
                key=lambda h: (
                    h.fact.confidence,
                    h.fact.evidence_count,
                    h.fact.ts_last,
                ),
                reverse=True,
            )
            survivor = group_sorted[0]
            for loser in group_sorted[1:]:
                if loser.fact.id == survivor.fact.id:
                    continue
                if dry_run:
                    merged += 1
                    continue
                if time.perf_counter() >= deadline:
                    break
                await self._svc.supersede(
                    old_fact_id=loser.fact.id,
                    new_fact_id=survivor.fact.id,
                )
                merged += 1
        return len(hits), merged

    async def _prune_scope_budgeted(
        self,
        *,
        scope: str,
        deadline: float,
        dry_run: bool,
        max_facts: int,
    ) -> int:
        """Downweight low-value facts: old + single-evidence + already
        low-confidence. Floors confidence to ``_PRUNE_FLOOR`` so they
        drop out of recall without being deleted (a later correction
        / re-mention revives them). Protected kinds (identity /
        persona_manual / commitment) are never pruned."""
        now = time.time()
        cutoff = now - _PRUNE_AGE_S
        hits = await self._svc.recall(
            None,
            k=max_facts,
            scopes=[scope],
            min_confidence=0.0,
            include_relations=False,
            include_superseded=False,
        )
        protected = {"identity", "persona_manual", "commitment"}
        pruned = 0
        for h in hits:
            if time.perf_counter() >= deadline:
                break
            f = h.fact
            if str(getattr(f, "kind", "")) in protected:
                continue
            if str(getattr(f, "layer", "")) == "procedural":
                continue
            if f.ts_last >= cutoff:
                continue
            if f.evidence_count > _PRUNE_MAX_EVIDENCE:
                continue
            if f.confidence > _PRUNE_CONF_CEILING:
                continue
            if f.confidence <= _PRUNE_FLOOR:
                continue  # already floored
            if dry_run:
                pruned += 1
                continue
            try:
                f.confidence = _PRUNE_FLOOR
                f.ts_last = now
                await self._svc._vec.upsert([f])
                pruned += 1
            except Exception as exc:  # noqa: BLE001
                _log.debug("curator.prune_one_failed id=%s err=%s", f.id, exc)
        return pruned


__all__ = ["MemoryCurator", "CurationReport"]
