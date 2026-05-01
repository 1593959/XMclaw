"""JournalWriter + JournalReader — Epic #24 Phase 2.1.

Writer
------
Subscribes to the bus, buffers per-session, flushes one
:class:`JournalEntry` per ``SESSION_LIFECYCLE phase=destroy``. All
event types contributing to one entry:

* ``USER_MESSAGE``                  → ``turn_count`` increment
* ``TOOL_INVOCATION_FINISHED``      → ``tool_calls`` append
* ``GRADER_VERDICT``                → grader stats running update
* ``ANTI_REQ_VIOLATION``            → ``anti_req_violations`` increment
* ``SESSION_LIFECYCLE phase=create`` → ``ts_start`` set
* ``SESSION_LIFECYCLE phase=destroy`` → flush + drop session buffer

Failures (disk full, malformed event payload, race on session id) log
a warning and skip the row; one bad session must not poison the whole
journal task.

Reader
------
Stateless. ``recent(n)`` walks ``<data>/v2/journal/<YYYY-MM>/`` newest
month first, returns up to ``n`` entries sorted by ``ts_end`` desc.
``by_session_id`` and ``iter_month`` complete the read API used by
the upcoming Web UI Evolution panel + ``journal_recall`` tool.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from xmclaw.core.bus import InProcessEventBus
from xmclaw.core.bus.events import BehavioralEvent, EventType
from xmclaw.core.bus.memory import Subscription
from xmclaw.core.journal.models import JournalEntry, ToolCallSummary
from xmclaw.utils.paths import journal_dir

_log = logging.getLogger(__name__)


@dataclass
class _SessionBuffer:
    """In-flight aggregate for one session_id."""

    session_id: str
    agent_id: str = ""
    ts_start: float = 0.0
    turn_count: int = 0
    tool_calls: list[ToolCallSummary] = field(default_factory=list)
    grader_scores: list[float] = field(default_factory=list)
    anti_req_violations: int = 0


_TYPES_OF_INTEREST: frozenset[EventType] = frozenset({
    EventType.USER_MESSAGE,
    EventType.TOOL_INVOCATION_FINISHED,
    EventType.GRADER_VERDICT,
    EventType.ANTI_REQ_VIOLATION,
    EventType.SESSION_LIFECYCLE,
})


class JournalWriter:
    """Buffers events per session and flushes one JSONL row on destroy.

    Construct with the shared bus + optional override of the journal
    root directory (tests inject ``tmp_path``; production uses
    :func:`xmclaw.utils.paths.journal_dir`).

    Lifecycle:
      * :meth:`start` — subscribe (idempotent).
      * :meth:`stop`  — unsubscribe + flush any pending sessions to
        disk so a daemon SIGINT doesn't drop the in-flight rows.
        Idempotent.

    Bus subscription is on a single filter that matches every event
    type of interest. Filter functions in ``InProcessEventBus`` are
    called sync per-event before the handler is dispatched, so this is
    cheap.
    """

    def __init__(
        self,
        bus: InProcessEventBus,
        *,
        root: Path | None = None,
    ) -> None:
        self._bus = bus
        self._root = root if root is not None else journal_dir()
        self._buffers: dict[str, _SessionBuffer] = {}
        self._lock = asyncio.Lock()
        self._sub: Subscription | None = None

    # ── public lifecycle ─────────────────────────────────────────────

    @property
    def root(self) -> Path:
        return self._root

    def is_running(self) -> bool:
        return self._sub is not None

    async def start(self) -> None:
        """Subscribe to the bus. Idempotent."""
        if self._sub is not None:
            return
        self._sub = self._bus.subscribe(
            lambda e: e.type in _TYPES_OF_INTEREST,
            self._on_event,
        )
        _log.info("journal.writer.start root=%s", self._root)

    async def stop(self) -> None:
        """Cancel subscription + flush still-open buffers.

        A daemon shutdown should not lose the rows for sessions that
        were active when the bus stopped accepting events. We
        flush each one as ``ts_end = now`` (best-effort timestamp; a
        real destroy would have set it more accurately, but a
        truncated row is more useful than a dropped one).
        """
        if self._sub is None:
            return
        self._sub.cancel()
        self._sub = None
        async with self._lock:
            sids = list(self._buffers.keys())
        for sid in sids:
            try:
                await self._flush(sid, ts_end=time.time())
            except Exception:  # noqa: BLE001 — flush failure must not
                # block daemon shutdown; we already log inside _flush.
                pass

    # ── bus callback ─────────────────────────────────────────────────

    async def _on_event(self, event: BehavioralEvent) -> None:
        try:
            await self._ingest(event)
        except Exception as exc:  # noqa: BLE001 — keep subscription
            # alive even if one event payload has a bad shape.
            _log.warning(
                "journal.ingest_failed type=%s err=%s",
                event.type.value, exc,
            )

    async def _ingest(self, event: BehavioralEvent) -> None:
        sid = event.session_id
        if not sid:
            return

        if event.type == EventType.SESSION_LIFECYCLE:
            phase = (event.payload or {}).get("phase")
            if phase == "create":
                async with self._lock:
                    buf = self._buffers.get(sid)
                    if buf is None:
                        buf = _SessionBuffer(
                            session_id=sid,
                            agent_id=event.agent_id,
                            ts_start=event.ts,
                        )
                        self._buffers[sid] = buf
                    elif buf.ts_start == 0.0:
                        # 我们订阅起步前 session 已 create — 用先到的 event.ts
                        # 作为起点（best-effort）。
                        buf.ts_start = event.ts
                return
            if phase == "destroy":
                await self._flush(sid, ts_end=event.ts)
                return
            # other phases (cancel_requested / undo_applied) ignored.
            return

        async with self._lock:
            buf = self._buffers.get(sid)
            if buf is None:
                # Event arrived before SESSION_LIFECYCLE create —
                # synthesize a buffer from the event itself. Keeps
                # journal complete for sessions older than the
                # writer's start time (e.g. daemon ran a turn before
                # JournalWriter started).
                buf = _SessionBuffer(
                    session_id=sid,
                    agent_id=event.agent_id,
                    ts_start=event.ts,
                )
                self._buffers[sid] = buf

            payload = event.payload or {}
            if event.type == EventType.USER_MESSAGE:
                buf.turn_count += 1
            elif event.type == EventType.TOOL_INVOCATION_FINISHED:
                ok = bool(payload.get("ok", False))
                err = payload.get("error")
                buf.tool_calls.append(ToolCallSummary(
                    name=str(payload.get("name", "")),
                    ok=ok,
                    error=str(err) if err else None,
                ))
            elif event.type == EventType.GRADER_VERDICT:
                score = payload.get("score")
                if isinstance(score, (int, float)):
                    buf.grader_scores.append(float(score))
            elif event.type == EventType.ANTI_REQ_VIOLATION:
                buf.anti_req_violations += 1

    # ── flushing ─────────────────────────────────────────────────────

    async def _flush(self, session_id: str, *, ts_end: float) -> None:
        async with self._lock:
            buf = self._buffers.pop(session_id, None)
        if buf is None:
            return
        ts_start = buf.ts_start or ts_end
        scores = buf.grader_scores
        avg = sum(scores) / len(scores) if scores else None
        entry = JournalEntry(
            session_id=session_id,
            agent_id=buf.agent_id,
            ts_start=ts_start,
            ts_end=ts_end,
            duration_s=max(0.0, ts_end - ts_start),
            turn_count=buf.turn_count,
            tool_calls=tuple(buf.tool_calls),
            grader_avg_score=avg,
            grader_play_count=len(scores),
            grader_lowest=min(scores) if scores else None,
            grader_highest=max(scores) if scores else None,
            anti_req_violations=buf.anti_req_violations,
        )

        # Path: <root>/<YYYY-MM>/<session_id>.jsonl. Same session_id can
        # have multiple destroy events (reconnect cycle); we append
        # rather than overwrite so each lifecycle is preserved.
        month = time.strftime("%Y-%m", time.localtime(ts_end))
        target = self._root / month / f"{_safe_filename(session_id)}.jsonl"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry.to_jsonable(), ensure_ascii=False))
                fh.write("\n")
        except OSError as exc:
            _log.warning(
                "journal.flush_failed session=%s err=%s", session_id, exc,
            )


_FILENAME_FORBIDDEN = set('/\\:*?"<>|')


def _safe_filename(session_id: str) -> str:
    """Map session_id → filename. Replace forbidden chars; cap length."""
    out = "".join("_" if c in _FILENAME_FORBIDDEN else c for c in session_id)
    return out[:120] or "_"


# ── Reader ──────────────────────────────────────────────────────────


class JournalReader:
    """Stateless read API over the journal directory tree.

    All methods are pure over the on-disk JSONL — no caching, no
    indexes. Reader and writer share exactly the same file paths so a
    journal entry written by ``JournalWriter._flush`` is immediately
    visible to ``JournalReader.recent``.
    """

    def __init__(self, *, root: Path | None = None) -> None:
        self._root = root if root is not None else journal_dir()

    @property
    def root(self) -> Path:
        return self._root

    def recent(self, limit: int = 20) -> list[JournalEntry]:
        """Newest ``limit`` entries across all months.

        Walks month directories newest first (lex-sortable
        ``YYYY-MM``) and stops once enough rows are collected. A
        single session_id can have multiple rows in its file (one per
        destroy cycle); we read each line as a separate entry.
        """
        if not self._root.exists():
            return []
        entries: list[JournalEntry] = []
        months = sorted(
            (p for p in self._root.iterdir() if p.is_dir()),
            reverse=True,
        )
        for month_dir in months:
            for entry in self._read_dir(month_dir):
                entries.append(entry)
            if len(entries) >= limit * 2:
                # Stop early; sort below trims to limit.
                break
        entries.sort(key=lambda e: e.ts_end, reverse=True)
        return entries[:limit]

    def by_session_id(self, session_id: str) -> list[JournalEntry]:
        """All entries for a given session_id (chronological)."""
        if not self._root.exists():
            return []
        target_name = f"{_safe_filename(session_id)}.jsonl"
        out: list[JournalEntry] = []
        for month_dir in self._root.iterdir():
            if not month_dir.is_dir():
                continue
            f = month_dir / target_name
            if not f.is_file():
                continue
            out.extend(_read_jsonl(f))
        out.sort(key=lambda e: e.ts_end)
        return out

    def iter_month(self, year: int, month: int) -> Iterator[JournalEntry]:
        """Yield every entry written in the given calendar month."""
        month_dir = self._root / f"{year:04d}-{month:02d}"
        if not month_dir.is_dir():
            return iter(())
        return iter(self._read_dir(month_dir))

    # ── internals ────────────────────────────────────────────────────

    def _read_dir(self, month_dir: Path) -> Iterator[JournalEntry]:
        for f in sorted(month_dir.glob("*.jsonl")):
            yield from _read_jsonl(f)


def _read_jsonl(path: Path) -> list[JournalEntry]:
    out: list[JournalEntry] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    out.append(JournalEntry.from_jsonable(data))
                except (json.JSONDecodeError, KeyError, ValueError) as exc:
                    _log.warning(
                        "journal.parse_failed path=%s err=%s", path, exc,
                    )
    except OSError as exc:
        _log.warning("journal.read_failed path=%s err=%s", path, exc)
    return out
