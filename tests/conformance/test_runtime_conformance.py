"""Parametric SkillRuntime conformance — anti-req #10, CI-conformance.

Every ``SkillRuntime`` implementation (local, docker, ssh, modal, ...)
must pass this matrix. Phase 3.2 registers ``LocalSkillRuntime``; later
runtimes (process-isolated, container-isolated, remote-remote-exec)
plug into the same builder registry.

Contract points this suite verifies:
  * fork(...) returns quickly (doesn't block on skill completion)
  * wait(...) returns a SkillOutput (never raises asyncio.TimeoutError
    up to the caller)
  * status(...) matches SkillOutput.ok after completion
  * kill(...) moves status to KILLED and is idempotent
  * enforce_manifest(...) rejects structurally invalid manifests
  * manifest.max_cpu_seconds caps runtime duration — timeouts produce
    ok=False with status=TIMEOUT, NOT propagating asyncio.TimeoutError
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

import pytest
import pytest_asyncio

from xmclaw.providers.runtime import LocalSkillRuntime, SkillRuntime, SkillStatus
from xmclaw.skills.base import Skill, SkillInput, SkillOutput
from xmclaw.skills.manifest import SkillManifest


# ── test-support skills ──────────────────────────────────────────────────


class _QuickSkill(Skill):
    id = "quick"
    version = 1

    async def run(self, inp: SkillInput) -> SkillOutput:
        return SkillOutput(ok=True, result={"args": inp.args}, side_effects=[])


class _SlowSkill(Skill):
    id = "slow"
    version = 1

    def __init__(self, duration: float) -> None:
        self._duration = duration

    async def run(self, inp: SkillInput) -> SkillOutput:  # noqa: ARG002
        await asyncio.sleep(self._duration)
        return SkillOutput(
            ok=True, result={"slept": self._duration}, side_effects=[],
        )


def _m(id_: str, v: int = 1, *, max_cpu: float = 5.0) -> SkillManifest:
    return SkillManifest(id=id_, version=v, max_cpu_seconds=max_cpu)


# ── runtime fixture registry ─────────────────────────────────────────────


@dataclass
class _RuntimeFixture:
    runtime: SkillRuntime
    name: str


async def _build_local() -> _RuntimeFixture:
    return _RuntimeFixture(runtime=LocalSkillRuntime(), name="local")


_RUNTIME_BUILDERS: list[tuple[str, Callable[[], Any]]] = [
    ("local", _build_local),
]


@pytest_asyncio.fixture(
    params=[b for _, b in _RUNTIME_BUILDERS],
    ids=[i for i, _ in _RUNTIME_BUILDERS],
)
async def runtime(request: Any) -> AsyncIterator[_RuntimeFixture]:
    builder = request.param
    f = await builder()
    yield f


# ── conformance tests ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fork_returns_before_skill_finishes(runtime: _RuntimeFixture) -> None:
    handle = await runtime.runtime.fork(
        _SlowSkill(duration=0.2), _m("slow"), args={},
    )
    # Status must be RUNNING immediately after fork.
    s = await runtime.runtime.status(handle)
    assert s == SkillStatus.RUNNING
    # Cleanup: wait so the asyncio loop has nothing leftover.
    await runtime.runtime.wait(handle)


@pytest.mark.asyncio
async def test_wait_returns_skill_output_with_args(runtime: _RuntimeFixture) -> None:
    handle = await runtime.runtime.fork(
        _QuickSkill(), _m("quick"), args={"x": 1},
    )
    out = await runtime.runtime.wait(handle)
    assert out.ok is True
    assert out.result == {"args": {"x": 1}}
    assert await runtime.runtime.status(handle) == SkillStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_timeout_produces_ok_false_not_exception(runtime: _RuntimeFixture) -> None:
    """The runtime must NEVER propagate asyncio.TimeoutError to the caller.
    A timeout MUST surface as ``SkillOutput(ok=False, ...)``."""
    handle = await runtime.runtime.fork(
        _SlowSkill(duration=5.0), _m("slow", max_cpu=0.1), args={},
    )
    out = await runtime.runtime.wait(handle)
    assert out.ok is False
    assert "timeout" in out.result["error"].lower()
    assert await runtime.runtime.status(handle) == SkillStatus.TIMEOUT


@pytest.mark.asyncio
async def test_kill_transitions_status_to_killed(runtime: _RuntimeFixture) -> None:
    handle = await runtime.runtime.fork(
        _SlowSkill(duration=5.0), _m("slow", max_cpu=10.0), args={},
    )
    await asyncio.sleep(0.05)
    await runtime.runtime.kill(handle)
    assert await runtime.runtime.status(handle) == SkillStatus.KILLED


@pytest.mark.asyncio
async def test_kill_is_idempotent(runtime: _RuntimeFixture) -> None:
    handle = await runtime.runtime.fork(_QuickSkill(), _m("quick"), args={})
    await runtime.runtime.wait(handle)
    await runtime.runtime.kill(handle)   # on finished handle — no raise
    await runtime.runtime.kill(handle)   # twice in a row — no raise


@pytest.mark.asyncio
async def test_enforce_manifest_rejects_negative_cpu(runtime: _RuntimeFixture) -> None:
    with pytest.raises(ValueError):
        runtime.runtime.enforce_manifest(
            SkillManifest(id="x", version=1, max_cpu_seconds=-1),
        )


@pytest.mark.asyncio
async def test_manifest_skill_identity_mismatch_rejected(runtime: _RuntimeFixture) -> None:
    with pytest.raises(ValueError):
        await runtime.runtime.fork(
            _QuickSkill(), _m("wrong_id"), args={},
        )
