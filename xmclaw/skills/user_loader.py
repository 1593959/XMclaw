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
from typing import Any

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
        # Epic #27 P2 G-06 (2026-05-19): read the marketplace install
        # ledger ONCE per loader instance so per-skill manifest trust
        # assignment is O(1) instead of repeatedly reopening
        # ``.marketplace.json``. The set is the truth — a skill_id
        # present here was installed via ``skill_install`` /
        # ``xmclaw skill install`` (trust=INSTALLED); absent means the
        # user authored it directly (trust=USER). Never raises — a
        # missing / malformed registry just yields the empty set.
        self._installed_skill_ids = self._read_installed_skill_ids()
        # Epic #27 P2 G-08 (2026-05-19): agent-proposed skills carry a
        # ``.proposed.json`` marker in their dir. Trust starts at
        # UNTRUSTED — the agent can self-write skills but they don't
        # get full USER privileges until manual review removes the
        # marker. Scan once per loader instance.
        self._proposed_skill_ids = self._scan_proposed_skill_ids()

    @staticmethod
    def _read_installed_skill_ids() -> frozenset[str]:
        try:
            from xmclaw.skills.marketplace import installed_registry_path
        except Exception:  # noqa: BLE001
            return frozenset()
        try:
            p = installed_registry_path()
            if not p.exists():
                return frozenset()
            import json as _json
            raw = _json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return frozenset()
        items = raw.get("skills") if isinstance(raw, dict) else None
        if not isinstance(items, list):
            return frozenset()
        ids: set[str] = set()
        for it in items:
            if isinstance(it, dict):
                sid = it.get("id")
                if isinstance(sid, str) and sid:
                    ids.add(sid)
        return frozenset(ids)

    def _scan_proposed_skill_ids(self) -> frozenset[str]:
        """Walk the canonical + extra roots looking for
        ``.proposed.json`` markers — these tag agent-proposed skills
        whose trust should start at UNTRUSTED (G-08).
        """
        ids: set[str] = set()
        for root in [self._root, *self._extra_roots]:
            if not root.is_dir():
                continue
            try:
                entries = list(root.iterdir())
            except OSError:
                continue
            for entry in entries:
                if not entry.is_dir():
                    continue
                if entry.name.startswith(".") or entry.name.startswith("_"):
                    continue
                if (entry / ".proposed.json").is_file():
                    ids.add(entry.name)
        return frozenset(ids)

    def _trust_for(self, skill_id: str) -> "SkillTrustLevel":
        """Source-based trust assignment. Marketplace-installed skills
        (recorded in ``.marketplace.json``) get INSTALLED; agent-
        proposed skills (with a ``.proposed.json`` marker) start at
        UNTRUSTED until manual review; everything else under the
        user-skills roots gets USER (the user authored / dropped the
        dir in themselves). Loader cannot mint BUILTIN — that's
        reserved for the static demo/plugin registration path.

        Precedence (lower trust wins when a skill_id is both
        proposed AND in the marketplace registry — proposed marker
        was deliberately written AFTER the install so the agent
        believes it's still in evaluation):
          1. UNTRUSTED if ``.proposed.json`` marker present
          2. INSTALLED if in marketplace registry
          3. USER (default)
        """
        from xmclaw.skills.manifest import SkillTrustLevel
        if skill_id in self._proposed_skill_ids:
            return SkillTrustLevel.UNTRUSTED
        if skill_id in self._installed_skill_ids:
            return SkillTrustLevel.INSTALLED
        return SkillTrustLevel.USER

    def load_all(self) -> list[LoadResult]:
        results: list[LoadResult] = []
        seen_ids: set[str] = set()
        # Epic #27 P1 G-09 (2026-05-19): de-dupe by FILESYSTEM IDENTITY
        # (realpath) so a symlink pointing two roots at the same dir
        # doesn't load the skill twice. Pre-fix the loader checked only
        # ``entry.name in seen_ids`` — fine for unique names but blind
        # to symlinks. Keys are realpath strings; both name-collision
        # and path-collision routes consult this.
        seen_realpaths: set[str] = set()
        # Track skill_ids that appeared in MULTIPLE roots at distinct
        # realpaths — the "I dropped foo/ in both ~/.xmclaw/skills_user
        # AND ~/.agents/skills" case. First-wins is the intended policy
        # but the operator should know about the conflict (so they can
        # delete the dupe and avoid surprise stale-content loads when
        # the canonical one is later removed).
        path_conflicts: dict[str, list[str]] = {}

        # Canonical first, then extras — first-wins on collisions.
        for root in [self._root, *self._extra_roots]:
            if not root.is_dir():
                continue
            for entry in sorted(root.iterdir()):
                if not entry.is_dir():
                    continue
                if entry.name.startswith(".") or entry.name.startswith("_"):
                    continue
                # Resolve to realpath for symlink-safe dedup. .resolve()
                # raises FileNotFoundError on broken symlinks; treat as
                # if the dir doesn't exist.
                try:
                    real = str(entry.resolve(strict=False))
                except OSError:
                    continue
                if real in seen_realpaths:
                    # Same physical path → silent (a symlink intent).
                    log.debug(
                        "user_skill.same_realpath_skipped path=%s", real,
                    )
                    continue
                if entry.name in seen_ids:
                    # Same id, different realpath = TRUE conflict.
                    log.warning(
                        "user_skill.duplicate_id_across_roots "
                        "skill_id=%s shadowed_path=%s",
                        entry.name, real,
                    )
                    path_conflicts.setdefault(entry.name, []).append(real)
                    continue
                res = self._load_one(entry)
                res = replace(res, source_root=str(root))
                results.append(res)
                if res.ok:
                    seen_ids.add(entry.name)
                seen_realpaths.add(real)
        # Inject synthetic LoadResult rows for path conflicts so the
        # SkillsWatcher → load_failures pipeline surfaces them in the
        # same channel as broken-module failures. ``kind="duplicate"``
        # lets callers tell these apart from genuine import errors.
        for sid, paths in path_conflicts.items():
            results.append(LoadResult(
                skill_id=sid,
                ok=False,
                skill_path=Path(paths[0]),
                error=(
                    f"skill_id {sid!r} found in multiple roots; the "
                    f"canonical (first scanned) wins, shadowed copies: "
                    + ", ".join(paths)
                    + ". Delete the duplicates to silence this warning."
                ),
                kind="duplicate",
                source_root="",
            ))
        return results

    def reload_one(
        self, skill_dir: Path,
    ) -> tuple[Skill | None, SkillManifest | None, str | None]:
        """Epic #27 sweep + Phase B follow-up (2026-05-19): single-dir
        hot reload for ``skill.py`` skills, used by SkillsWatcher when
        it detects a Python skill mtime change.

        Returns ``(skill_instance, manifest, error)``:
          * On success: ``(instance, manifest, None)`` — caller plugs
            these into ``SkillRegistry.hot_replace``.
          * On failure: ``(None, None, error_string)`` — caller emits
            the existing "requires_restart" signal as a fallback.

        Uses an mtime-stamped module name so each reload gets a FRESH
        module object even if ``sys.modules`` had the old one cached.
        Pre-fix, ``_load_one`` used ``f"xmclaw_user_skill__{skill_id}"``
        which IS deterministic — Python's ``module_from_spec`` does
        create a new module, but if anything in the new code did
        ``import xmclaw_user_skill__foo`` (rare), it'd hit the stale
        cache. The unique-name path sidesteps the whole concern.

        Never raises — all exceptions are converted to error strings.
        """
        skill_py = skill_dir / "skill.py"
        if not skill_py.is_file():
            return None, None, "no skill.py in directory"
        skill_id = skill_dir.name
        try:
            mtime = skill_py.stat().st_mtime
        except OSError as exc:
            return None, None, f"cannot stat skill.py: {exc}"
        # Mtime-stamped module name → fresh sys.modules entry each
        # reload. Strip non-identifier chars from the timestamp
        # representation to keep the name a valid Python identifier.
        mod_name = (
            f"xmclaw_user_skill__{skill_id}__"
            f"r{int(mtime * 1e6):d}"
        )
        try:
            spec = importlib.util.spec_from_file_location(mod_name, skill_py)
            if spec is None or spec.loader is None:
                return None, None, "importlib could not build a module spec"
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception as exc:  # noqa: BLE001
            return None, None, (
                f"import failed: {type(exc).__name__}: {exc}"
            )

        instance = self._instantiate(module)
        if instance is None:
            return None, None, (
                "no concrete Skill subclass found in reloaded module"
            )
        # Cross-check id matches dir name.
        instance_id = getattr(instance, "id", None)
        if instance_id != skill_id:
            return None, None, (
                f"reloaded Skill.id {instance_id!r} != dir {skill_id!r}"
            )
        version = getattr(instance, "version", None)
        if not isinstance(version, int) or version < 1:
            return None, None, (
                f"reloaded Skill.version must be positive int, got "
                f"{version!r}"
            )

        # Build manifest the same way _load_one does.
        mp = skill_dir / "manifest.json"
        if mp.is_file():
            try:
                manifest = self._load_manifest(mp, skill_id, version)
            except Exception as exc:  # noqa: BLE001
                return None, None, f"manifest.json invalid: {exc}"
            # G-06: source-based trust override — manifest authors can
            # claim a trust_level but the loader has the final word.
            from dataclasses import replace as _replace
            manifest = _replace(
                manifest, trust_level=self._trust_for(skill_id),
            )
        else:
            manifest = SkillManifest(
                id=skill_id, version=version, created_by="user",
                trust_level=self._trust_for(skill_id),
            )
        return instance, manifest, None

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
            spec.loader.exec_module(module)
        except Exception as exc:  # noqa: BLE001 — surface to caller as LoadResult
            return LoadResult(
                skill_id=skill_id, ok=False, skill_path=skill_py,
                error=f"import failed: {type(exc).__name__}: {exc}",
            )

        try:
            skill_instance = self._instantiate(module)
        except RuntimeError as exc:
            return LoadResult(
                skill_id=skill_id, ok=False, skill_path=skill_py,
                error=str(exc),
            )
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
            # G-06: loader has final word on trust regardless of
            # what manifest.json claims.
            from dataclasses import replace as _replace
            manifest = _replace(
                manifest, trust_level=self._trust_for(skill_id),
            )
        else:
            manifest = SkillManifest(
                id=skill_id, version=instance_version, created_by="user",
                trust_level=self._trust_for(skill_id),
            )

        # B-328: advisory cross-check. The manifest's ``permissions_*``
        # fields are NOT enforced by Local / Process runtimes (anti-req
        # #5 — sandbox is a future-runtime feature). When a user
        # authors a manifest claiming "no subprocess" but the source
        # actually calls ``subprocess.run`` / ``os.system`` / ``os.popen``,
        # the Skills UI would still display the (false) constraint as
        # if it were enforced. Surface the discrepancy at load time so
        # operators see the gap in daemon.log instead of silently
        # trusting a stale claim. Doesn't block load — that's a
        # behaviour change. Pure visibility.
        if manifest.permissions_are_meaningful():
            self._advisory_audit(skill_id, skill_py, manifest)

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

    def _advisory_audit(
        self,
        skill_id: str,
        skill_py: Path,
        manifest: SkillManifest,
    ) -> None:
        """B-328: AST cross-check between declared permissions and
        actual source. Logs WARNING when a Python skill's source uses
        subprocess primitives but the manifest claims none allowed —
        the most common author misconception (operators read SKILL.md
        permissions like firewall rules, but no current runtime
        enforces them).

        Best-effort: any failure (unreadable file, syntax error,
        missing scanner) is swallowed — this is purely visibility,
        the load path itself MUST not regress to ``ok=False`` on a
        scan glitch.
        """
        try:
            source = skill_py.read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001
            return
        try:
            from xmclaw.security.skill_scanner import scan_source
        except Exception:  # noqa: BLE001 — scanner optional in old installs
            return

        try:
            result = scan_source(source, filename=str(skill_py))
        except Exception:  # noqa: BLE001
            return

        # Subprocess discrepancy. Two cases get an advisory warning:
        #
        #   (1) The manifest's ``permissions_subprocess`` is empty
        #       (interpretable as "no subprocess allowed" — the
        #       caller's gate ``permissions_are_meaningful`` only
        #       lets us in when SOMETHING in permissions_* is non-
        #       empty, so the operator HAS engaged with the
        #       permissions system, just left this field empty
        #       → meant deny-all) AND the source uses subprocess.* /
        #       os.system / os.popen.
        #
        #   (2) The manifest's ``permissions_subprocess`` lists
        #       specific commands (an allowlist like ``("git",)``)
        #       AND the source uses subprocess primitives at all.
        #       Pre-B-341 this case was silently ignored — the
        #       check was only ``permissions_subprocess == ()``,
        #       so an operator who wrote ``permissions_subprocess:
        #       ["git"]`` and called ``os.system("rm -rf /")`` got
        #       no warning. The runtime can't actually enforce the
        #       allowlist anyway (anti-req #5 — sandbox is a
        #       future-runtime feature) so the warning is the only
        #       signal.
        #
        # The scanner already tags these with SKILL_AST_SUBPROCESS_*
        # (for various shapes) but we want any subprocess use,
        # including the safe argv form, because the manifest claim
        # is about the presence of subprocess, not the shell-
        # injection variant.
        # Heuristic: source uses subprocess if either an AST-detected
        # subprocess.* call exists or the scanner flagged any rule
        # whose pattern_id starts with SKILL_AST_SUBPROCESS_.
        subprocess_in_source = (
            "subprocess." in source
            or "subprocess as " in source
            or any(
                f.pattern_id.startswith("SKILL_AST_SUBPROCESS_")
                or f.pattern_id == "SKILL_AST_OS_SYSTEM"
                for f in (result.findings or ())
            )
        )
        if subprocess_in_source:
            if manifest.permissions_subprocess == ():
                log.warning(
                    "skill.permissions_advisory_violation skill_id=%s "
                    "field=permissions_subprocess "
                    "claim=none_allowed actual=subprocess_used "
                    "note=advisory_only_no_runtime_enforcement",
                    skill_id,
                )
            else:
                # B-341 (audit pass-2 #8): allowlist case. The runtime
                # cannot enforce the per-command filter; the operator
                # should be aware their list is informational only.
                log.warning(
                    "skill.permissions_advisory_violation skill_id=%s "
                    "field=permissions_subprocess "
                    "claim=allowlist=%s actual=subprocess_used "
                    "note=allowlist_advisory_only_no_runtime_enforcement",
                    skill_id, list(manifest.permissions_subprocess),
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
        # B-176: also parse ``created_by`` so migrated / evolution-
        # produced SKILL.md (which write ``created_by: evolved`` in
        # frontmatter) keep their EVOLVED tag across daemon restart.
        # Pre-B-176 this loader hardcoded "user" regardless of what
        # the file said, masking 6 of the 6 migrated lineages as USER.
        title, description, triggers = _parse_skill_md_frontmatter(body)
        created_by = _parse_skill_md_created_by(body) or "user"
        # Epic #27 P1 G-10: pull extra frontmatter fields so SKILL.md
        # authors can declare ``when_to_use`` / ``allowed_tools`` /
        # ``paths`` / ``model`` and have them ride through into the
        # manifest. Storage now; runtime enforcement of allowed_tools
        # / paths lands in G-05 / G-06.
        extras = _parse_skill_md_frontmatter_extras(body)

        skill = MarkdownProcedureSkill(id=skill_id, body=body, version=1, skill_dir=str(skill_dir))
        manifest = SkillManifest(
            id=skill_id, version=1, created_by=created_by,
            title=title, description=description, triggers=triggers,
            when_to_use=str(extras.get("when_to_use") or ""),
            allowed_tools=tuple(extras.get("allowed_tools") or ()),
            paths=tuple(extras.get("paths") or ()),
            requires_restart=bool(extras.get("requires_restart") or False),
            model=str(extras.get("model") or ""),
            # G-06: source-based trust override (loader has final word
            # over manifest authors).
            trust_level=self._trust_for(skill_id),
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
                    skill_dir=str(skill_dir),
                )
                v_manifest = SkillManifest(
                    id=skill_id, version=ver,
                    created_by="evolved",  # versions/ is mutator territory
                    title=v_title or title,
                    description=v_desc or description,
                    triggers=v_triggers or triggers,
                    # G-06: evolved variants inherit the head skill's
                    # source-based trust (typically USER, INSTALLED if
                    # the head came from the marketplace).
                    trust_level=self._trust_for(skill_id),
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
        def _as_tuple(key: str) -> tuple[str, ...]:
            v = data.get(key) or ()
            if isinstance(v, (list, tuple)):
                return tuple(str(x) for x in v)
            return ()

        # Epic #27 P1 G-10 — accept the new fields from manifest.json
        # too (Python-class skills can declare them just like SKILL.md
        # frontmatter does). Snake_case and hyphenated keys both
        # accepted to match the SKILL.md parser's tolerance.
        def _first_str(*keys: str) -> str:
            for k in keys:
                v = data.get(k)
                if isinstance(v, str) and v:
                    return v
            return ""

        def _first_tuple(*keys: str) -> tuple[str, ...]:
            for k in keys:
                t = _as_tuple(k)
                if t:
                    return t
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
            when_to_use=_first_str("when_to_use", "whenToUse"),
            allowed_tools=_first_tuple("allowed_tools", "allowedTools"),
            paths=_as_tuple("paths"),
            requires_restart=bool(
                data.get("requires_restart")
                or data.get("requiresRestart")
                or False
            ),
            model=str(data.get("model", "") or ""),
            permissions_enforced=bool(
                data.get("permissions_enforced")
                or data.get("permissionsEnforced")
                or False
            ),
        )


# ── B-170 SKILL.md frontmatter parser ──────────────────────────────


_FRONTMATTER_BLOCK_RE = re.compile(
    r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL,
)


def resolve_skill_roots(
    config: dict[str, Any] | None = None,
) -> tuple[Path, list[Path]]:
    """B-173: shared root-resolution so cli/main.py boot-time loader
    and daemon/skills_watcher.py runtime watcher agree on what to
    scan. Returns ``(canonical_root, extra_roots)``.

    * ``canonical_root`` is always ``user_skills_dir()``
      (``~/.xmclaw/skills_user/``).
    * ``extra_roots`` defaults to ``[~/.agents/skills]`` — the open
      agent-skills marketplace (``npx skills add ...`` writes here).
      B-234 dropped ``~/.claude/skills`` from the default: that
      directory is Claude Code's user-level config space and is NOT
      XMclaw's territory. Users who explicitly want to share skills
      between Claude Code and XMclaw can opt in via
      ``config.evolution.skill_paths.extra`` (the config wins; an
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
        extra_raw = ["~/.agents/skills"]
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
            # Skip YAML block-scalar indicators (multi-line scalars
            # like ``description: >`` or ``body: |``) — the flat-key
            # scanner can't handle them and would otherwise record ``>``
            # or ``|`` as the literal value.
            if line.rstrip().endswith((">", "|", ">-", "|-", ">+", "|+")):
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


# Epic #27 P1 G-10 (2026-05-19): parallel parser for the EXTRA
# frontmatter fields (when_to_use / allowed_tools / paths /
# requires_restart / model) added to SkillManifest. Lives alongside
# the 3-tuple parser instead of changing its return shape — the
# tuple unpack at all 3 call sites stays untouched, callers that
# want the new fields call this too. Returns a plain dict so the
# value types can vary per key without a custom dataclass.
def _parse_skill_md_frontmatter_extras(body: str) -> dict[str, Any]:
    """Parse the G-10 frontmatter extras.

    Recognised keys (case-insensitive, hyphen and underscore variants
    both accepted to match the Claude Code / Hermes conventions):

      * ``when_to_use`` / ``when-to-use`` / ``whenToUse`` — string
      * ``allowed_tools`` / ``allowed-tools`` / ``allowedTools`` —
        list-of-strings (bracketed or comma)
      * ``paths`` — list-of-strings (glob patterns)
      * ``requires_restart`` / ``requires-restart`` — bool
      * ``model`` — string

    Unknown keys are silently dropped. Missing keys map to:
    when_to_use="", allowed_tools=(), paths=(), requires_restart=False,
    model="".
    """
    out: dict[str, Any] = {
        "when_to_use": "",
        "allowed_tools": (),
        "paths": (),
        "requires_restart": False,
        "model": "",
    }
    m = _FRONTMATTER_BLOCK_RE.match(body or "")
    if m is None:
        return out

    def _normalise_key(k: str) -> str:
        return k.strip().lower().replace("-", "_")

    def _parse_list(val: str) -> tuple[str, ...]:
        s = val.strip()
        if s.startswith("[") and s.endswith("]"):
            inner = s[1:-1]
            parts = [p.strip().strip("'\"") for p in inner.split(",")]
            return tuple(p for p in parts if p)
        if not s:
            return ()
        # Comma-separated fallback.
        if "," in s:
            return tuple(
                p.strip().strip("'\"") for p in s.split(",") if p.strip()
            )
        return (s.strip("'\""),)

    def _parse_bool(val: str) -> bool:
        return val.strip().lower() in ("true", "yes", "1", "on")

    for raw in m.group(1).splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key_raw, _, val = line.partition(":")
        key = _normalise_key(key_raw)
        val = val.strip()
        # Strip wrapping quotes for scalar fields.
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            scalar = val[1:-1]
        else:
            scalar = val
        if key in ("when_to_use", "whentouse") and not out["when_to_use"]:
            out["when_to_use"] = scalar
        elif key in ("allowed_tools", "allowedtools") and not out["allowed_tools"]:
            out["allowed_tools"] = _parse_list(val)
        elif key == "paths" and not out["paths"]:
            out["paths"] = _parse_list(val)
        elif key in ("requires_restart", "requiresrestart"):
            out["requires_restart"] = _parse_bool(val)
        elif key == "model" and not out["model"]:
            out["model"] = scalar
    return out


# B-176: pull just ``created_by`` from frontmatter so the loader can
# preserve "evolved" / "llm" / "user" tags written by the proposal
# materializer + auto-evo migrator. Returning None means "no
# created_by line" — caller defaults to "user" for back-compat.
_CREATED_BY_RE = re.compile(
    r"^created_by:\s*['\"]?([a-zA-Z_][a-zA-Z_0-9\-]*)['\"]?\s*$",
    re.MULTILINE,
)


def _parse_skill_md_created_by(body: str) -> str | None:
    m = _FRONTMATTER_BLOCK_RE.match(body or "")
    if m is None:
        return None
    block = m.group(1)
    cb = _CREATED_BY_RE.search(block)
    if cb is None:
        return None
    return cb.group(1).lower()
