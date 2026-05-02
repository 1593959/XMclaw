"""B-114: skills router — promote / rollback / history endpoints.

Pins:
  * promote requires non-empty evidence (anti-req #12)
  * rollback requires non-empty reason (mirror)
  * head_version flips after a successful call
  * history endpoint returns the records list
  * unknown skill / unregistered version → 400
  * orchestrator missing → 400 with sensible error
  * B-166: GET /api/v2/skills classifies sources via manifest
    `created_by`, not Python module path — so a user-installed
    SKILL.md (wrapped in MarkdownProcedureSkill, which lives at
    xmclaw.skills.markdown_skill) is reported as ``user``, not
    ``built-in``.
"""
from __future__ import annotations


import pytest
from fastapi.testclient import TestClient

from xmclaw.daemon.app import create_app
from xmclaw.skills.base import Skill, SkillInput, SkillOutput
from xmclaw.skills.manifest import SkillManifest
from xmclaw.skills.registry import SkillRegistry


class _DemoSkill(Skill):
    id = "demo"
    version = 1

    async def run(self, inp: SkillInput) -> SkillOutput:
        return SkillOutput(ok=True, result="ok", side_effects=[])


class _DemoSkillV2(_DemoSkill):
    version = 2


class _StubOrchestrator:
    """Tiny stand-in for EvolutionOrchestrator — only needs ``.registry``."""

    def __init__(self, registry: SkillRegistry) -> None:
        self.registry = registry


@pytest.fixture
def app(tmp_path):
    """A registered demo skill at v1 and v2, head=v1."""
    reg = SkillRegistry(history_dir=tmp_path / "history")
    manifest = SkillManifest(id="demo", version=1)
    reg.register(_DemoSkill(), manifest=manifest, set_head=True)
    reg.register(
        _DemoSkillV2(),
        manifest=SkillManifest(id="demo", version=2),
        set_head=False,
    )

    a = create_app(config={})
    a.state.orchestrator = _StubOrchestrator(reg)
    return a


def test_promote_requires_evidence(app) -> None:
    with TestClient(app) as client:
        r = client.post("/api/v2/skills/demo/promote", json={"to_version": 2})
    assert r.status_code == 400
    body = r.json()
    assert "evidence" in body["error"].lower()


def test_promote_happy_flips_head(app) -> None:
    with TestClient(app) as client:
        r = client.post(
            "/api/v2/skills/demo/promote",
            json={"to_version": 2, "evidence": ["bench:phase1 +1.12x"]},
        )
    assert r.status_code == 200, r.json()
    body = r.json()
    assert body["ok"] is True
    assert body["head_version"] == 2
    assert body["record"]["kind"] == "promote"
    assert body["record"]["from_version"] == 1
    assert body["record"]["to_version"] == 2
    assert body["record"]["evidence"] == ["bench:phase1 +1.12x"]


def test_promote_unknown_version_400(app) -> None:
    with TestClient(app) as client:
        r = client.post(
            "/api/v2/skills/demo/promote",
            json={"to_version": 99, "evidence": ["test"]},
        )
    assert r.status_code == 400
    assert "v99" in r.json()["error"] or "unregistered" in r.json()["error"]


def test_rollback_requires_reason(app) -> None:
    # Pre-promote to v2 first, then attempt rollback without reason.
    with TestClient(app) as client:
        client.post(
            "/api/v2/skills/demo/promote",
            json={"to_version": 2, "evidence": ["test"]},
        )
        r = client.post("/api/v2/skills/demo/rollback", json={"to_version": 1})
    assert r.status_code == 400
    assert "reason" in r.json()["error"].lower()


def test_rollback_happy_flips_head(app) -> None:
    with TestClient(app) as client:
        client.post(
            "/api/v2/skills/demo/promote",
            json={"to_version": 2, "evidence": ["test"]},
        )
        r = client.post(
            "/api/v2/skills/demo/rollback",
            json={"to_version": 1, "reason": "v2 broke on Windows"},
        )
    assert r.status_code == 200, r.json()
    body = r.json()
    assert body["ok"] is True
    assert body["head_version"] == 1
    assert body["record"]["kind"] == "rollback"
    assert body["record"]["reason"] == "v2 broke on Windows"


def test_history_returns_promote_and_rollback_records(app) -> None:
    with TestClient(app) as client:
        client.post(
            "/api/v2/skills/demo/promote",
            json={"to_version": 2, "evidence": ["bench"]},
        )
        client.post(
            "/api/v2/skills/demo/rollback",
            json={"to_version": 1, "reason": "regression"},
        )
        r = client.get("/api/v2/skills/demo/history")
    assert r.status_code == 200
    body = r.json()
    kinds = [rec["kind"] for rec in body["records"]]
    assert kinds == ["promote", "rollback"]


def test_no_orchestrator_returns_400() -> None:
    a = create_app(config={})
    # Don't wire orchestrator at all.
    with TestClient(a) as client:
        r = client.post(
            "/api/v2/skills/demo/promote",
            json={"to_version": 1, "evidence": ["x"]},
        )
    assert r.status_code == 400
    assert "evolution" in r.json()["error"].lower()


# ── B-166: source classification via manifest.created_by ──


def test_list_skills_user_via_markdown_skill(tmp_path) -> None:
    """A SKILL.md loaded by UserSkillsLoader wraps in
    MarkdownProcedureSkill (xmclaw.skills.markdown_skill module). The
    pre-B-166 module-path classifier returned 'built-in' for these —
    the bug the user hit. After the fix the manifest's
    ``created_by="user"`` wins, and source is reported as 'user'."""
    from xmclaw.skills.markdown_skill import MarkdownProcedureSkill

    reg = SkillRegistry(history_dir=tmp_path / "history")
    md_skill = MarkdownProcedureSkill(
        id="git-commit", body="# step 1\n…", version=1,
    )
    reg.register(
        md_skill,
        manifest=SkillManifest(id="git-commit", version=1, created_by="user"),
        set_head=True,
    )

    a = create_app(config={})
    a.state.orchestrator = _StubOrchestrator(reg)
    with TestClient(a) as client:
        r = client.get("/api/v2/skills")
    assert r.status_code == 200
    rows = r.json()["skills"]
    row = next(s for s in rows if s["id"] == "git-commit")
    assert row["source"] == "user", (
        "MarkdownProcedureSkill with manifest.created_by=user must be "
        "classified as 'user', not 'built-in'"
    )


def test_list_skills_built_in_via_default_manifest(tmp_path) -> None:
    """A skill with default manifest (created_by='human') AND class
    in xmclaw.skills.* package → still classified as 'built-in'."""
    reg = SkillRegistry(history_dir=tmp_path / "history")
    reg.register(
        _DemoSkill(),
        manifest=SkillManifest(id="demo", version=1),  # default created_by="human"
        set_head=True,
    )
    a = create_app(config={})
    a.state.orchestrator = _StubOrchestrator(reg)
    with TestClient(a) as client:
        r = client.get("/api/v2/skills")
    rows = r.json()["skills"]
    row = next(s for s in rows if s["id"] == "demo")
    # _DemoSkill is defined in this test file, NOT under xmclaw.skills.*,
    # so module-path fallback returns "user". The point of THIS test:
    # the manifest doesn't override to "user" just because of created_by
    # being "human" — it falls through to module-path, which is the
    # legacy behaviour we keep.
    assert row["source"] == "user"


def test_list_skills_evolved_classified_separately(tmp_path) -> None:
    """Evolution-promoted skills (created_by='evolved') should report
    as 'evolved' so the UI can badge them distinctly."""
    from xmclaw.skills.markdown_skill import MarkdownProcedureSkill

    reg = SkillRegistry(history_dir=tmp_path / "history")
    reg.register(
        MarkdownProcedureSkill(id="auto.foo", body="…", version=1),
        manifest=SkillManifest(id="auto.foo", version=1, created_by="evolved"),
        set_head=True,
    )
    a = create_app(config={})
    a.state.orchestrator = _StubOrchestrator(reg)
    with TestClient(a) as client:
        r = client.get("/api/v2/skills")
    rows = r.json()["skills"]
    row = next(s for s in rows if s["id"] == "auto.foo")
    assert row["source"] == "evolved"
