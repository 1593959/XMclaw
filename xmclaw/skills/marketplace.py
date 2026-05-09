"""B-390 (Sprint 2): Skill marketplace MVP — curated GitHub-backed catalog.

Pure-utility module shared by:

* :mod:`xmclaw.cli.skill_marketplace` — the ``xmclaw skill *`` Typer commands
* :mod:`xmclaw.daemon.routers.skill_marketplace` — the ``/api/v2/skills/marketplace``
  HTTP surface

Responsibilities (and **only** these):

* Fetch the curated index JSON from GitHub raw (or a configurable URL).
  Cache it under ``~/.xmclaw/cache/skill_marketplace_index.json`` with a
  1-hour TTL. ``--refresh`` / ``?refresh=1`` bypasses the cache via a
  ``?v=<unix>`` query suffix that defeats GitHub's CDN.
* Resolve a skill ``id`` from the index, then ``git clone`` (or download
  a tarball) into ``~/.xmclaw/skills_user/<id>/`` — same canonical user
  skills root :class:`xmclaw.skills.user_loader.UserSkillsLoader` scans
  on daemon boot, so the CLI doesn't need to talk to a running daemon.
* Validate the cloned tree before recording the install: at minimum we
  need ``manifest.json`` OR ``skill.py`` OR ``SKILL.md``. We then pipe
  every ``*.py`` through :func:`xmclaw.security.skill_scanner.scan_directory`
  and refuse the install if any finding is CRITICAL — fail-closed.
* Track installs in ``~/.xmclaw/skills_user/.marketplace.json`` so we
  can list / remove later without re-fetching the index.

What this module deliberately does NOT do:

* It does not register with :class:`xmclaw.skills.registry.SkillRegistry`.
  The daemon's :class:`UserSkillsLoader` does that on next boot — the
  caller is told to ``xmclaw restart`` after install. This keeps the
  CLI install path independent of a running daemon process.
* It does not fetch from a paid registry, run ratings/reviews, or do
  signature verification — that's Epic #16 territory.
* It does not sandbox the install. Trust comes from (a) the curated
  index pointing at known repos and (b) the security scanner blocking
  CRITICAL findings.

Trust tiers in the index:

* ``"verified"`` — XMclaw maintainers built / vetted it. UI shows a
  green badge.
* ``"community"`` — curated but third-party. UI shows a neutral badge.
  (Future tiers like ``"vendor"`` slot in here without code changes.)

Install size budget: ~50 KB per skill. The marketplace is a discovery
layer for small, single-purpose skills, not a general-purpose package
manager — anything bigger should live in the user's own repo and be
``git clone``'d directly into ``~/.xmclaw/skills_user/<id>/``.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from xmclaw.utils.paths import data_dir, user_skills_dir

log = logging.getLogger(__name__)


# Hosted index URL. Override via ``XMC_SKILL_MARKETPLACE_URL`` for local
# mocks / private mirrors. The default points at this repo's main branch
# so the catalog is single-source-of-truth and edits land via PR review.
_DEFAULT_INDEX_URL = (
    "https://raw.githubusercontent.com/1593959/XMclaw/main/"
    "docs/skill_marketplace_index.json"
)
_CACHE_TTL_SECONDS = 60 * 60  # 1 hour
_DOWNLOAD_TIMEOUT_SECONDS = 30
_GIT_CLONE_TIMEOUT_SECONDS = 60


# ── Errors ───────────────────────────────────────────────────────────────


class MarketplaceError(Exception):
    """Base class for marketplace-specific errors. Carries an ``error_code``
    so the daemon router can map us to deterministic HTTP statuses without
    string-matching exception messages."""

    def __init__(self, message: str, *, code: str = "marketplace_error") -> None:
        super().__init__(message)
        self.error_code = code


class IndexFetchError(MarketplaceError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="index_fetch_failed")


class SkillNotInIndexError(MarketplaceError):
    def __init__(self, skill_id: str) -> None:
        super().__init__(
            f"skill {skill_id!r} not in marketplace index",
            code="skill_not_found",
        )


class InstallValidationError(MarketplaceError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="install_validation_failed")


class InstallScanFailed(MarketplaceError):
    def __init__(self, message: str, *, findings: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message, code="install_scan_failed")
        self.findings = findings or []


# ── Data classes ─────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class MarketplaceSkill:
    """One row from the index JSON. Unknown / missing fields are tolerated
    so a future schema bump doesn't break older clients reading a newer
    index."""

    id: str
    name: str
    description: str
    version: str
    source: str
    license: str = ""
    tags: tuple[str, ...] = ()
    author: str = ""
    trust_tier: str = "community"
    install_size_kb: int = 0
    min_xmclaw: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "MarketplaceSkill":
        # Permissive parse: skip an entry rather than raise if a required
        # field is missing — a half-curated entry shouldn't blank the
        # whole catalog.
        sid = str(raw.get("id") or "").strip()
        if not sid:
            raise ValueError("skill entry missing 'id'")
        tags_raw = raw.get("tags") or []
        tags: tuple[str, ...]
        if isinstance(tags_raw, list):
            tags = tuple(str(t) for t in tags_raw if t)
        else:
            tags = ()
        return cls(
            id=sid,
            name=str(raw.get("name") or sid),
            description=str(raw.get("description") or ""),
            version=str(raw.get("version") or "0.0.0"),
            source=str(raw.get("source") or ""),
            license=str(raw.get("license") or ""),
            tags=tags,
            author=str(raw.get("author") or ""),
            trust_tier=str(raw.get("trust_tier") or "community"),
            install_size_kb=int(raw.get("install_size_kb") or 0),
            min_xmclaw=str(raw.get("min_xmclaw") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "source": self.source,
            "license": self.license,
            "tags": list(self.tags),
            "author": self.author,
            "trust_tier": self.trust_tier,
            "install_size_kb": self.install_size_kb,
            "min_xmclaw": self.min_xmclaw,
        }


@dataclass(frozen=True, slots=True)
class MarketplaceIndex:
    version: int
    updated: str
    skills: tuple[MarketplaceSkill, ...]

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "MarketplaceIndex":
        if not isinstance(raw, dict):
            raise IndexFetchError("index root must be a JSON object")
        skills_raw = raw.get("skills") or []
        if not isinstance(skills_raw, list):
            raise IndexFetchError("index 'skills' must be a list")
        skills: list[MarketplaceSkill] = []
        for entry in skills_raw:
            if not isinstance(entry, dict):
                continue
            try:
                skills.append(MarketplaceSkill.from_dict(entry))
            except ValueError as exc:
                log.warning("marketplace.skip_bad_entry %s: %s", entry, exc)
                continue
        return cls(
            version=int(raw.get("version") or 1),
            updated=str(raw.get("updated") or ""),
            skills=tuple(skills),
        )

    def find(self, skill_id: str) -> MarketplaceSkill | None:
        for s in self.skills:
            if s.id == skill_id:
                return s
        return None

    def search(self, query: str) -> list[MarketplaceSkill]:
        """Substring + tag match. Empty query returns the full list."""
        q = (query or "").strip().lower()
        if not q:
            return list(self.skills)
        out: list[MarketplaceSkill] = []
        for s in self.skills:
            haystacks = [
                s.id.lower(),
                s.name.lower(),
                s.description.lower(),
                s.author.lower(),
                " ".join(s.tags).lower(),
            ]
            if any(q in h for h in haystacks):
                out.append(s)
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "updated": self.updated,
            "skills": [s.to_dict() for s in self.skills],
        }


@dataclass(frozen=True, slots=True)
class InstalledSkill:
    """Recorded in ``~/.xmclaw/skills_user/.marketplace.json``."""

    id: str
    version: str
    source: str
    install_path: str
    installed_at: float
    trust_tier: str = "community"
    name: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "source": self.source,
            "install_path": self.install_path,
            "installed_at": self.installed_at,
            "trust_tier": self.trust_tier,
            "name": self.name,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "InstalledSkill":
        return cls(
            id=str(raw.get("id") or ""),
            version=str(raw.get("version") or ""),
            source=str(raw.get("source") or ""),
            install_path=str(raw.get("install_path") or ""),
            installed_at=float(raw.get("installed_at") or 0),
            trust_tier=str(raw.get("trust_tier") or "community"),
            name=str(raw.get("name") or ""),
        )


@dataclass
class InstallResult:
    skill_id: str
    install_path: Path
    version: str
    source: str
    findings: list[dict[str, Any]] = field(default_factory=list)


# ── Path helpers ────────────────────────────────────────────────────────


def cache_dir() -> Path:
    """Marketplace cache root. Honours :func:`xmclaw.utils.paths.data_dir`
    so ``XMC_DATA_DIR`` reroutes the whole install."""
    return data_dir() / "cache"


def index_cache_path() -> Path:
    return cache_dir() / "skill_marketplace_index.json"


def installed_registry_path() -> Path:
    """Where we record marketplace-installed skills. Lives inside
    ``user_skills_dir()`` so a workspace-wipe also clears this manifest;
    leading dot keeps it out of the directory iteration that
    UserSkillsLoader does."""
    return user_skills_dir() / ".marketplace.json"


def index_url() -> str:
    return os.environ.get("XMC_SKILL_MARKETPLACE_URL") or _DEFAULT_INDEX_URL


# ── Index fetch + cache ─────────────────────────────────────────────────


def _read_cache() -> tuple[dict[str, Any] | None, float]:
    p = index_cache_path()
    if not p.exists():
        return None, 0.0
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, 0.0
    if not isinstance(raw, dict):
        return None, 0.0
    try:
        mtime = p.stat().st_mtime
    except OSError:
        mtime = 0.0
    return raw, mtime


def _write_cache(raw: dict[str, Any]) -> None:
    p = index_cache_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")


def fetch_index(*, refresh: bool = False, now: float | None = None) -> MarketplaceIndex:
    """Read the curated index. Returns a parsed :class:`MarketplaceIndex`.

    - ``refresh=False`` and a fresh cache (<= ``_CACHE_TTL_SECONDS`` old):
      serve from cache, no network call.
    - ``refresh=True`` OR stale cache: HTTP-fetch, write cache, return.
      ``refresh=True`` appends ``?v=<unix>`` to bust GitHub's raw CDN.
    - Network failure with a non-empty cache: fall back to the cache and
      log a warning. Network failure with no cache: raise
      :class:`IndexFetchError`.
    """
    now = now if now is not None else time.time()
    cached, mtime = _read_cache()
    if not refresh and cached is not None and (now - mtime) <= _CACHE_TTL_SECONDS:
        return MarketplaceIndex.from_dict(cached)

    url = index_url()
    if refresh:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}v={int(now)}"

    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "xmclaw-marketplace/1.0", "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=_DOWNLOAD_TIMEOUT_SECONDS) as resp:
            body = resp.read().decode("utf-8")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        if cached is not None:
            log.warning(
                "marketplace.index_fetch_failed; serving stale cache: %s", exc,
            )
            return MarketplaceIndex.from_dict(cached)
        raise IndexFetchError(f"failed to fetch {url}: {exc}") from exc

    try:
        raw = json.loads(body)
    except json.JSONDecodeError as exc:
        if cached is not None:
            log.warning(
                "marketplace.index_parse_failed; serving stale cache: %s", exc,
            )
            return MarketplaceIndex.from_dict(cached)
        raise IndexFetchError(f"index at {url} is not valid JSON: {exc}") from exc

    _write_cache(raw)
    return MarketplaceIndex.from_dict(raw)


# ── Installed-registry helpers ──────────────────────────────────────────


def _read_installed_registry() -> dict[str, InstalledSkill]:
    p = installed_registry_path()
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    rows = raw.get("skills") or []
    if not isinstance(rows, list):
        return {}
    out: dict[str, InstalledSkill] = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        rec = InstalledSkill.from_dict(r)
        if rec.id:
            out[rec.id] = rec
    return out


def _write_installed_registry(records: dict[str, InstalledSkill]) -> None:
    p = installed_registry_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "skills": [r.to_dict() for r in records.values()],
    }
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def list_installed() -> list[InstalledSkill]:
    """Return marketplace-installed skills, sorted by id. Skills installed
    by hand (``git clone`` directly into the user-skills dir) are NOT
    returned by this — the daemon's ``/api/v2/skills`` listing is
    canonical for "what skills does my agent have"."""
    return sorted(_read_installed_registry().values(), key=lambda r: r.id)


# ── Source resolution ───────────────────────────────────────────────────


def _resolve_source(source: str) -> dict[str, Any]:
    """Translate the index ``source`` field into a concrete strategy.

    Supported shapes:
      * ``github:<owner>/<repo>`` — git clone via HTTPS
      * ``git+<url>`` — generic git clone
      * ``https://...`` — direct URL (treated as git clone if it ends
        in ``.git``, else as tarball download)
    """
    s = source.strip()
    if s.startswith("github:"):
        slug = s[len("github:"):]
        # Reject path traversal / shell metacharacters — slug must look
        # like ``<owner>/<repo>``.
        if "/" not in slug or any(c in slug for c in ("..", " ", "\n", "\r", ";", "&", "|", "$", "`")):
            raise InstallValidationError(f"invalid github source slug: {source!r}")
        return {"kind": "git", "url": f"https://github.com/{slug}.git"}
    if s.startswith("git+"):
        url = s[len("git+"):]
        return {"kind": "git", "url": url}
    if s.startswith("https://"):
        if s.endswith(".git"):
            return {"kind": "git", "url": s}
        return {"kind": "tarball", "url": s}
    raise InstallValidationError(
        f"unsupported source scheme: {source!r}; "
        "expected 'github:<owner>/<repo>' / 'git+...' / 'https://...'"
    )


# ── Install flow ────────────────────────────────────────────────────────


def _git_clone(url: str, target: Path, *, runner: Any = None) -> None:
    """Run ``git clone --depth=1 <url> <target>``. ``runner`` is the
    callable used to invoke the subprocess so tests can monkeypatch."""
    fn = runner if runner is not None else subprocess.run
    try:
        result = fn(
            ["git", "clone", "--depth=1", "--quiet", url, str(target)],
            check=False,
            capture_output=True,
            text=True,
            timeout=_GIT_CLONE_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:
        raise InstallValidationError(
            "git executable not found on PATH — install git first"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise InstallValidationError(f"git clone timed out: {url}") from exc
    rc = getattr(result, "returncode", 1)
    if rc != 0:
        stderr = (getattr(result, "stderr", "") or "").strip()
        raise InstallValidationError(f"git clone failed (rc={rc}): {stderr}")


def _validate_structure(install_path: Path) -> dict[str, Any]:
    """Confirm the cloned dir looks like a skill. Returns a small report
    on what was found so the caller can surface it to the user."""
    if not install_path.is_dir():
        raise InstallValidationError(f"install path is not a directory: {install_path}")
    has_manifest = (install_path / "manifest.json").is_file()
    has_skill_md = (install_path / "SKILL.md").is_file()
    has_skill_py = (install_path / "skill.py").is_file()
    has_init_py = (install_path / "__init__.py").is_file()
    if not (has_manifest or has_skill_md or has_skill_py or has_init_py):
        raise InstallValidationError(
            f"directory at {install_path} has none of: manifest.json, "
            "SKILL.md, skill.py, __init__.py — does not look like a skill"
        )
    # If there's a python skill, do a cheap textual check that *something*
    # in it talks about Skill / SkillBase. We don't run the file (that's
    # the daemon's UserSkillsLoader job).
    if has_skill_py:
        try:
            src = (install_path / "skill.py").read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise InstallValidationError(
                f"skill.py exists but cannot be read: {exc}"
            ) from exc
        if "Skill" not in src and "skill" not in src.lower():
            raise InstallValidationError(
                "skill.py present but contains no 'Skill' identifier — "
                "expected a SkillBase / Skill subclass"
            )
    return {
        "has_manifest": has_manifest,
        "has_skill_md": has_skill_md,
        "has_skill_py": has_skill_py,
        "has_init_py": has_init_py,
    }


def _scan_for_critical(install_path: Path) -> list[dict[str, Any]]:
    """Run :func:`xmclaw.security.skill_scanner.scan_directory` on the
    install. Any CRITICAL finding raises :class:`InstallScanFailed` so we
    fail-closed. Lower-severity findings are returned to the caller for
    surfacing — they don't block install.
    """
    # Lazy import: keeps ``import xmclaw.skills.marketplace`` cheap when
    # the caller only needs index parsing (e.g. the daemon router's
    # ``GET /marketplace`` endpoint that doesn't install anything).
    from xmclaw.security.skill_scanner import scan_directory
    from xmclaw.security.tool_guard.models import GuardSeverity

    findings_out: list[dict[str, Any]] = []
    critical_msgs: list[str] = []
    for result in scan_directory(install_path):
        for finding in result.findings:
            entry = {
                "rule_id": finding.rule_id,
                "severity": finding.severity.value if hasattr(finding.severity, "value") else str(finding.severity),
                "title": finding.title,
                "file": finding.tool_name,
                "description": finding.description,
            }
            findings_out.append(entry)
            if finding.severity == GuardSeverity.CRITICAL:
                critical_msgs.append(
                    f"{finding.rule_id} in {finding.tool_name}: {finding.title}"
                )
    if critical_msgs:
        raise InstallScanFailed(
            "marketplace install rejected: CRITICAL findings — "
            + "; ".join(critical_msgs[:5]),
            findings=findings_out,
        )
    return findings_out


def install(
    skill_id: str,
    *,
    index: MarketplaceIndex | None = None,
    refresh: bool = False,
    git_runner: Any = None,
    install_root: Path | None = None,
    now: float | None = None,
) -> InstallResult:
    """Install a skill by id from the curated index.

    Resolves the index entry, clones the source into
    ``~/.xmclaw/skills_user/<id>/`` (or ``install_root/<id>/`` when
    explicitly overridden — used by tests), validates structure, runs the
    security scan, and records the install in
    ``~/.xmclaw/skills_user/.marketplace.json``.

    Idempotency: re-installing an already-installed skill is treated as
    an upgrade — the old directory is removed first. Callers should
    prompt before doing this.

    :raises SkillNotInIndexError: if the id isn't in the catalog.
    :raises InstallValidationError: structure check failed.
    :raises InstallScanFailed: skill_scanner found a CRITICAL finding.
    """
    idx = index if index is not None else fetch_index(refresh=refresh)
    skill = idx.find(skill_id)
    if skill is None:
        raise SkillNotInIndexError(skill_id)

    root = install_root if install_root is not None else user_skills_dir()
    target = root / skill.id
    # Wipe an existing install — user expectation for ``xmclaw skill install``
    # of a known id is "upgrade", not "fail because directory exists".
    if target.exists():
        shutil.rmtree(target)
    root.mkdir(parents=True, exist_ok=True)

    resolved = _resolve_source(skill.source)
    if resolved["kind"] == "git":
        _git_clone(resolved["url"], target, runner=git_runner)
    elif resolved["kind"] == "tarball":
        # Tarball path kept stub-shaped for now — we cover it in tests as
        # a "not yet implemented" branch so a future Epic #16 contributor
        # has a hook. Most useful skills are in git already.
        raise InstallValidationError(
            "tarball sources not supported yet; use 'github:...' or 'git+...'"
        )
    else:  # pragma: no cover — guarded above
        raise InstallValidationError(f"unknown source kind: {resolved['kind']}")

    findings: list[dict[str, Any]] = []
    try:
        _validate_structure(target)
        findings = _scan_for_critical(target)
    except MarketplaceError:
        # Roll back on any post-clone failure — leaving a half-installed
        # directory means the daemon's UserSkillsLoader picks it up on
        # next boot, which is exactly the wrong outcome.
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        raise

    # Record the install so ``xmclaw skill installed`` can list it.
    records = _read_installed_registry()
    records[skill.id] = InstalledSkill(
        id=skill.id,
        version=skill.version,
        source=skill.source,
        install_path=str(target),
        installed_at=now if now is not None else time.time(),
        trust_tier=skill.trust_tier,
        name=skill.name,
    )
    _write_installed_registry(records)

    return InstallResult(
        skill_id=skill.id,
        install_path=target,
        version=skill.version,
        source=skill.source,
        findings=findings,
    )


def remove(skill_id: str, *, install_root: Path | None = None) -> bool:
    """Uninstall by id. Returns ``True`` if anything was removed.

    Removes both the install directory and the entry from the
    installed-registry file. Idempotent — removing an already-uninstalled
    skill is a no-op that returns ``False``."""
    root = install_root if install_root is not None else user_skills_dir()
    records = _read_installed_registry()
    rec = records.pop(skill_id, None)
    target: Path
    if rec is not None:
        target = Path(rec.install_path)
    else:
        # Fall back to the canonical path so a hand-installed dir can be
        # removed via the same command.
        target = root / skill_id
    removed = False
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
        removed = True
    if rec is not None:
        _write_installed_registry(records)
        removed = True
    return removed
