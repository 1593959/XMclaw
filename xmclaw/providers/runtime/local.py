"""LocalSkillRuntime — in-process async execution with CPU timeouts.

See ``xmclaw.providers.runtime.__init__`` for the honest scope.

Key design choices:

* Each ``fork`` wraps ``skill.run(SkillInput(args=args))`` in an
  ``asyncio.Task``. The task runs concurrently; ``fork`` returns a
  handle immediately.
* ``wait(handle)`` awaits the task, optionally capped by
  ``manifest.max_cpu_seconds`` (via ``asyncio.wait_for``). A timeout
  cancels the task and returns a ``SkillOutput(ok=False, ...)`` — the
  runtime never raises ``asyncio.TimeoutError`` at the caller.
* ``kill`` cancels the task. Idempotent on already-finished handles.
* ``status`` maps asyncio-task state to ``SkillStatus``.

The handle's ``id`` is UUIDv4. ``pid`` is always None here (in-process).
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any

from xmclaw.providers.runtime.base import (
    SkillHandle,
    SkillRuntime,
    SkillStatus,
)
from xmclaw.skills.base import Skill, SkillInput, SkillOutput
from xmclaw.skills.manifest import SkillManifest


@dataclass
class _Slot:
    handle: SkillHandle
    task: asyncio.Task[SkillOutput]
    manifest: SkillManifest
    # Set once the task finishes — avoids re-awaiting an already-awaited task
    # in repeated wait() calls.
    output: SkillOutput | None = None
    killed: bool = False
    timed_out: bool = False
    errored: bool = False
    error_kind: str | None = None
    error_msg: str | None = None
    _result_cached: bool = field(default=False, init=False)


class LocalSkillRuntime(SkillRuntime):
    """In-process asyncio-task runtime with CPU-timeout enforcement."""

    def __init__(self) -> None:
        self._slots: dict[str, _Slot] = {}

    # ── contract ──

    def enforce_manifest(self, manifest: SkillManifest) -> None:
        """Reject structurally invalid manifests; no sandbox enforcement.

        Sandboxed subprocess/docker runtimes override this to reject
        disallowed fs/net/subprocess entries up-front. In-process we can
        only check that the manifest's declared limits are non-negative
        — a skill author who writes ``max_cpu_seconds=-1`` is asking
        for a crash.
        """
        if manifest.max_cpu_seconds < 0:
            raise ValueError(
                f"manifest.max_cpu_seconds must be >= 0, got {manifest.max_cpu_seconds}"
            )
        if manifest.max_memory_mb < 0:
            raise ValueError(
                f"manifest.max_memory_mb must be >= 0, got {manifest.max_memory_mb}"
            )

    async def fork(
        self,
        skill: Skill,
        manifest: SkillManifest,
        args: dict[str, Any],
    ) -> SkillHandle:
        self.enforce_manifest(manifest)
        if manifest.id != skill.id or manifest.version != skill.version:
            raise ValueError(
                f"manifest/skill identity mismatch: skill={skill.id}v{skill.version} "
                f"manifest={manifest.id}v{manifest.version}"
            )

        handle = SkillHandle(
            id=uuid.uuid4().hex,
            skill_id=skill.id,
            version=skill.version,
            pid=None,
        )
        task = asyncio.create_task(skill.run(SkillInput(args=args)))
        self._slots[handle.id] = _Slot(handle=handle, task=task, manifest=manifest)
        return handle

    async def wait(
        self,
        handle: SkillHandle,
        timeout: float | None = None,
    ) -> SkillOutput:
        slot = self._get_slot(handle)
        if slot.output is not None:
            return slot.output

        # Effective timeout: min(manifest.max_cpu_seconds, timeout).
        # max_cpu_seconds=0 means "no runtime cap" for in-process.
        cap = slot.manifest.max_cpu_seconds or None
        effective = cap if timeout is None else (
            min(cap, timeout) if cap else timeout
        )

        try:
            if effective is not None and effective > 0:
                output = await asyncio.wait_for(
                    asyncio.shield(slot.task), timeout=effective,
                )
            else:
                output = await slot.task
            slot.output = output
            return output
        except asyncio.TimeoutError:
            slot.timed_out = True
            slot.task.cancel()
            try:
                await slot.task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            slot.output = SkillOutput(
                ok=False,
                result={
                    "error": f"timeout: skill exceeded {effective}s cpu budget",
                    "kind": "timeout",
                },
                side_effects=[],
            )
            return slot.output
        except asyncio.CancelledError:
            slot.killed = True
            slot.output = SkillOutput(
                ok=False,
                result={"error": "killed", "kind": "killed"},
                side_effects=[],
            )
            return slot.output
        except Exception as exc:  # noqa: BLE001 — skill crash surfaces here
            slot.errored = True
            slot.error_kind = type(exc).__name__
            slot.error_msg = str(exc)
            slot.output = SkillOutput(
                ok=False,
                result={
                    "error": f"{slot.error_kind}: {slot.error_msg}",
                    "kind": "exception",
                },
                side_effects=[],
            )
            return slot.output

    async def kill(self, handle: SkillHandle) -> None:
        slot = self._get_slot(handle)
        if slot.task.done():
            return  # idempotent
        slot.killed = True
        slot.task.cancel()
        # Wait for cancellation to settle so status() is consistent afterwards.
        try:
            await slot.task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    async def status(self, handle: SkillHandle) -> SkillStatus:
        slot = self._get_slot(handle)
        if not slot.task.done():
            return SkillStatus.RUNNING
        if slot.timed_out:
            return SkillStatus.TIMEOUT
        if slot.killed:
            return SkillStatus.KILLED
        if slot.errored:
            return SkillStatus.FAILED
        # Finished without our observation — call wait() to populate.
        if slot.output is None:
            # wait hasn't run; infer from the task's raw state
            if slot.task.cancelled():
                return SkillStatus.KILLED
            exc = slot.task.exception()
            if exc is not None:
                return SkillStatus.FAILED
            return SkillStatus.SUCCEEDED
        return SkillStatus.SUCCEEDED if slot.output.ok else SkillStatus.FAILED

    # ── helpers ──

    def _get_slot(self, handle: SkillHandle) -> _Slot:
        slot = self._slots.get(handle.id)
        if slot is None:
            raise LookupError(f"unknown handle id={handle.id!r}")
        return slot
