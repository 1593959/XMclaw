"""ProcessSkillRuntime — subprocess-isolated skill execution.

Phase 3.4 delivery. Upgrades the runtime story from "asyncio-task
with CPU timeout" (LocalSkillRuntime) to "separate OS process, real
SIGKILL, parent state cannot leak in".

Honest scope (what you gain vs Local):
  ✓ Real process isolation — kill is a real kill, not a cooperative
    cancel. A skill that ignores asyncio.CancelledError still dies.
  ✓ A crashing skill cannot corrupt the parent's Python heap.
  ✓ Each skill gets its own import state — no hidden module-level
    caches shared with the parent.
  ✓ CPU timeout is enforced by Process.join(timeout) + .terminate()
    which goes through the OS.

Honest scope (what you do NOT get, compared to a docker runtime):
  ✗ No filesystem sandbox. A skill inside the subprocess can still
    Path('/').iterdir(). Manifest's permissions_fs is still advisory
    in this runtime.
  ✗ No network sandbox. Skill can still hit any URL.
  ✗ No memory hard cap. Python has no in-stdlib cgroup API;
    manifest.max_memory_mb is advisory here. Real enforcement requires
    Docker / Modal / nsjail — Phase 3.5+ runtimes.

Pickle constraint:
  Skills pass from parent to child through multiprocessing's spawn
  pickle. This means:
    * The Skill class must be importable by name in the child
      (top-level in a module, not a nested / closure class).
    * The Skill's constructor args must be picklable.
    * Live clients that wrap open sockets / SDK handles (e.g. a live
      AnthropicLLM wrapping AsyncAnthropic) WILL fail to pickle.
      Instead, pass config/credentials that the child can use to
      reconstruct the client.

    Skills that cannot be pickled surface as ``SkillOutput(ok=False,
    result={"kind":"pickle_error", ...})`` on fork — the caller is
    never surprised by a PicklingError.
"""
from __future__ import annotations

import asyncio
import multiprocessing as mp
import pickle
import sys
import time
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


# ── worker entry point (must be top-level so it's picklable on spawn) ──


def _worker_entry(
    skill_pickle: bytes,
    args: dict[str, Any],
    result_queue: mp.Queue,
) -> None:
    """Runs inside the child process. Unpickles the skill, runs it, ships
    the SkillOutput (or an error envelope) back via the queue."""
    try:
        skill = pickle.loads(skill_pickle)
    except Exception as exc:  # noqa: BLE001
        result_queue.put(("unpickle_error", f"{type(exc).__name__}: {exc}"))
        return

    try:
        # Each child runs a fresh event loop — no asyncio state leaks from
        # the parent because we're in a separate process.
        loop = asyncio.new_event_loop()
        try:
            output = loop.run_until_complete(skill.run(SkillInput(args=args)))
        finally:
            loop.close()
    except Exception as exc:  # noqa: BLE001
        result_queue.put(("skill_error", f"{type(exc).__name__}: {exc}"))
        return

    try:
        result_queue.put(("ok", output))
    except Exception as exc:  # noqa: BLE001 — output itself may not pickle
        result_queue.put((
            "output_pickle_error",
            f"{type(exc).__name__}: output could not be pickled: {exc}",
        ))


# ── slot (internal runtime state per fork) ──


@dataclass
class _Slot:
    handle: SkillHandle
    process: mp.Process
    queue: mp.Queue
    manifest: SkillManifest
    started_at: float = field(default_factory=time.monotonic)
    output: SkillOutput | None = None
    killed: bool = False
    timed_out: bool = False
    errored: bool = False


# ── runtime ──


class ProcessSkillRuntime(SkillRuntime):
    """Skill execution in a separate OS process. Uses ``multiprocessing``'s
    spawn context for cross-platform behaviour (Windows requires spawn;
    Linux/macOS gain a clean-import child this way too).
    """

    def __init__(self) -> None:
        # Spawn context: portable. Every skill run pays a process-startup
        # cost; if that becomes the bottleneck, later we can add a pool.
        self._ctx = mp.get_context("spawn")
        self._slots: dict[str, _Slot] = {}

    def enforce_manifest(self, manifest: SkillManifest) -> None:
        """Same structural invariants as LocalSkillRuntime. fs/net/memory
        are advisory here; documented in the module docstring."""
        if manifest.max_cpu_seconds < 0:
            raise ValueError(
                f"manifest.max_cpu_seconds must be >= 0, got "
                f"{manifest.max_cpu_seconds}"
            )
        if manifest.max_memory_mb < 0:
            raise ValueError(
                f"manifest.max_memory_mb must be >= 0, got "
                f"{manifest.max_memory_mb}"
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
                f"manifest/skill identity mismatch: skill="
                f"{skill.id}v{skill.version} manifest="
                f"{manifest.id}v{manifest.version}"
            )

        # Pickle the skill up-front. If it fails here, we surface a
        # structured failure via a stub slot that wait() will yield.
        try:
            skill_pickle = pickle.dumps(skill)
        except Exception as exc:  # noqa: BLE001
            # Create a fake handle + stub slot that immediately reports
            # a pickle_error on wait(). This keeps the ABC contract
            # (fork returns a handle; wait returns a SkillOutput).
            handle = SkillHandle(
                id=uuid.uuid4().hex, skill_id=skill.id,
                version=skill.version, pid=None,
            )
            queue = self._ctx.Queue()
            queue.put(("pickle_error",
                       f"{type(exc).__name__}: {exc}"))
            stub_proc = self._ctx.Process(target=_noop)  # not started
            self._slots[handle.id] = _Slot(
                handle=handle, process=stub_proc, queue=queue,
                manifest=manifest,
            )
            return handle

        queue: mp.Queue = self._ctx.Queue()
        proc = self._ctx.Process(
            target=_worker_entry, args=(skill_pickle, args, queue),
        )
        proc.start()
        handle = SkillHandle(
            id=uuid.uuid4().hex,
            skill_id=skill.id,
            version=skill.version,
            pid=proc.pid,
        )
        self._slots[handle.id] = _Slot(
            handle=handle, process=proc, queue=queue, manifest=manifest,
        )
        return handle

    async def wait(
        self,
        handle: SkillHandle,
        timeout: float | None = None,
    ) -> SkillOutput:
        slot = self._get_slot(handle)
        if slot.output is not None:
            return slot.output

        # Effective timeout: min(manifest.max_cpu_seconds, caller's timeout).
        cap = slot.manifest.max_cpu_seconds or None
        effective = cap if timeout is None else (
            min(cap, timeout) if cap else timeout
        )

        # Poll the queue off the event loop so asyncio callers don't block.
        # This is a small wrapper because mp.Queue.get() is synchronous.
        result = await asyncio.to_thread(
            _await_queue, slot.queue, slot.process, effective,
        )

        if result is _TIMEOUT:
            slot.timed_out = True
            _terminate(slot.process)
            slot.output = SkillOutput(
                ok=False,
                result={
                    "error": f"timeout: skill exceeded {effective}s cpu budget",
                    "kind": "timeout",
                },
                side_effects=[],
            )
            return slot.output

        if result is _CLOSED:
            # Process died before writing a result (e.g. killed externally).
            slot.errored = True
            slot.output = SkillOutput(
                ok=False,
                result={
                    "error": "subprocess exited without producing a result",
                    "kind": "subprocess_crash",
                    "exit_code": slot.process.exitcode,
                },
                side_effects=[],
            )
            return slot.output

        # result is a (tag, payload) tuple from the worker.
        tag, payload = result
        if tag == "ok":
            slot.output = payload  # SkillOutput from child
            return slot.output
        if tag == "pickle_error":
            slot.errored = True
            slot.output = SkillOutput(
                ok=False,
                result={"error": payload, "kind": "pickle_error"},
                side_effects=[],
            )
            return slot.output
        if tag == "unpickle_error":
            slot.errored = True
            slot.output = SkillOutput(
                ok=False,
                result={"error": payload, "kind": "unpickle_error"},
                side_effects=[],
            )
            return slot.output
        # Fallback — skill crashed / output couldn't pickle etc.
        slot.errored = True
        slot.output = SkillOutput(
            ok=False,
            result={"error": payload, "kind": tag},
            side_effects=[],
        )
        return slot.output

    async def kill(self, handle: SkillHandle) -> None:
        slot = self._get_slot(handle)
        # Idempotent: if a result is already in hand, the skill completed
        # on its own; kill becomes a no-op and MUST NOT flip the stored
        # status from SUCCEEDED/FAILED to KILLED.
        if slot.output is not None:
            _terminate(slot.process)  # best-effort reap, no status change
            return
        slot.killed = True
        _terminate(slot.process)
        # Drain the process so status() is correct after kill.
        await asyncio.to_thread(slot.process.join, 2.0)

    async def status(self, handle: SkillHandle) -> SkillStatus:
        slot = self._get_slot(handle)
        # Terminal flags (set by wait/kill/timeout) win over liveness
        # probes — ``is_alive()`` can briefly return True during OS-
        # level cleanup after the child writes its result and exits.
        if slot.timed_out:
            return SkillStatus.TIMEOUT
        if slot.killed:
            return SkillStatus.KILLED
        if slot.errored:
            return SkillStatus.FAILED
        if slot.output is not None:
            return SkillStatus.SUCCEEDED if slot.output.ok else SkillStatus.FAILED
        if slot.process.is_alive():
            return SkillStatus.RUNNING
        # Process finished but wait() hasn't been called. Best-effort
        # from exit code.
        if slot.process.exitcode == 0:
            return SkillStatus.SUCCEEDED
        return SkillStatus.FAILED

    def shutdown(self) -> None:
        """Terminate every live child. Useful in test teardown."""
        for slot in self._slots.values():
            _terminate(slot.process)

    # ── helpers ──

    def _get_slot(self, handle: SkillHandle) -> _Slot:
        slot = self._slots.get(handle.id)
        if slot is None:
            raise LookupError(f"unknown handle id={handle.id!r}")
        return slot


# ── module-level helpers (top-level so spawn can import them) ──


def _noop() -> None:
    """Placeholder target for stub processes that never run."""
    pass


_TIMEOUT = object()
_CLOSED = object()


def _await_queue(
    queue: mp.Queue, process: mp.Process, timeout: float | None,
) -> Any:  # noqa: ANN401
    """Synchronous queue poll — safe to run under asyncio.to_thread.

    Returns either a (tag, payload) tuple from the worker, ``_TIMEOUT``
    if the deadline elapsed, or ``_CLOSED`` if the process died before
    writing anything.
    """
    deadline = None if timeout is None else (time.monotonic() + timeout)
    while True:
        remaining = (
            None if deadline is None else max(0.0, deadline - time.monotonic())
        )
        try:
            if remaining == 0.0:
                return _TIMEOUT
            return queue.get(timeout=remaining)
        except Exception:  # noqa: BLE001 — Empty, OSError, etc.
            # Either we timed out on queue.get, or the queue's feeder
            # died. Check process state.
            if not process.is_alive():
                # If something landed between the last poll and now, grab it.
                try:
                    return queue.get_nowait()
                except Exception:  # noqa: BLE001
                    return _CLOSED
            if deadline is not None and time.monotonic() >= deadline:
                return _TIMEOUT
            # Otherwise keep polling.
            continue


def _terminate(process: mp.Process) -> None:
    """Best-effort kill: terminate → short wait → kill if still alive."""
    if not process.is_alive():
        return
    try:
        process.terminate()
    except Exception:  # noqa: BLE001
        pass
    process.join(1.0)
    if process.is_alive():
        try:
            process.kill()
        except Exception:  # noqa: BLE001
            pass
        process.join(1.0)
