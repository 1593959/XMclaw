"""SkillManifest — permissions, resource limits, provenance.

Anti-req #5 + #8. Every skill ships with a manifest; runtimes refuse to
``fork`` without one. Phase 3 deliverable.

B-170: ``title`` / ``description`` are first-class so the Skills page
can show what each skill is for. SKILL.md frontmatter (skills.sh
ecosystem standard) carries those — the user_loader / proposal
materializer parse them into the manifest. ``slots=False`` (was True)
to keep ``dataclasses.asdict`` cheap and to let ``to_dict`` round-
trip without a separate codec.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class SkillManifest:
    id: str
    version: int
    title: str = ""
    description: str = ""
    permissions_fs: tuple[str, ...] = ()
    permissions_net: tuple[str, ...] = ()
    permissions_subprocess: tuple[str, ...] = ()
    max_cpu_seconds: float = 30.0
    max_memory_mb: int = 512
    created_by: str = "human"   # "human" | "user" | "llm" | "evolved"
    evidence: tuple[str, ...] = field(default_factory=tuple)
    triggers: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable dict — what ``/api/v2/skills`` ships to UI.

        Tuples → lists so JSON encoders don't choke; everything else
        is already plain-data. Skill router already calls this when
        present, so adding a ``to_dict`` method beats forcing the
        router to know about dataclass internals.
        """
        d = asdict(self)
        for k, v in list(d.items()):
            if isinstance(v, tuple):
                d[k] = list(v)
        return d
