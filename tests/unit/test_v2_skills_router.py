"""B-114: skills router — promote / rollback / history endpoints.

Pins:
  * promote requires non-empty evidence (anti-req #12)
  * rollback requires non-empty reason (mirror)
  * head_version flips after a successful call
  * history endpoint returns the records list
  * unknown skill / unregistered version → 400
  * orchestrator missing → 400 with sensible error
"""
from __future__ import annotations

from typing import Any

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
