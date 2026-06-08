"""Skill ABC."""
from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class SkillInput:
    args: dict[str, Any]


@dataclass(frozen=True, slots=True)
class SkillOutput:
    ok: bool
    result: Any
    side_effects: list[str]


@dataclass(frozen=True, slots=True)
class SkillContext:
    """Read-only context injected into :meth:`Skill.run` for introspection.

    Allows a skill to discover other installed skills, read their
    metadata, and adapt its behaviour without being able to directly
    invoke them (preserving the sandbox boundary).
    """

    # Internal handle — do not access directly; use the methods below.
    _registry: Any  # SkillRegistry-like interface

    def list_skills(self, query: str = "", top_k: int = 8) -> list[dict[str, Any]]:
        """Return metadata of installed HEAD skills, optionally filtered
        by a fuzzy query string."""
        if self._registry is None:
            return []
        try:
            ids = self._registry.list_skill_ids()
        except Exception:  # noqa: BLE001
            return []
        out: list[dict[str, Any]] = []
        for sid in ids:
            try:
                ref = self._registry.ref(sid)
                out.append({
                    "id": sid,
                    "version": ref.version,
                    "title": getattr(ref.manifest, "title", "") or sid,
                    "description": getattr(ref.manifest, "description", "") or "",
                })
            except Exception:  # noqa: BLE001
                continue
        if query and query.strip():
            q = query.strip().lower()
            # Simple fuzzy score: name match > description match
            scored: list[tuple[float, dict[str, Any]]] = []
            for meta in out:
                score = 0.0
                name = meta.get("id", "").lower()
                desc = meta.get("description", "").lower()
                if q in name:
                    score += 3.0
                if q in desc:
                    score += 1.0
                # Token overlap
                q_tokens = set(q.split())
                name_tokens = set(name.replace("-", " ").replace("_", " ").split())
                desc_tokens = set(desc.split())
                score += len(q_tokens & name_tokens) * 2.0
                score += len(q_tokens & desc_tokens) * 0.5
                scored.append((score, meta))
            scored.sort(key=lambda x: x[0], reverse=True)
            out = [m for _, m in scored[:top_k] if _ > 0]
        return out[:top_k]

    def get_skill_info(self, skill_id: str) -> dict[str, Any] | None:
        """Return metadata for a specific skill, or None if not found."""
        if self._registry is None:
            return None
        try:
            ref = self._registry.ref(skill_id)
            return {
                "id": skill_id,
                "version": ref.version,
                "title": getattr(ref.manifest, "title", "") or skill_id,
                "description": getattr(ref.manifest, "description", "") or "",
                "trust_level": str(
                    getattr(ref.manifest, "trust_level", "")
                ),
                "permissions_fs": list(
                    getattr(ref.manifest, "permissions_fs", ()) or ()
                ),
                "permissions_net": list(
                    getattr(ref.manifest, "permissions_net", ()) or ()
                ),
            }
        except Exception:  # noqa: BLE001
            return None

    def active_version(self, skill_id: str) -> int | None:
        """Return the currently promoted (HEAD) version, or None."""
        if self._registry is None:
            return None
        try:
            return self._registry.active_version(skill_id)
        except Exception:  # noqa: BLE001
            return None


class Skill(abc.ABC):
    id: str
    version: int

    @abc.abstractmethod
    async def run(
        self,
        inp: SkillInput,
        ctx: SkillContext | None = None,
    ) -> SkillOutput: ...
