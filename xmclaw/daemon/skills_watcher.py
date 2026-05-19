"""SkillsWatcher — periodically rescan skills dirs so new installs +
edits to existing SKILL.md propagate without a daemon restart.
B-173 + B-175.

Pre-B-173 every fresh skill install (``npx skills add``,
``git clone <url> ~/.xmclaw/skills_user/<name>``, manual
``cp SKILL.md ~/.agents/skills/<name>/``) required a full
``xmclaw restart`` for the boot-time UserSkillsLoader to pick it
up. Competitors (Claude Code per-session, Hermes fs watcher,
skills.sh on-demand) all manage hot-reload one way or another;
forcing a daemon restart on us was a known papercut.

Pre-B-175 even with B-173 the watcher only registered NEW skills.
Editing an existing SKILL.md (same id+version, body changed) was a
silent no-op — UserSkillsLoader's idempotent ``(id, version)`` skip
deliberately doesn't re-register, so the in-memory body stayed
frozen until restart. B-175 closes that by tracking per-file mtime
and calling :meth:`SkillRegistry.update_body` when the SKILL.md
content actually changed.

Mechanism: per tick the watcher does two passes:

  1. ``UserSkillsLoader.load_all()`` — registers any newly-appeared
     skills. Idempotent on already-known ``(id, version)``.
  2. mtime-driven body refresh — for every SKILL.md (and
     ``versions/v<N>.md``) under the scanned roots, compare ``stat``
     mtime against last-seen; on change, re-read body, re-parse
     frontmatter, call ``registry.update_body`` to swap the
     in-memory body in place.

We poll rather than use OS-level fs watchers (``watchdog`` /
``inotify``) because:

* Cross-platform — Windows ProactorEventLoop + watchdog is a known
  flakiness vector.
* No new dependency.
* Predictable upper bound on "I just installed/edited this, when
  will it show up?" — exactly one tick.

Limitations (deferred to follow-ups):

* Removing a skill directory does NOT deregister. In-flight tool
  calls would crash if we yanked the live skill out from under
  them. Restart for clean slate.
* Python ``skill.py`` edits need a daemon restart due to
  ``importlib`` cache. ``update_body`` returns False for Python
  skills (silent no-op) so the watcher doesn't even try.
"""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any

from xmclaw.skills.registry import SkillRegistry
from xmclaw.skills.user_loader import (
    UserSkillsLoader,
    _parse_skill_md_frontmatter,
)

_log = logging.getLogger(__name__)

_VERSIONED_FILE_RE = re.compile(r"^v(\d+)\.md$")


class SkillsWatcher:
    """Background task that periodically rescans the user-skill roots.

    Lifespan-managed by the daemon ``app.py``. Idempotent re-scan
    means a tick on an unchanged tree is a no-op (no log noise, no
    side effects beyond a few file ``stat`` calls).

    Parameters
    ----------
    registry
        Same ``SkillRegistry`` the boot-time loader populated. New
        skills found on a tick are registered here, so
        ``SkillToolProvider`` picks them up on the agent's next
        turn.
    skills_root
        Canonical user-skills directory (``~/.xmclaw/skills_user/``).
    extra_roots
        Additional roots to scan for SKILL.md / skill.py. Default is
        just ``~/.agents/skills`` (the open agent-skills marketplace).
        B-234 dropped ``~/.claude/skills`` from the default — that's
        Claude Code's user-level config space, not XMclaw's territory.
        Same list the boot-time loader uses.
    interval_s
        Seconds between scans. Default 10s — short enough that a
        ``npx skills add`` install feels instant, long enough that
        the cost is invisible.
    enabled
        Off-switch. ``start()`` is a no-op when False.
    """

    def __init__(
        self,
        registry: SkillRegistry,
        skills_root: Path,
        *,
        extra_roots: list[Path] | None = None,
        interval_s: float = 10.0,
        enabled: bool = True,
        bus: "object | None" = None,
    ) -> None:
        self._registry = registry
        self._skills_root = skills_root
        self._extra_roots = list(extra_roots or [])
        self._interval_s = max(1.0, float(interval_s))
        self._enabled = bool(enabled)
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._tick_count: int = 0
        self._new_skill_count: int = 0
        self._updated_body_count: int = 0
        # B-175: per-file mtime cache. Path → last-seen mtime. First
        # observation seeds the entry without firing an update (the
        # boot loader already registered the body fresh-read).
        self._mtimes: dict[Path, float] = {}
        # B-333 (audit #19): when a Python ``skill.py`` file is edited
        # on disk we can't hot-reload it (importlib cache); instead
        # emit ``SKILL_UPDATE_REQUIRES_RESTART`` on the bus so the UI
        # can show a "restart needed" banner. Pre-B-333 the watcher
        # didn't even look at skill.py mtimes — operators had no
        # signal that their edit wouldn't take effect until restart.
        self._bus = bus
        # Separate seen-map so we only fire ONE event per (skill_id,
        # version) per daemon-lifetime, even though the mtime check
        # still runs every tick. Resets on daemon restart by design
        # (a restart picks up the change so the warning is no
        # longer relevant). B-341 (audit pass-2 #6): keyed map (was
        # set) so :meth:`pending_restarts` can return the path too —
        # the Skills page banner needs the file path to surface
        # which edit triggered the warning.
        # Pre-Epic-#27: ``str`` was just the file path. Now we store
        # the FULL payload dict (path + state + registered) so
        # :meth:`pending_restarts` can return the same shape we
        # emitted on the bus.
        self._py_restart_announced: dict[tuple[str, int], dict[str, object]] = {}
        # Buffer for restart-required event payloads detected during
        # the synchronous executor scan. Drained + published by the
        # async ``_tick`` caller after run_in_executor returns. We
        # can't publish from inside the executor thread (no running
        # event loop) and don't want to add the threadsafe-publish
        # complexity for a once-per-skill-edit signal.
        self._pending_restart_events: list[dict] = []
        # Epic #27 sweep follow-up (2026-05-19): buffer for
        # SKILL_HOT_RELOADED events. Drained + published by the async
        # ``_tick`` caller, same shape as ``_pending_restart_events``.
        self._pending_hot_reload_events: list[dict] = []
        # Epic #27 P0 G-02 (2026-05-19): track skills that failed to
        # load on the latest tick. Pre-fix UserSkillsLoader's
        # LoadResult was returned + discarded — the agent had NO way
        # to see "your skill.py couldn't instantiate, that's why
        # skill_browse can't find it". Now: keyed by skill_id so
        # subsequent ticks update / clear entries in place; cleared
        # when a previously-failed skill becomes registered.
        # Each entry: ``{skill_id, path, kind, error, source_root,
        # first_seen, last_seen}``.
        self._load_failures: dict[str, dict[str, object]] = {}
        # Pre-Epic-#27 had no persistence — when a skill recovered
        # (load_all succeeded next tick), its row dropped from
        # _load_failures. That broke the "state='fixed_after_failure'"
        # detection because by the time _refresh_changed_bodies ran in
        # the same tick that re-loaded the skill, the row was already
        # gone. So we keep a parallel set that ONLY grows
        # within a daemon lifetime (clears on restart) — "has this
        # skill_id ever failed to load while I've been running?".
        self._skills_with_history_of_failure: set[str] = set()

    # ── observability ───────────────────────────────────────────────

    @property
    def tick_count(self) -> int:
        return self._tick_count

    @property
    def new_skill_count(self) -> int:
        return self._new_skill_count

    @property
    def updated_body_count(self) -> int:
        return self._updated_body_count

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def load_failures(self) -> list[dict[str, object]]:
        """Epic #27 P0 G-02 (2026-05-19): return the skills that
        failed to load on the most recent tick. One entry per
        skill_id, keyed by directory name.

        Each row: ``{skill_id, path, kind, error, source_root,
        first_seen, last_seen, ticks_failing}``. Stable across
        ticks — a skill that's been broken for an hour stays in
        the list with ``ticks_failing`` incrementing.

        Cleared automatically when the skill becomes successfully
        registered (the SkillsWatcher tick that finally loads it
        removes the row).

        Used by:
        - ``GET /api/v2/skills/load_failures`` (UI banner)
        - ``skills_list`` LLM tool (agent introspection)
        - Skills page top-bar
        """
        # Sort by first_seen so oldest unresolved failures bubble up.
        rows = list(self._load_failures.values())
        rows.sort(key=lambda r: float(r.get("first_seen") or 0))
        # Return a copy so callers can't mutate our state.
        return [dict(r) for r in rows]

    def _update_load_failures(self, results: list[Any]) -> None:
        """Sync ``self._load_failures`` with the latest LoadResult set.

        Removes rows that now succeed, adds new failure rows, and
        bumps ``last_seen`` + ``ticks_failing`` on still-failing
        rows. ``results`` is whatever ``UserSkillsLoader.load_all()``
        returned (list of :class:`LoadResult`)."""
        import time as _time

        now = _time.time()
        seen_ids: set[str] = set()
        for r in results:
            sid = getattr(r, "skill_id", "")
            if not sid:
                continue
            seen_ids.add(sid)
            ok = bool(getattr(r, "ok", False))
            if ok:
                # Previously failing → recovered. Drop the row.
                self._load_failures.pop(sid, None)
                continue
            # Failure → record AND remember forever (within daemon
            # lifetime) so a later "skill.py edited" event can still
            # be tagged 'fixed_after_failure' even after recovery.
            self._skills_with_history_of_failure.add(sid)
            prev = self._load_failures.get(sid)
            row = {
                "skill_id": sid,
                "path": str(getattr(r, "skill_path", "")),
                "kind": str(getattr(r, "kind", "") or "python"),
                "error": str(getattr(r, "error", "") or "unknown"),
                "source_root": str(getattr(r, "source_root", "") or ""),
                "first_seen": (
                    prev.get("first_seen") if prev else now
                ),
                "last_seen": now,
                "ticks_failing": (
                    int(prev.get("ticks_failing", 0) or 0) + 1
                    if prev else 1
                ),
            }
            self._load_failures[sid] = row
        # Garbage-collect rows for ids that no longer appear in
        # results (skill directory was deleted while broken — we
        # don't want to keep complaining about it). seen_ids covers
        # both ok=True and ok=False entries from this tick.
        stale = [sid for sid in self._load_failures if sid not in seen_ids]
        for sid in stale:
            self._load_failures.pop(sid, None)

    def pending_restarts(self) -> list[dict[str, object]]:
        """B-341 (audit pass-2 #6): return the list of skill.py edits
        the watcher has detected this daemon-lifetime that need a
        ``xmclaw stop && xmclaw start`` to take effect.

        Each entry is ``{"skill_id": str, "version": int, "path": str}``.
        Empty list when no python-skill edits have been seen since
        the daemon started — the natural fresh-process state, since
        ``importlib`` is now serving the just-imported module.

        Backs the ``/api/v2/skills`` response field consumed by the
        Skills page banner. Pre-B-341 the watcher emitted
        :class:`EventType.SKILL_UPDATE_REQUIRES_RESTART` to the bus
        but no UI subscriber existed, so the warning was effectively
        invisible — operators kept hitting "edit + nothing happens".
        """
        out: list[dict[str, object]] = []
        for (skill_id, version), payload in sorted(
            self._py_restart_announced.items()
        ):
            out.append({
                "skill_id": skill_id,
                "version": version,
                **payload,  # path / state / registered
            })
        return out

    # ── lifecycle ───────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the periodic task. Idempotent. No-op when disabled."""
        if not self._enabled:
            return
        if self.is_running():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(
            self._loop(), name="skills-watcher-loop",
        )
        _log.info(
            "skills_watcher.start interval_s=%.1f roots=%s extras=%s",
            self._interval_s, str(self._skills_root),
            [str(p) for p in self._extra_roots],
        )

    async def stop(self) -> None:
        """Stop the periodic task. Idempotent."""
        self._stop_event.set()
        task = self._task
        self._task = None
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except asyncio.TimeoutError:
                task.cancel()
            except Exception:  # noqa: BLE001 — shutdown swallow
                pass

    # ── loop ────────────────────────────────────────────────────────

    async def _loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self._interval_s,
                    )
                    return
                except asyncio.TimeoutError:
                    pass
                try:
                    await self._tick()
                except Exception as exc:  # noqa: BLE001 — must not
                    # kill the loop. Logged at WARNING; next interval
                    # gets a fresh attempt.
                    _log.warning("skills_watcher.tick_failed err=%s", exc)
        except asyncio.CancelledError:
            return

    async def tick(self) -> int:
        """Public single-tick trigger — useful for tests + future
        REST endpoint (``POST /api/v2/skills/rescan``).

        Returns the count of newly-registered skill ids on this tick.
        """
        return await self._tick()

    async def _tick(self) -> int:
        before = set(self._registry.list_skill_ids())
        loader = UserSkillsLoader(
            self._registry, self._skills_root,
            extra_roots=self._extra_roots,
        )
        # Run sync filesystem + importlib calls in a thread so we
        # don't pin the event loop on a slow disk.
        results = await asyncio.get_event_loop().run_in_executor(
            None, loader.load_all,
        )
        after = set(self._registry.list_skill_ids())
        new_ids = sorted(after - before)
        self._tick_count += 1
        # Epic #27 P0 G-02 (2026-05-19): record load failures so the
        # daemon / agent can see "you wrote a broken skill.py / SKILL.md".
        self._update_load_failures(results)
        if new_ids:
            self._new_skill_count += len(new_ids)
            _log.info(
                "skills_watcher.new_skills count=%d ids=%s",
                len(new_ids), new_ids,
            )

        # B-175: scan for body edits on existing skills. Runs on the
        # same thread executor for the same disk-pin reason.
        updated = await asyncio.get_event_loop().run_in_executor(
            None, self._refresh_changed_bodies,
        )
        if updated:
            self._updated_body_count += updated
            _log.info(
                "skills_watcher.bodies_updated count=%d", updated,
            )

        # B-333: drain the python-skill-restart event buffer here,
        # back in the async context where the bus is awaitable.
        if self._pending_restart_events and self._bus is not None:
            from xmclaw.core.bus.events import EventType, make_event
            pending = self._pending_restart_events
            self._pending_restart_events = []
            for payload in pending:
                try:
                    event = make_event(
                        session_id="_system",
                        agent_id="skills-watcher",
                        type=EventType.SKILL_UPDATE_REQUIRES_RESTART,
                        payload=payload,
                    )
                    await self._bus.publish(event)
                except Exception as exc:  # noqa: BLE001 — visibility only
                    _log.warning(
                        "skills_watcher.publish_restart_event_failed "
                        "skill_id=%s err=%s",
                        payload.get("skill_id", "?"), exc,
                    )
        elif self._pending_restart_events and self._bus is None:
            # Bus not wired — clear the buffer so it doesn't grow
            # unbounded across ticks. The log line in
            # _maybe_announce_python_restart already covered it.
            self._pending_restart_events = []

        # Epic #27 sweep follow-up (2026-05-19): drain hot-reload
        # events. Mirror the restart-events handling so a bus-less
        # daemon doesn't grow the queue unboundedly either.
        if self._pending_hot_reload_events and self._bus is not None:
            from xmclaw.core.bus.events import EventType, make_event
            hr_pending = self._pending_hot_reload_events
            self._pending_hot_reload_events = []
            for payload in hr_pending:
                try:
                    event = make_event(
                        session_id="_system",
                        agent_id="skills-watcher",
                        type=EventType.SKILL_HOT_RELOADED,
                        payload=payload,
                    )
                    await self._bus.publish(event)
                except Exception as exc:  # noqa: BLE001
                    _log.warning(
                        "skills_watcher.publish_hot_reload_event_failed "
                        "skill_id=%s err=%s",
                        payload.get("skill_id", "?"), exc,
                    )
        elif self._pending_hot_reload_events and self._bus is None:
            self._pending_hot_reload_events = []

        return len(new_ids)

    def _refresh_changed_bodies(self) -> int:
        """Walk every scanned root, check SKILL.md / versions/v<N>.md
        mtimes against the per-file cache, and call
        :meth:`SkillRegistry.update_body` whenever a file changed
        since last tick. Returns the number of bodies actually updated.

        Epic #27 P0 G-03 (2026-05-19): pre-fix this loop SKIPPED any
        skill dir whose id wasn't in the registry (line ~411 "not
        registered yet — load_all handles it next tick"). That's
        wrong for the "I wrote a broken skill.py → fixed it → daemon
        still uses the failed import" case: the failed dir was
        never registered, so we never seeded its mtime, so we never
        detected the fix-edit, so the user never got the
        SKILL_REQUIRES_RESTART signal. Now we iterate ALL skill dirs
        — SKILL.md bodies for registered ones (hot-reloadable),
        skill.py mtime for ALL ones (importlib stale-cache hazard
        either way).
        """
        registered = set(self._registry.list_skill_ids())
        updated = 0
        for root in [self._skills_root, *self._extra_roots]:
            if not root.is_dir():
                continue
            for skill_dir in root.iterdir():
                if not skill_dir.is_dir():
                    continue
                if skill_dir.name.startswith(".") or skill_dir.name.startswith("_"):
                    continue
                is_registered = skill_dir.name in registered

                # v1 lives at <skill_dir>/SKILL.md — only hot-update
                # for skills already in the registry (update_body
                # would error otherwise). MD-only skills that aren't
                # registered yet get picked up by load_all next tick.
                if is_registered:
                    skill_md = skill_dir / "SKILL.md"
                    if skill_md.is_file() and self._maybe_update_body(
                        skill_dir.name, 1, skill_md,
                    ):
                        updated += 1

                # B-333 + Epic #27 G-03 + 2026-05-19 hot-reload:
                # watch skill.py for mtime changes on EVERY dir.
                # First we ATTEMPT the hot-reload path (fresh module
                # via mtime-stamped name + registry.hot_replace);
                # only fall back to "requires_restart" signal when
                # that path fails (syntax error in the new file, or
                # the skill wasn't previously registered so there's
                # nothing to replace). Pre-fix Python skills ALWAYS
                # needed a daemon restart; peers (Claude Code / Hermes
                # / Cline) sidestep this by being markdown-only —
                # XMclaw's hot-reload lets Python skills compete on
                # the same UX.
                skill_py = skill_dir / "skill.py"
                if skill_py.is_file():
                    payload = self._maybe_hot_reload_or_announce(
                        skill_dir, skill_py,
                        registered=is_registered,
                    )
                    if payload is not None:
                        self._pending_restart_events.append(payload)

                if not is_registered:
                    # No versions/ scanning for unregistered skills —
                    # that's tracking SUCCESSFUL multi-version skills.
                    continue

                # v2+ live at <skill_dir>/versions/v<N>.md
                versions_dir = skill_dir / "versions"
                if not versions_dir.is_dir():
                    continue
                for vfile in versions_dir.iterdir():
                    if not vfile.is_file():
                        continue
                    m = _VERSIONED_FILE_RE.match(vfile.name)
                    if m is None:
                        continue
                    ver = int(m.group(1))
                    if ver <= 1:
                        continue
                    if self._maybe_update_body(skill_dir.name, ver, vfile):
                        updated += 1
        return updated

    def _maybe_update_body(
        self, skill_id: str, version: int, file: Path,
    ) -> bool:
        """Compare cached mtime to current; update on change.

        First observation of a path seeds the cache without firing —
        the boot loader has already registered fresh content.
        """
        try:
            mtime = file.stat().st_mtime
        except OSError:
            return False
        cached = self._mtimes.get(file)
        self._mtimes[file] = mtime
        if cached is None or mtime <= cached:
            return False  # first sight or unchanged — no update.

        try:
            body = file.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            _log.warning(
                "skills_watcher.read_failed file=%s err=%s", file, exc,
            )
            return False
        title, description, triggers = _parse_skill_md_frontmatter(body)
        try:
            ok = self._registry.update_body(
                skill_id, version, body,
                title=title or None,
                description=description or None,
                triggers=triggers or None,
            )
        except Exception as exc:  # noqa: BLE001 — defend the watcher
            _log.warning(
                "skills_watcher.update_body_failed "
                "skill_id=%s version=%d err=%s",
                skill_id, version, exc,
            )
            return False
        if ok:
            _log.info(
                "skills_watcher.body_updated skill_id=%s version=%d "
                "from=%s",
                skill_id, version, file,
            )
        return ok

    def _maybe_hot_reload_or_announce(
        self, skill_dir: Path, skill_py: Path,
        *,
        registered: bool,
    ) -> "dict | None":
        """Epic #27 sweep follow-up (2026-05-19): attempt actual
        ``importlib`` reload + ``SkillRegistry.hot_replace`` BEFORE
        falling back to the legacy SKILL_REQUIRES_RESTART signal.

        Decision tree on mtime change:

          1. First observation of this skill.py path → seed mtime
             cache, no action (the boot loader / next tick handles
             initial registration).
          2. Subsequent observation with newer mtime:
             a. ``registered=True``: attempt hot reload. On success →
                ``SKILL_HOT_RELOADED`` event collected for emission;
                NO restart-required payload returned. On failure →
                fall through to restart announcement so the user
                still gets a signal.
             b. ``registered=False``: skill was never registered (or
                the previous attempt failed). Hot-reload doesn't
                apply (nothing to replace); return restart payload
                so the failed-load → fix → restart workflow stays
                visible. The next watcher tick's ``load_all`` will
                pick up the now-valid file.

        Returns the restart payload dict to publish, or ``None`` when
        either there's nothing to announce (first observation or
        successful hot reload).
        """
        # Mtime seed-then-fire: same gate as
        # ``_maybe_announce_python_restart``. First sight returns None.
        try:
            mtime = skill_py.stat().st_mtime
        except OSError:
            return None
        cached = self._mtimes.get(skill_py)
        self._mtimes[skill_py] = mtime
        if cached is None or mtime <= cached:
            return None

        skill_id = skill_dir.name

        # ---------- Hot-reload path (only when previously registered) ----------
        if registered:
            try:
                # Hot-reload requires the loader's reload_one method.
                # Lazy-construct a loader so we don't have to thread it
                # through __init__ (the watcher already has the roots).
                loader = UserSkillsLoader(
                    self._registry,
                    self._skills_root,
                    extra_roots=self._extra_roots,
                )
                new_skill, new_manifest, err = loader.reload_one(skill_dir)
            except Exception as exc:  # noqa: BLE001
                err = f"reload_one raised: {type(exc).__name__}: {exc}"
                new_skill = new_manifest = None
            if new_skill is not None and new_manifest is not None:
                version = int(getattr(new_skill, "version", 1))
                replaced = self._registry.hot_replace(
                    skill_id, version, new_skill, new_manifest,
                )
                if replaced:
                    _log.info(
                        "skills_watcher.python_skill_hot_reloaded "
                        "skill_id=%s version=%d path=%s",
                        skill_id, version, skill_py,
                    )
                    # Queue a SKILL_HOT_RELOADED event for the async
                    # _tick caller to emit (same pattern as the
                    # restart-required path — we're in a thread here
                    # with no running event loop).
                    self._pending_hot_reload_events.append({
                        "skill_id": skill_id,
                        "version": version,
                        "path": str(skill_py),
                        "kind": "python",
                    })
                    # Clear the "history of failure" entry so a
                    # future legitimate edit doesn't get tagged as
                    # ``state="fixed_after_failure"`` long after
                    # the failure was resolved.
                    self._skills_with_history_of_failure.discard(skill_id)
                    return None  # success — no restart payload
                err = (
                    "registry.hot_replace returned False — "
                    f"({skill_id}, v{version}) not in registry"
                )
            else:
                _log.warning(
                    "skills_watcher.hot_reload_failed "
                    "skill_id=%s err=%s — falling back to restart signal",
                    skill_id, err or "unknown",
                )

        # ---------- Fallback: existing requires_restart announcement ----------
        # Reuse the existing restart announcer but DON'T re-seed the
        # mtime cache (we just bumped it above). Drop into the same
        # payload-building logic by inlining: we replicate the once-per-
        # daemon dedup + state classification here.
        version_for_restart = 1
        try:
            v = self._registry.active_version(skill_id)
            if isinstance(v, int) and v > 0:
                version_for_restart = v
        except Exception:  # noqa: BLE001
            pass
        key = (skill_id, version_for_restart)
        if key in self._py_restart_announced:
            return None  # already announced
        was_failing = skill_id in self._skills_with_history_of_failure
        state = "fixed_after_failure" if was_failing else "edited"
        _log.warning(
            "skills_watcher.python_skill_changed_restart_required "
            "skill_id=%s version=%d path=%s state=%s",
            skill_id, version_for_restart, skill_py, state,
        )
        announce_payload = {
            "path": str(skill_py),
            "state": state,
            "registered": registered,
        }
        self._py_restart_announced[key] = announce_payload
        return {
            "skill_id": skill_id,
            "version": version_for_restart,
            **announce_payload,
        }

    def _maybe_announce_python_restart(
        self, skill_id: str, version: int, file: Path,
        *,
        registered: bool = True,
    ) -> "dict | None":
        """B-333: detect mtime changes on a Python skill's ``skill.py``.
        Returns a payload dict to publish OR None.

        Why "return-don't-publish": ``_refresh_changed_bodies`` runs
        in a ``run_in_executor`` thread and has no running event
        loop — calling ``asyncio.create_task(bus.publish(...))``
        there raises ``RuntimeError: no running event loop``. The
        async ``_tick`` collects payloads from the executor thread
        and publishes them itself. Same pattern as the existing
        body-update count path (returns int, async caller logs it).

        ``importlib`` caches the module so we can't hot-reload, but
        we CAN tell the user "we noticed your edit; daemon restart
        needed to pick it up". Pre-B-333 the watcher only watched
        SKILL.md and was silent on skill.py — operators editing a
        Python skill saw no feedback at all until they restarted.

        Epic #27 P0 G-03 (2026-05-19): ``registered`` flag separates
        "I edited a working skill" from "I just wrote / fixed a
        broken skill". Both surface a restart-required event but the
        payload carries ``state="edited"`` vs ``state="fixed_after_failure"``
        so the UI banner / agent prompt can phrase the message right
        ("your edit waits on a restart" vs "looks like you fixed the
        broken skill — restart to load it").
        """
        try:
            mtime = file.stat().st_mtime
        except OSError:
            return None
        cached = self._mtimes.get(file)
        self._mtimes[file] = mtime
        if cached is None or mtime <= cached:
            return None  # first sight or unchanged
        key = (skill_id, version)
        if key in self._py_restart_announced:
            return None  # already announced this daemon — don't spam
        announce_payload: dict[str, object] = {"path": str(file)}
        # Epic #27 G-03: distinguish edit-of-working from fix-of-broken
        # via the failure index. If the skill_id is in _load_failures
        # right now, this edit is most likely the user's fix attempt.
        # Pre-Epic-#27: `was_failing` checked _load_failures, but
        # that map clears the second a skill recovers, so a
        # "broken → fix" edit landed as 'edited' instead of
        # 'fixed_after_failure'. Now we check the persistent
        # history set so the recovery path itself is correctly
        # tagged.
        was_failing = skill_id in self._skills_with_history_of_failure
        state = "fixed_after_failure" if was_failing else "edited"
        _log.warning(
            "skills_watcher.python_skill_changed_restart_required "
            "skill_id=%s version=%d path=%s state=%s",
            skill_id, version, file, state,
        )
        announce_payload["state"] = state
        announce_payload["registered"] = registered
        self._py_restart_announced[key] = announce_payload
        return {
            "skill_id": skill_id,
            "version": version,
            **announce_payload,
        }
