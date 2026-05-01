"""Tests for Phase 6 — cron + ACP manifest + Canvas host."""
from __future__ import annotations

import json
import time

import pytest

from xmclaw.core.scheduler.cron import (
    CronJob,
    CronStore,
    CronTickTask,
    _parse_interval,
    parse_schedule,
)


# ── Schedule parsing ──────────────────────────────────────────────────


def test_parse_interval_seconds():
    now = 1000.0
    assert _parse_interval("every 30s", now=now) == 1030.0
    assert _parse_interval("every 5m", now=now) == 1300.0
    assert _parse_interval("every 2h", now=now) == 1000.0 + 7200
    assert _parse_interval("every 1d", now=now) == 1000.0 + 86400


def test_parse_interval_case_insensitive():
    now = 1000.0
    assert _parse_interval("EVERY 30S", now=now) == 1030.0


def test_parse_interval_returns_none_for_non_interval():
    assert _parse_interval("0 9 * * *", now=1000) is None


def test_parse_schedule_works_for_interval():
    now = 1000.0
    assert parse_schedule("every 5m", now=now) == 1300.0


def test_parse_schedule_raises_for_garbage():
    with pytest.raises(ValueError):
        parse_schedule("not a schedule", now=time.time())


# ── CronJob round-trip ────────────────────────────────────────────────


def test_cronjob_round_trip():
    j = CronJob(
        id="abc",
        name="ping",
        schedule="every 5m",
        prompt="check the build",
        agent_id="coder",
        enabled_toolsets=["bash", "file_read"],
    )
    raw = j.to_dict()
    assert raw["enabled_toolsets"] == ["bash", "file_read"]
    rebuilt = CronJob.from_dict(raw)
    assert rebuilt == j


def test_cronjob_with_updates_immutable():
    j = CronJob(id="x", name="n", schedule="every 1m", prompt="p")
    j2 = j.with_updates(run_count=5)
    assert j.run_count == 0
    assert j2.run_count == 5


# ── CronStore ─────────────────────────────────────────────────────────


def test_store_add_persists_to_disk(tmp_path):
    s = CronStore(jobs_path=tmp_path / "jobs.json", output_dir=tmp_path / "out")
    job = CronJob(id="j1", name="ping", schedule="every 5m", prompt="hi")
    saved = s.add(job)
    assert saved.next_run_at > time.time()
    raw = json.loads((tmp_path / "jobs.json").read_text(encoding="utf-8"))
    assert isinstance(raw, list) and len(raw) == 1
    assert raw[0]["id"] == "j1"


def test_store_list_due(tmp_path):
    s = CronStore(jobs_path=tmp_path / "jobs.json", output_dir=tmp_path / "out")
    past = CronJob(
        id="due", name="due", schedule="every 1m", prompt="x",
        next_run_at=time.time() - 100,
    )
    future = CronJob(
        id="later", name="later", schedule="every 1d", prompt="y",
        next_run_at=time.time() + 100,
    )
    s.add(past)
    s.add(future)
    # `add` resets next_run_at; force the past one back.
    s.remove("due")
    s._jobs["due"] = past
    s._dirty = True
    s._save()
    due = s.list_due()
    assert any(j.id == "due" for j in due)
    assert not any(j.id == "later" for j in due)


def test_store_mark_fired_advances_next_run(tmp_path):
    s = CronStore(jobs_path=tmp_path / "jobs.json", output_dir=tmp_path / "out")
    s.add(CronJob(id="j", name="n", schedule="every 5m", prompt="x"))
    updated = s.mark_fired("j")
    assert updated is not None
    assert updated.run_count == 1
    assert updated.last_run_at is not None
    # Next run should be ~5 min in the future.
    assert updated.next_run_at > time.time() + 200


def test_store_remove(tmp_path):
    s = CronStore(jobs_path=tmp_path / "jobs.json", output_dir=tmp_path / "out")
    s.add(CronJob(id="j", name="n", schedule="every 5m", prompt="x"))
    assert s.remove("j")
    assert not s.remove("j")


def test_store_write_output(tmp_path):
    s = CronStore(jobs_path=tmp_path / "jobs.json", output_dir=tmp_path / "out")
    p = s.write_output("job-a", "# Job ran successfully\n")
    assert p.exists()
    assert p.parent.name == "job-a"
    assert p.suffix == ".md"
    assert p.read_text(encoding="utf-8").startswith("# Job ran successfully")


def test_store_tolerates_corrupt_jobs_file(tmp_path):
    bad = tmp_path / "jobs.json"
    bad.write_text("not valid json", encoding="utf-8")
    s = CronStore(jobs_path=bad, output_dir=tmp_path / "out")
    assert s.list_jobs() == []
    s.add(CronJob(id="j", name="n", schedule="every 5m", prompt="x"))
    raw = json.loads(bad.read_text(encoding="utf-8"))
    assert len(raw) == 1


# ── CronTickTask ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tick_once_fires_due_job(tmp_path):
    s = CronStore(jobs_path=tmp_path / "jobs.json", output_dir=tmp_path / "out")
    fired_ids: list[str] = []

    async def runner(job):
        fired_ids.append(job.id)
        return f"output for {job.id}"

    tick = CronTickTask(store=s, runner=runner, tick_interval_s=0.05)
    # Add a job that's already due.
    job = CronJob(id="due", name="n", schedule="every 5m", prompt="x")
    s.add(job)
    s._jobs["due"] = job.with_updates(next_run_at=time.time() - 1)
    s._dirty = True
    s._save()

    out = await tick.tick_once()
    assert out == ["due"]
    assert fired_ids == ["due"]
    # Output file should have been written.
    listing = list((tmp_path / "out" / "due").iterdir())
    assert len(listing) == 1


@pytest.mark.asyncio
async def test_tick_once_records_runner_failure(tmp_path):
    s = CronStore(jobs_path=tmp_path / "jobs.json", output_dir=tmp_path / "out")

    async def runner(job):
        raise RuntimeError("network down")

    tick = CronTickTask(store=s, runner=runner)
    job = CronJob(id="bad", name="n", schedule="every 5m", prompt="x")
    s.add(job)
    s._jobs["bad"] = job.with_updates(next_run_at=time.time() - 1)
    s._dirty = True
    s._save()

    fired = await tick.tick_once()
    assert fired == []  # runner raised, not counted as success
    updated = s.get("bad")
    assert updated is not None
    assert updated.last_error and "network down" in updated.last_error
    assert updated.run_count == 1  # we still advance count + reschedule


# ── ACP manifest ─────────────────────────────────────────────────────


def test_acp_manifest_shape():
    from xmclaw.providers.channel.acp import AGENT_MANIFEST, MANIFEST
    assert AGENT_MANIFEST["name"] == "xmclaw"
    assert AGENT_MANIFEST["transport"] == "stdio"
    assert MANIFEST.id == "acp"
    assert MANIFEST.adapter_factory_path.endswith(":ACPAdapter")


@pytest.mark.asyncio
async def test_acp_adapter_raises_when_acp_pkg_missing():
    """The user explicitly enabled ACP but didn't pip install acp →
    we should surface a clean error pointing to the install command."""
    from xmclaw.providers.channel.acp import ACPAdapter
    a = ACPAdapter(agent_id="main")
    with pytest.raises((RuntimeError, NotImplementedError)):
        # Either ImportError surfaces as RuntimeError ("install acp"),
        # or the package is installed and the not-yet-implemented
        # NotImplementedError fires. Both are acceptable Phase 6 states.
        await a.start()


# ── Canvas host (smoke-test, no real bind to avoid port conflicts) ────


def test_default_canvas_port():
    from xmclaw.providers.runtime.canvas_host import default_canvas_port
    assert default_canvas_port() == 18793


def test_canvas_port_env_override(monkeypatch):
    from xmclaw.providers.runtime.canvas_host import default_canvas_port
    monkeypatch.setenv("XMC_CANVAS_PORT", "12345")
    assert default_canvas_port() == 12345


def test_canvas_host_construct_does_not_start_server():
    from xmclaw.providers.runtime.canvas_host import CanvasHost
    h = CanvasHost(port=0)  # port 0 = "we won't actually start"
    assert not h.is_running
