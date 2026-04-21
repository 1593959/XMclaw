"""SkillRuntime ABC — process-level skill execution with manifest sandbox."""
from __future__ import annotations

import abc
from dataclasses import dataclass
from enum import Enum
from typing import Any


class SkillStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    KILLED = "killed"


@dataclass(frozen=True, slots=True)
class SkillHandle:
    id: str
    pid: int | None  # None for in-process runtimes


@dataclass(frozen=True, slots=True)
class SkillSpec:
    id: str
    version: int
    manifest: dict[str, Any]


@dataclass(frozen=True, slots=True)
class SkillManifest:
    """Subset of the full manifest (see xmclaw.skills.manifest) that the
    runtime needs to enforce permissions. Anti-req #5 + #8."""

    permissions_fs: tuple[str, ...] = ()      # allow-list paths
    permissions_net: tuple[str, ...] = ()      # allow-list hosts
    permissions_subprocess: tuple[str, ...] = ()  # allow-list executables
    max_cpu_seconds: float = 30.0
    max_memory_mb: int = 512


class SkillRuntime(abc.ABC):
    @abc.abstractmethod
    async def fork(self, skill: SkillSpec, args: dict[str, Any]) -> SkillHandle: ...

    @abc.abstractmethod
    async def kill(self, handle: SkillHandle) -> None: ...

    @abc.abstractmethod
    async def status(self, handle: SkillHandle) -> SkillStatus: ...

    @abc.abstractmethod
    def enforce_manifest(self, manifest: SkillManifest) -> None: ...
