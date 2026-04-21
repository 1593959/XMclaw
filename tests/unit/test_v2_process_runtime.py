"""ProcessSkillRuntime — unit tests.

All test skills are imported from ``xmclaw.skills.demo.picklable_demo``
(top-level module) so ``multiprocessing``'s spawn context can pickle
them. Do NOT define test skills as nested classes in this file —
spawn would fail to import them by name.
"""
from __future__ import annotations

import asyncio

import pytest

from xmclaw.providers.runtime import ProcessSkillRuntime, SkillStatus
from xmclaw.providers.runtime.base import SkillHandle
from xmclaw.skills.demo.picklable_demo import (
    PickleEcho,
    PickleRaising,
    PickleSlow,
)
from xmclaw.skills.manifest import SkillManifest


def _m(id_: str, v: int = 1, *, max_cpu: float = 15.0) -> SkillManifest:
    return SkillManifest(id=id_, version=v, max_cpu_seconds=max_cpu)


# ── enforce_manifest ─────────────────────────────────────────────────────


def test_enforce_manifest_accepts_well_formed() -> None:
    ProcessSkillRuntime().enforce_manifest(_m("x"))


def test_enforce_manifest_rejects_negative_cpu() -> None:
    with pytest.raises(ValueError, match="max_cpu_seconds"):
        ProcessSkillRuntime().enforce_manifest(
            SkillManifest(id="x", version=1, max_cpu_seconds=-1),
        )


# ── fork + wait happy path ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fork_wait_returns_skill_output_from_child_process() -> None:
    rt = ProcessSkillRuntime()
    try:
        handle = await rt.fork(PickleEcho(), _m("demo.pickle_echo"), args={"x": 1})
        out = await rt.wait(handle)
        assert out.ok is True
        assert out.result == {"echoed": {"x": 1}}
        assert await rt.status(handle) == SkillStatus.SUCCEEDED
    finally:
        rt.shutdown()


@pytest.mark.asyncio
async def test_handle_carries_real_child_pid() -> None:
    rt = ProcessSkillRuntime()
    try:
        handle = await rt.fork(PickleEcho(), _m("demo.pickle_echo"), args={})
        assert handle.pid is not None
        assert handle.pid > 0
        await rt.wait(handle)
    finally:
        rt.shutdown()


# ── manifest / skill mismatch ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fork_refuses_manifest_mismatch() -> None:
    rt = ProcessSkillRuntime()
    try:
        with pytest.raises(ValueError, match="manifest/skill identity"):
            await rt.fork(PickleEcho(), _m("wrong_id"), args={})
    finally:
        rt.shutdown()


# ── timeout (real: the subprocess is SIGKILLed) ──────────────────────────


@pytest.mark.asyncio
async def test_timeout_kills_subprocess_and_returns_structured_output() -> None:
    rt = ProcessSkillRuntime()
    try:
        # Cap at 1.0s; child sleeps 10s. Real CPU timeout kicks in.
        handle = await rt.fork(
            PickleSlow(10.0), _m("demo.pickle_slow", max_cpu=1.0), args={},
        )
        out = await rt.wait(handle)
        assert out.ok is False
        assert "timeout" in out.result["error"].lower()
        assert await rt.status(handle) == SkillStatus.TIMEOUT
    finally:
        rt.shutdown()


@pytest.mark.asyncio
async def test_wait_timeout_overrides_manifest_when_smaller() -> None:
    rt = ProcessSkillRuntime()
    try:
        handle = await rt.fork(
            PickleSlow(10.0), _m("demo.pickle_slow", max_cpu=30.0), args={},
        )
        out = await rt.wait(handle, timeout=0.5)
        assert out.ok is False
        assert "timeout" in out.result["error"].lower()
    finally:
        rt.shutdown()


# ── kill: real process termination ───────────────────────────────────────


@pytest.mark.asyncio
async def test_kill_terminates_running_subprocess() -> None:
    rt = ProcessSkillRuntime()
    try:
        handle = await rt.fork(
            PickleSlow(10.0), _m("demo.pickle_slow", max_cpu=30.0), args={},
        )
        # Let the child actually start.
        await asyncio.sleep(0.3)
        assert await rt.status(handle) == SkillStatus.RUNNING
        await rt.kill(handle)
        # Confirm the child is now gone and status reflects it.
        assert await rt.status(handle) == SkillStatus.KILLED
    finally:
        rt.shutdown()


@pytest.mark.asyncio
async def test_kill_idempotent_on_finished_skill() -> None:
    rt = ProcessSkillRuntime()
    try:
        handle = await rt.fork(PickleEcho(), _m("demo.pickle_echo"), args={})
        await rt.wait(handle)
        # Kill after natural completion — no raise.
        await rt.kill(handle)
        assert await rt.status(handle) == SkillStatus.SUCCEEDED
    finally:
        rt.shutdown()


# ── skill crash in child ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_child_raising_surfaces_as_failed() -> None:
    rt = ProcessSkillRuntime()
    try:
        handle = await rt.fork(
            PickleRaising(), _m("demo.pickle_raising"), args={},
        )
        out = await rt.wait(handle)
        assert out.ok is False
        assert "intentional failure" in out.result["error"]
        assert out.result["kind"] == "skill_error"
        assert await rt.status(handle) == SkillStatus.FAILED
    finally:
        rt.shutdown()


# ── unpicklable skill: structured failure at fork ────────────────────────


class _UnpicklableSkill:
    """Defined here on purpose — nested inside a test module ⇒ spawn can't
    import it. Pickling fails → fork returns a stub handle whose wait()
    yields a structured error."""

    id = "unpicklable"
    version = 1

    def __init__(self) -> None:
        # A lambda that cannot be pickled — forces pickle.dumps to fail.
        self._lambda = lambda x: x


@pytest.mark.asyncio
async def test_unpicklable_skill_surfaces_structured_error() -> None:
    rt = ProcessSkillRuntime()
    try:
        handle = await rt.fork(
            _UnpicklableSkill(),  # type: ignore[arg-type] — shape matches Skill
            _m("unpicklable"), args={},
        )
        out = await rt.wait(handle)
        assert out.ok is False
        assert out.result["kind"] == "pickle_error"
        # Never raised at the caller; we got a ToolOutput.
    finally:
        rt.shutdown()


# ── unknown handle ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_kill_unknown_handle_raises() -> None:
    rt = ProcessSkillRuntime()
    with pytest.raises(LookupError):
        await rt.kill(SkillHandle(id="nope", skill_id="x", version=1))
