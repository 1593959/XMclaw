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
import re
import threading
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from xmclaw.skills.base import Skill
from xmclaw.skills.manifest import SkillManifest
from xmclaw.skills.markdown_skill import MarkdownProcedureSkill
from xmclaw.skills.versioning import PromotionRecord, now_ts


class UnknownSkillError(LookupError):
    """Raised when a skill_id or (skill_id, version) isn't registered."""


class DangerousPromotionError(PermissionError):
    """Epic #27 P2 G-06 (2026-05-19): raised when ``promote()`` would
    move HEAD to a version whose grader evidence carries a
    ``dangerous`` verdict, and the caller did not pass ``force=True``.

    Inherits from ``PermissionError`` rather than ``ValueError`` so
    catch-blocks can distinguish "caller passed bad args" from "policy
    refused this transition" — the latter typically needs operator
    review, not a retry with cleaned-up args.
    """


@dataclass(frozen=True, slots=True)
class SkillRef:
    """The addressable identity of a registered skill version."""

    skill_id: str
    version: int
    manifest: SkillManifest


@dataclass
class SkillUsageStats:
    """Usage statistics for a single skill.

    Tracked at the registry level so every invocation path (agent loop,
    REPL, HTTP API) is counted regardless of which caller initiated the
    run.  Latency is wall-clock from ``registry.get()`` through
    ``skill.run()`` completion — the caller is responsible for passing
    the measured value.
    """

    skill_id: str
    call_count: int = 0
    success_count: int = 0
    total_latency_ms: float = 0.0
    last_used: float = 0.0

    @property
    def success_rate(self) -> float:
        """Fraction of calls that reported success."""
        return self.success_count / self.call_count if self.call_count > 0 else 0.0

    @property
    def avg_latency_ms(self) -> float:
        """Mean latency across all recorded calls."""
        return self.total_latency_ms / self.call_count if self.call_count > 0 else 0.0


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

        self._lock = threading.RLock()

        # Usage statistics: skill_id -> SkillUsageStats.  Populated lazily
        # by ``record_usage``; never raises on missing keys.
        self._usage_stats: dict[str, SkillUsageStats] = {}

        # 2026-06-17: router invalidation listeners. CompositeToolProvider
        # instances register here so they rebuild their static router when
        # the registry mutates (new skill, promote, rollback, hot_replace).
        self._router_listeners: set[Any] = set()

    def add_router_listener(self, callback: Any) -> None:
        """Register a callback to be invoked when the registry mutates.

        The callback receives no arguments; callers typically call
        ``composite.invalidate_router()`` inside it. Idempotent — adding
        the same callable twice is a no-op.
        """
        with self._lock:
            self._router_listeners.add(callback)

    def remove_router_listener(self, callback: Any) -> None:
        """Unregister a previously-added router listener."""
        with self._lock:
            self._router_listeners.discard(callback)

    def _notify_router_listeners(self) -> None:
        """Fan-out invalidation to every registered listener."""
        # Snapshot under lock so a listener that mutates the set doesn't
        # affect this iteration.
        with self._lock:
            listeners = list(self._router_listeners)
        for cb in listeners:
            try:
                cb()
            except Exception:  # noqa: BLE001
                pass

    # ── usage statistics ──

    def record_usage(
        self,
        skill_id: str,
        success: bool,
        latency_ms: float,
    ) -> SkillUsageStats:
        """Record one invocation of ``skill_id``.

        Parameters
        ----------
        skill_id : str
            The skill that was executed.
        success : bool
            Whether the skill reported a successful result.
        latency_ms : float
            Wall-clock milliseconds from resolution to completion.

        Returns
        -------
        SkillUsageStats
            The updated stats object for this skill.
        """
        with self._lock:
            existing = self._usage_stats.get(skill_id)
            if existing is None:
                existing = SkillUsageStats(skill_id=skill_id)
                self._usage_stats[skill_id] = existing
            # dataclass is mutable, so update in place.
            existing.call_count += 1
            if success:
                existing.success_count += 1
            existing.total_latency_ms += latency_ms
            existing.last_used = now_ts()
            return existing

    def get_usage_stats(self, skill_id: str) -> SkillUsageStats | None:
        """Return usage stats for a single skill, or ``None`` if never recorded."""
        with self._lock:
            return self._usage_stats.get(skill_id)

    def get_all_usage_stats(self) -> dict[str, SkillUsageStats]:
        """Return a shallow copy of the full usage-stats map."""
        with self._lock:
            return dict(self._usage_stats)

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

        with self._lock:
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

            self._notify_router_listeners()
            return SkillRef(skill_id=skill_id, version=version, manifest=manifest)

    # ── lookup ──

    def get(self, skill_id: str, version: int | None = None) -> Skill:
        """Return a skill instance.

        ``version=None`` returns the HEAD-active version, NOT the newest
        registered version. This is important: a rollback moves HEAD but
        leaves the latest-registered version in place.
        """
        with self._lock:
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
        with self._lock:
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
        with self._lock:
            return list(self._versions.get(skill_id, ()))

    def active_version(self, skill_id: str) -> int | None:
        with self._lock:
            return self._head.get(skill_id)

    def list_skill_ids(self) -> list[str]:
        with self._lock:
            return sorted(self._head.keys())

    # ── fuzzy lookup: find / find_multi (Jarvis Phase 6.3) ──

    def find(self, intent: str, top_k: int = 1) -> Skill | None:
        """Return the single best-matching Skill for ``intent``, or None.

        This is the compatibility shim that Planner._materialize_step
        and ActionDispatcher._route_skill_invoke expect: a callable
        ``find(intent) -> Skill | None``.  Under the hood it delegates
        to :meth:`find_multi` and returns the first item.
        """
        matches = self.find_multi(intent, top_k=max(1, top_k))
        return matches[0] if matches else None

    def find_multi(self, intent: str, top_k: int = 3) -> list[Skill]:
        """Fuzzy-match ``intent`` against every HEAD skill.

        Scoring mirrors the prefilter (xmclaw/skills/prefilter.py) so
        the agent loop and the planner agree on what "relevant" means:

          * name-substring overlap:  +2.0 per query token in skill_id
          * description/title tokens: +1.0 per shared token
          * trigger keyword match:    +0.5 per shared trigger token

        Returns an empty list when ``intent`` is empty or nothing scores
        above zero.  Never raises.
        """
        intent = (intent or "").strip().lower()
        if not intent:
            return []

        query_tokens = _tokenize(intent) - _STOPWORDS
        if not query_tokens:
            # Fallback: raw intent as a single probe (catches id literals
            # like "deploy-to-vercel" that the tokenizer may have split).
            query_tokens = {intent}

        scored: list[tuple[float, Skill]] = []
        with self._lock:
            for skill_id in self._head:
                version = self._head[skill_id]
                manifest = self._manifests.get((skill_id, version))
                if manifest is None:
                    continue

                score = 0.0
                sid_lower = skill_id.lower()

                # 1. Name-substring (strongest signal).
                for tok in query_tokens:
                    if len(tok) >= 2 and tok in sid_lower:
                        score += 2.0

                # 2. Title + description token overlap.
                corpus = " ".join(
                    filter(None, [manifest.title, manifest.description])
                ).lower()
                corpus_tokens = _tokenize(corpus)
                for tok in query_tokens:
                    if tok in corpus_tokens:
                        score += 1.0

                # 3. Trigger keyword match.
                if manifest.triggers:
                    for trig in manifest.triggers:
                        trig_tokens = _tokenize(str(trig).lower())
                        for tok in query_tokens:
                            if tok in trig_tokens:
                                score += 0.5

                if score > 0:
                    skill = self._skills.get((skill_id, version))
                    if skill is not None:
                        scored.append((score, skill))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [skill for _score, skill in scored[:max(1, top_k)]]

    # ── mutation: promote + rollback (anti-req #12) ──

    def promote(
        self,
        skill_id: str,
        to_version: int,
        *,
        evidence: list[str],
        source: str = "manual",
        force: bool = False,
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

        Epic #27 P2 G-06 (2026-05-19): when the evidence list signals a
        ``dangerous`` grader verdict (any entry containing
        ``"dangerous:"`` or ``"verdict=dangerous"``), promotion is
        refused unless the caller passes ``force=True``. This is the
        "danger gate" — the HonestGrader can stamp dangerous verdicts,
        the user / controller cannot silently override them. The
        ``force=True`` escape hatch leaves the CLI / debug-time
        workflows possible while making the override loud at the
        call site.
        """
        if not evidence:
            raise ValueError(
                f"anti-req #12: promotion of {skill_id!r} refused without "
                f"evidence. Pass `evidence=[...]` with at least one entry "
                f"describing the grader verdict / bench result that "
                f"justifies this change."
            )

        with self._lock:
            if (skill_id, to_version) not in self._skills:
                raise UnknownSkillError(
                    f"cannot promote to unregistered version "
                    f"{skill_id!r} v{to_version}"
                )

            if not force:
                danger_evidence = [
                    e for e in evidence
                    if isinstance(e, str) and (
                        "dangerous:" in e.lower()
                        or "verdict=dangerous" in e.lower()
                    )
                ]
                if danger_evidence:
                    raise DangerousPromotionError(
                        f"refusing to promote {skill_id!r} v{to_version}: "
                        f"grader stamped dangerous verdict ({danger_evidence!r}). "
                        "Pass ``force=True`` to override (do this ONLY after "
                        "reviewing the dangerous-evidence rationale)."
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
            self._notify_router_listeners()
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

        with self._lock:
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
            self._notify_router_listeners()
            return record

    # ── audit ──

    def history(self, skill_id: str) -> list[PromotionRecord]:
        """Return the complete promote/rollback log for a skill (chronological).

        Anti-req #5: rollbacks are a first-class event, not a file edit.
        A skill promoted, rolled back, re-promoted shows three entries.
        """
        with self._lock:
            return list(self._history.get(skill_id, ()))

    # ── B-175: in-place body update for live SKILL.md edits ──

    def update_body(
        self,
        skill_id: str,
        version: int,
        new_body: str,
        *,
        title: str | None = None,
        description: str | None = None,
        triggers: tuple[str, ...] | None = None,
    ) -> bool:
        """Replace the in-memory body (and optionally frontmatter
        fields) of an already-registered Markdown skill.

        Pre-B-175 the only way to make a SKILL.md edit visible to the
        agent was a daemon restart — UserSkillsLoader's "already
        registered → idempotent skip" logic deliberately won't
        re-register the same ``(id, version)``, so the in-memory body
        never refreshed. The :class:`SkillsWatcher` now calls this
        when it detects an mtime change.

        Only :class:`MarkdownProcedureSkill` is updateable: Python
        ``skill.py`` modules are cached by ``importlib`` and a body
        edit there can't reliably take effect without a full restart.
        Trying to update a Python skill returns ``False`` (silent
        no-op) so a heterogeneous registry doesn't crash the watcher.

        Returns ``True`` if the body was actually replaced, ``False``
        if the skill wasn't a Markdown skill or wasn't registered.
        Never raises — the watcher path must be exception-free.
        """
        key = (skill_id, version)
        with self._lock:
            existing = self._skills.get(key)
            if existing is None:
                return False
            if not isinstance(existing, MarkdownProcedureSkill):
                # Python skill — body lives in source, importlib-cached.
                return False

            # Construct a fresh instance (dataclass, mutable but cleaner
            # to replace whole than to set body in place).
            self._skills[key] = MarkdownProcedureSkill(
                id=skill_id, body=new_body, version=version,
            )

            # Refresh the manifest fields the user can edit via SKILL.md
            # frontmatter. Permissions / max_cpu_seconds / created_by /
            # evidence stay at whatever the registration set — the YAML
            # frontmatter parser doesn't surface those, so leaving them
            # alone is the right call.
            if any(
                x is not None for x in (title, description, triggers)
            ):
                from dataclasses import replace as _replace
                old_manifest = self._manifests[key]
                updates: dict[str, Any] = {}
                if title is not None:
                    updates["title"] = title
                if description is not None:
                    updates["description"] = description
                if triggers is not None:
                    updates["triggers"] = triggers
                self._manifests[key] = _replace(old_manifest, **updates)

            self._notify_router_listeners()
            return True

    def hot_replace(
        self,
        skill_id: str,
        version: int,
        new_skill: Skill,
        new_manifest: SkillManifest,
    ) -> bool:
        """Epic #27 sweep + Phase B follow-up (2026-05-19): replace an
        already-registered ``(skill_id, version)`` entry's live Skill
        instance + manifest IN-PLACE.

        Pre-this-method, ``update_body`` could only swap the body of
        a ``MarkdownProcedureSkill`` (markdown skills are essentially
        a body string + class wrapper, so safe to replace). Python
        ``skill.py`` modules were declared un-hot-reloadable because
        ``importlib`` caches them in ``sys.modules``.

        The cache itself isn't the blocker — :class:`UserSkillsLoader`
        already uses ``spec_from_file_location`` which creates a fresh
        module object each call. The REAL blocker was that the
        registry's "already registered → idempotent skip" code path
        kept the OLD instance. ``hot_replace`` is the escape hatch:
        the caller hands us a freshly-loaded module's class instance,
        we swap it in.

        Returns ``True`` when the replacement happened, ``False`` when
        ``(skill_id, version)`` wasn't registered to begin with (the
        caller should ``register`` instead).

        **Caveats** (caller MUST be aware):

        * In-flight ``skill.run()`` calls finish on the old instance
          (they hold the ref in their frame). Only NEXT call goes to
          new instance. This is fine for the typical "I edited the
          skill, run it again" use case.
        * Other code holding ``registry.get(id)`` → keeps a ref to
          the old instance for its turn. ``SkillToolProvider`` already
          re-fetches HEAD on every invocation, so that path is safe.
        * If the new module's class subclasses a class also imported
          from the OLD module, things get weird (``isinstance`` checks
          across the old/new boundary fail). This isn't a real shape
          in practice — XMclaw skills are single-class files with no
          intra-module inheritance.

        Never raises — exceptions become ``False`` so the watcher
        path is exception-safe.
        """
        key = (skill_id, version)
        with self._lock:
            if key not in self._skills:
                return False
            if new_manifest.id != skill_id or new_manifest.version != version:
                return False
            self._skills[key] = new_skill
            self._manifests[key] = new_manifest
            self._notify_router_listeners()
            return True

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

            with self._lock:
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


class SkillRegistryView:
    """Read-only view over a :class:`SkillRegistry` with per-skill HEAD
    overrides.

    Used by :class:`xmclaw.cognition.self_experiment.SelfExperimentLoop`
    to build a *treatment* agent that sees candidate skill versions while
    the baseline agent continues to use the real HEAD.  The view delegates
    every call to the underlying registry except ``get()``, ``ref()`` and
    ``active_version()``, where an override (if present) wins.

    The view is intentionally **stateless** — it reads the base registry
    live on every call, so a candidate registered after the view is
    created is still visible.  Only the HEAD override dict is fixed at
    construction time.
    """

    def __init__(
        self,
        base: SkillRegistry,
        head_overrides: dict[str, int],
    ) -> None:
        self._base = base
        self._overrides = dict(head_overrides)

    # ── read-only delegation ──

    def get(self, skill_id: str, version: int | None = None) -> Skill:
        if version is None and skill_id in self._overrides:
            version = self._overrides[skill_id]
        return self._base.get(skill_id, version)

    def ref(self, skill_id: str, version: int | None = None) -> SkillRef:
        if version is None and skill_id in self._overrides:
            version = self._overrides[skill_id]
        return self._base.ref(skill_id, version)

    def active_version(self, skill_id: str) -> int | None:
        if skill_id in self._overrides:
            return self._overrides[skill_id]
        return self._base.active_version(skill_id)

    def list_skill_ids(self) -> list[str]:
        return self._base.list_skill_ids()

    def list_versions(self, skill_id: str) -> list[int]:
        return self._base.list_versions(skill_id)

    # ── fuzzy lookup delegation ──

    def find(self, intent: str, top_k: int = 1) -> Any | None:
        """Delegate to the base registry."""
        return self._base.find(intent, top_k=top_k)

    def find_multi(self, intent: str, top_k: int = 3) -> list[Any]:
        """Delegate to the base registry."""
        return self._base.find_multi(intent, top_k=top_k)

    def register(self, *args: Any, **kwargs: Any) -> Any:
        """Not supported — the view is read-only."""
        raise NotImplementedError("SkillRegistryView is read-only")

    def promote(self, *args: Any, **kwargs: Any) -> Any:
        """Not supported — the view is read-only."""
        raise NotImplementedError("SkillRegistryView is read-only")


# ── Jarvis Phase 6.3: fuzzy-match tokenizer (duplicated from prefilter
# to avoid a circular import: registry → prefilter → tool_bridge → registry)

_CJK_RE = re.compile(
    r"[぀-ヿ㐀-䶿一-鿿가-힯]"
)
_WORD_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9_-]+")

_STOPWORDS = frozenset({
    "the", "and", "for", "you", "your", "this", "that", "with",
    "from", "what", "when", "where", "how", "why", "use", "using",
    "skill", "tool", "agent", "please", "can", "would", "could",
    "help", "want", "need", "make", "create", "build", "run",
    # Chinese stopwords (single-char so they tokenise via _CJK_RE)
    "的", "了", "是", "我", "你", "他", "她", "它", "在", "和",
    "也", "都", "就", "要", "不", "有", "没", "去", "来", "下",
    "上", "里", "好", "把", "给", "让", "对", "为", "请", "能",
})


def _tokenize(text: str) -> set[str]:
    """Return the set of normalised tokens in ``text``.

    ASCII: lowercase words ≥ 2 chars (skip single letters, they
    over-match noise like "a" / "I" / "x").
    CJK: each Han / Hangul / Kana character is its own token.
    """
    if not text:
        return set()
    text_lower = text.lower()
    tokens: set[str] = set()
    for w in _WORD_RE.findall(text_lower):
        if len(w) >= 2:
            tokens.add(w)
    for c in _CJK_RE.findall(text):
        tokens.add(c)
    return tokens
