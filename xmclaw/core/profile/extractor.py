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
    """

    def __init__(
        self,
        bus: InProcessEventBus,
        persona_user_md_provider: Callable[[], Path],
        *,
        extractor_callable: ExtractorCallable = noop_extractor,
        flush_threshold: int = 3,
        min_confidence: float = 0.5,
    ) -> None:
        self._bus = bus
        self._provider = persona_user_md_provider
        self._extractor = extractor_callable
        self._flush_threshold = max(1, flush_threshold)
        self._min_confidence = max(0.0, min(1.0, min_confidence))
        self._buffers: dict[str, _SessionBuffer] = {}
        self._lock = asyncio.Lock()
        self._sub: Subscription | None = None

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

    async def _append_deltas(
        self, target: Path, deltas: list[ProfileDelta],
    ) -> None:
        """Atomically append delta lines to USER.md.

        Uses :func:`get_lock` (per-path async lock) + reads the
        existing file + appends + writes via
        :func:`atomic_write_text` (tmp + ``os.replace``) so a SIGKILL
        mid-flush leaves either the old or new file, never a
        truncated one.
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
                    "\n".join(d.render_line() for d in deltas) + "\n"
                )
                new_text = head + new_block + tail
            else:
                new_block = (
                    section_header
                    + "\n".join(d.render_line() for d in deltas) + "\n"
                )
                # Trim trailing whitespace before appending the new
                # section so we don't accumulate blank lines on each
                # append.
                new_text = existing.rstrip() + new_block

            atomic_write_text(target, new_text)
