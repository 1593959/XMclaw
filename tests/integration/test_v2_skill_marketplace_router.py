"""B-390 (Sprint 2): Skill marketplace HTTP API tests.

Pins down the daemon router at ``/api/v2/skills/marketplace`` /
``/installed`` / ``/install`` so the web UI Marketplace page has a
contract it can lean on without breaking on every internal refactor.

Covers:
  * GET /marketplace returns the parsed catalog wrapped in {ok, index}.
  * GET /marketplace?refresh=1 flips ``refresh=True`` through to
    fetch_index (we patch fetch_index to assert the kwarg).
  * Index fetch failure surfaces as 4xx + error_code.
  * GET /installed initially empty, then reflects an install.
  * POST /install with a known id runs the flow + returns 200.
  * POST /install with an unknown id → 404 + ``error_code='skill_not_found'``.
  * POST /install with malformed body → 400.
  * POST /install with a CRITICAL scanner finding → 400 + findings array.
  * DELETE /installed/{id} removes the install and the registry row.
  * DELETE /installed/{id} on an unknown id → 404 ``skill_not_installed``.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from xmclaw.core.bus import InProcessEventBus
from xmclaw.daemon.app import create_app
from xmclaw.skills import marketplace as mp


_FAKE_INDEX = {
    "version": 1,
    "updated": "2026-05-09",
    "skills": [
        {
            "id": "alpha-skill",
            "name": "Alpha Skill",
            "description": "First fake skill",
            "version": "1.0.0",
            "source": "github:fake/xmclaw-skill-alpha",
            "license": "MIT",
            "tags": ["dev"],
            "author": "alice",
            "trust_tier": "verified",
            "install_size_kb": 10,
        },
    ],
}


@pytest.fixture
def isolated_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XMC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XMC_V2_USER_SKILLS_DIR", str(tmp_path / "skills_user"))
    return tmp_path


@pytest.fixture
def client(isolated_workspace: Path) -> TestClient:
    bus = InProcessEventBus()
    return TestClient(create_app(bus=bus))


def _patch_fetch_index(
    monkeypatch: pytest.MonkeyPatch, raw: dict[str, Any] | Exception,
    *, capture: list[bool] | None = None,
) -> None:
    """Make fetch_index return a fixed catalog or raise. Optionally
    captures the ``refresh`` flag passed by the router."""
    def _fake(*, refresh: bool = False, now: float | None = None):
        if capture is not None:
            capture.append(refresh)
        if isinstance(raw, Exception):
            raise raw
        return mp.MarketplaceIndex.from_dict(raw)
    monkeypatch.setattr(mp, "fetch_index", _fake)


def _fake_git_runner(file_writes: dict[str, str]):
    def _runner(args, **kwargs):
        target = Path(args[-1])
        target.mkdir(parents=True, exist_ok=True)
        for rel, body in file_writes.items():
            f = target / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(body, encoding="utf-8")

        class _R:
            returncode = 0
            stderr = ""
            stdout = ""
        return _R()
    return _runner


# ── GET /marketplace ────────────────────────────────────────────────────


def test_get_marketplace_returns_catalog(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_fetch_index(monkeypatch, _FAKE_INDEX)
    r = client.get("/api/v2/skills/marketplace")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["index"]["version"] == 1
    ids = [s["id"] for s in body["index"]["skills"]]
    assert "alpha-skill" in ids


def test_get_marketplace_refresh_query_propagates(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[bool] = []
    _patch_fetch_index(monkeypatch, _FAKE_INDEX, capture=seen)
    r = client.get("/api/v2/skills/marketplace?refresh=1")
    assert r.status_code == 200
    assert seen == [True]


def test_get_marketplace_refresh_zero_does_not_force(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[bool] = []
    _patch_fetch_index(monkeypatch, _FAKE_INDEX, capture=seen)
    client.get("/api/v2/skills/marketplace")
    assert seen == [False]


def test_get_marketplace_fetch_failure_returns_4xx(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_fetch_index(monkeypatch, mp.IndexFetchError("network down"))
    r = client.get("/api/v2/skills/marketplace")
    assert r.status_code == 400
    body = r.json()
    assert body["ok"] is False
    assert body["error_code"] == "index_fetch_failed"


# ── GET /installed ──────────────────────────────────────────────────────


def test_get_installed_initially_empty(client: TestClient) -> None:
    r = client.get("/api/v2/skills/installed")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "skills": []}


def test_get_installed_lists_after_install(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_fetch_index(monkeypatch, _FAKE_INDEX)
    runner = _fake_git_runner({
        "manifest.json": json.dumps({"id": "alpha-skill", "version": 1}),
        "SKILL.md": "# alpha\n",
    })
    idx = mp.MarketplaceIndex.from_dict(_FAKE_INDEX)
    mp.install("alpha-skill", index=idx, git_runner=runner)

    r = client.get("/api/v2/skills/installed")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert len(body["skills"]) == 1
    assert body["skills"][0]["id"] == "alpha-skill"


# ── POST /install ───────────────────────────────────────────────────────


def test_post_install_unknown_id_returns_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_fetch_index(monkeypatch, _FAKE_INDEX)
    r = client.post("/api/v2/skills/install", json={"id": "ghost"})
    assert r.status_code == 404
    body = r.json()
    assert body["error_code"] == "skill_not_found"


def test_post_install_invalid_body_returns_400(
    client: TestClient,
) -> None:
    r = client.post(
        "/api/v2/skills/install",
        content="not json",
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 400


def test_post_install_missing_id_returns_400(client: TestClient) -> None:
    r = client.post("/api/v2/skills/install", json={"foo": "bar"})
    assert r.status_code == 400
    body = r.json()
    assert body["error_code"] == "missing_id"


def test_post_install_happy_path(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_fetch_index(monkeypatch, _FAKE_INDEX)
    runner = _fake_git_runner({
        "manifest.json": json.dumps({"id": "alpha-skill", "version": 1}),
        "SKILL.md": "# alpha\n",
    })

    real_install = mp.install
    def _patched(skill_id, **kw):
        kw["git_runner"] = runner
        return real_install(skill_id, **kw)
    monkeypatch.setattr(mp, "install", _patched)

    r = client.post("/api/v2/skills/install", json={"id": "alpha-skill"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["skill_id"] == "alpha-skill"
    assert body["version"] == "1.0.0"


def test_post_install_critical_finding_returns_400_with_findings(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_fetch_index(monkeypatch, _FAKE_INDEX)
    runner = _fake_git_runner({
        "manifest.json": json.dumps({"id": "alpha-skill", "version": 1}),
        "skill.py": "from xmclaw.skills.base import Skill\n"
                    "class Alpha(Skill):\n    pass\n"
                    "x = eval('1')\n",
    })
    real_install = mp.install
    def _patched(skill_id, **kw):
        kw["git_runner"] = runner
        return real_install(skill_id, **kw)
    monkeypatch.setattr(mp, "install", _patched)

    r = client.post("/api/v2/skills/install", json={"id": "alpha-skill"})
    assert r.status_code == 400
    body = r.json()
    assert body["error_code"] == "install_scan_failed"
    assert any(f["severity"].upper() == "CRITICAL" for f in body["findings"])


# ── DELETE /installed/{id} ──────────────────────────────────────────────


def test_delete_installed_removes_install(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_fetch_index(monkeypatch, _FAKE_INDEX)
    runner = _fake_git_runner({
        "manifest.json": json.dumps({"id": "alpha-skill", "version": 1}),
        "SKILL.md": "# alpha\n",
    })
    idx = mp.MarketplaceIndex.from_dict(_FAKE_INDEX)
    mp.install("alpha-skill", index=idx, git_runner=runner)

    r = client.delete("/api/v2/skills/installed/alpha-skill")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["removed"] is True
    assert mp.list_installed() == []


def test_delete_installed_unknown_returns_404(client: TestClient) -> None:
    r = client.delete("/api/v2/skills/installed/ghost")
    assert r.status_code == 404
    body = r.json()
    assert body["error_code"] == "skill_not_installed"
