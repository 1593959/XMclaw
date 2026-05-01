"""B-136 — AutomationTools unit tests.

Pins:
  * list_tools advertises 5 cron + 1 code_python + 2 process tools
  * cron_create + cron_list happy path round-trip via a temp store
  * cron_pause / cron_resume flip the enabled flag without removing
  * cron_remove deletes
  * code_python runs the snippet and captures stdout/stderr/returncode
  * code_python timeout produces a structured error
  * process_list returns rows when psutil is available
  * process_kill refuses to kill the daemon's own PID
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

from xmclaw.core.ir import ToolCall
from xmclaw.providers.tool.automation import AutomationTools


def _call(name: str, args: dict | None = None) -> ToolCall:
    return ToolCall(name=name, args=args or {}, provenance="synthetic")


# ── tool list ─────────────────────────────────────────────────────


def test_list_tools_default() -> None:
    names = {s.name for s in AutomationTools().list_tools()}
    assert names == {
        "cron_create", "cron_list", "cron_pause", "cron_resume",
        "cron_remove", "code_python", "process_list", "process_kill",
    }


def test_list_tools_disable_cron() -> None:
    names = {s.name for s in AutomationTools(enable_cron=False).list_tools()}
    assert "cron_create" not in names
    assert "code_python" in names


def test_list_tools_disable_code_and_process() -> None:
    names = {s.name for s in AutomationTools(
        enable_code=False, enable_process=False,
    ).list_tools()}
    assert "code_python" not in names
    assert "process_list" not in names
    assert "cron_create" in names


# ── cron round-trip ───────────────────────────────────────────────


@pytest.fixture
def isolated_cron(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Force the cron singleton to a fresh tmp store for each test."""
    from xmclaw.core.scheduler import cron as cron_mod
    cron_mod.reset_default_cron_store()
    jobs_path = tmp_path / "jobs.json"
    output = tmp_path / "out"

    def _factory():
        return cron_mod.CronStore(jobs_path=jobs_path, output_dir=output)

    monkeypatch.setattr(cron_mod, "default_cron_store", _factory)
    return jobs_path


@pytest.mark.asyncio
async def test_cron_create_then_list(isolated_cron) -> None:
    tools = AutomationTools()
    create = await tools.invoke(_call("cron_create", {
        "name": "every-30-min status",
        "schedule": "every 30m",
        "prompt": "report on cluster health",
    }))
    assert create.ok is True
    payload = json.loads(create.content)
    job_id = payload["job_id"]
    assert payload["next_run_at"] > 0

    listing = await tools.invoke(_call("cron_list"))
    assert listing.ok is True
    list_payload = json.loads(listing.content)
    assert list_payload["count"] == 1
    assert list_payload["jobs"][0]["id"] == job_id
    assert list_payload["jobs"][0]["enabled"] is True


@pytest.mark.asyncio
async def test_cron_pause_and_resume(isolated_cron) -> None:
    tools = AutomationTools()
    create = await tools.invoke(_call("cron_create", {
        "name": "x", "schedule": "every 1h", "prompt": "p",
    }))
    job_id = json.loads(create.content)["job_id"]

    pause = await tools.invoke(_call("cron_pause", {"job_id": job_id}))
    assert pause.ok is True
    pause_payload = json.loads(pause.content)
    assert pause_payload["enabled"] is False

    listing = json.loads((await tools.invoke(_call("cron_list"))).content)
    assert listing["jobs"][0]["enabled"] is False

    resume = await tools.invoke(_call("cron_resume", {"job_id": job_id}))
    assert json.loads(resume.content)["enabled"] is True


@pytest.mark.asyncio
async def test_cron_remove(isolated_cron) -> None:
    tools = AutomationTools()
    create = await tools.invoke(_call("cron_create", {
        "name": "x", "schedule": "every 1h", "prompt": "p",
    }))
    job_id = json.loads(create.content)["job_id"]

    remove = await tools.invoke(_call("cron_remove", {"job_id": job_id}))
    assert remove.ok is True

    listing = json.loads((await tools.invoke(_call("cron_list"))).content)
    assert listing["count"] == 0


@pytest.mark.asyncio
async def test_cron_create_rejects_invalid_schedule(isolated_cron) -> None:
    tools = AutomationTools()
    r = await tools.invoke(_call("cron_create", {
        "name": "x", "schedule": "totally not a schedule", "prompt": "p",
    }))
    assert r.ok is False
    assert "invalid schedule" in (r.error or "")


@pytest.mark.asyncio
async def test_cron_pause_unknown_job(isolated_cron) -> None:
    r = await AutomationTools().invoke(_call("cron_pause", {"job_id": "nope"}))
    assert r.ok is False
    assert "not found" in (r.error or "")


# ── code_python ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_code_python_captures_stdout() -> None:
    r = await AutomationTools().invoke(_call("code_python", {
        "code": "print('hello'); print('world')",
    }))
    assert r.ok is True
    payload = json.loads(r.content)
    assert payload["returncode"] == 0
    assert "hello" in payload["stdout"]
    assert "world" in payload["stdout"]


@pytest.mark.asyncio
async def test_code_python_captures_stderr_and_returncode() -> None:
    r = await AutomationTools().invoke(_call("code_python", {
        "code": "import sys; sys.exit(7)",
    }))
    assert r.ok is True
    payload = json.loads(r.content)
    assert payload["returncode"] == 7


@pytest.mark.asyncio
async def test_code_python_timeout() -> None:
    r = await AutomationTools().invoke(_call("code_python", {
        "code": "import time; time.sleep(10)",
        "timeout_s": 1,
    }))
    assert r.ok is False
    assert "timed out" in (r.error or "")


@pytest.mark.asyncio
async def test_code_python_requires_code() -> None:
    r = await AutomationTools().invoke(_call("code_python", {"code": ""}))
    assert r.ok is False
    assert "code required" in (r.error or "")


# ── process tools ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_process_list_returns_rows() -> None:
    if not importlib.util.find_spec("psutil"):
        pytest.skip("psutil not installed")
    r = await AutomationTools().invoke(_call("process_list", {"limit": 5}))
    assert r.ok is True
    payload = json.loads(r.content)
    assert payload["count"] >= 1
    # Every row has the four expected fields
    row = payload["rows"][0]
    assert "pid" in row and "name" in row


@pytest.mark.asyncio
async def test_process_kill_refuses_self() -> None:
    if not importlib.util.find_spec("psutil"):
        pytest.skip("psutil not installed")
    r = await AutomationTools().invoke(_call("process_kill", {
        "pid": os.getpid(),
    }))
    assert r.ok is False
    assert "refusing to kill the daemon" in (r.error or "")


@pytest.mark.asyncio
async def test_process_kill_unknown_pid() -> None:
    if not importlib.util.find_spec("psutil"):
        pytest.skip("psutil not installed")
    # PID 999_999_999 almost certainly doesn't exist
    r = await AutomationTools().invoke(_call("process_kill", {
        "pid": 999_999_999,
    }))
    assert r.ok is False
    assert "no process" in (r.error or "")
