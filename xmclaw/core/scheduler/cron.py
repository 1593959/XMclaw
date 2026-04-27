"""Cron scheduler — time-driven recurring jobs.

Direct port of Hermes ``cron/scheduler.py:1-9`` (60-second tick) +
``cron/jobs.py:25-29`` (croniter integration). Per-job
``enabled_toolsets`` controls which tools the agent can use during a
scheduled run, capping cost (Hermes ``cron/scheduler.py:60-72``); the
``wakeAgent`` gate skips agent invocation entirely for pure-script
jobs (Hermes ``RELEASE_v0.11.0.md:261``).

Persistence:
* ``~/.xmclaw/cron/jobs.json`` — JSON array of job definitions
* ``~/.xmclaw/cron/output/{job_id}/{ts}.md`` — per-fire output
  (markdown so users can read with any editor)

croniter is an optional dependency — when missing, only the simpler
"every N seconds" interval format works (still useful for the
common "ping every 5 minutes" case). With croniter installed, full
cron expressions like ``"0 9 * * MON-FRI"`` are supported.

Public API:
* :class:`CronStore` — load/save jobs.json, list_due, mark_fired
* :class:`CronJob` — frozen dataclass with the cron expression /
  interval / agent_id / prompt
* :class:`CronTickTask` — long-running coroutine that ticks every
  ``tick_interval_s``, fires due jobs, writes output, emits
  ``CRON_JOB_FIRED`` events on the bus
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# Schedule parsing
# ──────────────────────────────────────────────────────────────────────


_INTERVAL_RE = re.compile(r"^every\s+(\d+)\s*(s|m|h|d)$", re.IGNORECASE)


def _parse_interval(expr: str, *, now: float) -> float | None:
    """Return the next-fire wall-clock for an "every Nu" expression.

    ``"every 30s"`` / ``"every 5m"`` / ``"every 2h"`` / ``"every 1d"``.
    Returns ``None`` when not an interval expression (caller falls
    through to croniter).
    """
    m = _INTERVAL_RE.match(expr.strip())
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2).lower()
    seconds = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    return now + n * seconds


def _try_import_croniter() -> Any:
    try:
        from croniter import croniter
        return croniter
    except ImportError:
        return None


def parse_schedule(expr: str, *, now: float) -> float:
    """Compute next-fire from a schedule expression.

    Accepts:
    * ``"every Nu"`` (interval) — works without dependencies
    * full cron syntax (``"0 9 * * *"``) — needs croniter installed

    Raises ValueError on garbage input or when croniter is needed but
    not available.
    """
    nxt = _parse_interval(expr, now=now)
    if nxt is not None:
        return nxt
    croniter_cls = _try_import_croniter()
    if croniter_cls is None:
        raise ValueError(
            f"schedule {expr!r} requires croniter (pip install croniter); "
            "or use the simpler 'every Nu' form (e.g. 'every 5m')"
        )
    try:
        c = croniter_cls(expr, time.localtime(now))
        return float(c.get_next())
    except Exception as exc:
        raise ValueError(f"invalid cron expression {expr!r}: {exc}") from exc


# ──────────────────────────────────────────────────────────────────────
# Data model
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CronJob:
    """One scheduled job. Fields mirror Hermes cron/jobs.py:25-29."""
    id: str
    name: str
    schedule: str            # "every 5m" | full cron string
    prompt: str              # what to send the agent (or "" if wake_agent=False)
    agent_id: str = "main"   # which AgentLoop runs the prompt
    enabled: bool = True
    enabled_toolsets: list[str] = field(default_factory=list)  # empty = all
    wake_agent: bool = True  # False = pure-script job (no LLM call)
    next_run_at: float = 0.0
    last_run_at: float | None = None
    last_error: str | None = None
    run_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "CronJob":
        return cls(
            id=str(raw.get("id") or uuid.uuid4().hex),
            name=str(raw.get("name") or ""),
            schedule=str(raw.get("schedule") or "every 1h"),
            prompt=str(raw.get("prompt") or ""),
            agent_id=str(raw.get("agent_id") or "main"),
            enabled=bool(raw.get("enabled", True)),
            enabled_toolsets=list(raw.get("enabled_toolsets") or []),
            wake_agent=bool(raw.get("wake_agent", True)),
            next_run_at=float(raw.get("next_run_at") or 0.0),
            last_run_at=raw.get("last_run_at"),
            last_error=raw.get("last_error"),
            run_count=int(raw.get("run_count") or 0),
        )

    def with_updates(self, **changes: Any) -> "CronJob":
        return CronJob.from_dict({**self.to_dict(), **changes})


# ──────────────────────────────────────────────────────────────────────
# CronStore — JSON persistence + due selection
# ──────────────────────────────────────────────────────────────────────


def _default_jobs_path() -> Path:
    from xmclaw.utils.paths import data_dir
    return data_dir() / "cron" / "jobs.json"


def _default_output_dir() -> Path:
    from xmclaw.utils.paths import data_dir
    return data_dir() / "cron" / "output"


class CronStore:
    """Load + save the user's job list. Atomic write via tmp+rename."""

    def __init__(
        self,
        *,
        jobs_path: Path | None = None,
        output_dir: Path | None = None,
    ) -> None:
        self._jobs_path = jobs_path or _default_jobs_path()
        self._output_dir = output_dir or _default_output_dir()
        self._jobs: dict[str, CronJob] = {}
        self._dirty = False
        self._loaded = False

    def _load(self) -> None:
        if not self._jobs_path.exists():
            self._loaded = True
            return
        try:
            raw = json.loads(self._jobs_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _log.warning("cron.jobs_read_failed: %s", exc)
            self._loaded = True
            return
        if not isinstance(raw, list):
            self._loaded = True
            return
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            try:
                job = CronJob.from_dict(entry)
                self._jobs[job.id] = job
            except (TypeError, ValueError):
                continue
        self._loaded = True

    def _save(self) -> None:
        if not self._dirty:
            return
        self._jobs_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._jobs_path.with_suffix(self._jobs_path.suffix + ".write.tmp")
        tmp.write_text(
            json.dumps(
                [j.to_dict() for j in self._jobs.values()],
                indent=2, ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        import os
        os.replace(tmp, self._jobs_path)
        self._dirty = False

    def list_jobs(self) -> list[CronJob]:
        if not self._loaded:
            self._load()
        return list(self._jobs.values())

    def get(self, job_id: str) -> CronJob | None:
        if not self._loaded:
            self._load()
        return self._jobs.get(job_id)

    def add(self, job: CronJob) -> CronJob:
        if not self._loaded:
            self._load()
        if not job.next_run_at:
            try:
                next_at = parse_schedule(job.schedule, now=time.time())
            except ValueError:
                next_at = time.time() + 3600
            job = job.with_updates(next_run_at=next_at)
        self._jobs[job.id] = job
        self._dirty = True
        self._save()
        return job

    def remove(self, job_id: str) -> bool:
        if not self._loaded:
            self._load()
        if self._jobs.pop(job_id, None) is None:
            return False
        self._dirty = True
        self._save()
        return True

    def list_due(self, *, now: float | None = None) -> list[CronJob]:
        if not self._loaded:
            self._load()
        cur = now or time.time()
        return [
            j for j in self._jobs.values()
            if j.enabled and j.next_run_at and j.next_run_at <= cur
        ]

    def mark_fired(
        self,
        job_id: str,
        *,
        when: float | None = None,
        error: str | None = None,
    ) -> CronJob | None:
        if not self._loaded:
            self._load()
        job = self._jobs.get(job_id)
        if job is None:
            return None
        cur = when or time.time()
        try:
            next_at = parse_schedule(job.schedule, now=cur)
        except ValueError as exc:
            error = error or str(exc)
            next_at = cur + 3600
        updated = job.with_updates(
            last_run_at=cur,
            last_error=error,
            run_count=job.run_count + 1,
            next_run_at=next_at,
        )
        self._jobs[job_id] = updated
        self._dirty = True
        self._save()
        return updated

    def write_output(self, job_id: str, content: str) -> Path:
        ts = time.strftime("%Y%m%d-%H%M%S", time.localtime())
        out_dir = self._output_dir / job_id
        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / f"{ts}.md"
        target.write_text(content, encoding="utf-8")
        return target


# ──────────────────────────────────────────────────────────────────────
# Module-level singleton: shared between the REST router (writes) and
# the lifespan-owned CronTickTask (reads). Without this, the router and
# tick task each owned their own ``CronStore``; the router's POST landed
# in disk + its cache, the tick task only loaded once at boot and never
# saw new jobs. Tests that need an isolated store still construct
# ``CronStore(...)`` directly.
# ──────────────────────────────────────────────────────────────────────


_default_store_singleton: CronStore | None = None


def default_cron_store() -> CronStore:
    """Return the process-wide CronStore.

    Lazy-instantiated so importing this module is cheap. Both the FastAPI
    cron router and the lifespan-owned :class:`CronTickTask` MUST go
    through this — otherwise a job created via the API never reaches the
    tick task, because each side keeps its own in-memory ``_jobs`` dict
    and only reloads from disk on first access.
    """
    global _default_store_singleton
    if _default_store_singleton is None:
        _default_store_singleton = CronStore()
    return _default_store_singleton


def reset_default_cron_store() -> None:
    """Test hook: drop the singleton so the next call re-loads from disk."""
    global _default_store_singleton
    _default_store_singleton = None


# ──────────────────────────────────────────────────────────────────────
# CronTickTask — long-running coroutine that drives the store
# ──────────────────────────────────────────────────────────────────────


# Callback signature for firing a job: receives the job, returns the
# textual output to write. Raising propagates as last_error.
JobRunner = Callable[[CronJob], Awaitable[str]]


class CronTickTask:
    """Background tick. Mirrors Hermes cron/scheduler.py:1-9 polling shape.

    Args:
        store: CronStore to read/update
        runner: async callable to actually execute a due job
        tick_interval_s: how often to scan for due jobs (default 60s
            to match Hermes; tests use 0.05 for fast iteration)
    """

    def __init__(
        self,
        *,
        store: CronStore,
        runner: JobRunner,
        tick_interval_s: float = 60.0,
    ) -> None:
        self._store = store
        self._runner = runner
        self._tick_s = tick_interval_s
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.is_running:
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._loop(), name="xmclaw-cron-tick")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        try:
            await asyncio.wait_for(self._task, timeout=5.0)
        except asyncio.TimeoutError:
            self._task.cancel()
        self._task = None

    async def tick_once(self) -> list[str]:
        """Single tick — find due jobs, run them, return ids fired.

        Public so tests can drive it directly without waiting for the
        scheduler loop.
        """
        fired: list[str] = []
        for job in list(self._store.list_due()):
            try:
                output = await self._runner(job)
                self._store.write_output(job.id, output)
                self._store.mark_fired(job.id)
                fired.append(job.id)
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "cron.fire_failed job=%s err=%s", job.id, exc, exc_info=exc,
                )
                self._store.mark_fired(
                    job.id, error=f"{type(exc).__name__}: {exc}"
                )
        return fired

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.tick_once()
            except Exception as exc:  # noqa: BLE001
                _log.warning("cron.tick_failed: %s", exc, exc_info=exc)
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self._tick_s
                )
            except asyncio.TimeoutError:
                continue
