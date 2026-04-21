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

from xmclaw.providers.runtime import (
    LocalSkillRuntime,
    ProcessSkillRuntime,
    SkillRuntime,
    SkillStatus,
)
from xmclaw.skills.demo.picklable_demo import PickleEcho, PickleSlow
from xmclaw.skills.manifest import SkillManifest


# ── test-support skills ──────────────────────────────────────────────────
#
# These live at ``xmclaw/skills/demo/picklable_demo.py`` because
# ProcessSkillRuntime must be able to pickle them across a ``spawn``
# boundary — nested/test-file-local classes do NOT survive pickle.
# Both runtimes (Local and Process) accept them.


def _m_echo(*, max_cpu: float = 15.0) -> SkillManifest:
    return SkillManifest(id=PickleEcho.id, version=1, max_cpu_seconds=max_cpu)


def _m_slow(*, max_cpu: float = 15.0) -> SkillManifest:
    return SkillManifest(id=PickleSlow.id, version=1, max_cpu_seconds=max_cpu)


# ── runtime fixture registry ─────────────────────────────────────────────


@dataclass
class _RuntimeFixture:
    runtime: SkillRuntime
    name: str


async def _build_local() -> _RuntimeFixture:
    return _RuntimeFixture(runtime=LocalSkillRuntime(), name="local")


async def _build_process() -> _RuntimeFixture:
    return _RuntimeFixture(runtime=ProcessSkillRuntime(), name="process")


_RUNTIME_BUILDERS: list[tuple[str, Callable[[], Any]]] = [
    ("local", _build_local),
    ("process", _build_process),
]


@pytest_asyncio.fixture(
    params=[b for _, b in _RUNTIME_BUILDERS],
    ids=[i for i, _ in _RUNTIME_BUILDERS],
)
async def runtime(request: Any) -> AsyncIterator[_RuntimeFixture]:
    builder = request.param
    f = await builder()
    try:
        yield f
    finally:
        shutdown = getattr(f.runtime, "shutdown", None)
        if shutdown is not None:
            shutdown()


# ── conformance tests ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fork_returns_before_skill_finishes(runtime: _RuntimeFixture) -> None:
    # 2-second sleep is long enough to still be running after spawn overhead
    # (Windows spawn can take ~300-500ms for a fresh Python interpreter).
    handle = await runtime.runtime.fork(
        PickleSlow(duration=2.0), _m_slow(max_cpu=15.0), args={},
    )
    # Let the child actually start (Windows spawn overhead).
    await asyncio.sleep(0.5)
    s = await runtime.runtime.status(handle)
    assert s == SkillStatus.RUNNING
    # Cleanup: drain the pending execution.
    await runtime.runtime.kill(handle)


@pytest.mark.asyncio
async def test_wait_returns_skill_output_with_args(runtime: _RuntimeFixture) -> None:
    handle = await runtime.runtime.fork(
        PickleEcho(), _m_echo(), args={"x": 1},
    )
    out = await runtime.runtime.wait(handle)
    assert out.ok is True
    # PickleEcho returns {"echoed": inp.args}
    assert out.result == {"echoed": {"x": 1}}
    assert await runtime.runtime.status(handle) == SkillStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_timeout_produces_ok_false_not_exception(runtime: _RuntimeFixture) -> None:
    """The runtime must NEVER propagate asyncio.TimeoutError to the caller.
    A timeout MUST surface as ``SkillOutput(ok=False, ...)``."""
    # Cap at 1.0s so subprocess spawn can complete before the deadline;
    # child sleeps 10s so the timeout definitely fires.
    handle = await runtime.runtime.fork(
        PickleSlow(duration=10.0), _m_slow(max_cpu=1.0), args={},
    )
    out = await runtime.runtime.wait(handle)
    assert out.ok is False
    assert "timeout" in out.result["error"].lower()
    assert await runtime.runtime.status(handle) == SkillStatus.TIMEOUT


@pytest.mark.asyncio
async def test_kill_transitions_status_to_killed(runtime: _RuntimeFixture) -> None:
    handle = await runtime.runtime.fork(
        PickleSlow(duration=10.0), _m_slow(max_cpu=30.0), args={},
    )
    # Subprocess needs time to actually start before kill is meaningful.
    await asyncio.sleep(0.5)
    await runtime.runtime.kill(handle)
    assert await runtime.runtime.status(handle) == SkillStatus.KILLED


@pytest.mark.asyncio
async def test_kill_is_idempotent(runtime: _RuntimeFixture) -> None:
    handle = await runtime.runtime.fork(
        PickleEcho(), _m_echo(), args={},
    )
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
            PickleEcho(),
            SkillManifest(id="wrong_id", version=1, max_cpu_seconds=5.0),
            args={},
        )
