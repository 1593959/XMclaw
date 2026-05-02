"""UserSkillsLoader — discover + register user-authored skills.

Two formats accepted in the same canonical directory tree:

  ``<root>/<skill_id>/skill.py``
      Python ``Skill`` subclass (B-127 original). The loader picks
      the first concrete subclass with a zero-arg ``__init__``, or
      calls ``build_skill()`` if the module exports one.

  ``<root>/<skill_id>/SKILL.md``
      Markdown procedure body (Epic #24 Phase 5 immediate fix —
      previously broken: Phase 1 deleted the multi-path SKILL.md
      scanner without giving the canonical path a SKILL.md
      bridge, so users following the skills.sh convention ended
      up with files XMclaw literally couldn't see). The loader
      wraps the body in :class:`MarkdownProcedureSkill` so the
      agent can ``skill_<id>``-invoke it like any other tool;
      the wrapper returns the body text and the agent follows
      the steps using its existing tools.

  ``<root>/<skill_id>/manifest.json``  (optional)
      JSON object with :class:`SkillManifest` fields.

The loader scans :func:`xmclaw.utils.paths.user_skills_dir`
(``~/.xmclaw/skills_user/`` by default) plus any opt-in
``extra_roots`` (typically ``~/.agents/skills/`` so users can keep
``npx skills add`` muscle memory without the file becoming a ghost).

Trust model: user-authored Python and SKILL.md are fully trusted.
XMclaw is local-first and single-user; ``importlib`` runs the file
at module top-level, and SKILL.md is read directly.
"""
from __future__ import annotations

import importlib.util
import inspect
import json
import logging
import re
from dataclasses import dataclass, replace
from pathlib import Path

from xmclaw.skills.base import Skill
from xmclaw.skills.manifest import SkillManifest
from xmclaw.skills.markdown_skill import MarkdownProcedureSkill
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
    kind: str = "python"  # "python" or "markdown"
    source_root: str | None = None  # which root the skill came from


class UserSkillsLoader:
    """Scan one or more user-skills directories and register every
    found Skill. Accepts both ``skill.py`` (Python class) and
    ``SKILL.md`` (markdown procedure) formats in the same directory
    tree.

    Designed for boot-time use: instantiate with the registry +
    skills_root + optional ``extra_roots`` (e.g. ``~/.agents/skills``
    when ``evolution.skill_paths.extra`` is set in config), call
    :meth:`load_all`. Returns a list of :class:`LoadResult` for
    telemetry / startup logging.

    Skill-id collisions across roots: first wins (canonical root
    scanned first; extra roots only fill in missing ids). This makes
    the canonical path the source-of-truth and turns extra roots into
    a "convenience overlay" rather than a competing register.
    """

    def __init__(
        self,
        registry: SkillRegistry,
        skills_root: Path,
        extra_roots: list[Path] | None = None,
    ) -> None:
        self._registry = registry
        self._root = skills_root
        self._extra_roots = list(extra_roots or [])

    def load_all(self) -> list[LoadResult]:
        results: list[LoadResult] = []
        seen_ids: set[str] = set()

        # Canonical first, then extras — first-wins on collisions.
        for root in [self._root, *self._extra_roots]:
            if not root.is_dir():
                continue
            for entry in sorted(root.iterdir()):
                if not entry.is_dir():
                    continue
                if entry.name.startswith(".") or entry.name.startswith("_"):
                    continue
                if entry.name in seen_ids:
                    log.debug(
                        "user_skill.shadowed_by_canonical",
                        extra={
                            "skill_id": entry.name,
                            "shadowed_root": str(root),
                        },
                    )
                    continue
                res = self._load_one(entry)
                res = replace(res, source_root=str(root))
                results.append(res)
                if res.ok:
                    seen_ids.add(entry.name)
        return results

    def _load_one(self, skill_dir: Path) -> LoadResult:
        skill_py = skill_dir / "skill.py"
        skill_md = skill_dir / "SKILL.md"

        # SKILL.md branch (Epic #24 Phase 5 immediate fix). Python
        # ``skill.py`` takes priority when both exist — the user
        # signaled they want code, not a procedure.
        if not skill_py.is_file() and skill_md.is_file():
            return self._load_markdown(skill_dir, skill_md)

        if not skill_py.is_file():
            return LoadResult(
                skill_id=skill_dir.name, ok=False, skill_path=skill_py,
                error="neither skill.py nor SKILL.md found",
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

    def _load_markdown(
        self, skill_dir: Path, skill_md: Path,
    ) -> LoadResult:
        """Wrap a SKILL.md file in :class:`MarkdownProcedureSkill` and
        register it under the directory name.

        B-172: also scan ``<skill_dir>/versions/v<N>.md`` for additional
        versions produced by the mutation orchestrator. Each version is
        registered with ``set_head=False`` so HEAD stays at v1 unless
        an explicit promote event flips it.
        """
        skill_id = skill_dir.name
        try:
            body = skill_md.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return LoadResult(
                skill_id=skill_id, ok=False, skill_path=skill_md,
                kind="markdown",
                error=f"SKILL.md read failed: {exc}",
            )

        # B-170: parse SKILL.md frontmatter (skills.sh ecosystem
        # standard: ``description``, ``name`` / ``title``,
        # optional ``allowed-tools``) so the manifest carries the
        # one-line description the Skills page renders. Pre-B-170 the
        # manifest was empty → UI showed "—" for every skill.
        title, description, triggers = _parse_skill_md_frontmatter(body)

        skill = MarkdownProcedureSkill(id=skill_id, body=body, version=1)
        manifest = SkillManifest(
            id=skill_id, version=1, created_by="user",
            title=title, description=description, triggers=triggers,
        )
        try:
            self._registry.register(skill, manifest, set_head=True)
        except ValueError as exc:
            if "already registered" not in str(exc):
                return LoadResult(
                    skill_id=skill_id, ok=False, skill_path=skill_md,
                    kind="markdown",
                    error=f"register failed: {exc}",
                )
            # idempotent re-load — fall through to versions/ scan
            # so a fresh mutation v<N> file landed since last boot
            # still gets registered.

        # B-172: scan ``<skill_dir>/versions/v<N>.md`` for archived
        # mutation outputs. Each archived version registers as a
        # separate (id, version) pair with set_head=False — HEAD stays
        # at v1 until SKILL_PROMOTED flips it. On daemon restart this
        # is what makes mutation outputs survive.
        versions_dir = skill_dir / "versions"
        if versions_dir.is_dir():
            for vfile in sorted(versions_dir.glob("v*.md")):
                v_match = re.match(r"^v(\d+)\.md$", vfile.name)
                if v_match is None:
                    continue
                ver = int(v_match.group(1))
                if ver == 1:
                    continue  # v1 is already the SKILL.md above
                try:
                    v_body = vfile.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                v_title, v_desc, v_triggers = _parse_skill_md_frontmatter(v_body)
                v_skill = MarkdownProcedureSkill(
                    id=skill_id, body=v_body, version=ver,
                )
                v_manifest = SkillManifest(
                    id=skill_id, version=ver,
                    created_by="evolved",  # versions/ is mutator territory
                    title=v_title or title,
                    description=v_desc or description,
                    triggers=v_triggers or triggers,
                )
                try:
                    self._registry.register(
                        v_skill, v_manifest, set_head=False,
                    )
                except ValueError as exc:
                    if "already registered" in str(exc):
                        continue
                    log.warning(
                        "user_skill.version_register_failed",
                        extra={
                            "skill_id": skill_id, "version": ver,
                            "error": str(exc),
                        },
                    )

        return LoadResult(
            skill_id=skill_id, ok=True, skill_path=skill_md,
            kind="markdown", version=1,
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
            title=str(data.get("title", "") or ""),
            description=str(data.get("description", "") or ""),
            permissions_fs=_as_tuple("permissions_fs"),
            permissions_net=_as_tuple("permissions_net"),
            permissions_subprocess=_as_tuple("permissions_subprocess"),
            max_cpu_seconds=float(data.get("max_cpu_seconds", 30.0)),
            max_memory_mb=int(data.get("max_memory_mb", 512)),
            created_by=str(data.get("created_by", "user")),
            evidence=_as_tuple("evidence"),
            triggers=_as_tuple("triggers"),
        )


# ── B-170 SKILL.md frontmatter parser ──────────────────────────────


_FRONTMATTER_BLOCK_RE = re.compile(
    r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL,
)


def resolve_skill_roots(
    config: dict | None = None,
) -> tuple[Path, list[Path]]:
    """B-173: shared root-resolution so cli/main.py boot-time loader
    and daemon/skills_watcher.py runtime watcher agree on what to
    scan. Returns ``(canonical_root, extra_roots)``.

    * ``canonical_root`` is always ``user_skills_dir()``
      (``~/.xmclaw/skills_user/``).
    * ``extra_roots`` defaults to ``[~/.agents/skills, ~/.claude/skills]``
      (B-163 zero-config) UNLESS the config explicitly sets
      ``evolution.skill_paths.extra`` — then the config wins (an
      empty list disables shared-dir scanning entirely).
    """
    from xmclaw.utils.paths import user_skills_dir
    canonical = user_skills_dir()
    cfg = config or {}
    ev_cfg = cfg.get("evolution") or {}
    sp_cfg = ev_cfg.get("skill_paths")
    if isinstance(sp_cfg, dict) and "extra" in sp_cfg:
        extra_raw = sp_cfg.get("extra") or []
    else:
        extra_raw = ["~/.agents/skills", "~/.claude/skills"]
    extras: list[Path] = []
    if isinstance(extra_raw, list):
        for raw in extra_raw:
            try:
                extras.append(Path(str(raw)).expanduser())
            except Exception:  # noqa: BLE001
                continue
    return canonical, extras


def _parse_skill_md_frontmatter(
    body: str,
) -> tuple[str, str, tuple[str, ...]]:
    """Pull ``title`` / ``description`` / ``triggers`` from SKILL.md.

    The skills.sh ecosystem (``npx skills add``) and Claude Code share
    a YAML-frontmatter convention at the top of SKILL.md: ``name``,
    ``description``, optional ``triggers``. We don't pull a full YAML
    parser dep just for these — a flat-key scan covers the dialect we
    care about, and falls back to first-h1 / first-paragraph
    heuristics when frontmatter is absent.

    Returns ``(title, description, triggers)``. Each is empty when the
    file gives no signal — caller is expected to handle the empty
    case (the UI already does, displaying "—").

    Multi-line YAML scalars (``> | folded blocks``) are not supported
    because the ecosystem's typical SKILL.md keeps these one-liners.
    The bracketed-list form ``triggers: [a, b]`` *is* recognised so
    skills.sh templates round-trip cleanly.
    """
    title = ""
    description = ""
    triggers: tuple[str, ...] = ()

    m = _FRONTMATTER_BLOCK_RE.match(body or "")
    if m is not None:
        for raw in m.group(1).splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if ":" not in line:
                continue
            key, _, val = line.partition(":")
            key = key.strip().lower()
            val = val.strip()
            # Strip surrounding quotes.
            if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                val = val[1:-1]
            if key in ("name", "title") and not title:
                title = val
            elif key == "description" and not description:
                description = val
            elif key in ("triggers", "trigger") and not triggers:
                if val.startswith("[") and val.endswith("]"):
                    inner = val[1:-1]
                    parts = [
                        p.strip().strip("'\"")
                        for p in inner.split(",")
                    ]
                    triggers = tuple(p for p in parts if p)
                elif val:
                    triggers = (val,)

    # Fallback: no frontmatter description → first heading + first
    # paragraph. Cheap, often surprisingly good — most SKILL.md open
    # with "# Foo\n\nFoo does …".
    if not title or not description:
        # Skip frontmatter block if any.
        rest = body
        if m is not None:
            rest = body[m.end():]
        if not title:
            for line in rest.splitlines():
                stripped = line.strip()
                if stripped.startswith("# "):
                    title = stripped[2:].strip()
                    break
                if stripped:
                    # Hit non-heading content first → no h1, give up.
                    break
        if not description:
            # First non-blank non-heading paragraph (single line).
            for line in rest.splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                description = stripped[:280]
                break

    return title, description, triggers
