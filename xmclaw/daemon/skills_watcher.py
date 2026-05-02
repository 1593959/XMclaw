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
        Additional roots to scan for SKILL.md / skill.py
        (``~/.agents/skills``, ``~/.claude/skills`` by default per
        B-163). Same list the boot-time loader uses.
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
        await asyncio.get_event_loop().run_in_executor(
            None, loader.load_all,
        )
        after = set(self._registry.list_skill_ids())
        new_ids = sorted(after - before)
        self._tick_count += 1
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

        return len(new_ids)

    def _refresh_changed_bodies(self) -> int:
        """Walk every scanned root, check SKILL.md / versions/v<N>.md
        mtimes against the per-file cache, and call
        :meth:`SkillRegistry.update_body` whenever a file changed
        since last tick. Returns the number of bodies actually updated."""
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
                if skill_dir.name not in registered:
                    continue  # not registered yet — load_all handles it next tick

                # v1 lives at <skill_dir>/SKILL.md
                skill_md = skill_dir / "SKILL.md"
                if skill_md.is_file() and self._maybe_update_body(
                    skill_dir.name, 1, skill_md,
                ):
                    updated += 1

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
