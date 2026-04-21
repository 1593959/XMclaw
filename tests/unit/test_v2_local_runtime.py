"""LocalSkillRuntime — unit tests.

Covers fork/wait/kill/status/enforce_manifest on in-process skills.
CPU timeout enforcement is the ONE resource limit we can actually
claim in-process — tests verify it fires and that the handle ends up
in SkillStatus.TIMEOUT.
"""
from __future__ import annotations

import asyncio

import pytest

from xmclaw.providers.runtime import (
    LocalSkillRuntime,
    SkillStatus,
)
from xmclaw.skills.base import Skill, SkillInput, SkillOutput
from xmclaw.skills.manifest import SkillManifest


# ── helper skills ─────────────────────────────────────────────────────────


class _ImmediateSkill(Skill):
    def __init__(self, result: dict) -> None:
        self.id = "immediate"
        self.version = 1
        self._result = result

    async def run(self, inp: SkillInput) -> SkillOutput:
        return SkillOutput(ok=True, result=self._result, side_effects=[])


class _RaisingSkill(Skill):
    id = "raising"
    version = 1

    async def run(self, inp: SkillInput) -> SkillOutput:  # noqa: ARG002
        raise RuntimeError("intentional")


class _SlowSkill(Skill):
    """Sleeps for ``duration`` seconds, useful for timeout/kill tests."""

    id = "slow"
    version = 1

    def __init__(self, duration: float) -> None:
        self._duration = duration

    async def run(self, inp: SkillInput) -> SkillOutput:  # noqa: ARG002
        await asyncio.sleep(self._duration)
        return SkillOutput(ok=True, result={"slept": self._duration}, side_effects=[])


class _EchoSkill(Skill):
    id = "echo"
    version = 1

    async def run(self, inp: SkillInput) -> SkillOutput:
        return SkillOutput(
            ok=True, result={"echoed": inp.args}, side_effects=[],
        )


def _m(id_: str, v: int = 1, *, max_cpu: float = 5.0) -> SkillManifest:
    return SkillManifest(id=id_, version=v, max_cpu_seconds=max_cpu)


# ── enforce_manifest ──────────────────────────────────────────────────────


def test_enforce_manifest_accepts_well_formed() -> None:
    LocalSkillRuntime().enforce_manifest(_m("x"))


def test_enforce_manifest_rejects_negative_cpu_cap() -> None:
    rt = LocalSkillRuntime()
    with pytest.raises(ValueError, match="max_cpu_seconds"):
        rt.enforce_manifest(SkillManifest(id="x", version=1, max_cpu_seconds=-1))


def test_enforce_manifest_rejects_negative_memory() -> None:
    rt = LocalSkillRuntime()
    with pytest.raises(ValueError, match="max_memory_mb"):
        rt.enforce_manifest(SkillManifest(id="x", version=1, max_memory_mb=-1))


# ── fork + wait happy path ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fork_wait_returns_skill_output() -> None:
    rt = LocalSkillRuntime()
    skill = _ImmediateSkill({"answer": 42})
    handle = await rt.fork(skill, _m("immediate"), args={})
    out = await rt.wait(handle)
    assert out.ok
    assert out.result == {"answer": 42}
    assert await rt.status(handle) == SkillStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_fork_passes_args_to_skill() -> None:
    rt = LocalSkillRuntime()
    handle = await rt.fork(_EchoSkill(), _m("echo"), args={"k": "v"})
    out = await rt.wait(handle)
    assert out.result == {"echoed": {"k": "v"}}


@pytest.mark.asyncio
async def test_fork_refuses_manifest_mismatch() -> None:
    rt = LocalSkillRuntime()
    with pytest.raises(ValueError, match="manifest/skill identity"):
        await rt.fork(_ImmediateSkill({}), _m("wrong_id"), args={})


# ── failure paths ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_skill_exception_surfaces_as_ok_false() -> None:
    rt = LocalSkillRuntime()
    handle = await rt.fork(_RaisingSkill(), _m("raising"), args={})
    out = await rt.wait(handle)
    assert out.ok is False
    assert "intentional" in out.result["error"]
    assert await rt.status(handle) == SkillStatus.FAILED


@pytest.mark.asyncio
async def test_wait_twice_returns_same_output_without_re_running() -> None:
    rt = LocalSkillRuntime()
    handle = await rt.fork(_ImmediateSkill({"n": 1}), _m("immediate"), args={})
    a = await rt.wait(handle)
    b = await rt.wait(handle)
    assert a is b   # cached on the slot


# ── timeout enforcement ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_timeout_cancels_slow_skill() -> None:
    rt = LocalSkillRuntime()
    # Cap of 0.2 on a 5s sleep — timeout kicks in.
    handle = await rt.fork(_SlowSkill(5.0), _m("slow", max_cpu=0.2), args={})
    out = await rt.wait(handle)
    assert out.ok is False
    assert "timeout" in out.result["error"].lower()
    assert await rt.status(handle) == SkillStatus.TIMEOUT


@pytest.mark.asyncio
async def test_explicit_wait_timeout_wins_over_manifest_cap() -> None:
    """wait(timeout=X) should be enforced even if manifest allows more."""
    rt = LocalSkillRuntime()
    handle = await rt.fork(_SlowSkill(5.0), _m("slow", max_cpu=10.0), args={})
    out = await rt.wait(handle, timeout=0.1)
    assert out.ok is False
    assert "timeout" in out.result["error"].lower()


@pytest.mark.asyncio
async def test_manifest_cap_wins_over_larger_wait_timeout() -> None:
    """manifest.max_cpu_seconds is a hard ceiling; a larger wait timeout
    does not extend it."""
    rt = LocalSkillRuntime()
    handle = await rt.fork(_SlowSkill(5.0), _m("slow", max_cpu=0.1), args={})
    out = await rt.wait(handle, timeout=10.0)
    assert out.ok is False
    # Did not sleep 10 seconds — the manifest cap fired first.


# ── kill ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_kill_running_skill() -> None:
    rt = LocalSkillRuntime()
    handle = await rt.fork(_SlowSkill(5.0), _m("slow", max_cpu=10.0), args={})
    # Let it start
    await asyncio.sleep(0.05)
    assert await rt.status(handle) == SkillStatus.RUNNING
    await rt.kill(handle)
    assert await rt.status(handle) == SkillStatus.KILLED


@pytest.mark.asyncio
async def test_kill_idempotent_on_finished_skill() -> None:
    rt = LocalSkillRuntime()
    handle = await rt.fork(_ImmediateSkill({}), _m("immediate"), args={})
    await rt.wait(handle)
    # Second kill after natural completion should not raise.
    await rt.kill(handle)
    assert await rt.status(handle) == SkillStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_kill_unknown_handle_raises() -> None:
    from xmclaw.providers.runtime.base import SkillHandle
    rt = LocalSkillRuntime()
    with pytest.raises(LookupError):
        await rt.kill(SkillHandle(id="nope", skill_id="s", version=1))


# ── concurrency ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_forks_run_in_parallel() -> None:
    rt = LocalSkillRuntime()
    # Two sleeps of 0.1s each; if they were serialized this would take 0.2s.
    handles = [
        await rt.fork(_SlowSkill(0.1), _m("slow", max_cpu=5.0), args={})
        for _ in range(2)
    ]
    # Status is RUNNING concurrently right after fork.
    statuses = [await rt.status(h) for h in handles]
    assert statuses == [SkillStatus.RUNNING, SkillStatus.RUNNING]
    import time
    t0 = time.perf_counter()
    for h in handles:
        await rt.wait(h)
    elapsed = time.perf_counter() - t0
    # Should be close to 0.1s (parallel), not 0.2s (sequential).
    assert elapsed < 0.19
