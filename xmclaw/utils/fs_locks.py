"""Process-wide per-path async lock store (B-65).

XMclaw has multiple writers to the same files — agent tools
(``remember``, ``memory_pin``, ``update_persona``, ``note_write``,
``journal_append``), the BuiltinFileMemoryProvider's ``put`` /
``sync_turn``, and the DreamCompactor's daily rewrite. They all
need to coordinate so a long Dream pass (read MEMORY.md, call LLM
for 30-60s, write back) doesn't lose appends made by ``remember``
during that window.

Earlier batches gave each writer its own ``_fs_locks: dict[str,
asyncio.Lock]`` — fine for in-class concurrency, but two different
classes locking the same path with two different mutex instances
provides no actual mutual exclusion. This module hands out a SINGLE
lock per resolved path string, used by everyone.

Cross-process coordination (multiple daemon instances on the same
workspace) is out of scope; the single-daemon assumption stands.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

# Module-level dict; never freed (the universe of paths is bounded
# by the agent's actual file footprint, never grows past low
# hundreds in practice).
_LOCKS: dict[str, asyncio.Lock] = {}


def get_lock(path: str | Path) -> asyncio.Lock:
    """Return the asyncio.Lock for ``path``. Same identity for the
    same string. Lazily created on first call.

    Pass a *resolved* path when you need the same lock from two
    callers that might pass equivalent-but-not-identical strings
    (e.g. ``./MEMORY.md`` vs ``/abs/.../MEMORY.md``). All XMclaw
    callers already pass absolute paths.
    """
    key = str(path)
    lock = _LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _LOCKS[key] = lock
    return lock


def reset_for_tests() -> None:
    """Test seam: clear the lock store. Real callers must never use
    this — leaks acquired-elsewhere locks."""
    _LOCKS.clear()


def atomic_write_text(path: str | Path, content: str, *, encoding: str = "utf-8") -> None:
    """B-71: durable file write — tmp + ``os.replace``.

    ``Path.write_text`` does ``open + write + close`` directly. If
    the daemon dies mid-write (SIGKILL, OOM, disk full, machine
    crash, container OOMKilled), the file is left truncated /
    half-written. For files the user trusts to keep their state
    (MEMORY.md, USER.md, daily logs), that's silent data loss.

    POSIX (and modern Windows) guarantee ``os.replace`` is atomic
    on the same filesystem — readers see either the old file or
    the new file, never a half-written one. DreamCompactor was
    already using this pattern; this helper makes it the default
    for every persona / notes / journal write path.

    The tmp file lives next to the target (same filesystem) so
    ``os.replace`` is a metadata operation, not a copy. Suffix
    includes ``.tmp.write`` to distinguish from DreamCompactor's
    own ``.dream.tmp``.
    """
    import os as _os
    p = Path(path)
    tmp = p.with_suffix(p.suffix + ".write.tmp")
    tmp.write_text(content, encoding=encoding)
    _os.replace(tmp, p)


__all__ = ["get_lock", "reset_for_tests", "atomic_write_text"]
