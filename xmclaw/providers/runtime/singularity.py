"""SingularitySkillRuntime — HPC-container skill execution via Singularity.

The fifth sandbox runtime, targeting HPC clusters where Docker is unavailable
but Singularity / Apptainer (https://apptainer.org) is the standard container
runtime.  Behaves very similarly to ``DockerSkillRuntime`` but uses the
``singularity exec`` CLI instead of the Docker SDK.

Useful when:

  * Your compute nodes run Singularity / Apptainer (common on academic HPC).
  * You need to run skills inside an existing SIF image without Docker.
  * You want rootless container execution (Singularity's default).

Security posture
----------------
Singularity runs containers in the caller's user namespace by default —
root inside the container is mapped to the caller's UID on the host.
This is *more* secure than Docker's default rootful mode but *less*
isolated than Docker with user namespaces + seccomp.  Network isolation
is opt-in via ``--net`` (not enabled by default because many HPC
interconnects require host network access).

Requires the ``singularity`` or ``apptainer`` CLI on ``$PATH``.  No Python
SDK dependency — we shell out to the CLI.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
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
from xmclaw.skills.base import Skill, SkillOutput
from xmclaw.skills.manifest import SkillManifest


@dataclass
class _Slot:
    handle: SkillHandle
    manifest: SkillManifest
    proc: asyncio.subprocess.Process | None = None
    output: SkillOutput | None = None
    killed: bool = False
    timed_out: bool = False
    errored: bool = False


class SingularitySkillRuntime(SkillRuntime):
    """Run skills inside a Singularity / Apptainer container.

    Parameters
    ----------
    image : str
        SIF image path or remote URI (default ``docker://python:3.10-slim``).
    singularity_cmd : str
        CLI command name. Auto-detects ``apptainer`` if present, otherwise
        ``singularity``.
    bind_dirs : list[str]
        Additional ``--bind`` mount specs.
    network : bool
        Whether to enable ``--net`` network isolation. Default False
        because HPC networks often break with ``--net``.
    timeout_s : float
        Wall-clock cap for the container run (default 300.0).
    """

    def __init__(
        self,
        *,
        image: str = "docker://python:3.10-slim",
        singularity_cmd: str | None = None,
        bind_dirs: list[str] | None = None,
        network: bool = False,
        timeout_s: float = 300.0,
    ) -> None:
        self._image = image
        self._singularity_cmd = singularity_cmd or self._detect_cmd()
        self._bind_dirs = list(bind_dirs or [])
        self._network = network
        self._timeout_s = float(timeout_s)
        self._slots: dict[str, _Slot] = {}

    # ── helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _detect_cmd() -> str:
        for cmd in ("apptainer", "singularity"):
            if shutil.which(cmd):
                return cmd
        return "singularity"

    def _check_cmd(self) -> None:
        if shutil.which(self._singularity_cmd) is None:
            raise RuntimeError(
                f"SingularitySkillRuntime cannot find '{self._singularity_cmd}' on PATH. "
                "Install Apptainer (https://apptainer.org) or Singularity."
            )

    def _build_cmd(
        self,
        skill_dir: Path,
        input_file: Path,
    ) -> list[str]:
        cmd = [
            self._singularity_cmd,
            "exec",
            "--cleanenv",
        ]
        if self._network:
            cmd.append("--net")
        for b in self._bind_dirs:
            cmd.extend(["--bind", b])
        # Bind the skill directory and input file into the container.
        cmd.extend(["--bind", f"{skill_dir}:/skill:ro"])
        cmd.extend(["--bind", f"{input_file}:/_input.json:ro"])
        cmd.append(self._image)
        # The container entrypoint runs the bootstrap inline.
        cmd.extend([
            "python3", "-c",
            (
                "import json, sys, traceback; "
                "sys.path.insert(0, '/skill'); "
                "try:\n"
                "    from xmclaw.skills.base import SkillInput\n"
                "except ImportError:\n"
                "    SkillInput = dict\n"
                "with open('/_input.json') as f:\n"
                "    raw = json.load(f)\n"
                "try:\n"
                "    import skill as _skill_mod\n"
                "    result = _skill_mod.run(SkillInput(args=raw['args']))\n"
                "    out = {'tag': 'ok', 'output': result}\n"
                "except Exception as e:\n"
                "    out = {'tag': 'skill_error', 'error': traceback.format_exc()}\n"
                "print(json.dumps(out, ensure_ascii=False))\n"
            ),
        ])
        return cmd

    # ── contract ─────────────────────────────────────────────────────

    def enforce_manifest(self, manifest: SkillManifest) -> None:
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
        slot = _Slot(handle=handle, manifest=manifest)
        self._slots[handle.id] = slot

        # Prepare skill dir + input JSON.
        skill_dir = Path(skill.source_dir)
        tmp = Path(tempfile.mkdtemp(prefix="xmclaw-singularity-"))
        input_file = tmp / "_input.json"
        input_file.write_text(json.dumps({"args": args}), encoding="utf-8")

        self._check_cmd()
        cmd = self._build_cmd(skill_dir, input_file)

        effective_timeout = manifest.max_cpu_seconds or self._timeout_s

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        slot.proc = proc

        # Schedule a background task to collect output.
        async def _reap() -> SkillOutput:
            try:
                stdout_data, stderr_data = await asyncio.wait_for(
                    proc.communicate(), timeout=effective_timeout,
                )
            except asyncio.TimeoutError:
                slot.timed_out = True
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:  # noqa: BLE001
                    pass
                return SkillOutput(
                    ok=False,
                    result={
                        "error": f"timeout: skill exceeded {effective_timeout}s in Singularity",
                        "kind": "timeout",
                    },
                    side_effects=[],
                )

            if proc.returncode != 0:
                err = stderr_data.decode("utf-8", "replace") or f"exit {proc.returncode}"
                return SkillOutput(
                    ok=False,
                    result={
                        "error": f"Singularity exec failed: {err}",
                        "kind": "singularity_exec_error",
                    },
                    side_effects=[],
                )

            text = stdout_data.decode("utf-8", "replace").strip()
            if not text:
                return SkillOutput(
                    ok=False,
                    result={"error": "container produced no output", "kind": "singularity_empty_output"},
                    side_effects=[],
                )
            try:
                envelope = json.loads(text)
            except json.JSONDecodeError as exc:
                return SkillOutput(
                    ok=False,
                    result={
                        "error": f"container output is not valid JSON: {exc}",
                        "raw": text[:500],
                        "kind": "singularity_json_error",
                    },
                    side_effects=[],
                )

            tag = envelope.get("tag")
            if tag == "ok":
                return SkillOutput(
                    ok=True,
                    result=envelope.get("output", {}),
                    side_effects=[],
                )
            return SkillOutput(
                ok=False,
                result={
                    "error": envelope.get("error", "unknown container error"),
                    "kind": tag or "unknown",
                },
                side_effects=[],
            )

        slot.task = asyncio.create_task(_reap())  # type: ignore[attr-defined]
        return handle

    async def wait(
        self,
        handle: SkillHandle,
        timeout: float | None = None,
    ) -> SkillOutput:
        slot = self._get_slot(handle)
        if slot.output is not None:
            return slot.output
        if slot.task is None:  # type: ignore[attr-defined]
            raise LookupError(f"handle {handle.id} has no task")

        cap = slot.manifest.max_cpu_seconds or None
        effective = cap if timeout is None else (
            min(cap, timeout) if cap else timeout
        )

        try:
            if effective is not None and effective > 0:
                output = await asyncio.wait_for(
                    asyncio.shield(slot.task), timeout=effective,  # type: ignore[attr-defined]
                )
            else:
                output = await slot.task  # type: ignore[attr-defined]
            slot.output = output
            return output
        except asyncio.TimeoutError:
            slot.timed_out = True
            if slot.proc is not None and slot.proc.returncode is None:
                try:
                    slot.proc.kill()
                    await slot.proc.wait()
                except Exception:  # noqa: BLE001
                    pass
            slot.output = SkillOutput(
                ok=False,
                result={
                    "error": f"timeout: skill exceeded {effective}s in Singularity",
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

    async def kill(self, handle: SkillHandle) -> None:
        slot = self._get_slot(handle)
        if slot.proc is not None and slot.proc.returncode is None:
            slot.killed = True
            try:
                slot.proc.kill()
                await slot.proc.wait()
            except Exception:  # noqa: BLE001
                pass
        task = getattr(slot, "task", None)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def status(self, handle: SkillHandle) -> SkillStatus:
        slot = self._get_slot(handle)
        task = getattr(slot, "task", None)
        if task is None:
            return SkillStatus.PENDING
        if not task.done():
            return SkillStatus.RUNNING
        if slot.timed_out:
            return SkillStatus.TIMEOUT
        if slot.killed:
            return SkillStatus.KILLED
        if slot.errored:
            return SkillStatus.FAILED
        if slot.output is None:
            if task.cancelled():
                return SkillStatus.KILLED
            exc = task.exception()
            if exc is not None:
                return SkillStatus.FAILED
            return SkillStatus.SUCCEEDED
        return SkillStatus.SUCCEEDED if slot.output.ok else SkillStatus.FAILED

    # ── helpers ──────────────────────────────────────────────────────

    def _get_slot(self, handle: SkillHandle) -> _Slot:
        slot = self._slots.get(handle.id)
        if slot is None:
            raise LookupError(f"unknown handle id={handle.id!r}")
        return slot
