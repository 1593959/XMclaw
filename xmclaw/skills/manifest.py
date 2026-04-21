"""SkillManifest — permissions, resource limits, provenance.

Anti-req #5 + #8. Every skill ships with a manifest; runtimes refuse to
``fork`` without one. Phase 3 deliverable.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class SkillManifest:
    id: str
    version: int
    permissions_fs: tuple[str, ...] = ()
    permissions_net: tuple[str, ...] = ()
    permissions_subprocess: tuple[str, ...] = ()
    max_cpu_seconds: float = 30.0
    max_memory_mb: int = 512
    created_by: str = "human"   # "human" | "llm" | "evolved"
    evidence: tuple[str, ...] = field(default_factory=tuple)
