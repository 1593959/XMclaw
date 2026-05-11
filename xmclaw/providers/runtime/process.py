"""ProcessSkillRuntime — subprocess-isolated skill execution.

Phase 3.5 delivery. Upgrades the runtime story from "asyncio-task
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
  ✓ Filesystem sandbox (best-effort): each skill runs in a fresh
    temporary directory with cwd locked there and HOME/TEMP redirected.
  ✓ Subprocess sandbox: manifest.permissions_subprocess is enforced
    via a monkey-patched subprocess.Popen guard in the child.
  ✓ Memory soft-cap: a daemon thread in the child monitors RSS via
    psutil and self-terminates when the limit is breached.
  ✓ Environment sanitization: sensitive env vars (KEY, SECRET, TOKEN,
    PASSWORD, AUTH, CREDENTIAL, PRIVATE) are stripped before the child
    starts.

Honest scope (what you do NOT get, compared to a docker runtime):
  ✗ No network sandbox. Skill can still hit any URL. Per-skill
    permissions_net is not enforced in this runtime.
  ✗ Filesystem sandbox is cwd+redirect, not a true chroot. A skill
    that uses absolute paths can still escape the temp directory.
  ✗ Memory cap is self-policing (child thread), not a kernel cgroup.
    A pathological skill that allocates faster than the 0.5s poll
    interval may briefly exceed the limit.

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
import os
import pickle
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
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
    result_queue: mp.Queue[Any],
    *,
    work_dir: str | None = None,
    allowed_subprocesses: tuple[str, ...] = (),
    max_memory_mb: int = 0,
) -> None:
    """Runs inside the child process. Unpickles the skill, runs it, ships
    the SkillOutput (or an error envelope) back via the queue.

    Phase 3.5: sandbox setup happens *before* the skill is unpickled so
    even skill-module import-time side effects are contained.
    """
    original_dir = os.getcwd()
    tmp_dir: str | None = None
    try:
        # 1. Filesystem sandbox — fresh temp directory, cwd locked.
        tmp_dir = _make_skill_tmp(work_dir)
        os.chdir(tmp_dir)

        # 2. Environment sanitization — strip secrets, redirect HOME/TEMP.
        _sanitize_env(tmp_dir)

        # 3. Subprocess guard — whitelist only (empty list = block all).
        _install_subprocess_guard(allowed_subprocesses)

        # 4. Memory guard — self-policing daemon thread.
        if max_memory_mb > 0:
            _install_memory_guard(max_memory_mb)

        # ── existing worker logic ──
        try:
            skill = pickle.loads(skill_pickle)
        except Exception as exc:  # noqa: BLE001
            result_queue.put(("unpickle_error", f"{type(exc).__name__}: {exc}"))
            return

        try:
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
        except Exception as exc:  # noqa: BLE001
            result_queue.put((
                "output_pickle_error",
                f"{type(exc).__name__}: output could not be pickled: {exc}",
            ))
    finally:
        os.chdir(original_dir)
        if tmp_dir is not None:
            try:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            except Exception:  # noqa: BLE001
                pass


# ── sandbox helpers (top-level so spawn can import them) ──


def _make_skill_tmp(work_dir: str | None) -> str:
    """Create a fresh temporary directory for the skill."""
    import tempfile
    kwargs: dict[str, Any] = {"prefix": "xmclaw_skill_"}
    if work_dir is not None:
        kwargs["dir"] = work_dir
    return tempfile.mkdtemp(**kwargs)


def _sanitize_env(tmp_dir: str) -> None:
    """Strip sensitive env vars and redirect HOME/TEMP into the sandbox."""
    # Keep only known-harmless + OS-required variables.
    keep = {
        "PATH",
        "SYSTEMROOT",
        "WINDIR",
        "SYSTEMDRIVE",
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONIOENCODING",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "NUMBER_OF_PROCESSORS",
        "PROCESSOR_ARCHITECTURE",
        "OS",
        "COMSPEC",
        "PATHEXT",
    }
    if os.name == "nt":
        keep.update({
            "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA",
            "PUBLIC", "PROGRAMFILES", "PROGRAMFILES(X86)",
            "COMMONPROGRAMFILES", "COMMONPROGRAMFILES(X86)",
        })

    sensitive = ("KEY", "SECRET", "TOKEN", "PASSWORD", "AUTH",
                 "CREDENTIAL", "PRIVATE", "CERT", "SSH_", "AWS_",
                 "AZURE_", "GCP_", "GOOGLE_", "OPENAI_", "ANTHROPIC_")

    new_env: dict[str, str] = {}
    for k, v in os.environ.items():
        ku = k.upper()
        if ku in keep:
            new_env[k] = v
            continue
        if any(ku.startswith(s) or s in ku for s in sensitive):
            continue
        new_env[k] = v

    os.environ.clear()
    os.environ.update(new_env)

    # Redirect home and temp into the sandbox so ~ and $TMP resolve there.
    os.environ["HOME"] = tmp_dir
    os.environ["USERPROFILE"] = tmp_dir
    os.environ["TMPDIR"] = tmp_dir
    os.environ["TEMP"] = tmp_dir
    os.environ["TMP"] = tmp_dir


def _install_subprocess_guard(allowed: tuple[str, ...]) -> None:
    """Monkey-patch subprocess.Popen so only allowed executables run."""
    import subprocess as _subprocess

    allowed_set = set(allowed)
    _orig_popen = _subprocess.Popen

    def _guarded_popen(*args: Any, **kwargs: Any) -> Any:
        cmd = args[0] if args else kwargs.get("args", [])
        if isinstance(cmd, str):
            exe = cmd
        elif isinstance(cmd, (list, tuple)) and len(cmd) > 0:
            exe = cmd[0]
        else:
            exe = str(cmd)
        base = os.path.basename(exe)
        if base not in allowed_set:
            raise PermissionError(
                f"subprocess {base!r} not in manifest "
                f"permissions_subprocess allowlist: {allowed_set}"
            )
        return _orig_popen(*args, **kwargs)

    _subprocess.Popen = _guarded_popen  # type: ignore[assignment]


def _install_memory_guard(max_mb: int) -> None:
    """Start a daemon thread that self-kills the process when RSS > limit."""
    try:
        import psutil
    except ImportError:
        return

    import threading

    proc = psutil.Process(os.getpid())
    limit_bytes = max_mb * 1024 * 1024

    def _watch() -> None:
        while True:
            try:
                if proc.memory_info().rss > limit_bytes:
                    os._exit(77)
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.5)

    threading.Thread(target=_watch, daemon=True).start()


# ── slot (internal runtime state per fork) ──


@dataclass
class _Slot:
    handle: SkillHandle
    process: mp.Process
    queue: mp.Queue[Any]
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

    def __init__(self, work_dir: str | Path | None = None) -> None:
        # Spawn context: portable. Every skill run pays a process-startup
        # cost; if that becomes the bottleneck, later we can add a pool.
        self._ctx = mp.get_context("spawn")
        self._slots: dict[str, _Slot] = {}
        self._work_dir = str(work_dir) if work_dir is not None else None

    def enforce_manifest(self, manifest: SkillManifest) -> None:
        """Validate manifest. Phase 3.5: rejects network-sandbox demands
        when this runtime cannot satisfy them."""
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
        # If the manifest claims enforced permissions but demands a
        # network sandbox, we must refuse — this runtime has no network
        # isolation.
        if (
            manifest.permissions_enforced
            and manifest.permissions_net
        ):
            raise ValueError(
                "ProcessSkillRuntime cannot enforce permissions_net; "
                "use DockerSkillRuntime or set permissions_enforced=False"
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

        queue: mp.Queue[Any] = self._ctx.Queue()
        proc = self._ctx.Process(
            target=_worker_entry,
            args=(skill_pickle, args, queue),
            kwargs={
                "work_dir": self._work_dir,
                "allowed_subprocesses": manifest.permissions_subprocess,
                "max_memory_mb": manifest.max_memory_mb,
            },
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
            # Process died before writing a result (e.g. killed externally
            # or self-terminated due to memory limit).
            exit_code = slot.process.exitcode
            slot.errored = True
            if exit_code == 77:
                slot.output = SkillOutput(
                    ok=False,
                    result={
                        "error": (
                            f"memory limit exceeded: "
                            f"{slot.manifest.max_memory_mb} MB"
                        ),
                        "kind": "memory_limit",
                    },
                    side_effects=[],
                )
            else:
                slot.output = SkillOutput(
                    ok=False,
                    result={
                        "error": "subprocess exited without producing a result",
                        "kind": "subprocess_crash",
                        "exit_code": exit_code,
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
    queue: mp.Queue[Any], process: mp.Process, timeout: float | None,
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
