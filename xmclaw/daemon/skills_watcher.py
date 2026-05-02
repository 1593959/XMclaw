"""SkillsWatcher — periodically rescan skills dirs so new installs
appear without a daemon restart. B-173.

Pre-B-173 every fresh skill install (``npx skills add``,
``git clone <url> ~/.xmclaw/skills_user/<name>``, manual
``cp SKILL.md ~/.agents/skills/<name>/``) required a full
``xmclaw restart`` for the boot-time UserSkillsLoader to pick it
up. Competitors (Claude Code per-session, Hermes fs watcher,
skills.sh on-demand) all manage hot-reload one way or another;
forcing a daemon restart on us was a known papercut.

Mechanism: a tiny periodic task that re-runs
``UserSkillsLoader.load_all()`` against the same canonical +
``extra_roots`` the boot-time loader used. The loader is already
idempotent (B-127 guarantees re-registering an already-known
``(skill_id, version)`` pair short-circuits), so a 10s tick on a
50-skill install costs a few file stats per cycle — cheap.

We poll rather than use OS-level fs watchers (``watchdog`` /
``inotify``) because:

* Cross-platform — Windows ProactorEventLoop + watchdog is a known
  flakiness vector.
* No new dependency.
* Predictable upper bound on "I just installed this, when will it
  show up?" — exactly one tick.

Limitations (won't fix in B-173, deferred to B-173.5 / B-172):

* Edits to an EXISTING ``SKILL.md`` (same id + version, body
  changed) DO NOT propagate. Bump version explicitly or restart
  daemon. Fixing this needs either an mtime-based "update in place"
  (violates registry's version-immutability invariant) or auto-bump
  on content change (B-172 mutator territory).
* Removing a skill directory does NOT deregister. In-flight tool
  calls would crash if we yanked the live skill out from under
  them. Restart for clean slate.
* Python ``skill.py`` edits need a daemon restart due to
  ``importlib`` cache. SKILL.md (markdown procedure) hot-reload
  works because re-registration of the same id+version is
  idempotent and the body lives on disk, re-read at register time.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from xmclaw.skills.registry import SkillRegistry
from xmclaw.skills.user_loader import UserSkillsLoader

_log = logging.getLogger(__name__)


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
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._tick_count: int = 0
        self._new_skill_count: int = 0

    # ── observability ───────────────────────────────────────────────

    @property
    def tick_count(self) -> int:
        return self._tick_count

    @property
    def new_skill_count(self) -> int:
        return self._new_skill_count

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
        return len(new_ids)
