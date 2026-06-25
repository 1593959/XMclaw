"""Pending memory candidates.

Automatic extractors should not directly write durable memory. They create
candidate rows with evidence, provenance, and a decision trail. A candidate
becomes a verified fact only after user approval or a later promotion policy.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from xmclaw.utils.paths import data_dir

CandidateStatus = Literal["pending", "approved", "rejected", "promoted"]


@dataclass(slots=True, frozen=True)
class MemoryCandidate:
    id: str
    text: str
    kind: str = "lesson"
    scope: str = "project"
    bucket: str = ""
    source: str = ""
    source_event_id: str | None = None
    confidence: float = 0.5
    quality_score: float = 0.0
    quality_reasons: list[str] = field(default_factory=list)
    reason: str = ""
    evidence: list[dict[str, Any]] = field(default_factory=list)
    neighbor_ids: list[str] = field(default_factory=list)
    status: CandidateStatus = "pending"
    decision_reason: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    decided_at: float | None = None
    promoted_fact_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        text: str,
        kind: str = "lesson",
        scope: str = "project",
        bucket: str = "",
        source: str = "",
        source_event_id: str | None = None,
        confidence: float = 0.5,
        reason: str = "",
        evidence: list[dict[str, Any]] | None = None,
        neighbor_ids: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> "MemoryCandidate":
        now = time.time()
        clean_text = text.strip()
        evidence_list = list(evidence or [])
        quality_score, quality_reasons = score_candidate_quality(
            clean_text,
            confidence=float(confidence),
            evidence=evidence_list,
            source=source,
            reason=reason,
        )
        cid = _candidate_id(clean_text, kind, scope, bucket, source_event_id)
        return cls(
            id=cid,
            text=clean_text,
            kind=kind,
            scope=scope,
            bucket=bucket,
            source=source,
            source_event_id=source_event_id,
            confidence=float(confidence),
            quality_score=quality_score,
            quality_reasons=quality_reasons,
            reason=reason,
            evidence=evidence_list,
            neighbor_ids=list(neighbor_ids or []),
            created_at=now,
            updated_at=now,
            metadata=dict(metadata or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def default_candidate_db_path() -> Path:
    return data_dir() / "v2" / "memory_candidates.db"


class MemoryCandidateStore:
    """SQLite-backed pending candidate store."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        self._db_path = Path(db_path) if db_path is not None else default_candidate_db_path()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @property
    def db_path(self) -> Path:
        return self._db_path

    def create(self, candidate: MemoryCandidate) -> MemoryCandidate:
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM memory_candidates WHERE id = ?",
                (candidate.id,),
            ).fetchone()
            if existing is not None:
                return _row_to_candidate(existing)
            conn.execute(
                """
                INSERT INTO memory_candidates (
                    id, text, kind, scope, bucket, source, source_event_id,
                    confidence, quality_score, quality_reasons_json,
                    reason, evidence_json, neighbor_ids_json,
                    status, decision_reason, created_at, updated_at,
                    decided_at, promoted_fact_id, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _candidate_to_row(candidate),
            )
            conn.commit()
        return candidate

    def list(
        self,
        *,
        status: str | None = "pending",
        limit: int = 100,
        offset: int = 0,
    ) -> list[MemoryCandidate]:
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        where = ""
        params: list[Any] = []
        if status:
            where = "WHERE status = ?"
            params.append(status)
        params.extend([limit, offset])
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM memory_candidates
                {where}
                ORDER BY updated_at DESC
                LIMIT ? OFFSET ?
                """,
                params,
            ).fetchall()
        return [_row_to_candidate(row) for row in rows]

    def get(self, candidate_id: str) -> MemoryCandidate | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM memory_candidates WHERE id = ?",
                (candidate_id,),
            ).fetchone()
        return _row_to_candidate(row) if row is not None else None

    def decide(
        self,
        candidate_id: str,
        *,
        status: CandidateStatus,
        reason: str = "",
        promoted_fact_id: str | None = None,
    ) -> MemoryCandidate | None:
        if status not in {"approved", "rejected", "promoted"}:
            raise ValueError(f"unsupported candidate status: {status!r}")
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE memory_candidates
                SET status = ?, decision_reason = ?, decided_at = ?,
                    promoted_fact_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, reason, now, promoted_fact_id, now, candidate_id),
            )
            conn.commit()
        return self.get(candidate_id)

    def govern_pending(
        self,
        *,
        min_quality_score: float = 0.55,
        auto_reject_below: float = 0.35,
        reject_duplicates: bool = True,
        limit: int = 500,
    ) -> dict[str, Any]:
        """Apply deterministic quality gates to pending candidates.

        This is intentionally conservative: it only rejects candidates
        that are clearly too weak or duplicate another pending/decided row.
        Promotion remains a separate user or policy decision.
        """
        min_quality_score = _clamp_score(min_quality_score)
        auto_reject_below = _clamp_score(auto_reject_below)
        pending = self.list(status="pending", limit=limit)
        rejected: list[dict[str, str]] = []
        kept: list[str] = []
        seen: dict[tuple[str, str, str, str], str] = {}

        for candidate in sorted(
            pending,
            key=lambda c: (c.created_at, c.id),
        ):
            norm_key = (
                candidate.kind.strip().lower(),
                candidate.scope.strip().lower(),
                candidate.bucket.strip().lower(),
                _normalize_candidate_text(candidate.text),
            )
            reason = ""
            if candidate.quality_score < auto_reject_below:
                reason = (
                    "auto_reject_low_quality:"
                    f"{candidate.quality_score:.2f}<"
                    f"{auto_reject_below:.2f}"
                )
            elif (
                reject_duplicates
                and norm_key in seen
                and seen[norm_key] != candidate.id
            ):
                reason = f"auto_reject_duplicate:{seen[norm_key]}"
            elif candidate.quality_score < min_quality_score:
                kept.append(candidate.id)
                seen.setdefault(norm_key, candidate.id)
                continue

            if reason:
                updated = self.decide(
                    candidate.id,
                    status="rejected",
                    reason=reason,
                )
                rejected.append({
                    "id": candidate.id,
                    "reason": reason,
                    "text": candidate.text[:200],
                })
                if updated is not None:
                    seen.setdefault(norm_key, updated.id)
            else:
                kept.append(candidate.id)
                seen.setdefault(norm_key, candidate.id)

        return {
            "checked": len(pending),
            "rejected": rejected,
            "kept_ids": kept,
            "min_quality_score": min_quality_score,
            "auto_reject_below": auto_reject_below,
            "reject_duplicates": bool(reject_duplicates),
        }

    def stats(self) -> dict[str, Any]:
        with self._connect() as conn:
            total = int(conn.execute("SELECT COUNT(*) FROM memory_candidates").fetchone()[0])
            by_status = {
                str(row[0]): int(row[1])
                for row in conn.execute(
                    "SELECT status, COUNT(*) FROM memory_candidates GROUP BY status"
                ).fetchall()
            }
        return {"total": total, "by_status": by_status, "db_path": str(self._db_path)}

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_candidates (
                    id TEXT PRIMARY KEY,
                    text TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    bucket TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT '',
                    source_event_id TEXT,
                    confidence REAL NOT NULL DEFAULT 0.5,
                    quality_score REAL NOT NULL DEFAULT 0.0,
                    quality_reasons_json TEXT NOT NULL DEFAULT '[]',
                    reason TEXT NOT NULL DEFAULT '',
                    evidence_json TEXT NOT NULL DEFAULT '[]',
                    neighbor_ids_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'pending',
                    decision_reason TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    decided_at REAL,
                    promoted_fact_id TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_memory_candidates_status_updated
                ON memory_candidates(status, updated_at DESC)
                """
            )
            _ensure_column(
                conn,
                "memory_candidates",
                "quality_score",
                "REAL NOT NULL DEFAULT 0.0",
            )
            _ensure_column(
                conn,
                "memory_candidates",
                "quality_reasons_json",
                "TEXT NOT NULL DEFAULT '[]'",
            )
            conn.commit()


def _candidate_id(
    text: str,
    kind: str,
    scope: str,
    bucket: str,
    source_event_id: str | None,
) -> str:
    payload = "\n".join([
        kind.strip().lower(),
        scope.strip().lower(),
        bucket.strip().lower(),
        (source_event_id or "").strip(),
        " ".join(text.strip().lower().split()),
    ])
    return "mcand:" + hashlib.sha1(payload.encode("utf-8")).hexdigest()[:20]


def _candidate_to_row(candidate: MemoryCandidate) -> tuple[Any, ...]:
    return (
        candidate.id,
        candidate.text,
        candidate.kind,
        candidate.scope,
        candidate.bucket,
        candidate.source,
        candidate.source_event_id,
        candidate.confidence,
        candidate.quality_score,
        json.dumps(candidate.quality_reasons, ensure_ascii=False, sort_keys=True),
        candidate.reason,
        json.dumps(candidate.evidence, ensure_ascii=False, sort_keys=True),
        json.dumps(candidate.neighbor_ids, ensure_ascii=False, sort_keys=True),
        candidate.status,
        candidate.decision_reason,
        candidate.created_at,
        candidate.updated_at,
        candidate.decided_at,
        candidate.promoted_fact_id,
        json.dumps(candidate.metadata, ensure_ascii=False, sort_keys=True),
    )


def _row_to_candidate(row: sqlite3.Row) -> MemoryCandidate:
    return MemoryCandidate(
        id=row["id"],
        text=row["text"],
        kind=row["kind"],
        scope=row["scope"],
        bucket=row["bucket"],
        source=row["source"],
        source_event_id=row["source_event_id"],
        confidence=float(row["confidence"]),
        quality_score=float(row["quality_score"] or 0.0),
        quality_reasons=[
            str(x) for x in _json_list(row["quality_reasons_json"])
        ],
        reason=row["reason"],
        evidence=_json_list(row["evidence_json"]),
        neighbor_ids=[str(x) for x in _json_list(row["neighbor_ids_json"])],
        status=row["status"],
        decision_reason=row["decision_reason"],
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
        decided_at=row["decided_at"],
        promoted_fact_id=row["promoted_fact_id"],
        metadata=_json_dict(row["metadata_json"]),
    )


def _json_list(raw: str) -> list[Any]:
    try:
        value = json.loads(raw or "[]")
    except Exception:  # noqa: BLE001
        return []
    return value if isinstance(value, list) else []


def _json_dict(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except Exception:  # noqa: BLE001
        return {}
    return value if isinstance(value, dict) else {}


def _ensure_column(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    spec: str,
) -> None:
    cols = {
        str(row[1])
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {spec}")


def _clamp_score(value: float) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except Exception:  # noqa: BLE001
        return 0.0


def _normalize_candidate_text(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


def score_candidate_quality(
    text: str,
    *,
    confidence: float,
    evidence: list[dict[str, Any]],
    source: str,
    reason: str,
) -> tuple[float, list[str]]:
    """Return a transparent quality score for a pending memory candidate."""
    clean = " ".join((text or "").split())
    reasons: list[str] = []
    score = 0.45

    if len(clean) >= 12:
        score += 0.15
    else:
        score -= 0.25
        reasons.append("too_short")

    cjk_count = sum(1 for ch in clean if "\u4e00" <= ch <= "\u9fff")
    if cjk_count >= 6 or len(clean.split()) >= 4:
        score += 0.10
    else:
        reasons.append("low_information_density")

    if evidence:
        score += min(0.20, len(evidence) * 0.07)
    else:
        score -= 0.15
        reasons.append("no_evidence")

    if confidence >= 0.75:
        score += 0.10
    elif confidence < 0.45:
        score -= 0.10
        reasons.append("low_confidence")

    lower = clean.lower()
    speculative_markers = (
        "可能", "猜测", "也许", "似乎", "未验证", "失败", "报错",
        "找不到", "正在", "临时", "maybe", "probably", "todo",
    )
    if any(marker in lower for marker in speculative_markers):
        score -= 0.20
        reasons.append("speculative_or_unverified")

    if source in {"assistant_response", "tool_result"} and not evidence:
        score -= 0.10
        reasons.append("weak_source")

    if reason in {"tool_failed", "unverified_extracted_lesson", "task_in_progress"}:
        score -= 0.15
        reasons.append(reason)

    score = max(0.0, min(1.0, round(score, 3)))
    if not reasons and score >= 0.75:
        reasons.append("high_quality")
    return score, reasons


__all__ = [
    "CandidateStatus",
    "MemoryCandidate",
    "MemoryCandidateStore",
    "default_candidate_db_path",
    "score_candidate_quality",
]
