"""SSHSkillRuntime — remote skill execution over SSH/SFTP.

The fourth runtime in the SkillRuntime ladder, after ``LocalSkillRuntime``
(asyncio in-process), ``ProcessSkillRuntime`` (multiprocessing spawn), and
``DockerSkillRuntime`` (local container).  This one runs the skill on a
*remote host* reachable via SSH, which is useful when:

  * The target environment is a long-lived server / VM / cloud instance
    with dependencies already installed.
  * You want skills to run closer to data (e.g. a GPU box for ML
    inference, a staging DB host for schema migrations).
  * You need stronger isolation than a local process but can't run
    Docker on the daemon host.

Security posture
----------------
SSH credentials (key or password) grant whatever privileges the remote
user has.  This runtime does NOT add extra sandboxing on top — it is
exactly as secure as the remote user's shell account.  Use a dedicated
service account with limited sudo, chroot, or remote Docker for actual
untrusted workloads.

Requires ``paramiko`` (>=3).  Lazy-imported like Docker SDK so the
module can be imported without the dep.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
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
from xmclaw.skills.base import Skill, SkillInput, SkillOutput
from xmclaw.skills.manifest import SkillManifest


class _ParamikoMissing(Exception):
    """Raised when paramiko is not installed."""


@dataclass
class _Slot:
    handle: SkillHandle
    manifest: SkillManifest
    client: Any | None = None          # paramiko.SSHClient
    remote_dir: str | None = None
    task: asyncio.Task[SkillOutput] | None = None
    output: SkillOutput | None = None
    killed: bool = False
    timed_out: bool = False
    errored: bool = False
    error_kind: str | None = None
    error_msg: str | None = None


class SSHSkillRuntime(SkillRuntime):
    """Run skills on a remote host via SSH + SFTP.

    Parameters
    ----------
    host : str
        Remote hostname or IP.
    port : int
        SSH port (default 22).
    user : str
        Remote username.
    key_path : Path | str | None
        Path to SSH private key.  If None, ``password`` must be set.
    password : str | None
        SSH password.  Only used when ``key_path`` is None.
    remote_work_dir : str
        Base directory on the remote host where skill temp dirs are
        created (default ``/tmp/xmclaw-skills``).
    connect_timeout : float
        SSH connect timeout in seconds (default 10).
    """

    def __init__(
        self,
        host: str,
        port: int = 22,
        user: str | None = None,
        key_path: Path | str | None = None,
        password: str | None = None,
        *,
        remote_work_dir: str = "/tmp/xmclaw-skills",
        connect_timeout: float = 10.0,
    ) -> None:
        self._host = host
        self._port = port
        self._user = user or os.environ.get("USER", "xmclaw")
        self._key_path = Path(key_path) if key_path else None
        self._password = password
        self._remote_work_dir = remote_work_dir
        self._connect_timeout = connect_timeout
        self._slots: dict[str, _Slot] = {}

    # ── helpers ──────────────────────────────────────────────────────

    def _ensure_paramiko(self):
        try:
            import paramiko  # type: ignore[import-untyped]
        except ImportError as exc:
            raise _ParamikoMissing(
                "SSHSkillRuntime needs the 'paramiko' package. "
                "Install with: pip install 'xmclaw[sandbox-ssh]' "
                "(or: pip install paramiko>=3)"
            ) from exc
        return paramiko

    def _connect(self) -> Any:
        """Open one SSH connection.  Caller must close()."""
        paramiko = self._ensure_paramiko()
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kwargs: dict[str, Any] = {
            "hostname": self._host,
            "port": self._port,
            "username": self._user,
            "timeout": self._connect_timeout,
            "banner_timeout": self._connect_timeout,
        }
        if self._key_path and self._key_path.exists():
            kwargs["key_filename"] = str(self._key_path)
        elif self._password:
            kwargs["password"] = self._password
        client.connect(**kwargs)
        return client

    def _pack_skill(self, skill: Skill, manifest: SkillManifest) -> Path:
        """Zip the skill source into a temporary archive for SFTP."""
        tmp = Path(tempfile.mkdtemp(prefix="xmclaw-ssh-skill-"))
        archive = tmp / "skill.zip"
        root = Path(skill.source_dir)
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in root.rglob("*"):
                if f.is_file():
                    zf.write(f, f.relative_to(root))
            # Embed manifest so the remote bootstrapper can read it.
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

        # Launch the SSH work in a background thread because paramiko
        # is sync / blocking.
        loop = asyncio.get_running_loop()

        async def _wrapper() -> SkillOutput:
            return await loop.run_in_executor(
                None, self._run_remote, skill, manifest, args, slot,
            )

        slot.task = asyncio.create_task(_wrapper())
        return handle

    def _run_remote(
        self,
        skill: Skill,
        manifest: SkillManifest,
        args: dict[str, Any],
        slot: _Slot,
    ) -> SkillOutput:
        """Sync worker executed in a thread pool."""
        client: Any | None = None
        remote_dir: str | None = None
        local_archive: Path | None = None
        try:
            client = self._connect()
            sftp = client.open_sftp()

            # 1. Create remote temp dir.
            remote_dir = f"{self._remote_work_dir}/{slot.handle.id}"
            try:
                sftp.mkdir(remote_dir)
            except OSError:
                # parent might not exist
                stdin, stdout, stderr = client.exec_command(
                    f"mkdir -p {remote_dir}"
                )
                stdout.channel.recv_exit_status()

            # 2. Pack + upload skill.
            local_archive = self._pack_skill(skill, manifest)
            remote_archive = f"{remote_dir}/skill.zip"
            sftp.put(str(local_archive), remote_archive)

            # 3. Unzip on remote.
            stdin, stdout, stderr = client.exec_command(
                f"cd {remote_dir} && unzip -q skill.zip && rm skill.zip"
            )
            exit_status = stdout.channel.recv_exit_status()
            if exit_status != 0:
                err = stderr.read().decode("utf-8", "replace")
                return SkillOutput(
                    ok=False,
                    result={
                        "error": f"remote unzip failed (exit {exit_status}): {err}",
                        "kind": "ssh_unpack_error",
                    },
                    side_effects=[],
                )

            # 4. Write input JSON.
            input_json = json.dumps({"args": args})
            stdin, stdout, stderr = client.exec_command(
                f"cat > {remote_dir}/_input.json << 'XMC_EOF'\n{input_json}\nXMC_EOF"
            )
            stdout.channel.recv_exit_status()

            # 5. Build & execute the bootstrap command.
            # The bootstrapper mimics ProcessSkillRuntime's envelope:
            #   python -c "...import skill, run with SkillInput..."
            bootstrap = (
                f"cd {remote_dir} && "
                f"python3 -c '\n"
                "import json, sys, traceback\n"
                "from pathlib import Path\n"
                "sys.path.insert(0, str(Path.cwd()))\n"
                "\n"
                "try:\n"
                "    from xmclaw.skills.base import SkillInput\n"
                "except ImportError:\n"
                "    SkillInput = dict\n"
                "\n"
                "with open(\"_input.json\") as f:\n"
                "    raw = json.load(f)\n"
                "\n"
                "try:\n"
                "    import skill as _skill_mod\n"
                "    result = _skill_mod.run(SkillInput(args=raw[\"args\"]))\n"
                "    out = {\"tag\": \"ok\", \"output\": result}\n"
                "except Exception as e:\n"
                "    out = {\"tag\": \"skill_error\", \"error\": traceback.format_exc()}\n"
                "\n"
                "print(json.dumps(out, ensure_ascii=False))\n"
                "'"
            )
            effective_timeout = manifest.max_cpu_seconds or 300.0
            channel = client.get_transport().open_session()
            channel.settimeout(effective_timeout + 5.0)
            channel.exec_command(bootstrap)
            start = time.monotonic()

            # Collect stdout / stderr with timeout awareness.
            stdout_data = b""
            stderr_data = b""
            while not channel.exit_status_ready():
                if channel.recv_ready():
                    stdout_data += channel.recv(4096)
                if channel.recv_stderr_ready():
                    stderr_data += channel.recv_stderr(4096)
                if time.monotonic() - start > effective_timeout:
                    channel.close()
                    return SkillOutput(
                        ok=False,
                        result={
                            "error": f"timeout: skill exceeded {effective_timeout}s on remote host",
                            "kind": "timeout",
                        },
                        side_effects=[],
                    )
                time.sleep(0.05)

            # Drain remaining.
            while channel.recv_ready():
                stdout_data += channel.recv(4096)
            while channel.recv_stderr_ready():
                stderr_data += channel.recv_stderr(4096)

            exit_status = channel.recv_exit_status()
            channel.close()

            if exit_status != 0:
                err = stderr_data.decode("utf-8", "replace") or f"exit {exit_status}"
                return SkillOutput(
                    ok=False,
                    result={
                        "error": f"remote execution failed: {err}",
                        "kind": "ssh_exec_error",
                    },
                    side_effects=[],
                )

            # Parse envelope.
            text = stdout_data.decode("utf-8", "replace").strip()
            if not text:
                return SkillOutput(
                    ok=False,
                    result={"error": "remote produced no output", "kind": "ssh_empty_output"},
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
                        "kind": "ssh_json_error",
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

        except _ParamikoMissing as exc:
            return SkillOutput(
                ok=False,
                result={"error": str(exc), "kind": "missing_dependency"},
                side_effects=[],
            )
        except Exception as exc:  # noqa: BLE001
            return SkillOutput(
                ok=False,
                result={
                    "error": f"{type(exc).__name__}: {exc}",
                    "kind": "ssh_exception",
                },
                side_effects=[],
            )
        finally:
            # 6. Cleanup remote temp dir (best-effort).
            if client is not None and remote_dir is not None:
                try:
                    stdin, stdout, stderr = client.exec_command(
                        f"rm -rf {remote_dir}"
                    )
                    stdout.channel.recv_exit_status()
                except Exception:  # noqa: BLE001
                    pass
            if client is not None:
                try:
                    client.close()
                except Exception:  # noqa: BLE001
                    pass
            # Cleanup local temp archive.
            if local_archive is not None:
                try:
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
        if slot.task is None:
            raise LookupError(f"handle {handle.id} has no task")

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
                    "error": f"timeout: skill exceeded {effective}s on remote host",
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
        if slot.task is None or slot.task.done():
            return
        slot.killed = True
        slot.task.cancel()
        # NOTE: run_in_executor threads cannot be interrupted from Python.
        # The thread will finish on its own; ``wait()`` handles the
        # CancelledError when it is called.  We do NOT ``await slot.task``
        # here because that would block until the remote SSH session
        # naturally ends (which may be never for a long-running skill).

    async def status(self, handle: SkillHandle) -> SkillStatus:
        slot = self._get_slot(handle)
        if slot.task is None:
            return SkillStatus.PENDING
        if not slot.task.done():
            return SkillStatus.RUNNING
        if slot.timed_out:
            return SkillStatus.TIMEOUT
        if slot.killed:
            return SkillStatus.KILLED
        if slot.errored:
            return SkillStatus.FAILED
        if slot.output is None:
            if slot.task.cancelled():
                return SkillStatus.KILLED
            exc = slot.task.exception()
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
