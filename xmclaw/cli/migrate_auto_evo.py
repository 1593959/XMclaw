"""B-171: one-shot migrator for legacy ``~/.xmclaw/auto_evo/skills/``.

Pre-Epic-#24-Phase-1, evolution was driven by a Node.js subsystem
(``xm-auto-evo``) that wrote skills to ``~/.xmclaw/auto_evo/skills/<id>/``
with a hash + version suffix in the directory name (``auto_repair_40bb68_v38``).
Phase 1 deleted the loader; the skills are still on disk but XMclaw
can't see them anymore.

This module migrates the salvageable subset into the canonical
``~/.xmclaw/skills_user/<auto-kebab-id>/SKILL.md`` so the
post-Epic-#24 user_loader picks them up on next boot.

Migration rules
---------------

1. Skip ``xm-auto-evo`` (the deleted Node project itself, not a skill).
2. Skip directories without a ``SKILL.md`` file.
3. Skip directories whose frontmatter ``name`` is missing or non-string.
4. Group by canonical ``name`` (frontmatter, not directory name —
   directory names carry hash + version cruft that the agent never
   produced deliberately). Pick the highest version among siblings:
   ``_v<N>`` suffix when present, else ``mtime`` tie-break.
5. Build target id: ``auto-`` + ``name`` with ``_`` → ``-`` and
   lowercased; if the result has only one segment after ``auto-``,
   leave it (legacy data — we honour the original lineage name even
   when it would fail the B-169 LLM-side validation, because the
   user already typed it into the journal).
6. If the target directory already exists in ``skills_user``, skip
   (do NOT clobber a hand-installed skill).
7. Rewrite frontmatter:
     - keep ``name``, ``description``, ``signals_match`` (renamed to
       ``triggers`` so user_loader's parser picks them up)
     - drop ``auto_created`` / ``level`` / ``created_at`` (old-system
       housekeeping, no longer meaningful)
     - inject ``created_by: evolved`` so the Skills UI badge it
       correctly via B-166's manifest classifier
     - inject ``migrated_from: <old-dir-name>`` for audit
"""
from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

_log = logging.getLogger(__name__)


# Tail of an old auto_evo dir name: ``..._v38`` → version 38.
_VERSION_TAIL_RE = re.compile(r"_v(\d+)$")

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


@dataclass(frozen=True, slots=True)
class MigrationCandidate:
    """One lineage's winner (highest-version directory)."""

    canonical_name: str    # frontmatter `name` value, the lineage key
    source_dir: Path       # the chosen directory under auto_evo/skills/
    version: int           # 0 if no _v<N> suffix
    target_id: str         # the auto-kebab-case dir under skills_user/


@dataclass(frozen=True, slots=True)
class MigrationResult:
    """One migration outcome — what was written / skipped + why."""

    target_id: str
    source_dir: Path | None
    target_path: Path | None
    ok: bool
    skipped: bool = False
    reason: str = ""


# ── frontmatter helpers ─────────────────────────────────────────────


def _parse_frontmatter(text: str) -> dict[str, str | list[str]]:
    """Lightweight YAML-ish reader.

    Same dialect as :func:`xmclaw.skills.user_loader._parse_skill_md_frontmatter`
    handles — flat key/value lines plus ``key: [a, b]`` and indented
    ``- item`` lists. Anything else gets ignored.
    """
    out: dict[str, str | list[str]] = {}
    m = _FRONTMATTER_RE.match(text or "")
    if m is None:
        return out
    block = m.group(1).splitlines()
    i = 0
    while i < len(block):
        raw = block[i]
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        if ":" not in line:
            i += 1
            continue
        key, _, val = line.partition(":")
        key = key.strip().lower()
        val = val.strip()
        if val == "":
            # Multi-line list: peek ahead at indented "- item" lines.
            items: list[str] = []
            j = i + 1
            while j < len(block):
                nxt = block[j]
                stripped = nxt.strip()
                if stripped.startswith("- "):
                    items.append(stripped[2:].strip().strip("'\""))
                    j += 1
                    continue
                if not stripped:
                    j += 1
                    continue
                break
            if items:
                out[key] = items
            i = j
            continue
        if val.startswith("[") and val.endswith("]"):
            inner = val[1:-1]
            parts = [p.strip().strip("'\"") for p in inner.split(",")]
            out[key] = [p for p in parts if p]
            i += 1
            continue
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        out[key] = val
        i += 1
    return out


def _to_kebab_target_id(name: str) -> str:
    """``entity_reference`` → ``auto-entity-reference``.

    No length / multi-segment guard here — legacy lineages can be
    single-word (``repair``, ``analysis``) and we honour them. The
    B-169 normaliser only runs on LLM-produced names, not on
    backfill data the user clearly already considered useful.
    """
    cleaned = re.sub(r"[\s._]+", "-", str(name).strip().lower())
    cleaned = re.sub(r"[^a-z0-9-]", "", cleaned)
    cleaned = re.sub(r"-+", "-", cleaned).strip("-")
    if not cleaned:
        return ""
    if cleaned.startswith("auto-"):
        # Avoid double-prefix (rare but possible if frontmatter `name`
        # already has it).
        return cleaned
    return f"auto-{cleaned}"


def _version_from_dirname(dirname: str) -> int:
    """Extract trailing ``_v<N>`` if present; 0 otherwise."""
    m = _VERSION_TAIL_RE.search(dirname)
    return int(m.group(1)) if m is not None else 0


# ── discovery ──────────────────────────────────────────────────────


# B-178: shell-skill detector. Pre-Epic-#24-Phase-1 the auto_evo
# system synthesised SKILL.md bodies whose only "implementation" was
# "调用 <name> 的主要函数...具体函数取决于 index.js 中的导出". After
# Phase 1 ripped out the Node project, those index.js files are gone
# — so the skills are just text shells pointing at deleted code. Joint
# audit (probe `evolution_quality`) found 6 of these silently
# pollute the registry. Filter them at migration time so re-runs
# don't recreate them.
_SHELL_BODY_RE = re.compile(
    r"index\.js|具体函数取决于|调用.*主要函数|"
    r"specialword|magicstring",  # b29 test artifact
    re.IGNORECASE,
)


def _is_shell_body(body_after_frontmatter: str) -> bool:
    """True when the body is one of the known auto_evo placeholder
    patterns (references to deleted index.js, b29 test stub, etc.).
    Caller skips these skills rather than copying them forward."""
    if not body_after_frontmatter or len(body_after_frontmatter.strip()) < 30:
        # Empty or trivially short bodies — also shells.
        return True
    if _SHELL_BODY_RE.search(body_after_frontmatter):
        return True
    return False


def discover_candidates(
    auto_evo_skills_root: Path,
) -> list[MigrationCandidate]:
    """Walk ``auto_evo_skills_root`` and pick one winner per lineage.

    Returns an empty list when the root doesn't exist or holds no
    salvageable directories. B-178: directories whose SKILL.md body
    matches a known shell pattern (referencing the deleted index.js
    Node project) are skipped — they migrated as zombies pre-B-178
    and cluttered the registry without ever doing useful work.
    """
    if not auto_evo_skills_root.is_dir():
        return []

    # Lineage name → list of (source_dir, version).
    lineages: dict[str, list[tuple[Path, int]]] = {}

    for entry in sorted(auto_evo_skills_root.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name == "xm-auto-evo":
            # The deleted Node project itself, never a skill.
            continue
        skill_md = entry / "SKILL.md"
        if not skill_md.is_file():
            continue
        try:
            text = skill_md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fm = _parse_frontmatter(text)
        name = fm.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        # B-178: skip shells.
        m = _FRONTMATTER_RE.match(text)
        body_after = text[m.end():] if m is not None else text
        if _is_shell_body(body_after):
            _log.info(
                "migrate_auto_evo.shell_skipped dir=%s — body references "
                "deleted index.js / placeholder pattern",
                entry.name,
            )
            continue
        version = _version_from_dirname(entry.name)
        lineages.setdefault(name.strip(), []).append((entry, version))

    candidates: list[MigrationCandidate] = []
    for name, versions in lineages.items():
        winner = max(
            versions,
            key=lambda pv: (pv[1], pv[0].stat().st_mtime),
        )
        target_id = _to_kebab_target_id(name)
        if not target_id:
            continue
        candidates.append(MigrationCandidate(
            canonical_name=name,
            source_dir=winner[0],
            version=winner[1],
            target_id=target_id,
        ))
    candidates.sort(key=lambda c: c.target_id)
    return candidates


# ── migration ──────────────────────────────────────────────────────


def _rewrite_frontmatter(
    text: str, *, source_dirname: str,
) -> str:
    """Strip auto_evo housekeeping fields, inject ``created_by`` +
    ``migrated_from``, rename ``signals_match`` → ``triggers``."""
    fm = _parse_frontmatter(text)
    m = _FRONTMATTER_RE.match(text or "")
    body_after = text[m.end():] if m is not None else (text or "")

    # Drop fields that no longer apply.
    for k in ("auto_created", "created_at", "level"):
        fm.pop(k, None)
    # Rename signals_match → triggers (skills.sh + B-170 convention).
    if "triggers" not in fm and "signals_match" in fm:
        fm["triggers"] = fm.pop("signals_match")
    elif "signals_match" in fm:
        fm.pop("signals_match", None)

    fm["created_by"] = "evolved"
    fm["migrated_from"] = source_dirname

    def _yaml_value(v: str | list[str]) -> str:
        if isinstance(v, list):
            return "[" + ", ".join(repr(str(x)) for x in v) + "]"
        s = str(v).replace("---", "—")
        # Quote if it contains a colon, comma, or starts with [ to keep
        # the parser unambiguous — many SKILL.md descriptions do.
        if any(ch in s for ch in (":", ",")) or s.startswith("["):
            return f"'{s}'"
        return s

    lines = ["---"]
    # Stable ordering: name → description → triggers → others alphabetical.
    primary = ["name", "description", "triggers"]
    for k in primary:
        if k in fm:
            lines.append(f"{k}: {_yaml_value(fm[k])}")
    for k in sorted(fm):
        if k in primary:
            continue
        lines.append(f"{k}: {_yaml_value(fm[k])}")
    lines.append("---")
    lines.append("")
    return "\n".join(lines) + body_after.lstrip("\n")


def migrate(
    auto_evo_skills_root: Path,
    skills_user_root: Path,
    *,
    dry_run: bool = False,
) -> list[MigrationResult]:
    """Materialize each lineage's winner into ``skills_user_root``.

    Returns one :class:`MigrationResult` per discovered candidate.
    Already-existing target directories are skipped (we never clobber
    a hand-installed or already-migrated skill).
    """
    results: list[MigrationResult] = []
    candidates = discover_candidates(auto_evo_skills_root)
    for c in candidates:
        target_dir = skills_user_root / c.target_id
        target_md = target_dir / "SKILL.md"

        if target_dir.exists():
            results.append(MigrationResult(
                target_id=c.target_id,
                source_dir=c.source_dir,
                target_path=target_md,
                ok=True, skipped=True,
                reason="target already exists — left untouched",
            ))
            continue

        try:
            text = (c.source_dir / "SKILL.md").read_text(
                encoding="utf-8", errors="replace",
            )
        except OSError as exc:
            results.append(MigrationResult(
                target_id=c.target_id, source_dir=c.source_dir,
                target_path=None, ok=False, skipped=False,
                reason=f"read failed: {exc}",
            ))
            continue

        rewritten = _rewrite_frontmatter(
            text, source_dirname=c.source_dir.name,
        )

        if dry_run:
            results.append(MigrationResult(
                target_id=c.target_id, source_dir=c.source_dir,
                target_path=target_md, ok=True, skipped=False,
                reason="dry-run (would write)",
            ))
            continue

        try:
            target_dir.mkdir(parents=True, exist_ok=False)
            target_md.write_text(rewritten, encoding="utf-8")
        except OSError as exc:
            # Roll back the empty dir we just created if write failed.
            if target_dir.exists() and not target_md.exists():
                try:
                    shutil.rmtree(target_dir)
                except OSError:
                    pass
            results.append(MigrationResult(
                target_id=c.target_id, source_dir=c.source_dir,
                target_path=target_md, ok=False, skipped=False,
                reason=f"write failed: {exc}",
            ))
            continue

        results.append(MigrationResult(
            target_id=c.target_id, source_dir=c.source_dir,
            target_path=target_md, ok=True, skipped=False,
            reason="migrated",
        ))
    return results
