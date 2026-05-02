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

    # ── boot-time replay (B-174) ──

    def replay_history(
        self, *, skill_id: str | None = None,
    ) -> dict[str, int]:
        """Restore HEAD pointers from the persisted promote/rollback log.

        Pre-B-174 the registry persisted promote / rollback records to
        ``~/.xmclaw/skills/<id>.jsonl`` but **never replayed them at
        boot**. Symptom: agent runs for a week, mutator promotes
        ``auto-repair`` v1 → v2 → v3 (auto_apply=true), user restarts
        daemon → HEAD silently reverts to v1 because that was the
        first ``register(set_head=True)`` call. All evolution work
        looked retained on disk + UI but the agent quietly used the
        worst version.

        Boot order required: register every (id, version) FIRST (so
        the records can resolve their target versions), THEN call
        ``replay_history()``. Records pointing at a version that no
        longer exists in the registry (skill was deleted between
        sessions) are skipped silently — replay never raises.

        Returns: ``{skill_id: replayed_head_version}`` for the skills
        whose HEAD actually moved, useful for boot-log telemetry.

        Note: ``rollback`` reasons + ``promote`` evidence stay in the
        in-memory history list verbatim — replay re-appends them so
        ``history(skill_id)`` returns the same chronological view as
        before the restart.
        """
        if self._history_dir is None or not self._history_dir.is_dir():
            return {}

        if skill_id is not None:
            paths = [self._history_dir / f"{skill_id}.jsonl"]
        else:
            paths = sorted(self._history_dir.glob("*.jsonl"))

        replayed_heads: dict[str, int] = {}
        for path in paths:
            if not path.is_file():
                continue
            sid = path.stem
            records: list[PromotionRecord] = []
            try:
                with path.open("r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        rec = self._record_from_dict(obj)
                        if rec is not None:
                            records.append(rec)
            except OSError:
                continue
            if not records:
                continue

            # Apply chronologically. Skip records whose to_version was
            # never re-registered this boot (deleted skill).
            for rec in sorted(records, key=lambda r: r.ts):
                if (sid, rec.to_version) not in self._skills:
                    continue
                self._head[sid] = rec.to_version
                self._history[sid].append(rec)
                replayed_heads[sid] = rec.to_version
        return replayed_heads

    def _record_from_dict(self, obj: dict[str, Any]) -> PromotionRecord | None:
        """Reverse of ``_persist`` payload shape. Tolerates partial
        rows so a corrupt JSONL line drops one record, not the file."""
        try:
            kind = str(obj.get("kind") or "promote")
            if kind not in ("promote", "rollback"):
                return None
            return PromotionRecord(
                kind=kind,  # type: ignore[arg-type]
                skill_id=str(obj["skill_id"]),
                from_version=int(obj.get("from_version", 0) or 0),
                to_version=int(obj["to_version"]),
                ts=float(obj.get("ts", 0.0) or 0.0),
                evidence=tuple(str(e) for e in (obj.get("evidence") or ())),
                reason=obj.get("reason"),
                source=str(obj.get("source") or "manual"),
            )
        except (KeyError, TypeError, ValueError):
            return None
