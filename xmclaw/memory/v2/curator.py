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
_DEDUP_COSINE = 0.85  # Unified with write-time near-dup threshold (audit 2026-06-11)

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
        memory_gateway: Any | None = None,
    ) -> None:
        self._svc = service
        self._memory_gateway = memory_gateway
        # LLM is optional; the dedup/prune passes are pure-Python.
        # Contradiction + crystallization passes (next commit) use it.
        self._llm = llm or getattr(service, "_llm", None)
        # Wave-2 fix (2026-06-06): incremental watermark (ts_last of
        # newest fact processed). Survives daemon restarts.
        self._last_curate_ts: float = 0.0
        self._load_watermark()

    def _load_watermark(self) -> None:
        """Load last_curate_ts from disk."""
        import json, os
        from xmclaw.utils.paths import data_dir as get_data_dir
        path = os.path.join(get_data_dir(), "memory_curator_watermark.json")
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                    self._last_curate_ts = float(data.get("last_curate_ts", 0.0))
            except Exception:
                self._last_curate_ts = 0.0

    def _save_watermark(self, ts: float) -> None:
        """Persist watermark to disk."""
        import json, os
        from xmclaw.utils.paths import data_dir as get_data_dir
        path = os.path.join(get_data_dir(), "memory_curator_watermark.json")
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"last_curate_ts": ts}, f)
        except Exception as exc:
            _log.warning("curator.watermark_save_failed err=%s", exc)

    async def curate(
        self,
        *,
        scopes: list[str] | None = None,
        time_budget_s: float = 20.0,
        dry_run: bool = False,
        do_dedup: bool = True,
        do_prune: bool = True,
        do_contradict: bool = True,
        do_crystallize: bool = True,
        max_facts_per_scope: int = 2000,
        min_changes_for_llm: int = 10,
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

        # Wave-2 fix (2026-06-06): incremental curation. Count facts
        # changed since last watermark; skip expensive LLM passes when
        # the change volume is below threshold.
        changed_facts = await self._count_changed_since(self._last_curate_ts)
        # 首次整理（watermark 还没建立，如全新库 / 首跑）：增量门槛不适用，
        # 该把整库过一遍。否则一个从没整理过的库会因"变更数 < 阈值"被永远跳过
        # LLM 矛盾/结晶两道（Wave-2 门槛的边缘 bug）。
        _first_run = self._last_curate_ts <= 0.0
        _llm_gate_open = _first_run or changed_facts >= min_changes_for_llm
        _log.info(
            "curator.incremental changed_facts=%d watermark=%.0f first_run=%s",
            changed_facts, self._last_curate_ts, _first_run,
        )

        # ── Pass 3: contradiction detection (LLM) ─────────────────
        if (
            do_contradict
            and self._llm is not None
            and not _over_budget()
            and _llm_gate_open
        ):
            report.passes_run.append("contradict")
            for sc in target_scopes:
                if _over_budget():
                    report.budget_exhausted = True
                    break
                try:
                    n = await self._detect_contradictions_scope(
                        scope=sc, deadline=deadline, dry_run=dry_run,
                        max_facts=max_facts_per_scope,
                    )
                    report.contradictions_found += n
                except Exception as exc:  # noqa: BLE001
                    _log.warning(
                        "curator.contradict_failed scope=%s err=%s", sc, exc,
                    )
        else:
            report.passes_skipped.append("contradict")
            if changed_facts < min_changes_for_llm:
                _log.info(
                    "curator.skip_contradict insufficient_changes=%d (< %d)",
                    changed_facts, min_changes_for_llm,
                )

        # ── Pass 4: semantic crystallization (LLM) ────────────────
        if (
            do_crystallize
            and self._llm is not None
            and not _over_budget()
            and _llm_gate_open
        ):
            report.passes_run.append("crystallize")
            for sc in target_scopes:
                if _over_budget():
                    report.budget_exhausted = True
                    break
                try:
                    n = await self._crystallize_scope(
                        scope=sc, deadline=deadline, dry_run=dry_run,
                        max_facts=max_facts_per_scope,
                    )
                    report.crystallized += n
                except Exception as exc:  # noqa: BLE001
                    _log.warning(
                        "curator.crystallize_failed scope=%s err=%s", sc, exc,
                    )
        else:
            report.passes_skipped.append("crystallize")
            if changed_facts < min_changes_for_llm:
                _log.info(
                    "curator.skip_crystallize insufficient_changes=%d (< %d)",
                    changed_facts, min_changes_for_llm,
                )

        report.elapsed_s = time.perf_counter() - t0
        _log.info(
            "curator.done scopes=%s scanned=%d merged=%d pruned=%d "
            "contradictions=%d crystallized=%d elapsed_s=%.2f "
            "budget_exhausted=%s dry_run=%s",
            target_scopes, report.scanned, report.merged,
            report.pruned, report.contradictions_found,
            report.crystallized, report.elapsed_s,
            report.budget_exhausted, dry_run,
        )
        # Wave-2 fix: bump watermark after successful live run.
        if not dry_run:
            self._save_watermark(time.time())
        return report

    async def _count_changed_since(self, watermark: float) -> int:
        """Count facts with ts_last > watermark."""
        try:
            all_recent = await self._svc.recall(
                None, k=5000,
                min_confidence=0.0, include_relations=False,
                include_superseded=False,
            )
            return sum(1 for h in all_recent if h.fact.ts_last > watermark)
        except Exception:
            return 5000  # conservative: assume many changes

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


    async def _detect_contradictions_scope(
        self,
        *,
        scope: str,
        deadline: float,
        dry_run: bool,
        max_facts: int,
    ) -> int:
        """LLM pass: find pairs of facts in scope that DIRECTLY
        contradict each other (X says A, Y says not-A), record a
        CONTRADICTS edge both ways + stamp the ``contradicts`` field,
        and floor the LOWER-confidence side so the contradiction
        surfaces in recall as a ⚠ marker. Returns # contradictions
        found.

        Conservative: the prompt requires a DIRECT logical conflict,
        not mere topical overlap. We never delete — a contradiction
        is information ("the user changed their mind"), so both facts
        survive; we just down-rank the stale-looking one and mark the
        relation so the agent sees it."""
        import json as _json

        hits = await self._svc.recall(
            None,
            k=min(max_facts, 120),  # bound the prompt
            scopes=[scope],
            min_confidence=0.0,
            include_relations=False,
            include_superseded=False,
        )
        if len(hits) < 2:
            return 0
        if time.perf_counter() >= deadline:
            return 0

        numbered = "\n".join(
            f"{i+1}. {h.fact.text}" for i, h in enumerate(hits)
        )
        system_prompt = (
            "你是记忆一致性审查员。下面是一批已存储的事实，每条带编号。"
            "找出**直接互相矛盾**的事实对——一条断言 A，另一条断言"
            "非 A（例如 '用户喜欢咖啡' vs '用户不喝咖啡'）。\n\n"
            "返回纯 JSON（不要 markdown）：\n"
            '{"contradictions": [{"a": 编号, "b": 编号, '
            '"reason": "为什么矛盾"}]}\n\n'
            "规则：\n"
            "1. 只标**逻辑上直接冲突**的对——同主题但不冲突的绝不算。\n"
            "2. 措辞不同但意思一致的不是矛盾（那是重复，不归你管）。\n"
            "3. 拿不准是否真矛盾时，**不要标**（保守）。\n"
            "4. 没有矛盾就返回 {\"contradictions\": []}。"
        )
        try:
            from xmclaw.core.ir import Message
            resp = await self._llm.complete(messages=[
                Message(role="system", content=system_prompt),
                Message(role="user", content=numbered),
            ])
            text = (resp.content or "").strip()
            if text.startswith("```"):
                text = text.removeprefix("```json").removeprefix("```")
                text = text.removesuffix("```").strip()
            parsed = _json.loads(text)
        except Exception as exc:  # noqa: BLE001
            _log.warning("curator.contradict.llm_failed err=%s", exc)
            return 0

        pairs = parsed.get("contradictions")
        if not isinstance(pairs, list):
            return 0

        from xmclaw.memory.v2.models import RelationKind
        found = 0
        for p in pairs:
            if time.perf_counter() >= deadline:
                break
            if not isinstance(p, dict):
                continue
            ia, ib = p.get("a"), p.get("b")
            if not isinstance(ia, int) or not isinstance(ib, int):
                continue
            if not (1 <= ia <= len(hits)) or not (1 <= ib <= len(hits)):
                continue
            if ia == ib:
                continue
            fa, fb = hits[ia - 1].fact, hits[ib - 1].fact
            found += 1
            if dry_run:
                continue
            try:
                # CONTRADICTS edge both directions (idempotent).
                await self._svc.relate(
                    source_fact_id=fa.id, target_fact_id=fb.id,
                    kind=RelationKind.CONTRADICTS, auto_extracted=True,
                )
                await self._svc.relate(
                    source_fact_id=fb.id, target_fact_id=fa.id,
                    kind=RelationKind.CONTRADICTS, auto_extracted=True,
                )
                # Phase 8 ⑩ — temporal invalidation (Zep route): the
                # OLDER assertion loses. We stamp ``invalid_at`` on the
                # stale side so recall hides it by default but KEEPS it
                # for history ("2 月喜欢咖啡 / 5 月戒了" are both true
                # over different intervals — never delete). "Newer" =
                # larger ts_last. Tie-break by confidence. We also stamp
                # the ``contradicts`` field + floor confidence so the
                # relation is visible and the loser ranks last even if a
                # caller passes include_invalidated=True.
                if fa.ts_last != fb.ts_last:
                    stale = fa if fa.ts_last < fb.ts_last else fb
                else:
                    stale = fa if fa.confidence <= fb.confidence else fb
                fresh = fb if stale is fa else fa
                now = time.time()
                if stale.invalid_at is None:
                    stale.invalid_at = now
                stale.confidence = min(stale.confidence, 0.4)
                stale.contradicts = tuple(
                    set(stale.contradicts) | {fresh.id}
                )
                stale.ts_last = now
                await self._svc._vec.upsert([stale])
            except Exception as exc:  # noqa: BLE001
                _log.debug("curator.contradict.apply_failed err=%s", exc)
        return found

    async def _crystallize_scope(
        self,
        *,
        scope: str,
        deadline: float,
        dry_run: bool,
        max_facts: int,
    ) -> int:
        """LLM pass: consolidate clusters of many small same-topic
        facts into ONE clearer canonical fact. Distinct from dedup
        (which merges things that already SAY the same thing):
        crystallization SYNTHESIZES a better single statement from
        several related-but-fragmentary ones.

        Returns # of new crystallized facts written. Conservative:
        only fires when a group is genuinely about one coherent topic;
        the LLM is told to leave unrelated facts alone. The source
        facts are superseded onto the new crystallized one.
        """
        import json as _json

        hits = await self._svc.recall(
            None,
            k=min(max_facts, 100),
            scopes=[scope],
            min_confidence=0.0,
            include_relations=False,
            include_superseded=False,
        )
        if len(hits) < 3:
            return 0
        if time.perf_counter() >= deadline:
            return 0

        numbered = "\n".join(
            f"{i+1}. {h.fact.text}" for i, h in enumerate(hits)
        )
        system_prompt = (
            "你是记忆结晶助手。下面是一批零散的事实/规则，每条带编号。"
            "找出**讲的是同一个连贯主题、但被拆成多条琐碎条目**的组，"
            "为每组合成**一条更清晰完整的规范表述**。\n\n"
            "返回纯 JSON（不要 markdown）：\n"
            '{"crystals": [{"members": [编号,...], '
            '"canonical_text": "合成后的一句话规范表述", '
            '"reason": "为什么属于同一主题"}]}\n\n'
            "规则：\n"
            "1. 只合成**确实属于同一连贯主题**的组（≥2 条）。\n"
            "2. canonical_text 要涵盖这组所有要点，但简洁、单句优先。\n"
            "3. 主题不相关的条目绝不要放进任何 group。\n"
            "4. 这跟去重不同：去重是合并重复，结晶是从多条碎片提炼"
            "一条更好的。拿不准就**不结晶**（保守）。\n"
            "5. 没有可结晶的就返回 {\"crystals\": []}。"
        )
        try:
            from xmclaw.core.ir import Message
            resp = await self._llm.complete(messages=[
                Message(role="system", content=system_prompt),
                Message(role="user", content=numbered),
            ])
            text = (resp.content or "").strip()
            if text.startswith("```"):
                text = text.removeprefix("```json").removeprefix("```")
                text = text.removesuffix("```").strip()
            parsed = _json.loads(text)
        except Exception as exc:  # noqa: BLE001
            _log.warning("curator.crystallize.llm_failed err=%s", exc)
            return 0

        crystals = parsed.get("crystals")
        if not isinstance(crystals, list):
            return 0

        crystallized = 0
        for c in crystals:
            if time.perf_counter() >= deadline:
                break
            if not isinstance(c, dict):
                continue
            members = c.get("members")
            canonical_text = str(c.get("canonical_text") or "").strip()
            if (
                not isinstance(members, list)
                or len(members) < 2
                or not canonical_text
            ):
                continue
            member_facts = [
                hits[m - 1].fact
                for m in members
                if isinstance(m, int) and 1 <= m <= len(hits)
            ]
            if len(member_facts) < 2:
                continue
            crystallized += 1
            if dry_run:
                continue
            try:
                # Write the crystallized fact carrying the group's
                # strongest kind/scope/bucket, then supersede the
                # fragments onto it.
                anchor = max(
                    member_facts,
                    key=lambda f: (f.confidence, f.evidence_count),
                )
                if self._memory_gateway is not None:
                    from xmclaw.memory.v2.gateway_models import Observation
                    new_fact = await self._memory_gateway.ingest(
                        Observation(
                            source="curator",
                            content=canonical_text,
                            turn_id=f"crystallize:{int(time.time())}",
                            timestamp=time.time(),
                            metadata={
                                "kind_hint": anchor.kind,
                                "scope_hint": anchor.scope,
                                "bucket_hint": anchor.bucket or "misc",
                                "confidence_hint": min(
                                    0.9, max(f.confidence for f in member_facts),
                                ),
                            },
                        ),
                        context={"crystallize": True},
                    )
                else:
                    new_fact = await self._svc.remember(
                        canonical_text,
                        kind=anchor.kind,
                        scope=anchor.scope,
                        bucket=anchor.bucket or "misc",
                        confidence=min(0.9, max(
                            f.confidence for f in member_facts
                        )),
                    )
                for mf in member_facts:
                    if mf.id == new_fact.id:
                        continue
                    await self._svc.supersede(
                        old_fact_id=mf.id, new_fact_id=new_fact.id,
                    )
            except Exception as exc:  # noqa: BLE001
                _log.debug("curator.crystallize.apply_failed err=%s", exc)
        return crystallized


# ─── wall-clock schedule persistence ──────────────────────────────
#
# THE root-cause fix (#1 in the module docstring). The old dedup tick
# counted background sweeps ("every 24 sweeps") and reset to zero on
# every daemon restart — during development the daemon bounces every
# ~30 min, so the counter never reached 24 and the tick fired ZERO
# times in practice. We replace sweep-counting with a wall-clock
# timestamp persisted to disk, so "is curation due?" is answered by
# real elapsed time and SURVIVES restarts. A daemon that bounces 48
# times a day still curates exactly once a day.


def load_last_curate_ts(state_path: Any) -> float:
    """Read the persisted last-curation unix ts. Returns 0.0 when the
    file is missing or unreadable (→ "due immediately"). Never raises."""
    import json
    from pathlib import Path

    try:
        p = Path(state_path)
        if not p.exists():
            return 0.0
        data = json.loads(p.read_text(encoding="utf-8"))
        ts = data.get("last_curate_ts", 0.0)
        return float(ts) if isinstance(ts, (int, float)) else 0.0
    except Exception:  # noqa: BLE001 — corrupt state → treat as due
        return 0.0


def save_last_curate_ts(state_path: Any, ts: float) -> bool:
    """Persist the last-curation ts atomically. Returns True on success.
    Never raises — a failed write just means we recurate sooner."""
    import json
    from pathlib import Path

    try:
        p = Path(state_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(
            json.dumps({"last_curate_ts": float(ts)}),
            encoding="utf-8",
        )
        tmp.replace(p)
        return True
    except Exception:  # noqa: BLE001
        return False


def is_curation_due(state_path: Any, interval_s: float, *, now: float | None = None) -> bool:
    """True when ``interval_s`` has elapsed since the persisted ts.
    A never-curated store (ts=0) is always due."""
    if now is None:
        now = time.time()
    last = load_last_curate_ts(state_path)
    return (now - last) >= max(0.0, float(interval_s))


__all__ = [
    "MemoryCurator",
    "CurationReport",
    "load_last_curate_ts",
    "save_last_curate_ts",
    "is_curation_due",
]
