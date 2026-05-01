"""UserSkillsLoader — discover + register user-authored Python Skills.

B-127. Closes the "I want to install my own skill" gap.

Today users have two ways to give XMclaw a skill:

  1. Drop a ``SKILL.md`` under ``~/.xmclaw/auto_evo/skills/<id>/`` —
     procedural / text-only. Exposed as ``learned_skill_<id>`` tool by
     :class:`xmclaw.daemon.learned_skills_tool.LearnedSkillToolProvider`
     (B-125).

  2. Subclass :class:`Skill` in their own Python and call
     ``registry.register(...)`` somewhere — but XMclaw has no boot
     hook for that, so in practice no user has done this.

This loader fills gap #2. Layout:

  ``~/.xmclaw/skills_user/<skill_id>/skill.py``
      Contains exactly one :class:`Skill` subclass (the loader scans
      module attrs and picks the first concrete subclass that is not
      :class:`Skill` itself). Optional zero-arg ``__init__``; otherwise
      provide a ``build_skill()`` factory in the same module.

  ``~/.xmclaw/skills_user/<skill_id>/manifest.json``  (optional)
      JSON object whose keys map to :class:`SkillManifest` fields.
      When absent, a minimal manifest is synthesised (created_by =
      "user"). Required fields ``id`` / ``version`` default to the
      directory name and ``skill.version``.

The loader is invoked once at daemon boot (CLI), AFTER the
SkillRegistry is constructed but BEFORE the agent is wired. Errors
loading one skill are logged + skipped; other skills still load.

Trust model
-----------

User-authored Python is fully trusted — XMclaw is local-first and
single-user. ``importlib`` runs whatever the user wrote at module
top-level. We do NOT sandbox; that would be theatre at this layer.
"""
from __future__ import annotations

import importlib.util
import inspect
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from xmclaw.skills.base import Skill
from xmclaw.skills.manifest import SkillManifest
from xmclaw.skills.registry import SkillRegistry


log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class LoadResult:
    """One row per discovered skill — outcome of loading it."""

    skill_id: str
    ok: bool
    skill_path: Path
    manifest_path: Path | None = None
    version: int | None = None
    error: str | None = None


class UserSkillsLoader:
    """Scan a user-skills directory and register every found Skill.

    Designed for boot-time use: instantiate with the registry +
    skills root, call :meth:`load_all`. Returns a list of
    :class:`LoadResult` for telemetry / startup logging.
    """

    def __init__(self, registry: SkillRegistry, skills_root: Path) -> None:
        self._registry = registry
        self._root = skills_root

    def load_all(self) -> list[LoadResult]:
        results: list[LoadResult] = []
        if not self._root.is_dir():
            return results
        for entry in sorted(self._root.iterdir()):
            if not entry.is_dir():
                continue
            if entry.name.startswith(".") or entry.name.startswith("_"):
                continue
            results.append(self._load_one(entry))
        return results

    def _load_one(self, skill_dir: Path) -> LoadResult:
        skill_py = skill_dir / "skill.py"
        if not skill_py.is_file():
            return LoadResult(
                skill_id=skill_dir.name, ok=False, skill_path=skill_py,
                error="skill.py not found",
            )

        skill_id = skill_dir.name
        manifest_path: Path | None = None

        # Import the module under a unique synthetic name so two
        # user skills with the same internal symbol names don't
        # collide in sys.modules.
        mod_name = f"xmclaw_user_skill__{skill_id}"
        try:
            spec = importlib.util.spec_from_file_location(mod_name, skill_py)
            if spec is None or spec.loader is None:
                return LoadResult(
                    skill_id=skill_id, ok=False, skill_path=skill_py,
                    error="importlib could not build a module spec",
                )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001 — surface to caller as LoadResult
            return LoadResult(
                skill_id=skill_id, ok=False, skill_path=skill_py,
                error=f"import failed: {type(exc).__name__}: {exc}",
            )

        skill_instance = self._instantiate(module)
        if skill_instance is None:
            return LoadResult(
                skill_id=skill_id, ok=False, skill_path=skill_py,
                error=(
                    "no concrete Skill subclass found. Either define "
                    "a Skill subclass with a zero-arg __init__ at "
                    "module level, or expose a build_skill() factory."
                ),
            )

        # B-127: id/version on the instance MUST match the directory.
        # Mismatch is the kind of mistake that produces silent
        # surprises (skill registered as foo while you intended bar) —
        # cheaper to fail loudly here than to track down a phantom
        # later.
        instance_id = getattr(skill_instance, "id", None)
        instance_version = getattr(skill_instance, "version", None)
        if instance_id != skill_id:
            return LoadResult(
                skill_id=skill_id, ok=False, skill_path=skill_py,
                error=(
                    f"directory name {skill_id!r} disagrees with "
                    f"Skill.id {instance_id!r} — rename the directory "
                    f"or the class attribute"
                ),
            )
        if not isinstance(instance_version, int) or instance_version < 1:
            return LoadResult(
                skill_id=skill_id, ok=False, skill_path=skill_py,
                error=(
                    f"Skill.version must be a positive int, "
                    f"got {instance_version!r}"
                ),
            )

        # Manifest: load from disk if present, else synthesise.
        mp = skill_dir / "manifest.json"
        if mp.is_file():
            manifest_path = mp
            try:
                manifest = self._load_manifest(mp, skill_id, instance_version)
            except Exception as exc:  # noqa: BLE001
                return LoadResult(
                    skill_id=skill_id, ok=False, skill_path=skill_py,
                    manifest_path=mp,
                    error=f"manifest.json invalid: {exc}",
                )
        else:
            manifest = SkillManifest(
                id=skill_id, version=instance_version, created_by="user",
            )

        # Register. set_head=True so the first registration of a
        # given skill_id becomes HEAD immediately — user skills don't
        # need an evidence-bearing promote to be active. Subsequent
        # versions go through the normal promote() path.
        try:
            self._registry.register(
                skill_instance, manifest, set_head=True,
            )
        except ValueError as exc:
            # Already registered under same (id, version) — treat as
            # idempotent re-load (user re-installed the same version).
            if "already registered" in str(exc):
                log.info(
                    "user_skill.already_registered",
                    extra={"skill_id": skill_id, "version": instance_version},
                )
                return LoadResult(
                    skill_id=skill_id, ok=True, skill_path=skill_py,
                    manifest_path=manifest_path,
                    version=instance_version,
                )
            return LoadResult(
                skill_id=skill_id, ok=False, skill_path=skill_py,
                manifest_path=manifest_path,
                error=f"register failed: {exc}",
            )

        return LoadResult(
            skill_id=skill_id, ok=True, skill_path=skill_py,
            manifest_path=manifest_path, version=instance_version,
        )

    def _instantiate(self, module: object) -> Skill | None:
        """Find and instantiate the Skill subclass in ``module``.

        Two paths, in order of preference:

          1. ``build_skill()`` factory in the module — explicit, lets
             the user pass dependencies. Returns the constructed
             instance.
          2. The first concrete Skill subclass with a zero-arg
             ``__init__``. Convention-over-configuration for the
             simple case.
        """
        factory = getattr(module, "build_skill", None)
        if callable(factory):
            try:
                inst = factory()
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(
                    f"build_skill() raised {type(exc).__name__}: {exc}"
                ) from exc
            if not isinstance(inst, Skill):
                raise RuntimeError(
                    f"build_skill() returned {type(inst).__name__}, "
                    f"not a Skill subclass"
                )
            return inst

        for _, member in inspect.getmembers(module, inspect.isclass):
            if member is Skill:
                continue
            if not issubclass(member, Skill):
                continue
            if inspect.isabstract(member):
                continue
            try:
                return member()
            except TypeError:
                # Constructor needs args — user must use build_skill().
                continue
        return None

    def _load_manifest(
        self, mp: Path, skill_id: str, default_version: int,
    ) -> SkillManifest:
        data = json.loads(mp.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("manifest.json must be a JSON object")
        # id/version defaults from the skill_dir + Skill.version. User
        # can still override but they have to be consistent.
        sid = data.get("id", skill_id)
        ver = int(data.get("version", default_version))
        if sid != skill_id:
            raise ValueError(
                f"manifest id {sid!r} disagrees with directory {skill_id!r}"
            )
        if ver != default_version:
            raise ValueError(
                f"manifest version {ver} disagrees with Skill.version "
                f"{default_version}"
            )
        # Permission/limit fields land verbatim. Tuple-of-str shapes
        # come back as list[str] from json; SkillManifest is a
        # frozen dataclass with tuple-typed fields, so coerce.
        def _as_tuple(key: str) -> tuple:
            v = data.get(key) or ()
            if isinstance(v, (list, tuple)):
                return tuple(str(x) for x in v)
            return ()

        return SkillManifest(
            id=skill_id,
            version=ver,
            permissions_fs=_as_tuple("permissions_fs"),
            permissions_net=_as_tuple("permissions_net"),
            permissions_subprocess=_as_tuple("permissions_subprocess"),
            max_cpu_seconds=float(data.get("max_cpu_seconds", 30.0)),
            max_memory_mb=int(data.get("max_memory_mb", 512)),
            created_by=str(data.get("created_by", "user")),
            evidence=_as_tuple("evidence"),
        )
