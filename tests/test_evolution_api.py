"""Phase E9 regression tests: retrospective + approval HTTP endpoints.

Pins:

* **PR-E9-1** — ``/api/agent/{agent_id}/evolution/summary``,
  ``.../funnel``, ``.../rejects``, and ``.../rollbacks`` return the
  same shapes the journal produces, now over HTTP.
* **PR-E9-2** — ``/api/agent/{agent_id}/evolution/pending_approvals``
  lists needs_approval artifacts; the approve/decline POST endpoints
  resolve them idempotently and return 404 for unknown artifact IDs.

Tests use a unique ``agent_id`` per run so writes to the real
``shared/memory.db`` don't collide with production data or other tests.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from xmclaw.daemon.server import app
from xmclaw.evolution.journal import (
    CYCLE_PASSED,
    CYCLE_REJECTED,
    CYCLE_SKIPPED,
    KIND_GENE,
    KIND_SKILL,
    STATUS_NEEDS_APPROVAL,
    STATUS_PROMOTED,
    STATUS_ROLLED_BACK,
    STATUS_SHADOW,
    get_journal,
    reset_journal_cache,
)


def _unique_agent_id(tag: str) -> str:
    return f"agent_e9_{tag}_{uuid.uuid4().hex[:8]}"


async def _seed_cycles_and_artifacts(agent_id: str) -> dict[str, str]:
    """Seed a known mix of cycle + lineage rows. Returns key artifact ids."""
    j = get_journal(agent_id)
    cid_pass = await j.open_cycle(trigger="manual")
    await j.close_cycle(cid_pass, verdict=CYCLE_PASSED)

    cid_reject = await j.open_cycle(trigger="manual")
    await j.close_cycle(cid_reject, verdict=CYCLE_REJECTED,
                        reject_reason="all_candidates_failed")

    cid_skip = await j.open_cycle(trigger="pattern_threshold")
    await j.close_cycle(cid_skip, verdict=CYCLE_SKIPPED,
                        reject_reason="no_insights")

    # A promoted skill, a needs_approval gene, and a rolled-back skill.
    promoted_id = f"skill_{uuid.uuid4().hex[:8]}"
    pending_id = f"gene_{uuid.uuid4().hex[:8]}"
    rolled_id = f"skill_{uuid.uuid4().hex[:8]}"
    await j.record_artifact(cid_pass, KIND_SKILL, promoted_id,
                            status=STATUS_PROMOTED)
    await j.record_artifact(cid_pass, KIND_GENE, pending_id,
                            status=STATUS_SHADOW)
    await j.update_artifact_status(pending_id, STATUS_NEEDS_APPROVAL)
    await j.record_artifact(cid_pass, KIND_SKILL, rolled_id,
                            status=STATUS_ROLLED_BACK)
    return {
        "promoted": promoted_id, "pending": pending_id, "rolled": rolled_id,
    }


# ── PR-E9-1: retrospective endpoints ────────────────────────────────────────

def test_evolution_summary_endpoint():
    agent_id = _unique_agent_id("summary")
    asyncio.get_event_loop().run_until_complete(
        _seed_cycles_and_artifacts(agent_id)
    )
    with TestClient(app) as client:
        resp = client.get(f"/api/agent/{agent_id}/evolution/summary")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] >= 3
        assert body["by_verdict"].get(CYCLE_PASSED, 0) >= 1
        assert body["by_verdict"].get(CYCLE_REJECTED, 0) >= 1
        assert body["by_verdict"].get(CYCLE_SKIPPED, 0) >= 1
        assert "all_candidates_failed" in body["by_reject_reason"]
        # window_seconds=0 is vacuously empty.
        resp_zero = client.get(
            f"/api/agent/{agent_id}/evolution/summary?window_seconds=0",
        )
        assert resp_zero.json()["total"] == 0


def test_evolution_funnel_endpoint():
    agent_id = _unique_agent_id("funnel")
    asyncio.get_event_loop().run_until_complete(
        _seed_cycles_and_artifacts(agent_id)
    )
    with TestClient(app) as client:
        resp = client.get(f"/api/agent/{agent_id}/evolution/funnel")
        assert resp.status_code == 200
        body = resp.json()
        assert body["by_status"].get(STATUS_PROMOTED, 0) >= 1
        assert body["by_status"].get(STATUS_NEEDS_APPROVAL, 0) >= 1
        assert body["by_status"].get(STATUS_ROLLED_BACK, 0) >= 1
        # kind filter narrows the view.
        resp_skill = client.get(
            f"/api/agent/{agent_id}/evolution/funnel?kind=skill",
        )
        assert resp_skill.status_code == 200
        assert resp_skill.json()["kind_filter"] == "skill"
        # Invalid kind returns 400.
        resp_bad = client.get(
            f"/api/agent/{agent_id}/evolution/funnel?kind=garbage",
        )
        assert resp_bad.status_code == 400


def test_evolution_rejects_endpoint():
    agent_id = _unique_agent_id("rejects")
    asyncio.get_event_loop().run_until_complete(
        _seed_cycles_and_artifacts(agent_id)
    )
    with TestClient(app) as client:
        resp = client.get(
            f"/api/agent/{agent_id}/evolution/rejects?limit=5",
        )
        assert resp.status_code == 200
        body = resp.json()
        # List of {reason, count}; sorted desc by count.
        assert isinstance(body, list)
        assert all("reason" in item and "count" in item for item in body)
        reasons = {item["reason"] for item in body}
        assert "all_candidates_failed" in reasons
        assert "no_insights" in reasons


def test_evolution_rollbacks_endpoint():
    agent_id = _unique_agent_id("rollbacks")
    seeded = asyncio.get_event_loop().run_until_complete(
        _seed_cycles_and_artifacts(agent_id)
    )
    with TestClient(app) as client:
        resp = client.get(
            f"/api/agent/{agent_id}/evolution/rollbacks?limit=10",
        )
        assert resp.status_code == 200
        rows = resp.json()
        assert any(r["artifact_id"] == seeded["rolled"] for r in rows)
        # Promoted artifact must NOT appear.
        assert all(r["artifact_id"] != seeded["promoted"] for r in rows)


# ── PR-E9-2: approval endpoints ────────────────────────────────────────────

def test_pending_approvals_endpoint_lists_only_needs_approval():
    agent_id = _unique_agent_id("pending")
    seeded = asyncio.get_event_loop().run_until_complete(
        _seed_cycles_and_artifacts(agent_id)
    )
    with TestClient(app) as client:
        resp = client.get(
            f"/api/agent/{agent_id}/evolution/pending_approvals",
        )
        assert resp.status_code == 200
        body = resp.json()
        pending_ids = {r["artifact_id"] for r in body["pending"]}
        assert seeded["pending"] in pending_ids
        assert seeded["promoted"] not in pending_ids
        assert seeded["rolled"] not in pending_ids


def test_approve_artifact_not_found_returns_404():
    agent_id = _unique_agent_id("notfound")
    with TestClient(app) as client:
        resp = client.post(
            f"/api/agent/{agent_id}/evolution/artifacts/skill_nope/approve",
        )
        assert resp.status_code == 404
        assert resp.json()["status"] == "not_found"


def test_decline_artifact_not_found_returns_404():
    agent_id = _unique_agent_id("notfound2")
    with TestClient(app) as client:
        resp = client.post(
            f"/api/agent/{agent_id}/evolution/artifacts/skill_nope/decline",
        )
        assert resp.status_code == 404


def test_decline_endpoint_retires_pending_artifact(tmp_path, monkeypatch):
    """End-to-end: a parked shadow gene is declined via HTTP → status flips
    to retired, shadow file (if we plant one) is deleted."""
    monkeypatch.setattr("xmclaw.evolution.skill_forge.BASE_DIR", tmp_path)
    monkeypatch.setattr("xmclaw.evolution.gene_forge.BASE_DIR", tmp_path)
    reset_journal_cache()

    agent_id = _unique_agent_id("decline_http")
    # Seed lineage row through the journal singleton.
    async def _seed():
        j = get_journal(agent_id)
        cid = await j.open_cycle(trigger="test_e9")
        artifact_id = f"gene_{uuid.uuid4().hex[:8]}"
        await j.record_artifact(cid, KIND_GENE, artifact_id,
                                status=STATUS_SHADOW)
        await j.update_artifact_status(artifact_id, STATUS_NEEDS_APPROVAL)
        return artifact_id
    artifact_id = asyncio.get_event_loop().run_until_complete(_seed())

    # The decline endpoint's `_evolution_engine_for` lazy-creates a fresh
    # engine for our unique agent_id; that engine's GeneForge reads the
    # monkeypatched BASE_DIR, so its shadow_dir is rooted under tmp_path.
    shadow_dir = tmp_path / "shared" / "genes" / "shadow"
    shadow_dir.mkdir(parents=True, exist_ok=True)
    shadow_file = shadow_dir / f"{artifact_id}.py"
    shadow_file.write_text("# placeholder\n", encoding="utf-8")

    with TestClient(app) as client:
        resp = client.post(
            f"/api/agent/{agent_id}/evolution/artifacts/{artifact_id}/decline",
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "retired"

    # Shadow file should be gone.
    assert not shadow_file.exists()
    # Status must have flipped.
    row = asyncio.get_event_loop().run_until_complete(
        get_journal(agent_id).get_artifact(artifact_id)
    )
    assert row["status"] == "retired"


def test_approve_endpoint_is_idempotent_on_already_decided():
    agent_id = _unique_agent_id("idempotent")
    async def _seed():
        j = get_journal(agent_id)
        cid = await j.open_cycle(trigger="test_e9")
        artifact_id = f"skill_{uuid.uuid4().hex[:8]}"
        await j.record_artifact(cid, KIND_SKILL, artifact_id,
                                status=STATUS_PROMOTED)
        return artifact_id
    artifact_id = asyncio.get_event_loop().run_until_complete(_seed())

    with TestClient(app) as client:
        resp = client.post(
            f"/api/agent/{agent_id}/evolution/artifacts/{artifact_id}/approve",
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "noop"
        assert body["current_status"] == STATUS_PROMOTED
