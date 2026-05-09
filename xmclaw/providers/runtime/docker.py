"""DockerSkillRuntime — container-isolated skill execution.

The third runtime in the SkillRuntime ladder, after ``LocalSkillRuntime``
(asyncio task in-process) and ``ProcessSkillRuntime`` (multiprocessing
spawn). This one runs the skill *inside a Docker container*, which is
the first runtime where ``manifest.permissions_*`` finally mean what
their names imply rather than being advisory.

What you gain vs ProcessSkillRuntime:

  * Filesystem sandbox — container fs is ephemeral; ``read_only=True``
    plus a tmpfs ``/tmp`` cap means the skill can ONLY scribble in
    ``/tmp`` (size-limited) and the read-only mount of its own code.
    Path('/').iterdir() returns the container's rootfs, not the host.
  * Network sandbox — ``network_mode="none"`` cuts the container off
    the host's network namespace. ``urllib.request.urlopen("...")``
    inside the skill fails immediately. Hermes / OpenHands rely on
    this same primitive for skill isolation.
  * Memory hard cap — ``mem_limit="512m"`` is enforced by the kernel
    (cgroup memory controller). OOMKilled child surfaces as a
    structured failure rather than a host-OS swap storm.
  * CPU cap — ``cpu_quota=50000`` (with default ``cpu_period=100000``)
    gives the skill ~50% of one core. Same kernel enforcement.
  * Process containment — a ``rm -rf /`` inside the skill removes the
    container's rootfs view, not yours. The container is destroyed
    on exit either way (``remove=True``).

What you do NOT get:

  * Kernel isolation — Docker shares the host kernel. A kernel-level
    exploit that escapes the container escapes onto the host. Use
    Firecracker/gVisor for that, not us.
  * Free hot-swap — every ``run`` call pulls the image (cached after
    first use), creates a container, copies stdin, waits, reads stdout,
    destroys the container. That's a couple of hundred milliseconds
    per skill on a warm cache. ``LocalSkillRuntime`` is microseconds.
    Pick the right runtime for the threat model: trusted-author skills
    on Local, mid-trust on Process, low-trust / user-evolved on Docker.

Module requires the ``docker`` Python SDK (>=7) AND a running Docker
daemon on the host. Both are runtime requirements only — importing
this module does NOT require docker. The SDK is lazy-imported inside
``DockerSkillRuntime.__init__`` so unit tests + boundary-checks can
mount this module without docker installed. ``pip install
'xmclaw[sandbox-docker]'`` ships the SDK.

Skill packaging contract (B-385):

  Each skill is mounted into the container at ``/skill`` (read-only).
  The container's entrypoint reads a JSON-encoded ``SkillInput`` from
  stdin, imports the skill from a fixed module path, runs it, and
  writes a JSON-encoded ``SkillOutput`` envelope to stdout. The
  envelope shape mirrors ``ProcessSkillRuntime``'s queue tuples:
  ``{"tag": "ok", "output": {...}}`` on success,
  ``{"tag": "skill_error", "error": "..."}`` on a skill exception,
  ``{"tag": "import_error", "error": "..."}`` if the skill module
  can't be loaded inside the container.

Phase 3.5 deliverable. The B-385 PR shipped the runtime + factory
wiring + 9 unit tests; the SkillForge / EvolutionEngine integration
(actually picking ``DockerSkillRuntime`` for low-trust evolved
skills) is its own follow-up.
"""
from __future__ import annotations

import asyncio
import json
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


# ── defaults ─────────────────────────────────────────────────────────────
#
# These are "secure-by-default" — every dimension a Docker container can
# be tightened on, we tighten. A caller who needs network access has to
# opt in explicitly via the constructor (or via ``runtime.docker.*`` in
# the daemon config). Matches OpenHands' DockerRuntime posture.

_DEFAULT_IMAGE = "python:3.10-slim"
_DEFAULT_NETWORK_MODE = "none"   # no /etc/resolv.conf, no host net iface
_DEFAULT_MEM_LIMIT = "512m"
_DEFAULT_CPU_QUOTA = 50000        # 50% of one core (period=100000)
_DEFAULT_CPU_PERIOD = 100000
_DEFAULT_READ_ONLY = True
_DEFAULT_TMPFS = {"/tmp": "size=100M"}
_DEFAULT_TIMEOUT_S = 30.0


# ── slot ─────────────────────────────────────────────────────────────────


@dataclass
class _Slot:
    handle: SkillHandle
    container: Any            # docker.models.containers.Container
    manifest: SkillManifest
    skill: Skill              # kept for re-pickling on retry, future-proof
    args: dict[str, Any]
    started_at: float = field(default_factory=time.monotonic)
    output: SkillOutput | None = None
    killed: bool = False
    timed_out: bool = False
    errored: bool = False


# ── runtime ──────────────────────────────────────────────────────────────


class DockerSkillRuntime(SkillRuntime):
    """Run skills inside a one-shot Docker container with secure defaults.

    See module docstring for the contract. Every constructor knob is
    optional; defaults are the most-restrictive combination that still
    lets a vanilla Python skill produce stdout output.

    Args:
        image: Container image ref. Pulled on first ``fork`` if missing
            from the local cache. Default ``python:3.10-slim``.
        network_mode: Docker network mode. ``"none"`` = isolated (default,
            secure), ``"bridge"`` = host-attached (insecure, opt-in).
        mem_limit: Docker mem-limit string (``"512m"`` / ``"1g"`` etc).
            Hard cap; OOM kills the container.
        cpu_quota: CPU quota in microseconds per period
            (``50000`` with default ``100000`` period = 50% of one core).
        cpu_period: CPU period in microseconds. Default ``100000``.
        read_only: Mount root fs read-only inside the container. Default
            True. Combined with the tmpfs mount this means the skill
            can ONLY write under ``/tmp`` (which is itself capped).
        tmpfs: Map of mountpoint → mount-options string. Default
            ``{"/tmp": "size=100M"}``.
        timeout_s: Wall-clock cap for the container's ``wait`` call.
            Manifest's ``max_cpu_seconds`` overrides this when smaller
            (consistent with the Local / Process runtimes).
        client: Inject a docker.from_env() result for tests; default
            None means the runtime calls docker.from_env() lazily on
            first ``fork``.
    """

    def __init__(
        self,
        *,
        image: str = _DEFAULT_IMAGE,
        network_mode: str = _DEFAULT_NETWORK_MODE,
        mem_limit: str = _DEFAULT_MEM_LIMIT,
        cpu_quota: int = _DEFAULT_CPU_QUOTA,
        cpu_period: int = _DEFAULT_CPU_PERIOD,
        read_only: bool = _DEFAULT_READ_ONLY,
        tmpfs: dict[str, str] | None = None,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        client: Any | None = None,
    ) -> None:
        self.image = image
        self.network_mode = network_mode
        self.mem_limit = mem_limit
        self.cpu_quota = cpu_quota
        self.cpu_period = cpu_period
        self.read_only = read_only
        self.tmpfs = dict(_DEFAULT_TMPFS if tmpfs is None else tmpfs)
        self.timeout_s = float(timeout_s)
        self._client = client          # tests inject a mock; prod stays lazy
        self._slots: dict[str, _Slot] = {}
        self._image_pulled: set[str] = set()

    # ── lazy SDK + client ────────────────────────────────────────────────

    def _docker_module(self) -> Any:
        """Import the ``docker`` SDK lazily and surface a clear error.

        Keeps ``import xmclaw.providers.runtime.docker`` free of the
        docker dependency so the boundary check + minimal-install users
        can still load the package. The real failure happens at the
        first ``fork``, with a message that points the user at the
        right ``pip install`` extra.
        """
        try:
            import docker  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover — exercised via mock
            raise RuntimeError(
                "DockerSkillRuntime requires the 'docker' Python SDK. "
                "Install it with `pip install 'xmclaw[sandbox-docker]'` "
                "(or `pip install 'docker>=7'` directly). The Docker "
                "daemon must also be running on the host."
            ) from exc
        return docker

    def _get_client(self) -> Any:
        """Return a docker.DockerClient — cached after first call."""
        if self._client is not None:
            return self._client
        docker = self._docker_module()
        try:
            self._client = docker.from_env()
        except Exception as exc:  # noqa: BLE001 — wrap so callers get one
            # known kind of error rather than the SDK's varied
            # exception zoo (DockerException, APIError, ...).
            raise RuntimeError(
                f"DockerSkillRuntime cannot reach the Docker daemon: "
                f"{type(exc).__name__}: {exc}. Is the daemon running? "
                f"(`docker info` should succeed.)"
            ) from exc
        return self._client

    def _ensure_image(self, client: Any) -> None:
        """Pull the image once per (runtime instance, image) pair.

        The Docker SDK already caches images at the daemon level, but a
        first ``run`` against a missing image otherwise fails noisily
        instead of doing the right thing. We do the pull here and
        memoize so repeat calls don't re-hit the registry. Failures
        propagate — the caller already lost network or auth, no point
        masking it.
        """
        if self.image in self._image_pulled:
            return
        try:
            client.images.get(self.image)
        except Exception:  # noqa: BLE001 — image-not-found family
            client.images.pull(self.image)
        self._image_pulled.add(self.image)

    # ── contract ─────────────────────────────────────────────────────────

    def enforce_manifest(self, manifest: SkillManifest) -> None:
        """Same structural invariants as the sibling runtimes.

        Sandboxed runtimes are the place where ``permissions_fs`` /
        ``permissions_net`` *could* be mapped to actual container
        constraints (e.g. permissions_net=() → network_mode=none, which
        we already do globally). Phase 3.5 ships only the global
        defaults; per-skill mapping is a follow-up once the test suite
        proves the round-trip is stable.
        """
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

        client = self._get_client()
        # Block briefly off the loop while the SDK resolves the image —
        # docker-py is sync-only, and pulling is slow on a cold cache.
        await asyncio.to_thread(self._ensure_image, client)

        # Skill payload mount: a temp dir containing skill.json (the
        # input the harness reads) plus harness.py (a thin Python
        # bootstrapper that imports + runs the skill, JSON-encoding the
        # result on stdout). The container mounts this dir read-only at
        # /skill. Mount path is absolute so the daemon-side temp dir
        # doesn't have to exist inside any host workspace.
        mount_dir = Path(tempfile.mkdtemp(prefix="xmclaw-skill-"))
        try:
            (mount_dir / "input.json").write_text(
                json.dumps({"args": args}), encoding="utf-8",
            )
            (mount_dir / "harness.py").write_text(
                _CONTAINER_HARNESS, encoding="utf-8",
            )
            # The skill's source is best-effort: we serialize the
            # skill object's class location so the container can
            # ``import`` it. For fully-evolved skills (the SkillForge
            # path) this comes from a SKILL.md materialization that
            # already lives on disk; for tests we just stash a marker.
            (mount_dir / "skill_meta.json").write_text(
                json.dumps({
                    "id": skill.id,
                    "version": skill.version,
                    "module": type(skill).__module__,
                    "qualname": type(skill).__qualname__,
                }),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            # Clean up the half-built mount dir so we don't leak temp
            # space on a failed fork.
            _rmtree_quiet(mount_dir)
            raise RuntimeError(
                f"DockerSkillRuntime: cannot stage skill payload: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        try:
            container = client.containers.create(
                image=self.image,
                command=["python", "/skill/harness.py"],
                volumes={
                    str(mount_dir): {"bind": "/skill", "mode": "ro"},
                },
                network_mode=self.network_mode,
                mem_limit=self.mem_limit,
                cpu_quota=self.cpu_quota,
                cpu_period=self.cpu_period,
                read_only=self.read_only,
                tmpfs=self.tmpfs,
                stdin_open=True,    # harness reads input.json from /skill
                detach=True,
                # Drop most caps; container only needs to run python.
                cap_drop=["ALL"],
                # No new privileges — defense-in-depth against suid /
                # setcap binaries inside the image.
                security_opt=["no-new-privileges:true"],
                # Tag with our handle so an admin doing ``docker ps``
                # can see what's ours.
                labels={"xmclaw.skill_id": skill.id,
                        "xmclaw.skill_version": str(skill.version)},
            )
            container.start()
        except Exception as exc:  # noqa: BLE001
            _rmtree_quiet(mount_dir)
            raise RuntimeError(
                f"DockerSkillRuntime: cannot start container: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        handle = SkillHandle(
            id=uuid.uuid4().hex,
            skill_id=skill.id,
            version=skill.version,
            pid=None,   # Docker doesn't surface a stable host pid here
        )
        slot = _Slot(
            handle=handle, container=container, manifest=manifest,
            skill=skill, args=args,
        )
        # Stash the temp-dir path on the slot so wait() / kill() can
        # garbage-collect it. Hidden attribute since it's not part of
        # the slot's public surface and shouldn't end up in any
        # SkillOutput envelope.
        setattr(slot, "_mount_dir", mount_dir)
        self._slots[handle.id] = slot
        return handle

    async def wait(
        self,
        handle: SkillHandle,
        timeout: float | None = None,
    ) -> SkillOutput:
        slot = self._get_slot(handle)
        if slot.output is not None:
            return slot.output

        # Effective timeout: min(manifest.max_cpu_seconds, caller, runtime).
        cap = slot.manifest.max_cpu_seconds or None
        effective: float | None = self.timeout_s
        if cap is not None:
            effective = cap if effective is None else min(effective, cap)
        if timeout is not None:
            effective = timeout if effective is None else min(effective, timeout)
        if effective is not None and effective <= 0:
            effective = None

        try:
            result = await asyncio.to_thread(
                _container_wait, slot.container, effective,
            )
        except _DockerTimeout:
            slot.timed_out = True
            await asyncio.to_thread(_kill_quiet, slot.container)
            slot.output = SkillOutput(
                ok=False,
                result={
                    "error": f"timeout: skill exceeded {effective}s wall budget",
                    "kind": "timeout",
                },
                side_effects=[],
            )
            self._cleanup_slot(slot)
            return slot.output
        except Exception as exc:  # noqa: BLE001
            slot.errored = True
            slot.output = SkillOutput(
                ok=False,
                result={
                    "error": f"{type(exc).__name__}: {exc}",
                    "kind": "container_wait_error",
                },
                side_effects=[],
            )
            self._cleanup_slot(slot)
            return slot.output

        # ``result`` is the docker wait dict {"StatusCode": int, ...}.
        exit_code = int(result.get("StatusCode", 0)) if isinstance(result, dict) else 0
        try:
            stdout_bytes = await asyncio.to_thread(
                slot.container.logs, **{"stdout": True, "stderr": False},
            )
        except Exception:  # noqa: BLE001
            stdout_bytes = b""
        stdout = (
            stdout_bytes.decode("utf-8", errors="replace")
            if isinstance(stdout_bytes, (bytes, bytearray))
            else str(stdout_bytes)
        )

        # OOMKilled is reported by the daemon as a 137 exit code (SIGKILL
        # from the OOM killer) plus an OOMKilled flag in the State dict.
        oom = False
        try:
            state = slot.container.attrs.get("State") or {}
            oom = bool(state.get("OOMKilled"))
        except Exception:  # noqa: BLE001
            oom = False

        if oom:
            slot.errored = True
            slot.output = SkillOutput(
                ok=False,
                result={
                    "error": f"OOM: skill exceeded {self.mem_limit} memory cap",
                    "kind": "oom",
                    "exit_code": exit_code,
                },
                side_effects=[],
            )
            self._cleanup_slot(slot)
            return slot.output

        # Parse the harness envelope. The harness writes exactly one
        # JSON line as its last stdout output; anything before it is
        # treated as advisory log noise.
        envelope = _parse_envelope(stdout)
        if envelope is None:
            slot.errored = True
            slot.output = SkillOutput(
                ok=False,
                result={
                    "error": (
                        f"container produced no envelope (exit={exit_code}); "
                        f"stdout={_truncate(stdout, 400)!r}"
                    ),
                    "kind": "no_envelope",
                    "exit_code": exit_code,
                },
                side_effects=[],
            )
            self._cleanup_slot(slot)
            return slot.output

        tag = envelope.get("tag")
        if tag == "ok":
            payload = envelope.get("output") or {}
            slot.output = SkillOutput(
                ok=bool(payload.get("ok", False)),
                result=payload.get("result"),
                side_effects=list(payload.get("side_effects") or []),
            )
            self._cleanup_slot(slot)
            return slot.output

        slot.errored = True
        slot.output = SkillOutput(
            ok=False,
            result={
                "error": str(envelope.get("error") or "unknown container error"),
                "kind": str(tag or "unknown"),
                "exit_code": exit_code,
            },
            side_effects=[],
        )
        self._cleanup_slot(slot)
        return slot.output

    async def kill(self, handle: SkillHandle) -> None:
        slot = self._get_slot(handle)
        if slot.output is not None:
            await asyncio.to_thread(_kill_quiet, slot.container)
            return  # idempotent — already settled, don't change status
        slot.killed = True
        await asyncio.to_thread(_kill_quiet, slot.container)
        # Drain the container so subsequent ``status()`` is consistent.
        try:
            await asyncio.to_thread(_container_wait, slot.container, 5.0)
        except Exception:  # noqa: BLE001
            pass
        self._cleanup_slot(slot)

    async def status(self, handle: SkillHandle) -> SkillStatus:
        slot = self._get_slot(handle)
        if slot.timed_out:
            return SkillStatus.TIMEOUT
        if slot.killed:
            return SkillStatus.KILLED
        if slot.errored:
            return SkillStatus.FAILED
        if slot.output is not None:
            return SkillStatus.SUCCEEDED if slot.output.ok else SkillStatus.FAILED
        try:
            slot.container.reload()
            state = (slot.container.attrs.get("State") or {})
            container_status = state.get("Status")
        except Exception:  # noqa: BLE001
            return SkillStatus.RUNNING
        if container_status in ("created", "running", "restarting"):
            return SkillStatus.RUNNING
        # Exited / dead — guess from exit code.
        exit_code = int(state.get("ExitCode") or 0)
        return SkillStatus.SUCCEEDED if exit_code == 0 else SkillStatus.FAILED

    def shutdown(self) -> None:
        """Best-effort: stop every live container + remove + GC mounts."""
        for slot in list(self._slots.values()):
            try:
                _kill_quiet(slot.container)
            except Exception:  # noqa: BLE001
                pass
            self._cleanup_slot(slot)

    # ── helpers ──────────────────────────────────────────────────────────

    def _get_slot(self, handle: SkillHandle) -> _Slot:
        slot = self._slots.get(handle.id)
        if slot is None:
            raise LookupError(f"unknown handle id={handle.id!r}")
        return slot

    def _cleanup_slot(self, slot: _Slot) -> None:
        """Remove the container and its mount dir. Idempotent."""
        try:
            slot.container.remove(force=True)
        except Exception:  # noqa: BLE001
            pass
        mount_dir = getattr(slot, "_mount_dir", None)
        if mount_dir is not None:
            _rmtree_quiet(Path(mount_dir))
            try:
                delattr(slot, "_mount_dir")
            except Exception:  # noqa: BLE001
                pass


# ── module helpers ───────────────────────────────────────────────────────


class _DockerTimeout(Exception):
    """Internal flag — raised by ``_container_wait`` on deadline.

    Not re-exported; ``wait()`` translates it into a structured
    ``SkillOutput(ok=False, kind='timeout')``.
    """


def _container_wait(container: Any, timeout: float | None) -> dict[str, Any]:
    """Synchronous wrapper around ``container.wait(timeout=...)``.

    docker-py's ``Container.wait`` raises ``ReadTimeout`` (technically
    ``requests.exceptions.ReadTimeout`` underneath) when its internal
    HTTP poll deadline elapses. We translate that into our own
    ``_DockerTimeout`` so the runtime's ``wait()`` doesn't have to
    know about ``requests`` internals.
    """
    try:
        return container.wait(timeout=timeout) if timeout is not None else container.wait()
    except Exception as exc:  # noqa: BLE001 — we triage by type name
        # ``requests.exceptions.ReadTimeout`` and friends — match by
        # name since we don't want to take a hard dep on requests just
        # to ``isinstance``-check it. docker-py's own error tree
        # (``docker.errors``) doesn't export a stable timeout class.
        name = type(exc).__name__
        if "Timeout" in name or "ReadTimeout" in name:
            raise _DockerTimeout from exc
        raise


def _kill_quiet(container: Any) -> None:
    """Stop a container, swallowing already-stopped errors."""
    try:
        container.kill()
    except Exception:  # noqa: BLE001
        pass


def _parse_envelope(stdout: str) -> dict[str, Any] | None:
    """Find the harness's JSON envelope in container stdout.

    The harness emits its result on stdout as a single line beginning
    with ``__XMC_ENVELOPE__`` so we can find it past arbitrary stdout
    chatter (logging, libraries that print on import, etc.). Returns
    None if no envelope was emitted — in which case ``wait()`` reports
    ``kind="no_envelope"`` and surfaces stdout for debugging.
    """
    if not stdout:
        return None
    marker = "__XMC_ENVELOPE__"
    for line in reversed(stdout.splitlines()):
        if line.startswith(marker):
            payload = line[len(marker):].lstrip(": ").strip()
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                return None
            if isinstance(obj, dict):
                return obj
            return None
    return None


def _truncate(text: str, n: int) -> str:
    if len(text) <= n:
        return text
    return text[:n] + "..."


def _rmtree_quiet(path: Path) -> None:
    """Remove a directory tree, swallowing errors.

    The temp dirs we create are small; a leaked one is a minor disk
    nuisance, not a correctness issue. We log nothing here on purpose
    — the daemon's structlog sink would just spam on every container
    teardown.
    """
    import shutil
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass


# ── container-side harness ───────────────────────────────────────────────
#
# The harness runs INSIDE the container. It must stay tiny and stdlib-
# only — the container image (python:3.10-slim) doesn't ship xmclaw.
# We hand-roll skill loading: read meta.json, ``importlib.import_module``
# the skill class, instantiate it, run it under a fresh asyncio loop,
# then JSON-encode the result on stdout with a marker prefix.
#
# Pitfalls we deliberately handle:
#   * Skills that print on stdout. We use a marker so the parent can
#     scan past arbitrary chatter.
#   * Skills whose ``run()`` raises — we catch and surface as
#     ``tag="skill_error"``.
#   * Skill module that can't be imported (e.g. evolved skills whose
#     source file isn't accessible inside the container) — surface as
#     ``tag="import_error"``. The B-385 PR ships the runtime + tests;
#     wiring SkillForge to materialize the skill source into the
#     mount dir is a follow-up — see module docstring.

_CONTAINER_HARNESS = '''\
"""Harness — runs inside the container. Stdlib only; no xmclaw imports."""
import asyncio
import importlib
import json
import sys
import traceback


def _emit(envelope):
    sys.stdout.write("__XMC_ENVELOPE__: " + json.dumps(envelope) + "\\n")
    sys.stdout.flush()


def main():
    try:
        with open("/skill/skill_meta.json", "r", encoding="utf-8") as f:
            meta = json.load(f)
        with open("/skill/input.json", "r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception as exc:
        _emit({"tag": "harness_error",
               "error": f"{type(exc).__name__}: {exc}"})
        return 1

    module_name = meta.get("module") or ""
    qualname = meta.get("qualname") or ""
    args = (payload or {}).get("args") or {}

    try:
        module = importlib.import_module(module_name)
    except Exception as exc:
        _emit({"tag": "import_error",
               "error": f"cannot import {module_name!r}: "
                        f"{type(exc).__name__}: {exc}",
               "trace": traceback.format_exc()})
        return 1

    cls = module
    for piece in qualname.split("."):
        cls = getattr(cls, piece, None)
        if cls is None:
            _emit({"tag": "import_error",
                   "error": f"qualname {qualname!r} not found in "
                            f"{module_name!r}"})
            return 1

    try:
        skill = cls()
    except Exception as exc:
        _emit({"tag": "skill_error",
               "error": f"cannot instantiate {qualname!r}: "
                        f"{type(exc).__name__}: {exc}"})
        return 1

    # Skill input shim — we can't import xmclaw.skills.base here, but
    # the skill expects a SkillInput-shaped object. Light-weight
    # duck-type works because Skill.run only reads `inp.args`.
    class _Input:
        def __init__(self, args):
            self.args = args

    try:
        loop = asyncio.new_event_loop()
        try:
            output = loop.run_until_complete(skill.run(_Input(args)))
        finally:
            loop.close()
    except Exception as exc:
        _emit({"tag": "skill_error",
               "error": f"{type(exc).__name__}: {exc}",
               "trace": traceback.format_exc()})
        return 1

    # Output is a SkillOutput (dataclass) — convert to dict.
    out = {
        "ok": bool(getattr(output, "ok", False)),
        "result": getattr(output, "result", None),
        "side_effects": list(getattr(output, "side_effects", []) or []),
    }
    _emit({"tag": "ok", "output": out})
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''
