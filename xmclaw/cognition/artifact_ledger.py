"""Task artifact ledger.

The ledger is execution state, not long-term memory. It records concrete
artifacts produced by tools so later steps can answer "where did that file
go?" without guessing from chat text or re-searching the whole machine.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from xmclaw.core.bus.events import BehavioralEvent, EventType
from xmclaw.utils.paths import data_dir


_WINDOWS_PATH_RE = re.compile(
    r"(?P<path>[A-Za-z]:\\[^\r\n\t\"'<>|]+)"
)
_PREFIX_RE = re.compile(
    r"^(?:wrote|generated|created|saved|downloaded)(?:\s+\w+)?\s*(?:to|at|:)\s*",
    re.IGNORECASE,
)


@dataclass(slots=True, frozen=True)
class ArtifactRecord:
    id: str
    session_id: str
    event_id: str
    call_id: str = ""
    tool_name: str = ""
    artifact_type: str = "file"
    path: str = ""
    url: str = ""
    name: str = ""
    mime: str = ""
    source: str = "side_effect"
    exists: bool = False
    size_bytes: int = 0
    target_drive: str = ""
    expected_version: str = ""
    checksum: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def default_artifact_db_path() -> Path:
    return data_dir() / "v2" / "artifact_ledger.db"


class ArtifactLedgerStore:
    """SQLite-backed artifact ledger."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        self._db_path = Path(db_path) if db_path is not None else default_artifact_db_path()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @property
    def db_path(self) -> Path:
        return self._db_path

    def add_many(self, records: list[ArtifactRecord]) -> int:
        if not records:
            return 0
        inserted = 0
        with self._connect() as conn:
            for record in records:
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO artifact_ledger (
                        id, session_id, event_id, call_id, tool_name,
                        artifact_type, path, url, name, mime, source,
                        exists_flag, size_bytes, target_drive,
                        expected_version, checksum, metadata_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    _record_to_row(record),
                )
                inserted += int(cur.rowcount or 0)
            conn.commit()
        return inserted

    def list_recent(
        self,
        *,
        session_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        params: list[Any] = []
        where = ""
        if session_id:
            where = "WHERE session_id = ?"
            params.append(session_id)
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM artifact_ledger
                {where}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def search(
        self,
        *,
        query: str = "",
        session_id: str | None = None,
        artifact_type: str | None = None,
        target_drive: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Search recorded artifacts by task scope and lightweight fields.

        This is intentionally a ledger lookup, not a memory lookup: it
        answers "what concrete artifact did this task produce, and where?"
        without teaching the long-term memory system a possibly transient
        fact. The search stays simple and deterministic so tools can call it
        before falling back to broad filesystem scans.
        """
        limit = max(1, min(int(limit), 100))
        clauses: list[str] = []
        params: list[Any] = []
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)
        if artifact_type:
            clauses.append("artifact_type = ?")
            params.append(artifact_type)
        if target_drive:
            clauses.append("UPPER(target_drive) = ?")
            params.append(target_drive.upper())
        needle = (query or "").strip().lower()
        if needle:
            like = f"%{needle}%"
            clauses.append(
                "("
                "LOWER(name) LIKE ? OR LOWER(path) LIKE ? OR "
                "LOWER(url) LIKE ? OR LOWER(tool_name) LIKE ? OR "
                "LOWER(source) LIKE ? OR LOWER(metadata_json) LIKE ?"
                ")"
            )
            params.extend([like, like, like, like, like, like])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM artifact_ledger
                {where}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def stats(self) -> dict[str, Any]:
        with self._connect() as conn:
            total = int(conn.execute("SELECT COUNT(*) FROM artifact_ledger").fetchone()[0])
            by_type = {
                str(row[0]): int(row[1])
                for row in conn.execute(
                    "SELECT artifact_type, COUNT(*) FROM artifact_ledger GROUP BY artifact_type"
                ).fetchall()
            }
        return {"total": total, "by_type": by_type, "db_path": str(self._db_path)}

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS artifact_ledger (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    call_id TEXT NOT NULL DEFAULT '',
                    tool_name TEXT NOT NULL DEFAULT '',
                    artifact_type TEXT NOT NULL DEFAULT 'file',
                    path TEXT NOT NULL DEFAULT '',
                    url TEXT NOT NULL DEFAULT '',
                    name TEXT NOT NULL DEFAULT '',
                    mime TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT '',
                    exists_flag INTEGER NOT NULL DEFAULT 0,
                    size_bytes INTEGER NOT NULL DEFAULT 0,
                    target_drive TEXT NOT NULL DEFAULT '',
                    expected_version TEXT NOT NULL DEFAULT '',
                    checksum TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_artifact_ledger_session_created
                ON artifact_ledger(session_id, created_at DESC)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_artifact_ledger_event
                ON artifact_ledger(event_id)
                """
            )
            conn.commit()


class ArtifactLedger:
    """Bus subscriber that materializes tool outputs into artifact records."""

    def __init__(self, *, bus: Any, store: ArtifactLedgerStore | None = None) -> None:
        self._bus = bus
        self._store = store or ArtifactLedgerStore()

    @property
    def store(self) -> ArtifactLedgerStore:
        return self._store

    def start(self) -> None:
        self._bus.subscribe(
            lambda e: e.type == EventType.TOOL_INVOCATION_FINISHED,
            self._on_tool_finished,
        )

    async def _on_tool_finished(self, event: BehavioralEvent) -> None:
        records = event_to_artifacts(event)
        self._store.add_many(records)


def event_to_artifacts(event: BehavioralEvent) -> list[ArtifactRecord]:
    payload = event.payload or {}
    session_id = event.session_id or ""
    if not session_id:
        return []
    base = {
        "session_id": session_id,
        "event_id": event.id,
        "call_id": str(payload.get("call_id") or ""),
        "tool_name": str(payload.get("name") or ""),
        "created_at": float(getattr(event, "ts", 0.0) or time.time()),
    }
    metadata = {
        "ok": bool(payload.get("ok")),
        "error": str(payload.get("error") or ""),
    }
    out: list[ArtifactRecord] = []

    for raw in _iter_side_effect_paths(payload):
        out.append(_record_from_path(raw, source="side_effect", base=base, metadata=metadata))

    for source, items in (
        ("attachment", payload.get("attachments") or []),
        ("document", payload.get("documents") or []),
    ):
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict):
                out.append(_record_from_attachment(item, source=source, base=base, metadata=metadata))

    for source, key, artifact_type in (
        ("image_url", "images", "image"),
        ("video_url", "videos", "video"),
        ("audio_url", "audios", "audio"),
    ):
        values = payload.get(key) or []
        if not isinstance(values, list):
            continue
        for url in values:
            if isinstance(url, str) and url:
                out.append(_record_from_url(url, artifact_type=artifact_type, source=source, base=base, metadata=metadata))

    return _dedupe_records(out)


def _iter_side_effect_paths(payload: dict[str, Any]) -> list[str]:
    values = payload.get("expected_side_effects") or []
    if not isinstance(values, list):
        return []
    out: list[str] = []
    for raw in values:
        if not isinstance(raw, str) or not raw.strip():
            continue
        text = raw.strip()
        text = _PREFIX_RE.sub("", text).strip()
        match = _WINDOWS_PATH_RE.search(text)
        out.append((match.group("path") if match else text).strip())
    return out


def _record_from_path(
    raw_path: str,
    *,
    source: str,
    base: dict[str, Any],
    metadata: dict[str, Any],
) -> ArtifactRecord:
    path = raw_path.strip().strip("\"'")
    name = Path(path).name if path else ""
    exists = False
    size = 0
    try:
        p = Path(path)
        exists = p.exists()
        size = p.stat().st_size if p.is_file() else 0
    except OSError:
        pass
    drive = path[:2].upper() if re.match(r"^[A-Za-z]:", path) else ""
    artifact_type = _classify_artifact(name, "")
    return ArtifactRecord(
        id=_artifact_id(base["event_id"], source, path),
        path=path,
        name=name,
        artifact_type=artifact_type,
        source=source,
        exists=exists,
        size_bytes=size,
        target_drive=drive,
        metadata=dict(metadata),
        **base,
    )


def _record_from_attachment(
    item: dict[str, Any],
    *,
    source: str,
    base: dict[str, Any],
    metadata: dict[str, Any],
) -> ArtifactRecord:
    path = str(item.get("path") or "")
    url = str(item.get("url") or "")
    name = str(item.get("name") or (Path(path).name if path else ""))
    mime = str(item.get("mime") or "")
    artifact_type = str(item.get("kind") or "") or _classify_artifact(name, mime)
    md = dict(metadata)
    md.update({k: v for k, v in item.items() if k not in {"path", "url", "name", "mime", "kind"}})
    drive = path[:2].upper() if re.match(r"^[A-Za-z]:", path) else ""
    return ArtifactRecord(
        id=_artifact_id(base["event_id"], source, path or url or name),
        path=path,
        url=url,
        name=name,
        mime=mime,
        artifact_type=artifact_type,
        source=source,
        target_drive=drive,
        metadata=md,
        **base,
    )


def _record_from_url(
    url: str,
    *,
    artifact_type: str,
    source: str,
    base: dict[str, Any],
    metadata: dict[str, Any],
) -> ArtifactRecord:
    name = url.rsplit("/", 1)[-1].split("?", 1)[0]
    return ArtifactRecord(
        id=_artifact_id(base["event_id"], source, url),
        url=url,
        name=name,
        artifact_type=artifact_type,
        source=source,
        metadata=dict(metadata),
        **base,
    )


def _classify_artifact(name: str, mime: str) -> str:
    lower = name.lower()
    if mime.startswith("image/") or lower.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
        return "image"
    if mime.startswith("video/") or lower.endswith((".mp4", ".mov", ".webm", ".mkv")):
        return "video"
    if mime.startswith("audio/") or lower.endswith((".mp3", ".wav", ".m4a", ".ogg")):
        return "audio"
    if lower.endswith((".zip", ".7z", ".rar", ".tar", ".gz")):
        return "archive"
    if lower.endswith((".exe", ".msi", ".dmg", ".pkg")):
        return "installer"
    if lower.endswith((".pdf", ".docx", ".pptx", ".xlsx", ".csv", ".txt", ".md")):
        return "document"
    return "file"


def _artifact_id(event_id: str, source: str, identity: str) -> str:
    payload = "\n".join([event_id, source, identity.strip().lower()])
    return "art:" + hashlib.sha1(payload.encode("utf-8")).hexdigest()[:20]


def _record_to_row(record: ArtifactRecord) -> tuple[Any, ...]:
    return (
        record.id,
        record.session_id,
        record.event_id,
        record.call_id,
        record.tool_name,
        record.artifact_type,
        record.path,
        record.url,
        record.name,
        record.mime,
        record.source,
        1 if record.exists else 0,
        int(record.size_bytes or 0),
        record.target_drive,
        record.expected_version,
        record.checksum,
        json.dumps(record.metadata, ensure_ascii=False, sort_keys=True),
        record.created_at,
    )


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "session_id": row["session_id"],
        "event_id": row["event_id"],
        "call_id": row["call_id"],
        "tool_name": row["tool_name"],
        "artifact_type": row["artifact_type"],
        "path": row["path"],
        "url": row["url"],
        "name": row["name"],
        "mime": row["mime"],
        "source": row["source"],
        "exists": bool(row["exists_flag"]),
        "size_bytes": int(row["size_bytes"] or 0),
        "target_drive": row["target_drive"],
        "expected_version": row["expected_version"],
        "checksum": row["checksum"],
        "metadata": _json_dict(row["metadata_json"]),
        "created_at": float(row["created_at"] or 0.0),
    }


def _json_dict(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except Exception:  # noqa: BLE001
        return {}
    return value if isinstance(value, dict) else {}


def _dedupe_records(records: list[ArtifactRecord]) -> list[ArtifactRecord]:
    seen: set[str] = set()
    out: list[ArtifactRecord] = []
    for record in records:
        if record.id in seen:
            continue
        seen.add(record.id)
        out.append(record)
    return out


__all__ = [
    "ArtifactLedger",
    "ArtifactLedgerStore",
    "ArtifactRecord",
    "default_artifact_db_path",
    "event_to_artifacts",
]
