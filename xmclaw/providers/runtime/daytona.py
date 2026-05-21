"""DaytonaSkillRuntime — skill execution inside Daytona workspaces.

Daytona (https://daytona.io) provides on-demand development environments.
This runtime creates a Daytona workspace, uploads skill code, executes it,
and tears the workspace down.

Useful when:

  * You want a full Linux VM/Container with a long-lived filesystem.
  * You need pre-configured dev environments (e.g. specific language
    versions, tools, or IDEs).
  * You want to run skills in an environment that mirrors your CI/CD.

Security posture
----------------
Daytona credentials (API key / server URL) grant access to your Daytona
account.  This runtime does NOT add extra sandboxing beyond what Daytona
provides natively.

Requires ``daytona-sdk``.  Lazy-imported so the module can be imported
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
    workspace_id: str | None = None
    output: SkillOutput | None = None
    killed: bool = False
    timed_out: bool = False
    errored: bool = False


class DaytonaSkillRuntime(SkillRuntime):
    """Run skills inside Daytona workspaces.

    Parameters
    ----------
    api_key : str | None
        Daytona API key. Falls back to ``DAYTONA_API_KEY`` env var.
    server_url : str | None
        Daytona server URL. Falls back to ``DAYTONA_SERVER_URL`` env var.
    target : str
        Daytona target (default ``"local"``).
    template : str
        Workspace template (default ``"python"``).
    timeout_s : float
        Wall-clock cap for workspace execution (default 300.0).
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        server_url: str | None = None,
        target: str = "local",
        template: str = "python",
        timeout_s: float = 300.0,
    ) -> None:
        self._api_key = api_key or os.environ.get("DAYTONA_API_KEY")
        self._server_url = server_url or os.environ.get("DAYTONA_SERVER_URL")
        self._target = target
        self._template = template
        self._timeout_s = float(timeout_s)
        self._slots: dict[str, _Slot] = {}

    # ── helpers ──────────────────────────────────────────────────────

    def _ensure_daytona(self) -> Any:
        try:
            import daytona_sdk  # type: ignore[import-untyped]
        except ImportError as exc:
            raise RuntimeError(
                "DaytonaSkillRuntime needs the 'daytona-sdk' package. "
                "Install with: pip install 'xmclaw[sandbox-daytona]' "
                "(or: pip install daytona-sdk)"
            ) from exc
        return daytona_sdk

    def _pack_skill(self, skill: Skill, manifest: SkillManifest) -> Path:
        tmp = Path(tempfile.mkdtemp(prefix="xmclaw-daytona-skill-"))
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
        daytona_sdk = self._ensure_daytona()
        local_archive: Path | None = None
        workspace = None
        try:
            # 1. Create client.
            client = daytona_sdk.Daytona(
                api_key=self._api_key,
                server_url=self._server_url,
            )

            # 2. Create workspace.
            workspace = client.create_workspace(
                target=self._target,
                template=self._template,
                timeout=self._timeout_s,
            )
            slot.workspace_id = workspace.id

            # 3. Pack and upload skill.
            local_archive = self._pack_skill(skill, manifest)
            remote_archive = "/tmp/skill.zip"
            workspace.upload_file(str(local_archive), remote_archive)

            # 4. Unpack.
            exec_result = workspace.execute_command(
                "cd /tmp && unzip -q skill.zip && rm skill.zip"
            )
            if exec_result.exit_code != 0:
                return SkillOutput(
                    ok=False,
                    result={
                        "error": f"unpack failed: {exec_result.output}",
                        "kind": "daytona_unpack_error",
                    },
                    side_effects=[],
                )

            # 5. Write input JSON.
            input_json = json.dumps({"args": args})
            workspace.execute_command(
                f"cat > /tmp/_input.json << 'EOF'\n{input_json}\nEOF"
            )

            # 6. Run skill.
            bootstrap = (
                "python3 -c '\n"
                "import json, sys, traceback\n"
                "sys.path.insert(0, \"/tmp\")\n"
                "try:\n"
                "    from xmclaw.skills.base import SkillInput\n"
                "except ImportError:\n"
                "    SkillInput = dict\n"
                "with open(\"/tmp/_input.json\") as f:\n"
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
            exec_result = workspace.execute_command(bootstrap)

            if exec_result.exit_code != 0:
                return SkillOutput(
                    ok=False,
                    result={
                        "error": f"remote execution failed: {exec_result.output}",
                        "kind": "daytona_exec_error",
                    },
                    side_effects=[],
                )

            text = (exec_result.output or "").strip()
            if not text:
                return SkillOutput(
                    ok=False,
                    result={"error": "remote produced no output", "kind": "daytona_empty_output"},
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
                        "kind": "daytona_json_error",
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
                    "kind": "daytona_exception",
                },
                side_effects=[],
            )
        finally:
            if workspace is not None:
                try:
                    workspace.remove()
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
                    "error": f"timeout: skill exceeded {effective}s on Daytona",
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
