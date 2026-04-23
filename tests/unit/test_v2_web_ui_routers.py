"""Epic #18 Phase A — Web UI HTTP router tests.

Covers the four new routers mounted by
:func:`xmclaw.daemon.app.create_app`:

    * ``/api/v2/profiles``    — persona markdown listing / reads
    * ``/api/v2/workspaces``  — JSON config CRUD
    * ``/api/v2/memory``      — markdown editor + grep search
    * ``/api/v2/files``       — directory browser + file reads

Every test monkeypatches ``XMC_DATA_DIR`` to ``tmp_path`` so nothing
touches the user's real ``~/.xmclaw/`` during CI.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from xmclaw.daemon.app import create_app


@pytest.fixture
def tmp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Reroute XMC_DATA_DIR at ``<data_dir>`` so every helper in
    ``xmclaw.utils.paths`` returns a subpath of ``tmp_path``."""
    monkeypatch.setenv("XMC_DATA_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def client(tmp_home: Path) -> TestClient:
    """TestClient over an app created with an empty config dict.

    ``config={}`` is enough for the routers — the files router reads
    ``tools.allowed_dirs`` defensively and falls back to $HOME when
    the key is missing, so we don't need a full daemon.
    """
    app = create_app(config={})
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------
class TestProfilesRouter:
    def test_empty_returns_empty_list(self, client: TestClient) -> None:
        resp = client.get("/api/v2/profiles")
        assert resp.status_code == 200
        assert resp.json() == {"profiles": []}

    def test_lists_markdown_files_with_title_from_first_line(
        self, client: TestClient, tmp_home: Path
    ) -> None:
        pdir = tmp_home / "persona" / "profiles"
        pdir.mkdir(parents=True)
        (pdir / "coder.md").write_text("# Senior Coder\n\nAct like X.", encoding="utf-8")
        (pdir / "writer.md").write_text("## Prose Buddy\n\nHelp with drafts.", encoding="utf-8")

        resp = client.get("/api/v2/profiles")
        assert resp.status_code == 200
        profs = {p["id"]: p for p in resp.json()["profiles"]}
        assert profs["coder"]["title"] == "Senior Coder"
        assert profs["writer"]["title"] == "Prose Buddy"

    def test_title_fallback_to_stem_when_empty_file(
        self, client: TestClient, tmp_home: Path
    ) -> None:
        pdir = tmp_home / "persona" / "profiles"
        pdir.mkdir(parents=True)
        (pdir / "blank.md").write_text("", encoding="utf-8")

        profs = client.get("/api/v2/profiles").json()["profiles"]
        assert profs[0]["title"] == "blank"

    def test_get_profile_returns_full_content(
        self, client: TestClient, tmp_home: Path
    ) -> None:
        pdir = tmp_home / "persona" / "profiles"
        pdir.mkdir(parents=True)
        body = "# Coder\n\nAct like X."
        (pdir / "coder.md").write_text(body, encoding="utf-8")

        resp = client.get("/api/v2/profiles/coder")
        assert resp.status_code == 200
        assert resp.json() == {"id": "coder", "content": body}

    def test_missing_profile_returns_404(self, client: TestClient) -> None:
        resp = client.get("/api/v2/profiles/ghost")
        assert resp.status_code == 404

    def test_path_traversal_blocked(
        self, client: TestClient, tmp_home: Path
    ) -> None:
        """``../`` style ids must not escape the persona dir."""
        (tmp_home / "persona" / "profiles").mkdir(parents=True)
        # Even if this file existed, the sanitizer would rewrite the id
        # before we touch disk — so the route sees ``___etc_passwd.md``
        # under persona_dir, which does not exist → 404.
        resp = client.get("/api/v2/profiles/..%2Fetc%2Fpasswd")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Workspaces
# ---------------------------------------------------------------------------
class TestWorkspacesRouter:
    def test_empty_list(self, client: TestClient) -> None:
        resp = client.get("/api/v2/workspaces")
        assert resp.status_code == 200
        assert resp.json() == {"workspaces": []}

    def test_create_then_list(self, client: TestClient, tmp_home: Path) -> None:
        r = client.post("/api/v2/workspaces", json={
            "id": "coder-workspace",
            "name": "Coder",
            "description": "daily driver",
            "model": "claude-opus-4-7",
        })
        assert r.status_code == 200
        assert r.json() == {"ok": True, "id": "coder-workspace"}

        # File on disk matches what we POSTed
        cfg_path = tmp_home / "workspaces" / "coder-workspace.json"
        assert cfg_path.exists()
        on_disk = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert on_disk["name"] == "Coder"
        assert on_disk["model"] == "claude-opus-4-7"

        listed = client.get("/api/v2/workspaces").json()["workspaces"]
        assert len(listed) == 1
        assert listed[0]["id"] == "coder-workspace"
        assert listed[0]["name"] == "Coder"

    def test_id_is_sanitized(self, client: TestClient, tmp_home: Path) -> None:
        r = client.post("/api/v2/workspaces", json={"id": "../etc/passwd", "name": "bad"})
        assert r.status_code == 200
        returned_id = r.json()["id"]
        # Every non-alnum char is replaced with _
        assert "/" not in returned_id
        assert ".." not in returned_id
        # The file must live inside workspaces_dir — nothing escaped
        cfg_path = tmp_home / "workspaces" / f"{returned_id}.json"
        assert cfg_path.exists()

    def test_create_upserts(self, client: TestClient) -> None:
        client.post("/api/v2/workspaces", json={"id": "x", "name": "v1"})
        client.post("/api/v2/workspaces", json={"id": "x", "name": "v2"})
        workspaces = client.get("/api/v2/workspaces").json()["workspaces"]
        assert len(workspaces) == 1
        assert workspaces[0]["name"] == "v2"

    def test_delete(self, client: TestClient) -> None:
        client.post("/api/v2/workspaces", json={"id": "doomed", "name": "Doomed"})
        r = client.delete("/api/v2/workspaces/doomed")
        assert r.status_code == 200
        assert r.json() == {"ok": True}
        assert client.get("/api/v2/workspaces").json() == {"workspaces": []}

    def test_delete_missing_returns_404(self, client: TestClient) -> None:
        r = client.delete("/api/v2/workspaces/ghost")
        assert r.status_code == 404

    def test_malformed_json_rejected(self, client: TestClient) -> None:
        r = client.post(
            "/api/v2/workspaces",
            content=b"not json",
            headers={"content-type": "application/json"},
        )
        assert r.status_code == 400

    def test_corrupted_file_skipped_in_list(
        self, client: TestClient, tmp_home: Path
    ) -> None:
        wdir = tmp_home / "workspaces"
        wdir.mkdir(parents=True)
        (wdir / "good.json").write_text('{"name": "G"}', encoding="utf-8")
        (wdir / "bad.json").write_text("not json at all", encoding="utf-8")

        workspaces = client.get("/api/v2/workspaces").json()["workspaces"]
        # Malformed entry silently dropped; good one survives
        ids = [w["id"] for w in workspaces]
        assert "good" in ids and "bad" not in ids


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------
class TestMemoryRouter:
    def test_empty_list(self, client: TestClient) -> None:
        assert client.get("/api/v2/memory").json() == {"files": []}

    def test_save_then_list_and_read(self, client: TestClient, tmp_home: Path) -> None:
        body = "# Notes\n\nInteresting fact."
        r = client.post("/api/v2/memory/notes.md", json={"content": body})
        assert r.status_code == 200
        assert r.json() == {"ok": True, "name": "notes.md"}
        assert (tmp_home / "memory" / "notes.md").read_text(encoding="utf-8") == body

        listed = client.get("/api/v2/memory").json()["files"]
        assert len(listed) == 1 and listed[0]["name"] == "notes.md"

        got = client.get("/api/v2/memory/notes.md").json()
        assert got["name"] == "notes.md"
        assert got["content"] == body

    def test_missing_file_is_404(self, client: TestClient) -> None:
        assert client.get("/api/v2/memory/nope.md").status_code == 404

    def test_md_suffix_auto_added(self, client: TestClient) -> None:
        r = client.post("/api/v2/memory/no_suffix", json={"content": "x"})
        assert r.json()["name"] == "no_suffix.md"

    def test_path_traversal_sanitized(self, client: TestClient, tmp_home: Path) -> None:
        """A filename containing a slash must not escape memory_dir.

        The router strips directory components via ``Path(...).name``,
        so even a crafted ``sub/evil`` input collapses to ``evil.md``
        inside memory_dir. We go in through the route prefix directly
        to avoid FastAPI's percent-decoded ``..`` tripping the router
        matcher before the handler runs.
        """
        # Use a filename the router handler sees directly (no / in URL
        # path segment — FastAPI would 404 before reaching the handler
        # on a true ``..%2F`` traversal). Confirm the sanitizer strips
        # directory components off a name-like input with a separator.
        r = client.post(
            "/api/v2/memory/safe_name.md",
            json={"content": "not malicious"},
        )
        assert r.status_code == 200

        # Unit-level confirmation that ``_safe_name`` strips path parts
        # even if a future route accepts slashes:
        from xmclaw.daemon.routers.memory import _safe_name
        assert _safe_name("../evil") == "evil.md"
        assert _safe_name("sub/evil.md") == "evil.md"
        assert _safe_name("..\\weird") == "weird.md"

    def test_search_finds_substring(self, client: TestClient) -> None:
        client.post("/api/v2/memory/fox.md", json={"content": "The quick brown fox."})
        client.post("/api/v2/memory/cat.md", json={"content": "Just a sleepy cat."})
        results = client.post("/api/v2/memory/search", json={"query": "fox"}).json()["results"]
        assert len(results) == 1
        assert results[0]["topic"] == "fox"
        assert "fox" in results[0]["snippet"]

    def test_search_empty_query_returns_empty(self, client: TestClient) -> None:
        r = client.post("/api/v2/memory/search", json={"query": ""})
        assert r.json() == {"results": []}

    def test_search_missing_match_returns_empty(self, client: TestClient) -> None:
        client.post("/api/v2/memory/a.md", json={"content": "alpha"})
        r = client.post("/api/v2/memory/search", json={"query": "zeta"})
        assert r.json() == {"results": []}


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------
class TestFilesRouter:
    @pytest.fixture
    def allowed_app(
        self, tmp_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> TestClient:
        """App with ``tools.allowed_dirs`` pointing at an isolated sandbox.

        ``Path.home()`` is also patched to a disjoint ``tmp_home / "fake_home"``
        so the router's implicit "home is always a root" fallback does NOT
        silently whitelist the whole ``tmp_path`` tree under pytest's
        temp dir (which typically sits under the real ``$HOME``).
        """
        sandbox = tmp_home / "sandbox"
        sandbox.mkdir()
        fake_home = tmp_home / "fake_home"
        fake_home.mkdir()
        # ``Path.home()`` is a classmethod; patch at the module the router
        # imports it from. Using setattr on the module-level ``Path`` name
        # keeps the patch scoped to this test via monkeypatch teardown.
        import xmclaw.daemon.routers.files as _files_mod

        class _FakePathHome:
            @staticmethod
            def home() -> Path:
                return fake_home

        monkeypatch.setattr(_files_mod.Path, "home", _FakePathHome.home)

        config = {"tools": {"allowed_dirs": [str(sandbox)]}}
        app = create_app(config=config)
        with TestClient(app) as c:
            c.__dict__["_sandbox"] = sandbox
            c.__dict__["_fake_home"] = fake_home
            yield c

    def test_list_sandbox_root(self, allowed_app: TestClient) -> None:
        sandbox: Path = allowed_app.__dict__["_sandbox"]
        (sandbox / "a.txt").write_text("hello", encoding="utf-8")
        (sandbox / "sub").mkdir()

        r = allowed_app.get(f"/api/v2/files?path={sandbox}")
        assert r.status_code == 200
        data = r.json()
        assert data["is_dir"] is True
        names = [e["name"] for e in data["entries"]]
        # Dirs sort before files
        assert names == ["sub", "a.txt"]

    def test_read_text_file(self, allowed_app: TestClient) -> None:
        sandbox: Path = allowed_app.__dict__["_sandbox"]
        f = sandbox / "hi.txt"
        f.write_text("hello world", encoding="utf-8")

        r = allowed_app.get(f"/api/v2/files?path={f}")
        assert r.status_code == 200
        data = r.json()
        assert data["is_dir"] is False
        assert data["content"] == "hello world"
        assert data["size"] == len(b"hello world")

    def test_path_outside_roots_is_403(self, allowed_app: TestClient, tmp_path: Path) -> None:
        outside = tmp_path / "outside_sandbox"
        outside.mkdir()
        (outside / "secret.txt").write_text("hidden", encoding="utf-8")

        r = allowed_app.get(f"/api/v2/files?path={outside}/secret.txt")
        assert r.status_code == 403

    def test_nonexistent_inside_root_is_404(self, allowed_app: TestClient) -> None:
        sandbox: Path = allowed_app.__dict__["_sandbox"]
        r = allowed_app.get(f"/api/v2/files?path={sandbox / 'missing.txt'}")
        assert r.status_code == 404

    def test_default_path_lists_first_root(self, allowed_app: TestClient) -> None:
        sandbox: Path = allowed_app.__dict__["_sandbox"]
        (sandbox / "marker.md").write_text("x", encoding="utf-8")
        r = allowed_app.get("/api/v2/files")
        assert r.status_code == 200
        names = [e["name"] for e in r.json()["entries"]]
        assert "marker.md" in names

    def test_huge_file_rejected_with_413(self, allowed_app: TestClient) -> None:
        sandbox: Path = allowed_app.__dict__["_sandbox"]
        f = sandbox / "huge.bin"
        # Just over the 1 MiB limit.
        f.write_bytes(b"x" * (1 * 1024 * 1024 + 1))
        r = allowed_app.get(f"/api/v2/files?path={f}")
        assert r.status_code == 413

    def test_home_is_always_a_root_even_without_config(
        self, client: TestClient, tmp_home: Path
    ) -> None:
        # ``client`` fixture created the app with ``config={}`` — so only
        # the $HOME fallback is in the allowed roots. ``tmp_home`` is the
        # value $HOME resolves to via XMC_DATA_DIR — but HOME is NOT
        # rerouted by XMC_DATA_DIR. So we just check the route returns
        # 200 for the real home (the actual contents vary by CI runner).
        r = client.get(f"/api/v2/files?path={Path.home()}")
        assert r.status_code == 200
        assert r.json()["is_dir"] is True


# ---------------------------------------------------------------------------
# Router registration smoke
# ---------------------------------------------------------------------------
class TestRouterRegistration:
    """Catch accidental router removals early — a one-line import cycle
    regression would silently 404 every panel in the UI."""

    def test_all_four_groups_are_mounted(self, client: TestClient) -> None:
        spec = client.get("/openapi.json").json()
        paths = set(spec["paths"].keys())
        assert "/api/v2/profiles" in paths
        assert "/api/v2/workspaces" in paths
        assert "/api/v2/memory" in paths
        assert "/api/v2/files" in paths
