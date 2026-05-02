"""ProfileExtractor — Epic #24 Phase 2.2.

Buffers recent USER_MESSAGE / LLM_RESPONSE pairs per session, calls a
user-supplied extractor callable every ``flush_threshold`` turns (or
on ``SESSION_LIFECYCLE phase=destroy``), appends the returned
:class:`ProfileDelta` lines atomically to ``<persona>/USER.md``, and
emits :data:`EventType.USER_PROFILE_UPDATED` so the agent's frozen
system-prompt cache invalidates next turn.

Design constraints
------------------

* **Single write path** — the extractor MUST write to the same
  ``USER.md`` the persona assembler reads. We resolve it via a
  caller-supplied ``persona_user_md_provider`` callable so this
  module stays in ``core/`` (cannot import ``daemon/`` per
  ``xmclaw/core/AGENTS.md``).

* **Atomic** — uses :func:`xmclaw.utils.fs_locks.atomic_write_text`
  so a SIGKILL mid-flush cannot truncate the user's file.

* **Per-file lock** — concurrent extractors / ``learn_about_user``
  tool calls share :func:`xmclaw.utils.fs_locks.get_lock` so two
  writers don't race.

* **Confidence floor** — deltas with ``confidence < min_confidence``
  are dropped on the way to disk. The default 0.5 keeps obvious
  LLM hallucinations from polluting USER.md.

* **No daemon import** — the extractor is fed everything it needs at
  construction time. Phase 2.3 wires it from
  ``xmclaw/daemon/app.py`` lifespan.
"""
from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from xmclaw.core.bus import InProcessEventBus
from xmclaw.core.bus.events import BehavioralEvent, EventType, make_event
from xmclaw.core.bus.memory import Subscription
from xmclaw.core.profile.models import ProfileDelta
from xmclaw.utils.fs_locks import atomic_write_text, get_lock

_log = logging.getLogger(__name__)


# Callable signature: given recent (role, content) pairs + provenance,
# return any new deltas worth persisting. Implementations may be sync
# or async; we always await via ``asyncio.iscoroutinefunction`` /
# ``asyncio.iscoroutine`` to keep the extractor cooperative.
ExtractorCallable = Callable[
    [list[dict[str, Any]], dict[str, Any]],
    "list[ProfileDelta] | Awaitable[list[ProfileDelta]]",
]


def noop_extractor(
    _messages: list[dict[str, Any]], _meta: dict[str, Any],
) -> list[ProfileDelta]:
    """Default extractor — returns no deltas. Useful for tests and
    for daemon installs without an LLM configured."""
    return []


@dataclass
class _SessionBuffer:
    """In-flight buffer for one session_id."""

    session_id: str
    agent_id: str = ""
    messages: list[dict[str, Any]] = field(default_factory=list)
    last_flush_at: int = 0          # turn count at last flush
    last_user_event_id: str = ""    # provenance for next batch


_TYPES_OF_INTEREST: frozenset[EventType] = frozenset({
    EventType.USER_MESSAGE,
    EventType.LLM_RESPONSE,
    EventType.SESSION_LIFECYCLE,
})


class ProfileExtractor:
    """Bus-driven user-profile delta extractor.

    Parameters
    ----------
    bus : InProcessEventBus
        Shared event bus.
    persona_user_md_provider : callable
        Returns a :class:`pathlib.Path` to the active persona's
        ``USER.md``. Resolved fresh on every flush so a persona switch
        between turns lands in the right file.
    extractor_callable : ExtractorCallable, default noop
        The actual LLM call (or any other delta-producer). Receives
        ``(messages, meta)`` where ``messages`` is the recent buffer
        and ``meta`` carries ``session_id``, ``agent_id``,
        ``last_user_event_id``.
    flush_threshold : int, default 3
        Number of *user* turns buffered before an out-of-band flush
        fires. Session destroy always flushes regardless of count.
    min_confidence : float, default 0.5
        Deltas below this confidence are dropped before writing.
    fact_writer : optional async callable
        B-197: when provided, each accepted delta also gets written
        as a DB row via this callback ``(text, metadata) -> awaitable``.
        The callback's job is to construct a memory item, embed it
        (if applicable), and persist — kept as a callable so this
        ``core/`` module stays free of ``providers/`` imports per
        the layering rule. ``None`` keeps legacy markdown-only
        behaviour for tests / installs without a vec store. Wired by
        the daemon's lifespan; see ``xmclaw/daemon/app.py``.
    """

    def __init__(
        self,
        bus: InProcessEventBus,
        persona_user_md_provider: Callable[[], Path],
        *,
        extractor_callable: ExtractorCallable = noop_extractor,
        flush_threshold: int = 3,
        min_confidence: float = 0.5,
        fact_writer: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        self._bus = bus
        self._provider = persona_user_md_provider
        self._extractor = extractor_callable
        self._flush_threshold = max(1, flush_threshold)
        self._min_confidence = max(0.0, min(1.0, min_confidence))
        self._buffers: dict[str, _SessionBuffer] = {}
        self._lock = asyncio.Lock()
        self._sub: Subscription | None = None
        # B-197: optional DB sink supplied by daemon side as a callable
        # to keep this module free of providers/ imports.
        self._fact_writer = fact_writer

    # ── public lifecycle ─────────────────────────────────────────────

    @property
    def flush_threshold(self) -> int:
        return self._flush_threshold

    @property
    def min_confidence(self) -> float:
        return self._min_confidence

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
        _log.info("profile.extractor.start threshold=%d", self._flush_threshold)

    async def stop(self) -> None:
        """Cancel subscription + flush still-open buffers."""
        if self._sub is None:
            return
        self._sub.cancel()
        self._sub = None
        async with self._lock:
            sids = list(self._buffers.keys())
        for sid in sids:
            try:
                await self._flush(sid)
            except Exception:  # noqa: BLE001 — flush failure must not
                # block daemon shutdown; we already log inside _flush.
                pass

    # ── bus callback ─────────────────────────────────────────────────

    async def _on_event(self, event: BehavioralEvent) -> None:
        try:
            await self._ingest(event)
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "profile.ingest_failed type=%s err=%s",
                event.type.value, exc,
            )

    async def _ingest(self, event: BehavioralEvent) -> None:
        sid = event.session_id
        if not sid:
            return

        if event.type == EventType.SESSION_LIFECYCLE:
            phase = (event.payload or {}).get("phase")
            if phase == "destroy":
                await self._flush(sid)
            return

        async with self._lock:
            buf = self._buffers.get(sid)
            if buf is None:
                buf = _SessionBuffer(session_id=sid, agent_id=event.agent_id)
                self._buffers[sid] = buf

            payload = event.payload or {}
            if event.type == EventType.USER_MESSAGE:
                buf.messages.append({
                    "role": "user",
                    "content": str(payload.get("content", "")),
                })
                buf.last_user_event_id = event.id
            elif event.type == EventType.LLM_RESPONSE:
                buf.messages.append({
                    "role": "assistant",
                    "content": str(payload.get("content", "")),
                })

            should_flush = self._should_flush(buf)

        if should_flush:
            await self._flush(sid)

    def _should_flush(self, buf: _SessionBuffer) -> bool:
        """Count user turns since last flush; ≥ threshold → flush."""
        user_turns = sum(1 for m in buf.messages if m["role"] == "user")
        new_user_turns = user_turns - buf.last_flush_at
        return new_user_turns >= self._flush_threshold

    # ── flushing ────────────────────────────────────────────────────

    async def _flush(self, session_id: str) -> None:
        async with self._lock:
            buf = self._buffers.get(session_id)
            if buf is None or not buf.messages:
                return
            messages_snapshot = list(buf.messages)
            meta = {
                "session_id": session_id,
                "agent_id": buf.agent_id,
                "last_user_event_id": buf.last_user_event_id,
            }
            # Mark the boundary BEFORE the (possibly slow) extractor
            # call so concurrent ingest doesn't re-flush the same window.
            buf.last_flush_at = sum(
                1 for m in buf.messages if m["role"] == "user"
            )

        deltas = await self._invoke_extractor(messages_snapshot, meta)
        accepted = [d for d in deltas if d.confidence >= self._min_confidence]
        if not accepted:
            return

        try:
            target = Path(self._provider())
        except Exception as exc:  # noqa: BLE001 — provider may raise on
            # config errors; profile flush is best-effort.
            _log.warning(
                "profile.flush_no_target session=%s err=%s",
                session_id, exc,
            )
            return

        try:
            await self._append_deltas(target, accepted)
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "profile.flush_failed session=%s path=%s err=%s",
                session_id, target, exc,
            )
            return

        # B-197: dual-write to memory provider (DB rows) so deltas are
        # vector-searchable + filterable by kind=preference. Failure
        # here is logged but does not block the markdown path —
        # markdown stays the user-facing surface; DB is the indexing
        # layer.
        if self._fact_writer is not None:
            await self._write_deltas_via_writer(
                accepted,
                session_id=session_id,
                agent_id=buf.agent_id or "agent",
            )

        # Broadcast so the system-prompt cache can invalidate.
        try:
            await self._bus.publish(make_event(
                session_id=session_id,
                agent_id=buf.agent_id or "agent",
                type=EventType.USER_PROFILE_UPDATED,
                payload={
                    "file_path": str(target),
                    "delta_count": len(accepted),
                    "session_id": session_id,
                    "deltas": [d.to_jsonable() for d in accepted],
                },
            ))
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "profile.flush_publish_failed session=%s err=%s",
                session_id, exc,
            )

    async def _invoke_extractor(
        self, messages: list[dict[str, Any]], meta: dict[str, Any],
    ) -> list[ProfileDelta]:
        try:
            result = self._extractor(messages, meta)
            if asyncio.iscoroutine(result):
                result = await result
            if not isinstance(result, list):
                _log.warning(
                    "profile.extractor_bad_return type=%s",
                    type(result).__name__,
                )
                return []
            # Filter to actual ProfileDelta instances (extractors
            # might return dicts; we accept ProfileDelta only here so
            # downstream code always sees the typed shape).
            return [d for d in result if isinstance(d, ProfileDelta)]
        except Exception as exc:  # noqa: BLE001
            _log.warning("profile.extractor_failed err=%s", exc)
            return []

    async def _write_deltas_via_writer(
        self, deltas: list[ProfileDelta], *,
        session_id: str, agent_id: str,
    ) -> None:
        """B-197: hand each accepted delta to the daemon-supplied
        ``fact_writer`` callback so it can land as a DB row with
        ``kind=preference``. The callback knows about MemoryItem +
        embedding; this module stays import-direction-clean.

        Failure here MUST NOT break the markdown path — markdown
        stays the user-visible surface, DB is best-effort indexing.
        """
        if self._fact_writer is None:
            return
        import time as _t
        for delta in deltas:
            try:
                metadata: dict[str, Any] = {
                    "kind": "preference",
                    "delta_kind": delta.kind,
                    "confidence": delta.confidence,
                    "source_session_id": delta.source_session_id,
                    "source_event_id": delta.source_event_id,
                    "session_id": session_id,
                    "agent_id": agent_id,
                    "evidence_count": 1,
                    "ts": _t.time(),
                }
                await self._fact_writer(delta.text, metadata)
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "profile.db_write_failed session=%s err=%s",
                    session_id, exc,
                )

    async def _append_deltas(
        self, target: Path, deltas: list[ProfileDelta],
    ) -> None:
        """Atomically append delta lines to USER.md.

        Uses :func:`get_lock` (per-path async lock) + reads the
        existing file + appends + writes via
        :func:`atomic_write_text` (tmp + ``os.replace``) so a SIGKILL
        mid-flush leaves either the old or new file, never a
        truncated one.

        B-179 dedup: every incoming delta gets fingerprinted against
        the existing ``## Auto-extracted preferences`` block. If a
        line with the same ``(kind, fingerprint)`` already exists,
        the new delta is dropped — pre-B-179 the joint audit found
        "用中文" written 4× / "Python" written 3× / "ruff + pytest"
        written 2× because every session that mentioned them produced
        a fresh delta and nothing collapsed them.
        """
        lock = get_lock(target)
        async with lock:
            target.parent.mkdir(parents=True, exist_ok=True)
            existing = ""
            if target.is_file():
                try:
                    existing = target.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    existing = ""

            # B-179: build a set of (kind, fingerprint) pairs already
            # in the file so we can drop incoming duplicates.
            seen = _existing_fingerprints(existing)
            keep: list[ProfileDelta] = []
            for d in deltas:
                fp = _fingerprint(d.text)
                key = (d.kind, fp)
                if key in seen:
                    continue
                keep.append(d)
                seen.add(key)
            if not keep:
                # Every incoming delta was already represented; nothing
                # to write. Avoids touching the file (mtime stable).
                return

            section_header = "\n\n## Auto-extracted preferences\n\n"
            if "## Auto-extracted preferences" in existing:
                # Append delta lines just below the heading. Easier to
                # parse later than scattering them through the file.
                marker = "## Auto-extracted preferences"
                idx = existing.find(marker)
                # Find the end-of-line of the heading, then append our
                # new lines right after.
                eol = existing.find("\n", idx)
                if eol < 0:
                    eol = len(existing)
                head = existing[:eol + 1]
                tail = existing[eol + 1:]
                new_block = (
                    "\n".join(d.render_line() for d in keep) + "\n"
                )
                new_text = head + new_block + tail
            else:
                new_block = (
                    section_header
                    + "\n".join(d.render_line() for d in keep) + "\n"
                )
                # Trim trailing whitespace before appending the new
                # section so we don't accumulate blank lines on each
                # append.
                new_text = existing.rstrip() + new_block

            atomic_write_text(target, new_text)


# B-179: dedup helpers — keep here (not in models.py) because they
# are extractor-side concerns: USER.md is the canonical persistence
# format, and the fingerprint definition is tied to the rendered
# line shape. ProfileDelta itself stays a pure data class.

# Same shape ``ProfileDelta.render_line()`` produces. Tolerant of
# the ASCII vs CJK middle-dot variants we've actually seen on disk
# (the rest of the parsers in builtin.py already handle both).
_AUTO_LINE_RE = re.compile(
    r"^- \[auto\s*[·•・]\s*([^\s·•・]+)\s*[·•・]\s*conf=[^\s]+\s*"
    r"[·•・]\s*session=[^\]]+\]\s*(.+)$",
)


def _normalize_for_fp(text: str) -> str:
    """Lowercase, strip surrounding whitespace, collapse internal
    whitespace, drop punctuation that doesn't change semantics so
    near-duplicates ('uses Python.' vs 'uses Python') collapse."""
    if not text:
        return ""
    # NFKC handles full-width vs half-width and other compatibility
    # forms — important for Chinese punctuation.
    text = unicodedata.normalize("NFKC", text).lower()
    # Strip common terminal punctuation that often varies:
    text = text.rstrip("。.!！?？")
    # Collapse internal whitespace to single space.
    text = re.sub(r"\s+", " ", text).strip()
    # Drop quotes / parentheses that wrap-or-don't-wrap inconsistently.
    text = text.strip("\"'`「」『』()（）[]【】")
    return text


def _fingerprint(text: str) -> str:
    """Cheap fingerprint for dedup. Currently the normalised text
    itself — short enough that `set` membership is fine. We don't
    hash because we want deterministic equality-on-content; if two
    deltas with the SAME text differ only in confidence / session,
    they should still collapse to the same fingerprint."""
    return _normalize_for_fp(text)


def _existing_fingerprints(file_text: str) -> set[tuple[str, str]]:
    """Walk ``file_text`` line by line, find every ``[auto · ...]``
    line, and return ``(kind, fingerprint)`` pairs. Lines outside
    the auto-extracted format (hand-written, headings, etc.) are
    ignored — dedup only applies between auto-extract entries."""
    out: set[tuple[str, str]] = set()
    for line in file_text.splitlines():
        m = _AUTO_LINE_RE.match(line.strip())
        if m is None:
            continue
        kind = m.group(1).strip().lower()
        text = m.group(2).strip()
        out.add((kind, _fingerprint(text)))
    return out
