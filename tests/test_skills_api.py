"""HTTP tests for the /api/skills aggregation endpoint.

``/api/skills`` is what the 技能 view in the web UI consumes. It walks
three directories and stamps a ``source`` tag on each row so the UI can
chip-filter them:

* ``xmclaw/skills/``    → ``builtin``
* ``shared/skills/``    → ``generated``   (SkillForge output)
* ``plugins/skills/``   → ``downloaded``  (user-installed)

These tests pin the contract so nothing in the UI has to guess at the
shape. They also cover the robustness expectations: a missing directory
(fresh install, no plugins/skills/ yet) must NOT 500, and a malformed
sidecar .json must NOT poison the other entries.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from xmclaw.daemon.server import app


@pytest.fixture
def client_with_skill_dirs(tmp_path, monkeypatch):
    """Point the endpoint at a throw-away BASE_DIR populated with fake skills.

    The fixture hot-patches ``server.BASE_DIR`` so we control every
    directory the endpoint reads, rather than relying on whatever the
    dev has in their real shared/skills/ folder.
    """
    from xmclaw.daemon import server as server_module

    builtin = tmp_path / "xmclaw" / "skills"
    generated = tmp_path / "shared" / "skills"
    downloaded = tmp_path / "plugins" / "skills"
    for d in (builtin, generated, downloaded):
        d.mkdir(parents=True)

    # One skill per source, each with a well-formed sidecar so we can
    # assert metadata flows through.
    (builtin / "skill_seed.py").write_text("# seed skill\n", encoding="utf-8")
    (builtin / "skill_seed.json").write_text(
        json.dumps({
            "name": "skill_seed",
            "description": "Seed skill",
            "category": "seed",
            "version": "1.0",
        }),
        encoding="utf-8",
    )
    (generated / "skill_evolved.py").write_text("# evolved\n", encoding="utf-8")
    (generated / "skill_evolved.json").write_text(
        json.dumps({
            "name": "skill_evolved",
            "description": "Evolved skill",
            "category": "auto",
            "version": "0.1",
        }),
        encoding="utf-8",
    )
    (downloaded / "skill_plugin.py").write_text("# plugin\n", encoding="utf-8")
    (downloaded / "skill_plugin.json").write_text(
        json.dumps({
            "name": "skill_plugin",
            "description": "Downloaded skill",
            "category": "ext",
            "version": "2.3",
        }),
        encoding="utf-8",
    )

    monkeypatch.setattr(server_module, "BASE_DIR", tmp_path)
    return TestClient(app)


def test_list_skills_shape(client_with_skill_dirs):
    r = client_with_skill_dirs.get("/api/skills")
    assert r.status_code == 200
    body = r.json()
    assert "skills" in body
    assert "total" in body
    assert body["total"] == len(body["skills"]) == 3


def test_list_skills_source_tags(client_with_skill_dirs):
    """Each entry must carry the source tag the UI groups by — otherwise
    the filter chips silently misclassify new rows."""
    body = client_with_skill_dirs.get("/api/skills").json()
    by_source = {s["source"]: s for s in body["skills"]}
    assert set(by_source) == {"builtin", "generated", "downloaded"}
    assert by_source["builtin"]["name"] == "skill_seed"
    assert by_source["generated"]["name"] == "skill_evolved"
    assert by_source["downloaded"]["name"] == "skill_plugin"


def test_list_skills_includes_sidecar_metadata(client_with_skill_dirs):
    body = client_with_skill_dirs.get("/api/skills").json()
    evolved = next(s for s in body["skills"] if s["source"] == "generated")
    assert evolved["description"] == "Evolved skill"
    assert evolved["category"] == "auto"
    assert evolved["version"] == "0.1"
    assert evolved["filename"] == "skill_evolved.py"


def test_list_skills_missing_directory_is_not_fatal(tmp_path, monkeypatch):
    """Fresh install: plugins/skills/ doesn't exist yet. The endpoint
    must still return 200 with whatever it could read, not 500."""
    from xmclaw.daemon import server as server_module

    # Only create the generated dir; leave builtin + downloaded missing.
    generated = tmp_path / "shared" / "skills"
    generated.mkdir(parents=True)
    (generated / "skill_only.py").write_text("# lone\n", encoding="utf-8")

    monkeypatch.setattr(server_module, "BASE_DIR", tmp_path)
    client = TestClient(app)
    r = client.get("/api/skills")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["skills"][0]["source"] == "generated"


def test_list_skills_malformed_sidecar_does_not_poison_others(tmp_path, monkeypatch):
    """A corrupt JSON sidecar on one skill must not blow up the response
    — the rest of the skills should still render."""
    from xmclaw.daemon import server as server_module

    generated = tmp_path / "shared" / "skills"
    generated.mkdir(parents=True)
    (generated / "skill_bad.py").write_text("# bad sidecar\n", encoding="utf-8")
    (generated / "skill_bad.json").write_text("{not valid json", encoding="utf-8")
    (generated / "skill_good.py").write_text("# good\n", encoding="utf-8")
    (generated / "skill_good.json").write_text(
        json.dumps({"name": "skill_good", "description": "fine"}),
        encoding="utf-8",
    )

    monkeypatch.setattr(server_module, "BASE_DIR", tmp_path)
    client = TestClient(app)
    body = client.get("/api/skills").json()
    names = {s["name"] for s in body["skills"]}
    assert "skill_good" in names
    assert "skill_bad" in names  # fell back to file stem
    assert body["total"] == 2
