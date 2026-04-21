"""SkillRuntime ABC — where skills actually execute.

Contract:

  ``fork(skill, manifest, args)``
      Launch the skill. Returns a ``SkillHandle``. The runtime schedules
      the skill in the background; ``fork`` itself returns quickly.

  ``wait(handle, timeout=None)``
      Block until the skill finishes (or is killed). Returns the
      ``SkillOutput`` the skill produced, or raises on internal error.
      A runtime-level timeout returns a ``SkillOutput(ok=False, ...)``
      rather than propagating TimeoutError.

  ``kill(handle)``
      Terminate the running skill. Safe to call on already-finished
      handles (idempotent).

  ``status(handle)``
      Non-blocking state probe.

  ``enforce_manifest(manifest)``
      Validate the manifest before any ``fork`` with it. Runtime-
      specific: in-process runtimes may accept anything; sandboxed
      runtimes may refuse manifests with disallowed syscalls/paths.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass
from enum import Enum
from typing import Any

from xmclaw.skills.base import Skill, SkillOutput
from xmclaw.skills.manifest import SkillManifest


class SkillStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    KILLED = "killed"
    TIMEOUT = "timeout"


@dataclass(frozen=True, slots=True)
class SkillHandle:
    id: str                    # opaque runtime-generated id
    skill_id: str
    version: int
    pid: int | None = None     # None for in-process runtimes


class SkillRuntime(abc.ABC):
    @abc.abstractmethod
    async def fork(
        self,
        skill: Skill,
        manifest: SkillManifest,
        args: dict[str, Any],
    ) -> SkillHandle: ...

    @abc.abstractmethod
    async def wait(
        self,
        handle: SkillHandle,
        timeout: float | None = None,
    ) -> SkillOutput: ...

    @abc.abstractmethod
    async def kill(self, handle: SkillHandle) -> None: ...

    @abc.abstractmethod
    async def status(self, handle: SkillHandle) -> SkillStatus: ...

    @abc.abstractmethod
    def enforce_manifest(self, manifest: SkillManifest) -> None: ...
