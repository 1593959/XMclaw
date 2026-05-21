"""SkillRuntime interface + in-process execution (Phase 3.2).

Phase 3.2 ships the ``LocalSkillRuntime`` — in-process asyncio tasks
with CPU-timeout enforcement via ``asyncio.wait_for``. Phase 3.3
brings subprocess / Docker / remote (Modal, Daytona) runtimes.

Honest scope of Phase 3.2 in-process runtime:
  * CPU-seconds: REAL enforcement (wait_for).
  * Kill: REAL enforcement (task.cancel).
  * Memory / filesystem / network sandbox: NOT enforced in-process —
    Python lacks seccomp-level isolation. Any skill that bypasses the
    BuiltinTools allowlist with its own ``Path(...).read_text()`` will
    succeed. Phase 3.3 fixes this with process isolation.
  * ``enforce_manifest`` in LocalSkillRuntime is a no-op for the
    advisory fields — it only rejects structurally invalid manifests.
"""
from xmclaw.providers.runtime.base import (
    SkillHandle,
    SkillRuntime,
    SkillStatus,
)
from xmclaw.providers.runtime.local import LocalSkillRuntime
from xmclaw.providers.runtime.process import ProcessSkillRuntime

__all__ = [
    "LocalSkillRuntime",
    "ProcessSkillRuntime",
    "SkillHandle",
    "SkillRuntime",
    "SkillStatus",
]

# Lazy re-export for sandbox runtimes so they don't drag heavy deps
# at import time (docker/paramiko/modal/daytona are runtime-only).
for _mod, _name in [
    ("xmclaw.providers.runtime.docker", "DockerSkillRuntime"),
    ("xmclaw.providers.runtime.ssh", "SSHSkillRuntime"),
    ("xmclaw.providers.runtime.modal", "ModalSkillRuntime"),
    ("xmclaw.providers.runtime.daytona", "DaytonaSkillRuntime"),
    ("xmclaw.providers.runtime.vercel", "VercelSkillRuntime"),
    ("xmclaw.providers.runtime.singularity", "SingularitySkillRuntime"),
]:
    try:
        exec(f"from {_mod} import {_name}")  # noqa: S102
        __all__.append(_name)
    except ImportError:  # pragma: no cover
        pass
