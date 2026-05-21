"""ModalSkillRuntime — serverless skill execution on Modal.

Runs skills inside Modal Sandboxes (https://modal.com/docs/guide/sandbox),
which are ephemeral containers with configurable CPU, memory, and GPU.
Useful when:

  * You need GPU acceleration for ML inference skills.
  * You want elastic scale — Modal auto-scales from 0.
  * You don't want to manage a persistent Docker daemon locally.

Security posture
----------------
Modal credentials (token ID + token secret) grant access to your Modal
account.  This runtime does NOT add extra sandboxing beyond what Modal
provides natively.  Treat the Modal token as a sensitive credential.

Requires ``modal`` (>=0.63).  Lazy-imported so the module can be imported
without the dep.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import uuid
import zipfile
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
    sandbox_id: str | None = None
    output: SkillOutput | None = None
    killed: bool = False
    timed_out: bool = False
    errored: bool = False


class ModalSkillRuntime(SkillRuntime):
    """Run skills inside ephemeral Modal Sandboxes.

    Parameters
    ----------
    token_id : str | None
        Modal token ID. Falls back to ``MODAL_TOKEN_ID`` env var.
    token_secret : str | None
        Modal token secret. Falls back to ``MODAL_TOKEN_SECRET`` env var.
    image : str
        Container image ref (default ``python:3.10-slim``).
    cpu : float
        vCPU count per sandbox (default 1.0).
    memory : int
        Memory in MiB per sandbox (default 512).
    gpu : str | None
        GPU type, e.g. ``"A10G"``, ``"T4"``, or ``None`` (default).
    timeout_s : float
        Wall-clock cap for the sandbox (default 300.0).
    """

    def __init__(
        self,
        *,
        token_id: str | None = None,
        token_secret: str | None = None,
        image: str = "python:3.10-slim",
        cpu: float = 1.0,
        memory: int = 512,
        gpu: str | None = None,
        timeout_s: float = 300.0,
    ) -> None:
        self._token_id = token_id or os.environ.get("MODAL_TOKEN_ID")
        self._token_secret = token_secret or os.environ.get("MODAL_TOKEN_SECRET")
        self._image = image
        self._cpu = cpu
        self._memory = memory
        self._gpu = gpu
        self._timeout_s = float(timeout_s)
        self._slots: dict[str, _Slot] = {}

    # ── helpers ──────────────────────────────────────────────────────

    def _ensure_modal(self) -> Any:
        try:
            import modal  # type: ignore[import-untyped]
        except ImportError as exc:
            raise RuntimeError(
                "ModalSkillRuntime needs the 'modal' package. "
                "Install with: pip install 'xmclaw[sandbox-modal]' "
                "(or: pip install modal>=0.63)"
            ) from exc
        return modal

    def _pack_skill(self, skill: Skill, manifest: SkillManifest) -> Path:
        tmp = Path(tempfile.mkdtemp(prefix="xmclaw-modal-skill-"))
        archive = tmp / "skill.zip"
        root = Path(skill.source_dir)
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in root.rglob("*"):
                if f.is_file():
                    zf.write(f, f.relative_to(root))
            zf.writestr("_manifest.json", json.dumps(manifest.to_dict()))
        return archive

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

        loop = asyncio.get_running_loop()

        async def _wrapper() -> SkillOutput:
            return await loop.run_in_executor(
                None, self._run_remote, skill, manifest, args, slot,
            )

        slot.task = asyncio.create_task(_wrapper())  # type: ignore[attr-defined]
        return handle

    def _run_remote(
        self,
        skill: Skill,
        manifest: SkillManifest,
        args: dict[str, Any],
        slot: _Slot,
    ) -> SkillOutput:
        """Sync worker executed in a thread pool."""
        modal = self._ensure_modal()
        local_archive: Path | None = None
        try:
            # 1. Authenticate.
            client = modal.Client.from_credentials(
                self._token_id, self._token_secret
            )

            # 2. Pack skill.
            local_archive = self._pack_skill(skill, manifest)

            # 3. Create sandbox.
            sandbox = modal.Sandbox.create(
                client=client,
                image=self._image,
                cpu=self._cpu,
                memory=self._memory,
                gpu=self._gpu,
                timeout=int(self._timeout_s),
            )
            slot.sandbox_id = sandbox.object_id

            # 4. Upload and unpack.
            remote_archive = "/skill.zip"
            sandbox.mount.put_file(str(local_archive), remote_archive)
            exec_unpack = sandbox.exec("bash", "-c", "cd / && unzip -q skill.zip && rm skill.zip")
            exec_unpack.wait()
            if exec_unpack.returncode != 0:
                err = exec_unpack.stdout.read() or exec_unpack.stderr.read() or "unpack failed"
                return SkillOutput(
                    ok=False,
                    result={"error": str(err), "kind": "modal_unpack_error"},
                    side_effects=[],
                )

            # 5. Write input JSON.
            input_json = json.dumps({"args": args})
            exec_input = sandbox.exec("bash", "-c", f"cat > /_input.json << 'EOF'\n{input_json}\nEOF")
            exec_input.wait()

            # 6. Run skill.
            bootstrap = (
                "python3 -c '\n"
                "import json, sys, traceback\n"
                "sys.path.insert(0, \"/\")\n"
                "try:\n"
                "    from xmclaw.skills.base import SkillInput\n"
                "except ImportError:\n"
                "    SkillInput = dict\n"
                "with open(\"/_input.json\") as f:\n"
                "    raw = json.load(f)\n"
                "try:\n"
                "    import skill as _skill_mod\n"
                "    result = _skill_mod.run(SkillInput(args=raw[\"args\"]))\n"
                "    out = {\"tag\": \"ok\", \"output\": result}\n"
                "except Exception as e:\n"
                "    out = {\"tag\": \"skill_error\", \"error\": traceback.format_exc()}\n"
                "print(json.dumps(out, ensure_ascii=False))\n"
                "'"
            )
            exec_run = sandbox.exec("bash", "-c", bootstrap)
            exec_run.wait()

            stdout = exec_run.stdout.read() or ""
            stderr = exec_run.stderr.read() or ""

            if exec_run.returncode != 0:
                return SkillOutput(
                    ok=False,
                    result={
                        "error": f"remote execution failed: {stderr}",
                        "kind": "modal_exec_error",
                    },
                    side_effects=[],
                )

            text = stdout.decode("utf-8", "replace").strip() if isinstance(stdout, bytes) else stdout.strip()
            if not text:
                return SkillOutput(
                    ok=False,
                    result={"error": "remote produced no output", "kind": "modal_empty_output"},
                    side_effects=[],
                )
            try:
                envelope = json.loads(text)
            except json.JSONDecodeError as exc:
                return SkillOutput(
                    ok=False,
                    result={
                        "error": f"remote output is not valid JSON: {exc}",
                        "raw": text[:500],
                        "kind": "modal_json_error",
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
                    "error": envelope.get("error", "unknown remote error"),
                    "kind": tag or "unknown",
                },
                side_effects=[],
            )

        except Exception as exc:  # noqa: BLE001
            return SkillOutput(
                ok=False,
                result={
                    "error": f"{type(exc).__name__}: {exc}",
                    "kind": "modal_exception",
                },
                side_effects=[],
            )
        finally:
            if slot.sandbox_id is not None:
                try:
                    client = modal.Client.from_credentials(
                        self._token_id, self._token_secret
                    )
                    sb = modal.Sandbox.from_id(client, slot.sandbox_id)
                    sb.terminate()
                except Exception:  # noqa: BLE001
                    pass
            if local_archive is not None:
                try:
                    import shutil
                    shutil.rmtree(local_archive.parent, ignore_errors=True)
                except Exception:  # noqa: BLE001
                    pass

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
            slot.task.cancel()  # type: ignore[attr-defined]
            slot.output = SkillOutput(
                ok=False,
                result={
                    "error": f"timeout: skill exceeded {effective}s on Modal",
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
        task = getattr(slot, "task", None)
        if task is None or task.done():
            return
        slot.killed = True
        task.cancel()
        # run_in_executor threads can't be interrupted; the Modal sandbox
        # will be terminated in _run_remote's finally block once the thread
        # naturally unwinds.

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
