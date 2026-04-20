"""Evolution journal: every self-evolution cycle becomes a first-class record.

Each cycle captures the full lineage — what the agent observed, what it
decided to change, what artifacts it produced, whether the artifacts passed
validation, and how the artifacts performed in subsequent turns. This is the
substrate that makes meta-evaluation (Phase E5) possible.

Design principles:
- Pure storage. No event bus, no LLM, no file I/O beyond SQLite.
- Writes fail loud. We would rather raise than log-and-continue, because a
  missing journal row is indistinguishable from a cycle that never happened.
- JSON payloads are stored as text; the caller shapes them.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any

from xmclaw.memory.sqlite_store import SQLiteStore


# Cycle verdicts (final state of a cycle row)
CYCLE_PENDING = "pending"
CYCLE_PASSED = "passed"         # artifacts forged, validated, at least one shadow/promoted
CYCLE_REJECTED = "rejected"     # validation or policy blocked the cycle
CYCLE_CRASHED = "crashed"       # unhandled exception mid-cycle
CYCLE_SKIPPED = "skipped"       # cycle started but inputs were insufficient

# Artifact status (mutable — moves as the artifact's fate is decided)
STATUS_SHADOW = "shadow"
STATUS_PROMOTED = "promoted"
STATUS_RETIRED = "retired"
STATUS_ROLLED_BACK = "rolled_back"
STATUS_REJECTED = "rejected"
# Phase E7: artifact passed validation but the risk assessor flagged it.
# The engine parks it in shadow/ with this status and emits
# EVOLUTION_APPROVAL_REQUESTED; nothing auto-promotes until approve_artifact
# is called. This is a terminal-ish status — the only transitions out are
# to PROMOTED (user approved) or RETIRED (user declined / timed out).
STATUS_NEEDS_APPROVAL = "needs_approval"

# Artifact kinds
KIND_GENE = "gene"
KIND_SKILL = "skill"
KIND_MD = "md"            # SOUL.md / PROFILE.md / AGENTS.md edits


def new_cycle_id() -> str:
    return f"cycle_{uuid.uuid4().hex[:12]}"


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def _loads(raw: str | None, fallback: Any) -> Any:
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return fallback


class EvolutionJournal:
    """Thin async facade over SQLiteStore for evolution cycle records.

    Typical usage from EvolutionEngine:

        journal = EvolutionJournal(store, agent_id)
        cid = await journal.open_cycle(trigger="pattern_threshold")
        await journal.record_inputs(cid, {"observations": [...], "reflection": {...}})
        await journal.record_decisions(cid, {"forge_skills": [...]})
        await journal.record_artifact(cid, KIND_SKILL, skill_id, status=STATUS_SHADOW)
        await journal.close_cycle(cid, verdict=CYCLE_PASSED)
    """

    def __init__(self, store: SQLiteStore, agent_id: str):
        self._store = store
        self._agent_id = agent_id

    # ── Cycle lifecycle ──────────────────────────────────────────────────────

    async def open_cycle(
        self, trigger: str, initial_inputs: dict | None = None,
    ) -> str:
        cycle_id = new_cycle_id()
        self._store.journal_insert_cycle(
            cycle_id=cycle_id,
            agent_id=self._agent_id,
            trigger=trigger,
            inputs_json=_dumps(initial_inputs or {}),
        )
        return cycle_id

    async def record_inputs(self, cycle_id: str, inputs: dict) -> None:
        self._store.journal_update_cycle(
            cycle_id, inputs_json=_dumps(inputs),
        )

    async def record_decisions(self, cycle_id: str, decisions: dict) -> None:
        self._store.journal_update_cycle(
            cycle_id, decisions_json=_dumps(decisions),
        )

    async def record_artifact(
        self, cycle_id: str, kind: str, artifact_id: str,
        parent_artifact_id: str | None = None,
        status: str = STATUS_SHADOW,
    ) -> None:
        if kind not in (KIND_GENE, KIND_SKILL, KIND_MD):
            raise ValueError(f"unknown artifact kind: {kind}")
        self._store.lineage_insert(
            artifact_id=artifact_id,
            cycle_id=cycle_id,
            agent_id=self._agent_id,
            kind=kind,
            parent_artifact_id=parent_artifact_id,
            status=status,
        )
        # Append artifact_id to the cycle's artifacts_json so a single read
        # of the cycle row is enough to list its products.
        cycle = self._store.journal_get_cycle(cycle_id)
        if cycle:
            existing = _loads(cycle.get("artifacts_json"), [])
            if not isinstance(existing, list):
                existing = []
            if artifact_id not in existing:
                existing.append(artifact_id)
            self._store.journal_update_cycle(
                cycle_id, artifacts_json=_dumps(existing),
            )

    async def close_cycle(
        self, cycle_id: str, verdict: str,
        reject_reason: str | None = None,
        metrics: dict | None = None,
    ) -> None:
        valid = {CYCLE_PASSED, CYCLE_REJECTED, CYCLE_CRASHED, CYCLE_SKIPPED}
        if verdict not in valid:
            raise ValueError(f"invalid verdict: {verdict}")
        self._store.journal_update_cycle(
            cycle_id,
            verdict=verdict,
            reject_reason=reject_reason,
            metrics_json=_dumps(metrics or {}),
            ended_at=int(time.time()),
        )

    # ── Artifact fate & metrics ──────────────────────────────────────────────

    async def update_artifact_status(
        self, artifact_id: str, status: str,
    ) -> None:
        valid = {
            STATUS_SHADOW, STATUS_PROMOTED, STATUS_RETIRED,
            STATUS_ROLLED_BACK, STATUS_REJECTED, STATUS_NEEDS_APPROVAL,
        }
        if status not in valid:
            raise ValueError(f"invalid artifact status: {status}")
        self._store.lineage_update_status(artifact_id, status)

    async def increment_metric(
        self, artifact_id: str, metric: str, delta: int = 1,
    ) -> None:
        # metric must be one of matched_count | helpful_count | harmful_count
        self._store.lineage_increment(artifact_id, metric, delta)

    # ── Queries ──────────────────────────────────────────────────────────────

    async def get_cycle(self, cycle_id: str) -> dict | None:
        row = self._store.journal_get_cycle(cycle_id)
        return self._hydrate_cycle(row) if row else None

    async def list_cycles(self, limit: int = 50) -> list[dict]:
        rows = self._store.journal_list_cycles(self._agent_id, limit=limit)
        return [self._hydrate_cycle(r) for r in rows]

    async def get_lineage(self, cycle_id: str) -> list[dict]:
        return self._store.lineage_for_cycle(cycle_id)

    async def get_artifact(self, artifact_id: str) -> dict | None:
        return self._store.lineage_for_artifact(artifact_id)

    async def get_active_artifacts(
        self, kind: str | None = None,
        statuses: tuple[str, ...] = (
            STATUS_PROMOTED, STATUS_SHADOW, STATUS_NEEDS_APPROVAL,
        ),
    ) -> list[dict]:
        return self._store.lineage_active(
            self._agent_id, kind=kind, statuses=statuses,
        )

    async def snapshot_active_artifacts(
        self, kind: str | None = None, max_items: int = 25,
    ) -> list[dict]:
        """Return health snapshots for every live (promoted+shadow) artifact.

        This is the feedback-loop hook (Phase E4). The agent loop calls it
        before reflection so the LLM generating the next insight knows what
        the previous cycles produced and how those artifacts are performing.
        A 'suspect' or 'dead' artifact should bias the reflection toward
        fixing it rather than forging yet another near-duplicate.

        The list is sorted by newest-first (matches `lineage_active`).
        """
        rows = self._store.lineage_active(
            self._agent_id, kind=kind,
            statuses=(STATUS_PROMOTED, STATUS_SHADOW, STATUS_NEEDS_APPROVAL),
        )
        out: list[dict] = []
        for row in rows[:max_items]:
            matched = int(row.get("matched_count", 0) or 0)
            helpful = int(row.get("helpful_count", 0) or 0)
            harmful = int(row.get("harmful_count", 0) or 0)
            status = row.get("status") or STATUS_SHADOW
            if status in (STATUS_RETIRED, STATUS_ROLLED_BACK, STATUS_REJECTED):
                verdict = "unused"
            elif status == STATUS_NEEDS_APPROVAL:
                # Reflection should know an artifact is blocked on a human
                # decision — that's a different signal from "dead". The UI
                # also uses this to render an approval prompt.
                verdict = "pending_approval"
            elif matched == 0:
                verdict = "dead"
            elif matched >= 2 and harmful > helpful:
                verdict = "suspect"
            else:
                verdict = "healthy"
            out.append({
                "artifact_id": row.get("artifact_id"),
                "kind": row.get("kind"),
                "status": status,
                "matched": matched,
                "helpful": helpful,
                "harmful": harmful,
                "verdict": verdict,
            })
        return out

    async def get_artifact_health(self, artifact_id: str) -> dict | None:
        """Return a health snapshot of an artifact for meta-evaluation.

        Shape:
            {
              "artifact_id":  str,
              "kind":         str,
              "status":       str,          # shadow/promoted/retired/...
              "matched":      int,
              "helpful":      int,
              "harmful":      int,
              "helpful_ratio": float | None,  # None if matched == 0
              "harmful_ratio": float | None,
              "verdict":      "healthy" | "suspect" | "dead" | "unused",
            }

        Verdict rules (coarse, used by the UI and by reflection prompts —
        not authoritative for rollback, which has its own threshold logic):
          * unused   — status in (retired, rolled_back, rejected)
          * dead     — matched == 0 (promoted but never fired)
          * suspect  — matched ≥ 2 AND harmful > helpful
          * healthy  — otherwise
        """
        row = self._store.lineage_for_artifact(artifact_id)
        if not row:
            return None
        matched = int(row.get("matched_count", 0) or 0)
        helpful = int(row.get("helpful_count", 0) or 0)
        harmful = int(row.get("harmful_count", 0) or 0)
        status = row.get("status") or STATUS_SHADOW

        helpful_ratio: float | None = None
        harmful_ratio: float | None = None
        if matched > 0:
            helpful_ratio = helpful / matched
            harmful_ratio = harmful / matched

        if status in (STATUS_RETIRED, STATUS_ROLLED_BACK, STATUS_REJECTED):
            verdict = "unused"
        elif status == STATUS_NEEDS_APPROVAL:
            verdict = "pending_approval"
        elif matched == 0:
            verdict = "dead"
        elif matched >= 2 and harmful > helpful:
            verdict = "suspect"
        else:
            verdict = "healthy"

        return {
            "artifact_id": artifact_id,
            "kind": row.get("kind"),
            "status": status,
            "matched": matched,
            "helpful": helpful,
            "harmful": harmful,
            "helpful_ratio": helpful_ratio,
            "harmful_ratio": harmful_ratio,
            "verdict": verdict,
        }

    # ── Phase E8 retrospective queries ──────────────────────────────────────
    #
    # Aggregate reads over cycle + lineage rows so the dashboard can answer
    # "what has the agent been evolving lately" without the caller having to
    # reimplement the same groupby/histogram logic. All methods are pure
    # reads — no side effects, no event emission, safe to call from the
    # daemon's request handlers.

    async def cycle_summary(
        self, window_seconds: int | None = None, limit: int = 500,
    ) -> dict[str, Any]:
        """Histogram of recent cycles.

        Args:
            window_seconds: if provided, restricts cycles to those whose
                ``started_at`` falls within the window. None → most recent
                ``limit`` cycles regardless of age.
            limit: hard cap on how many cycle rows to scan.

        Shape:
            {
              "total": int,
              "by_verdict": {"passed": n, "rejected": n, ...},
              "by_trigger": {"manual": n, "pattern_threshold": n, ...},
              "by_reject_reason": {"no_insights": n, "all_candidates_failed": n, ...},
              "window_seconds": int | None,
            }

        ``by_reject_reason`` only counts cycles whose ``reject_reason`` is
        set — i.e. closed with a rejected verdict or skipped with a named
        reason. A null ``reject_reason`` is not included.
        """
        if window_seconds is not None:
            rows = self._store.journal_list_cycles_since(
                self._agent_id, window_seconds=window_seconds, limit=limit,
            )
        else:
            rows = self._store.journal_list_cycles(
                self._agent_id, limit=limit,
            )

        by_verdict: dict[str, int] = {}
        by_trigger: dict[str, int] = {}
        by_reject_reason: dict[str, int] = {}
        for row in rows:
            verdict = row.get("verdict") or CYCLE_PENDING
            by_verdict[verdict] = by_verdict.get(verdict, 0) + 1
            trigger = row.get("trigger") or "unknown"
            by_trigger[trigger] = by_trigger.get(trigger, 0) + 1
            reason = row.get("reject_reason")
            if reason:
                by_reject_reason[reason] = by_reject_reason.get(reason, 0) + 1

        return {
            "total": len(rows),
            "by_verdict": by_verdict,
            "by_trigger": by_trigger,
            "by_reject_reason": by_reject_reason,
            "window_seconds": window_seconds,
        }

    async def artifact_funnel(
        self, kind: str | None = None, limit: int = 1000,
    ) -> dict[str, Any]:
        """Count lineage rows by status (and kind).

        The dashboard funnel: forged → shadow → promoted → retired. Shape:

            {
              "total": int,
              "by_status": {"shadow": n, "promoted": n, ...},
              "by_kind": {"skill": n, "gene": n},
              "kind_filter": str | None,
            }

        Counts include every lineage row — retired and rolled-back rows
        stay in the count because the dashboard uses them to explain
        what happened to cycles that no longer have live artifacts.
        """
        rows = self._store.lineage_all(
            self._agent_id, kind=kind, limit=limit,
        )
        by_status: dict[str, int] = {}
        by_kind: dict[str, int] = {}
        for row in rows:
            status = row.get("status") or STATUS_SHADOW
            by_status[status] = by_status.get(status, 0) + 1
            k = row.get("kind") or "unknown"
            by_kind[k] = by_kind.get(k, 0) + 1
        return {
            "total": len(rows),
            "by_status": by_status,
            "by_kind": by_kind,
            "kind_filter": kind,
        }

    async def reject_reason_histogram(
        self, limit: int = 10, window_seconds: int | None = None,
    ) -> list[dict[str, Any]]:
        """Top ``limit`` cycle-level reject reasons, sorted by count desc.

        Output shape (list preserves ranking):
            [{"reason": "no_insights", "count": 42}, ...]

        The dashboard uses this to surface "why is evolution not landing
        anything" at a glance. A flat list is easier for the frontend to
        render than nested groups.
        """
        summary = await self.cycle_summary(
            window_seconds=window_seconds, limit=1000,
        )
        items = sorted(
            summary["by_reject_reason"].items(),
            key=lambda kv: kv[1], reverse=True,
        )[:limit]
        return [{"reason": r, "count": c} for r, c in items]

    async def rollback_history(self, limit: int = 20) -> list[dict[str, Any]]:
        """Recent rolled-back artifacts (status == rolled_back).

        Returns the lineage rows directly — the artifact_id, kind,
        matched/helpful/harmful counts, and updated_at timestamp are
        enough for the dashboard to render a table without a join.
        """
        return self._store.lineage_by_status(
            self._agent_id, status=STATUS_ROLLED_BACK, limit=limit,
        )

    # ── Internal ─────────────────────────────────────────────────────────────

    @staticmethod
    def _hydrate_cycle(row: dict) -> dict:
        """Parse JSON columns into real dicts/lists for callers."""
        out = dict(row)
        out["inputs"] = _loads(out.pop("inputs_json", None), {})
        out["decisions"] = _loads(out.pop("decisions_json", None), {})
        out["artifacts"] = _loads(out.pop("artifacts_json", None), [])
        out["metrics"] = _loads(out.pop("metrics_json", None), {})
        return out


# ── Process-wide singleton access ───────────────────────────────────────────
#
# Telemetry hooks (skill execution → lineage metric increments, auto-rollback
# checks) need a journal handle at call sites that don't own the evolution
# engine — most notably ToolRegistry.execute(). A per-agent singleton mirrors
# get_event_bus() and spares us from threading a journal object through every
# call path.

_JOURNAL_CACHE: dict[str, "EvolutionJournal"] = {}


def get_journal(agent_id: str) -> "EvolutionJournal":
    """Return a shared EvolutionJournal for this agent_id.

    The journal owns a SQLiteStore, which owns a single connection. Caching
    per agent_id means skill-execution telemetry and the evolution engine
    share the same connection — no risk of write conflicts from two journals
    racing against the same DB file.
    """
    cached = _JOURNAL_CACHE.get(agent_id)
    if cached is not None:
        return cached
    from xmclaw.memory.sqlite_store import SQLiteStore
    from xmclaw.utils.paths import BASE_DIR
    store = SQLiteStore(BASE_DIR / "shared" / "memory.db")
    j = EvolutionJournal(store, agent_id=agent_id)
    _JOURNAL_CACHE[agent_id] = j
    return j


def reset_journal_cache() -> None:
    """Drop the per-agent journal cache. Test helper only; production code
    should never need to call this."""
    _JOURNAL_CACHE.clear()
