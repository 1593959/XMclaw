"""SkillRegistry — versioned, auditable, rollback-able skill storage.

Anti-req #5 + #12 in concrete form:

  * Every skill is stored as ``(skill_id, version)`` — multiple versions
    can coexist. There is no "overwrite" path; newer code lands at a new
    version number.
  * ``HEAD`` per skill is an explicit pointer. ``get(skill_id)`` returns
    whatever HEAD says — it is never implicitly the latest version.
  * ``promote(skill_id, to_version, evidence=...)`` MOVES the HEAD.
    Non-empty ``evidence`` is required. No evidence → ``ValueError``.
    This is how anti-req #12 ("no evidence, no promotion") becomes
    impossible-to-bypass in code: a PR that auto-promotes without
    evidence just won't compile a valid call.
  * ``rollback(skill_id, to_version, reason=...)`` moves HEAD back.
    Both directions are logged to the history, so an auditor can see
    why an old version was reinstated.
  * History is append-only. A skill that has been promoted and
    rolled back still shows both events in its log.

Phase 3.1 ships the in-memory implementation plus a JSONL-per-skill
file persistor (best-effort; the registry keeps working if the on-disk
files are missing). Phase 3.2 adds manifest-driven sandbox enforcement
at ``get()`` time.
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from xmclaw.skills.base import Skill
from xmclaw.skills.manifest import SkillManifest
from xmclaw.skills.versioning import PromotionRecord, now_ts


class UnknownSkillError(LookupError):
    """Raised when a skill_id or (skill_id, version) isn't registered."""


@dataclass(frozen=True, slots=True)
class SkillRef:
    """The addressable identity of a registered skill version."""

    skill_id: str
    version: int
    manifest: SkillManifest


class SkillRegistry:
    """Versioned in-memory store for skills. Optional disk persistence
    for the history log; skills themselves stay in-process because they
    carry live code objects.
    """

    def __init__(self, history_dir: Path | str | None = None) -> None:
        # (skill_id, version) -> Skill instance
        self._skills: dict[tuple[str, int], Skill] = {}
        # skill_id -> sorted list of versions that exist
        self._versions: dict[str, list[int]] = defaultdict(list)
        # skill_id -> currently-active version (HEAD)
        self._head: dict[str, int] = {}
        # skill_id -> append-only log
        self._history: dict[str, list[PromotionRecord]] = defaultdict(list)
        # skill_id -> manifest-at-version (indexed by (id, version))
        self._manifests: dict[tuple[str, int], SkillManifest] = {}

        self._history_dir: Path | None = (
            Path(history_dir) if history_dir is not None else None
        )
        if self._history_dir is not None:
            self._history_dir.mkdir(parents=True, exist_ok=True)

    # ── registration ──

    def register(
        self,
        skill: Skill,
        manifest: SkillManifest,
        *,
        set_head: bool = True,
    ) -> SkillRef:
        """Register a skill version.

        The skill object carries its ``id`` and ``version`` as class
        attributes. ``manifest`` supplies the permission/resource
        declarations (anti-req #5: skills without a manifest cannot run).

        ``set_head=True`` (default) makes this the active version if no
        HEAD exists yet, but does NOT move HEAD if it's already set —
        that requires an explicit ``promote()`` call with evidence.
        """
        skill_id = skill.id
        version = skill.version
        key = (skill_id, version)

        if key in self._skills:
            raise ValueError(
                f"{skill_id!r} v{version} already registered — bump version "
                f"rather than re-registering"
            )
        if manifest.id != skill_id or manifest.version != version:
            raise ValueError(
                f"manifest id/version mismatch: skill={skill_id} v{version}, "
                f"manifest={manifest.id} v{manifest.version}"
            )

        self._skills[key] = skill
        self._manifests[key] = manifest
        versions = self._versions[skill_id]
        versions.append(version)
        versions.sort()

        if set_head and skill_id not in self._head:
            self._head[skill_id] = version

        return SkillRef(skill_id=skill_id, version=version, manifest=manifest)

    # ── lookup ──

    def get(self, skill_id: str, version: int | None = None) -> Skill:
        """Return a skill instance.

        ``version=None`` returns the HEAD-active version, NOT the newest
        registered version. This is important: a rollback moves HEAD but
        leaves the latest-registered version in place.
        """
        if version is None:
            if skill_id not in self._head:
                raise UnknownSkillError(
                    f"skill {skill_id!r} has no HEAD — never promoted"
                )
            version = self._head[skill_id]
        try:
            return self._skills[(skill_id, version)]
        except KeyError as exc:
            raise UnknownSkillError(
                f"skill {skill_id!r} v{version} not registered"
            ) from exc

    def ref(self, skill_id: str, version: int | None = None) -> SkillRef:
        if version is None:
            if skill_id not in self._head:
                raise UnknownSkillError(
                    f"skill {skill_id!r} has no HEAD"
                )
            version = self._head[skill_id]
        key = (skill_id, version)
        if key not in self._skills:
            raise UnknownSkillError(f"{skill_id!r} v{version} not registered")
        return SkillRef(
            skill_id=skill_id, version=version, manifest=self._manifests[key],
        )

    def list_versions(self, skill_id: str) -> list[int]:
        return list(self._versions.get(skill_id, ()))

    def active_version(self, skill_id: str) -> int | None:
        return self._head.get(skill_id)

    def list_skill_ids(self) -> list[str]:
        return sorted(self._head.keys())

    # ── mutation: promote + rollback (anti-req #12) ──

    def promote(
        self,
        skill_id: str,
        to_version: int,
        *,
        evidence: list[str],
        source: str = "manual",
    ) -> PromotionRecord:
        """Move HEAD to ``to_version``.

        Anti-req #12: ``evidence`` MUST be non-empty. An empty evidence
        list raises ``ValueError`` — callers cannot silently promote.
        The returned record is also appended to the history.

        B-121: ``source`` defaults to ``"manual"`` — every direct call
        is treated as human-driven unless the caller explicitly tags it
        as ``"controller"`` (auto-evolution path) or ``"system"`` (boot
        / migrations). This makes the audit log answer "who decided?"
        without the consumer having to reverse-engineer it from the
        evidence strings.
        """
        if not evidence:
            raise ValueError(
                f"anti-req #12: promotion of {skill_id!r} refused without "
                f"evidence. Pass `evidence=[...]` with at least one entry "
                f"describing the grader verdict / bench result that "
                f"justifies this change."
            )
        if (skill_id, to_version) not in self._skills:
            raise UnknownSkillError(
                f"cannot promote to unregistered version "
                f"{skill_id!r} v{to_version}"
            )

        from_version = self._head.get(skill_id, 0)
        self._head[skill_id] = to_version
        record = PromotionRecord(
            kind="promote",
            skill_id=skill_id,
            from_version=from_version,
            to_version=to_version,
            ts=now_ts(),
            evidence=tuple(evidence),
            source=source,
        )
        self._history[skill_id].append(record)
        self._persist(record)
        return record

    def rollback(
        self,
        skill_id: str,
        to_version: int,
        *,
        reason: str,
        source: str = "manual",
    ) -> PromotionRecord:
        """Move HEAD back to an earlier version.

        Reason is mandatory — rollbacks without reasons are the
        mirror-image anti-pattern to promotions without evidence.
        ``source`` follows the same B-121 convention as ``promote``.
        """
        if not reason:
            raise ValueError(
                f"rollback of {skill_id!r} refused without reason. Rollbacks "
                f"must be explained — callers are never silent about them."
            )
        if (skill_id, to_version) not in self._skills:
            raise UnknownSkillError(
                f"cannot rollback to unregistered version "
                f"{skill_id!r} v{to_version}"
            )

        from_version = self._head.get(skill_id, 0)
        self._head[skill_id] = to_version
        record = PromotionRecord(
            kind="rollback",
            skill_id=skill_id,
            from_version=from_version,
            to_version=to_version,
            ts=now_ts(),
            reason=reason,
            source=source,
        )
        self._history[skill_id].append(record)
        self._persist(record)
        return record

    # ── audit ──

    def history(self, skill_id: str) -> list[PromotionRecord]:
        """Return the complete promote/rollback log for a skill (chronological).

        Anti-req #5: rollbacks are a first-class event, not a file edit.
        A skill promoted, rolled back, re-promoted shows three entries.
        """
        return list(self._history.get(skill_id, ()))

    # ── persistence ──

    def _persist(self, record: PromotionRecord) -> None:
        if self._history_dir is None:
            return
        path = self._history_dir / f"{record.skill_id}.jsonl"
        with path.open("a", encoding="utf-8") as f:
            payload: dict[str, Any] = asdict(record)
            # tuple → list for json
            payload["evidence"] = list(record.evidence)
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
