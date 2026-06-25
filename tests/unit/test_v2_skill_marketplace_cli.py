"""B-390 (Sprint 2): Skill marketplace MVP — CLI + library tests.

Covers :mod:`xmclaw.skills.marketplace` (the pure utility) and
:mod:`xmclaw.cli.skill_marketplace` (the Typer subcommands). Tests:

Library layer:
  * Index parsing tolerates malformed entries (missing id → skipped,
    not raised).
  * Index search filters by id / name / description / tag / author.
  * Cache TTL: <1h returns cache without HTTP, >=1h refetches.
  * ``--refresh`` adds ``?v=<unix>`` cache-buster to the URL.
  * Network failure with cache → serve cache + warn.
  * Network failure with no cache → :class:`IndexFetchError`.
  * Source resolution: ``github:`` / ``git+`` / ``https://...`` shapes
    + rejection of path-traversal slugs.
  * Install flow: monkeypatched git-clone clones a fake skill, validates
    structure, scans, records install, returns install path.
  * Install rejects when CRITICAL findings appear.
  * Install rolls back the directory if validation fails after clone.
  * ``remove`` deletes the install dir + drops the registry row.

CLI layer:
  * ``xmclaw skill list-marketplace`` prints catalog + ``--json`` mode.
  * ``xmclaw skill search`` filters output.
  * ``xmclaw skill install <id> --yes`` runs the install with monkey-
    patched git, prints the success line.
  * ``xmclaw skill remove <id> --yes`` removes the install + the
    registry row.
  * ``xmclaw skill installed`` enumerates the install registry.
  * Unknown id surfaces as a clean error, exit code 1.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from xmclaw.cli.main import app as _cli_app
from xmclaw.skills import marketplace as mp


# ── Fixtures ────────────────────────────────────────────────────────────


_FAKE_INDEX = {
    "version": 1,
    "updated": "2026-05-09",
    "skills": [
        {
            "id": "alpha-skill",
            "name": "Alpha Skill",
            "description": "First fake skill for testing",
            "version": "1.0.0",
            "source": "github:fake/xmclaw-skill-alpha",
            "license": "MIT",
            "tags": ["dev", "test"],
            "author": "alice",
            "trust_tier": "verified",
            "install_size_kb": 10,
            "min_xmclaw": "0.2.0",
        },
        {
            "id": "beta-skill",
            "name": "Beta Skill",
            "description": "Second skill, focuses on git workflow",
            "version": "0.5.0",
            "source": "github:fake/xmclaw-skill-beta",
            "license": "Apache-2.0",
            "tags": ["git"],
            "author": "bob",
            "trust_tier": "community",
            "install_size_kb": 12,
        },
        {
            # malformed entry: missing id — should be skipped silently
            "name": "Headless Skill",
            "version": "1.0.0",
            "source": "github:fake/headless",
        },
    ],
}


@pytest.fixture
def isolated_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Reroute XMC_DATA_DIR + XMC_V2_USER_SKILLS_DIR so cache + install
    paths land in tmp_path, keeping tests hermetic."""
    monkeypatch.setenv("XMC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XMC_V2_USER_SKILLS_DIR", str(tmp_path / "skills_user"))
    return tmp_path


def _seed_cache(workspace: Path, raw: dict[str, Any], *, age_seconds: float = 0) -> Path:
    """Write a fake index cache file with controllable age."""
    cache = workspace / "cache" / "skill_marketplace_index.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(raw), encoding="utf-8")
    if age_seconds > 0:
        old = time.time() - age_seconds
        import os
        os.utime(cache, (old, old))
    return cache


def _fake_git_runner_factory(file_writes: dict[str, str]):
    """Returns a runner callable that, on `git clone`, writes the given
    files into the target dir and returns rc=0."""
    def _runner(args, **kwargs):
        # Find the target arg (last positional-ish to git clone)
        target = Path(args[-1])
        target.mkdir(parents=True, exist_ok=True)
        for rel, body in file_writes.items():
            f = target / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(body, encoding="utf-8")

        class _Result:
            returncode = 0
            stdout = ""
            stderr = ""
        return _Result()
    return _runner


# ── Library — index parsing ─────────────────────────────────────────────


def test_index_parses_and_skips_malformed_entries() -> None:
    idx = mp.MarketplaceIndex.from_dict(_FAKE_INDEX)
    assert idx.version == 1
    assert idx.updated == "2026-05-09"
    # 2 valid + 1 missing-id should be skipped → 2 entries
    assert len(idx.skills) == 2
    assert idx.skills[0].id == "alpha-skill"
    assert idx.skills[1].id == "beta-skill"


def test_index_search_matches_substring_and_tags() -> None:
    idx = mp.MarketplaceIndex.from_dict(_FAKE_INDEX)
    # Full list when query is empty
    assert len(idx.search("")) == 2
    # Tag match
    assert {s.id for s in idx.search("git")} == {"beta-skill"}
    # Description substring
    assert {s.id for s in idx.search("workflow")} == {"beta-skill"}
    # Author match
    assert {s.id for s in idx.search("alice")} == {"alpha-skill"}
    # Case insensitive
    assert {s.id for s in idx.search("ALPHA")} == {"alpha-skill"}
    # No match
    assert idx.search("nope") == []


def test_index_find_returns_skill_or_none() -> None:
    idx = mp.MarketplaceIndex.from_dict(_FAKE_INDEX)
    assert idx.find("alpha-skill") is not None
    assert idx.find("does-not-exist") is None


def test_index_from_dict_rejects_non_object() -> None:
    with pytest.raises(mp.IndexFetchError):
        mp.MarketplaceIndex.from_dict([])  # type: ignore[arg-type]


# ── Library — fetch_index cache TTL ─────────────────────────────────────


def test_fetch_index_uses_fresh_cache_without_http(
    isolated_workspace: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_cache(isolated_workspace, _FAKE_INDEX, age_seconds=10)

    def _explode(*a, **kw):
        raise AssertionError("fetch_index reached the network despite fresh cache")
    monkeypatch.setattr(mp.urllib.request, "urlopen", _explode)

    idx = mp.fetch_index()
    assert len(idx.skills) == 2


def test_fetch_index_refetches_on_stale_cache(
    isolated_workspace: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_cache(isolated_workspace, _FAKE_INDEX, age_seconds=mp._CACHE_TTL_SECONDS + 60)
    fresh = dict(_FAKE_INDEX, updated="2026-05-10")
    calls: list[str] = []

    class _FakeResp:
        def __init__(self, body: bytes) -> None:
            self._body = body
        def read(self) -> bytes:
            return self._body
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def _fake_urlopen(req, timeout=0):
        calls.append(req.full_url)
        return _FakeResp(json.dumps(fresh).encode("utf-8"))

    monkeypatch.setattr(mp.urllib.request, "urlopen", _fake_urlopen)
    idx = mp.fetch_index()
    assert idx.updated == "2026-05-10"
    assert len(calls) == 1


def test_fetch_index_refresh_appends_cache_buster(
    isolated_workspace: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_cache(isolated_workspace, _FAKE_INDEX, age_seconds=10)
    seen_urls: list[str] = []

    class _FakeResp:
        def __init__(self, body: bytes) -> None:
            self._body = body
        def read(self):
            return self._body
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def _fake_urlopen(req, timeout=0):
        seen_urls.append(req.full_url)
        return _FakeResp(json.dumps(_FAKE_INDEX).encode("utf-8"))

    monkeypatch.setattr(mp.urllib.request, "urlopen", _fake_urlopen)
    mp.fetch_index(refresh=True)
    assert len(seen_urls) == 1
    assert "?v=" in seen_urls[0] or "&v=" in seen_urls[0]


def test_fetch_index_falls_back_to_cache_on_network_error(
    isolated_workspace: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_cache(isolated_workspace, _FAKE_INDEX, age_seconds=mp._CACHE_TTL_SECONDS + 60)

    def _broken(*a, **kw):
        raise mp.urllib.error.URLError("network down")

    monkeypatch.setattr(mp.urllib.request, "urlopen", _broken)
    idx = mp.fetch_index()
    assert len(idx.skills) == 2  # served from cache


def test_fetch_index_raises_when_no_cache_and_network_down(
    isolated_workspace: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _broken(*a, **kw):
        raise mp.urllib.error.URLError("no network")
    monkeypatch.setattr(mp.urllib.request, "urlopen", _broken)
    with pytest.raises(mp.IndexFetchError):
        mp.fetch_index()


def test_fetch_index_uses_env_url(
    isolated_workspace: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XMC_SKILL_MARKETPLACE_URL", "https://example.test/index.json")
    seen: list[str] = []

    class _FakeResp:
        def read(self):
            return json.dumps(_FAKE_INDEX).encode("utf-8")
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def _fake_urlopen(req, timeout=0):
        seen.append(req.full_url)
        return _FakeResp()

    monkeypatch.setattr(mp.urllib.request, "urlopen", _fake_urlopen)
    mp.fetch_index(refresh=True)
    assert any("example.test" in u for u in seen)


# ── Library — source resolution ─────────────────────────────────────────


def test_resolve_source_github_shape() -> None:
    r = mp._resolve_source("github:foo/bar")
    assert r["kind"] == "git"
    assert r["url"] == "https://github.com/foo/bar.git"


def test_resolve_source_rejects_traversal() -> None:
    with pytest.raises(mp.InstallValidationError):
        mp._resolve_source("github:foo/../etc")
    with pytest.raises(mp.InstallValidationError):
        mp._resolve_source("github:foo/bar; rm -rf /")


def test_resolve_source_git_plus() -> None:
    r = mp._resolve_source("git+https://gitlab.example/x/y.git")
    assert r["kind"] == "git"
    assert r["url"] == "https://gitlab.example/x/y.git"


def test_resolve_source_unsupported_scheme() -> None:
    with pytest.raises(mp.InstallValidationError):
        mp._resolve_source("ftp://nope.example/x")


# ── Wave-32+ (2026-05-19): local-path source support ──────────────────


def test_resolve_source_local_posix_absolute() -> None:
    r = mp._resolve_source("/home/me/my-skill")
    assert r["kind"] == "local"
    assert r["path"] == "/home/me/my-skill"


def test_resolve_source_local_windows_drive() -> None:
    r = mp._resolve_source(r"C:\Users\me\my-skill")
    assert r["kind"] == "local"
    assert r["path"] == r"C:\Users\me\my-skill"


def test_resolve_source_local_file_url() -> None:
    r = mp._resolve_source("file:///tmp/foo")
    assert r["kind"] == "local"


def test_resolve_source_local_unc_path() -> None:
    r = mp._resolve_source(r"\\fileserver\share\skill")
    assert r["kind"] == "local"


def test_install_from_source_local_copies_dir(
    isolated_workspace: Path, tmp_path: Path,
) -> None:
    """End-to-end: a local directory with a SKILL.md gets copied
    into the install root and the install registry records it."""
    src = tmp_path / "hyperframes-clone"
    src.mkdir()
    (src / "SKILL.md").write_text(
        "---\nname: hyperframes\ndescription: demo\n---\n# hi\n",
        encoding="utf-8",
    )
    result = mp.install_from_source(str(src))
    assert result.skill_id == "hyperframes-clone"
    assert (result.install_path / "SKILL.md").is_file()
    # Registry round-trip — list_installed picks it up.
    rows = mp.list_installed()
    assert any(r.id == "hyperframes-clone" for r in rows)


def test_install_from_source_local_missing_dir_raises(
    isolated_workspace: Path, tmp_path: Path,
) -> None:
    bogus = tmp_path / "does-not-exist"
    with pytest.raises(mp.InstallValidationError) as exc:
        mp.install_from_source(str(bogus))
    assert "does not exist" in str(exc.value)


def test_install_from_source_local_overlap_with_target_raises(
    isolated_workspace: Path,
) -> None:
    """``install_from_source('~/.xmclaw/skills_user/foo')`` would
    otherwise copy the install root into a subdir of itself."""
    from xmclaw.utils.paths import user_skills_dir
    root = user_skills_dir()
    overlap = root / "overlap-test"
    overlap.mkdir(parents=True)
    (overlap / "SKILL.md").write_text("# overlap\n", encoding="utf-8")
    # Pass a parent that contains the install target → should refuse.
    with pytest.raises(mp.InstallValidationError) as exc:
        mp.install_from_source(str(root), skill_id="overlap-test")
    assert "overlap" in str(exc.value).lower()


# ── Library — install / remove flow ─────────────────────────────────────


def test_install_happy_path_clones_validates_and_records(
    isolated_workspace: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    idx = mp.MarketplaceIndex.from_dict(_FAKE_INDEX)
    runner = _fake_git_runner_factory({
        "manifest.json": json.dumps({"id": "alpha-skill", "version": 1}),
        "SKILL.md": "# Alpha skill\nDoes alpha things.\n",
    })
    result = mp.install("alpha-skill", index=idx, git_runner=runner)
    assert result.skill_id == "alpha-skill"
    assert result.install_path.is_dir()
    assert (result.install_path / "manifest.json").is_file()
    assert (result.install_path / "SKILL.md").is_file()
    # Registry was written
    rows = mp.list_installed()
    assert len(rows) == 1
    assert rows[0].id == "alpha-skill"
    assert rows[0].source == "github:fake/xmclaw-skill-alpha"


def test_install_rejects_when_index_missing_id(
    isolated_workspace: Path,
) -> None:
    idx = mp.MarketplaceIndex.from_dict(_FAKE_INDEX)
    with pytest.raises(mp.SkillNotInIndexError):
        mp.install("does-not-exist", index=idx)


def test_install_rejects_when_structure_invalid(
    isolated_workspace: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    idx = mp.MarketplaceIndex.from_dict(_FAKE_INDEX)
    # Clone produces only random.txt — none of the required shape markers
    runner = _fake_git_runner_factory({"random.txt": "hi"})
    with pytest.raises(mp.InstallValidationError):
        mp.install("alpha-skill", index=idx, git_runner=runner)
    # And the half-installed dir was rolled back
    assert not (mp.user_skills_dir() / "alpha-skill").exists()


def test_install_rejects_critical_scanner_finding(
    isolated_workspace: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    idx = mp.MarketplaceIndex.from_dict(_FAKE_INDEX)
    # eval() triggers SKILL_AST_EVAL CRITICAL in skill_scanner
    runner = _fake_git_runner_factory({
        "manifest.json": json.dumps({"id": "alpha-skill", "version": 1}),
        "skill.py": "from xmclaw.skills.base import Skill\n"
                   "class Alpha(Skill):\n    pass\n"
                   "x = eval('1+1')\n",
    })
    with pytest.raises(mp.InstallScanFailed) as exc_info:
        mp.install("alpha-skill", index=idx, git_runner=runner)
    assert any(f["severity"].upper() == "CRITICAL" for f in exc_info.value.findings)
    # Roll-back happened
    assert not (mp.user_skills_dir() / "alpha-skill").exists()
    # Registry stays empty
    assert mp.list_installed() == []


def test_install_allows_critical_when_block_critical_false(
    isolated_workspace: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    idx = mp.MarketplaceIndex.from_dict(_FAKE_INDEX)
    runner = _fake_git_runner_factory({
        "manifest.json": json.dumps({"id": "alpha-skill", "version": 1}),
        "skill.py": "from xmclaw.skills.base import Skill\n"
                   "class Alpha(Skill):\n    pass\n"
                   "x = eval('1+1')\n",
    })
    result = mp.install(
        "alpha-skill", index=idx, git_runner=runner,
        block_critical=False,
    )
    assert any(f["severity"].upper() == "CRITICAL" for f in result.findings)
    assert (mp.user_skills_dir() / "alpha-skill").exists()
    assert any(s.id == "alpha-skill" for s in mp.list_installed())


def test_install_overwrites_existing_install(
    isolated_workspace: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    idx = mp.MarketplaceIndex.from_dict(_FAKE_INDEX)
    runner = _fake_git_runner_factory({
        "manifest.json": json.dumps({"id": "alpha-skill", "version": 1}),
        "SKILL.md": "# alpha\n",
    })
    mp.install("alpha-skill", index=idx, git_runner=runner)
    # Pre-existing crud in the install dir gets replaced
    install_dir = mp.user_skills_dir() / "alpha-skill"
    (install_dir / "stale.txt").write_text("old", encoding="utf-8")
    mp.install("alpha-skill", index=idx, git_runner=runner)
    assert not (install_dir / "stale.txt").exists()
    assert (install_dir / "SKILL.md").is_file()


def test_remove_drops_directory_and_registry_entry(
    isolated_workspace: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    idx = mp.MarketplaceIndex.from_dict(_FAKE_INDEX)
    runner = _fake_git_runner_factory({
        "manifest.json": json.dumps({"id": "alpha-skill", "version": 1}),
        "SKILL.md": "# alpha\n",
    })
    mp.install("alpha-skill", index=idx, git_runner=runner)
    assert (mp.user_skills_dir() / "alpha-skill").is_dir()
    assert mp.list_installed()
    removed = mp.remove("alpha-skill")
    assert removed
    assert not (mp.user_skills_dir() / "alpha-skill").exists()
    assert mp.list_installed() == []


def test_remove_idempotent_returns_false_when_nothing_to_remove(
    isolated_workspace: Path,
) -> None:
    assert mp.remove("never-installed") is False


def test_install_handles_git_clone_failure(
    isolated_workspace: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    idx = mp.MarketplaceIndex.from_dict(_FAKE_INDEX)

    def _failing_runner(args, **kwargs):
        class _R:
            returncode = 128
            stderr = "fatal: nope"
            stdout = ""
        return _R()

    with pytest.raises(mp.InstallValidationError) as exc_info:
        mp.install("alpha-skill", index=idx, git_runner=_failing_runner)
    assert "git clone failed" in str(exc_info.value)


# ── CLI — Typer surface ─────────────────────────────────────────────────


@pytest.fixture
def cli_runner() -> CliRunner:
    return CliRunner()


def _patch_index(monkeypatch: pytest.MonkeyPatch, raw: dict[str, Any]) -> None:
    """Make ``fetch_index`` return our fixed catalog without HTTP."""
    def _fake(*, refresh: bool = False, now: float | None = None):
        return mp.MarketplaceIndex.from_dict(raw)
    # Patch in BOTH modules — the CLI imports from
    # xmclaw.skills.marketplace and re-exports the symbol bound at
    # import time.
    monkeypatch.setattr(mp, "fetch_index", _fake)
    from xmclaw.cli import skill_marketplace as _cli_mp
    monkeypatch.setattr(_cli_mp, "fetch_index", _fake)


def test_cli_list_marketplace_text_mode(
    cli_runner: CliRunner, isolated_workspace: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_index(monkeypatch, _FAKE_INDEX)
    r = cli_runner.invoke(_cli_app, ["skill", "list-marketplace"])
    assert r.exit_code == 0, r.stdout
    assert "alpha-skill" in r.stdout
    assert "beta-skill" in r.stdout
    assert "VERIFIED" not in r.stdout  # we use ✓ glyph not literal text
    assert "2 skills" in r.stdout


def test_cli_list_marketplace_json_mode(
    cli_runner: CliRunner, isolated_workspace: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_index(monkeypatch, _FAKE_INDEX)
    r = cli_runner.invoke(_cli_app, ["skill", "list-marketplace", "--json"])
    assert r.exit_code == 0
    payload = json.loads(r.stdout)
    assert "skills" in payload
    assert {s["id"] for s in payload["skills"]} == {"alpha-skill", "beta-skill"}


def test_cli_search_filters(
    cli_runner: CliRunner, isolated_workspace: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_index(monkeypatch, _FAKE_INDEX)
    r = cli_runner.invoke(_cli_app, ["skill", "search", "git"])
    assert r.exit_code == 0
    assert "beta-skill" in r.stdout
    assert "alpha-skill" not in r.stdout


def test_cli_search_no_match(
    cli_runner: CliRunner, isolated_workspace: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_index(monkeypatch, _FAKE_INDEX)
    r = cli_runner.invoke(_cli_app, ["skill", "search", "doesnotexist"])
    assert r.exit_code == 0
    assert "0 match" in r.stdout
    assert "(no matches)" in r.stdout


def test_cli_install_unknown_id_errors_cleanly(
    cli_runner: CliRunner, isolated_workspace: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_index(monkeypatch, _FAKE_INDEX)
    # CliRunner with mix_stderr=False default (newer Typer/Click) routes
    # ``typer.echo(err=True)`` to r.stderr; handle both.
    r = cli_runner.invoke(_cli_app, ["skill", "install", "nope", "--yes"])
    assert r.exit_code == 1
    combined = (r.stdout or "") + (getattr(r, "stderr", "") or "")
    assert "not in marketplace" in combined.lower() or "marketplace" in combined.lower()


def test_cli_install_happy_path(
    cli_runner: CliRunner, isolated_workspace: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_index(monkeypatch, _FAKE_INDEX)

    runner = _fake_git_runner_factory({
        "manifest.json": json.dumps({"id": "alpha-skill", "version": 1}),
        "SKILL.md": "# alpha\n",
    })
    # Patch the install function to inject our git runner
    real_install = mp.install
    def _patched_install(skill_id, **kwargs):
        kwargs["git_runner"] = runner
        return real_install(skill_id, **kwargs)
    from xmclaw.cli import skill_marketplace as _cli_mp
    monkeypatch.setattr(_cli_mp, "install", _patched_install)

    r = cli_runner.invoke(_cli_app, ["skill", "install", "alpha-skill", "--yes"])
    assert r.exit_code == 0, r.stdout
    assert "installed alpha-skill" in r.stdout
    rows = mp.list_installed()
    assert len(rows) == 1


def test_cli_install_json_mode(
    cli_runner: CliRunner, isolated_workspace: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_index(monkeypatch, _FAKE_INDEX)
    runner = _fake_git_runner_factory({
        "manifest.json": json.dumps({"id": "alpha-skill", "version": 1}),
        "SKILL.md": "# alpha\n",
    })
    real_install = mp.install
    def _patched_install(skill_id, **kwargs):
        kwargs["git_runner"] = runner
        return real_install(skill_id, **kwargs)
    from xmclaw.cli import skill_marketplace as _cli_mp
    monkeypatch.setattr(_cli_mp, "install", _patched_install)
    r = cli_runner.invoke(_cli_app, ["skill", "install", "alpha-skill", "--yes", "--json"])
    assert r.exit_code == 0
    payload = json.loads(r.stdout)
    assert payload["ok"] is True
    assert payload["skill_id"] == "alpha-skill"


def test_cli_remove_happy_path(
    cli_runner: CliRunner, isolated_workspace: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_index(monkeypatch, _FAKE_INDEX)
    # Pre-install
    runner = _fake_git_runner_factory({
        "manifest.json": json.dumps({"id": "alpha-skill", "version": 1}),
        "SKILL.md": "# alpha\n",
    })
    idx = mp.MarketplaceIndex.from_dict(_FAKE_INDEX)
    mp.install("alpha-skill", index=idx, git_runner=runner)
    assert mp.list_installed()

    r = cli_runner.invoke(_cli_app, ["skill", "remove", "alpha-skill", "--yes"])
    assert r.exit_code == 0, r.stdout
    assert "removed alpha-skill" in r.stdout
    assert mp.list_installed() == []


def test_cli_remove_unknown_returns_nonzero(
    cli_runner: CliRunner, isolated_workspace: Path,
) -> None:
    r = cli_runner.invoke(_cli_app, ["skill", "remove", "ghost", "--yes"])
    assert r.exit_code == 1
    combined = (r.stdout or "") + (getattr(r, "stderr", "") or "")
    assert "was not installed" in combined


def test_cli_installed_lists_records(
    cli_runner: CliRunner, isolated_workspace: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _fake_git_runner_factory({
        "manifest.json": json.dumps({"id": "alpha-skill", "version": 1}),
        "SKILL.md": "# alpha\n",
    })
    idx = mp.MarketplaceIndex.from_dict(_FAKE_INDEX)
    mp.install("alpha-skill", index=idx, git_runner=runner)

    r = cli_runner.invoke(_cli_app, ["skill", "installed"])
    assert r.exit_code == 0
    assert "alpha-skill" in r.stdout
    assert "v1.0.0" in r.stdout


def test_cli_installed_empty_state(
    cli_runner: CliRunner, isolated_workspace: Path,
) -> None:
    r = cli_runner.invoke(_cli_app, ["skill", "installed"])
    assert r.exit_code == 0
    assert "(none)" in r.stdout
