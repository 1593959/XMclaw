"""EvolutionLoop auto-approval — Wave-32+ (2026-05-18).

Pins:

  * High-confidence proposals (>= threshold) are persisted as
    ``status="approved"`` directly (not "pending"), so they bypass
    the operator-approval queue
  * Low-confidence proposals stay "pending"
  * Disabling the feature flag turns everything back into pending
  * Threshold is read from the feature flag engine + clamped to [0,1]
  * Misconfigured threshold doesn't crash the loop
"""
from __future__ import annotations

import json

import pytest

from xmclaw.cognition.evolution_loop import (
    EvolutionLoop,
    EvolutionProposal,
    _resolve_auto_approve_config,
    _with_status,
)
from xmclaw.core.feature_flags import set_default_engine
from xmclaw.core.feature_flags.engine import FeatureFlagEngine
from xmclaw.core.feature_flags.registry import BUILTIN_FLAGS


@pytest.fixture(autouse=True)
def _fresh_engine(tmp_path, monkeypatch):
    """Each test gets a fresh FeatureFlagEngine so flag overrides
    from one test don't leak into the next."""
    monkeypatch.setenv("XMC_DATA_DIR", str(tmp_path / "xdata"))
    eng = FeatureFlagEngine(disk_path=tmp_path / "flags.json")
    eng.register_many(BUILTIN_FLAGS)
    set_default_engine(eng)
    yield eng
    set_default_engine(None)


# ── pure helpers ─────────────────────────────────────────────────────────


def test_with_status_returns_new_instance() -> None:
    p = EvolutionProposal(
        id="p1", type="skill_promote", description="d",
        target="t", diff="x", confidence=0.5,
    )
    q = _with_status(p, "approved")
    assert q.status == "approved"
    assert p.status == "pending"  # original untouched (frozen)
    assert q.id == p.id  # everything else preserved


def test_resolve_config_defaults_when_no_overrides(_fresh_engine) -> None:
    enabled, threshold = _resolve_auto_approve_config()
    assert enabled is True
    assert threshold == 0.8


def test_resolve_config_picks_up_engine_overrides(_fresh_engine) -> None:
    _fresh_engine.set("evolution.auto_approve.enabled", False, persist=False)
    _fresh_engine.set("evolution.auto_approve.threshold", 0.95, persist=False)
    enabled, threshold = _resolve_auto_approve_config()
    assert enabled is False
    assert threshold == 0.95


def test_resolve_config_clamps_out_of_range(_fresh_engine) -> None:
    _fresh_engine.set("evolution.auto_approve.threshold", 1.7, persist=False)
    _, t = _resolve_auto_approve_config()
    assert t == 1.0
    _fresh_engine.set("evolution.auto_approve.threshold", -0.5, persist=False)
    _, t2 = _resolve_auto_approve_config()
    assert t2 == 0.0


def test_resolve_config_bad_threshold_value_falls_back(_fresh_engine) -> None:
    _fresh_engine.set("evolution.auto_approve.threshold", "not-a-number", persist=False)
    _, t = _resolve_auto_approve_config()
    assert t == 0.8


# ── EvolutionLoop integration ────────────────────────────────────────────


def _make_proposal(confidence: float, suffix: str = "") -> EvolutionProposal:
    return EvolutionProposal(
        id=f"prop-{confidence}-{suffix}",
        type="skill_promote",
        description="test proposal",
        target=f"skills/test{suffix}.md",
        diff="",
        confidence=confidence,
    )


def _stub_analyzer(*proposals: EvolutionProposal):
    """Helper: return an async no-op analyzer that yields the given
    proposals when its analyze() method is called."""
    class _Stub:
        async def analyze(self, *_args, **_kw):
            return list(proposals)
    return _Stub()


@pytest.mark.asyncio
async def test_high_confidence_auto_approved(tmp_path) -> None:
    """A 0.9-confidence proposal lands on disk with status=approved."""
    loop = EvolutionLoop(proposals_dir=tmp_path)
    # Replace analyzers with stubs that yield deterministic output.
    loop.skill_promoter = _stub_analyzer(_make_proposal(0.9))
    loop.prompt_evolver = _stub_analyzer()
    loop.perf_analyzer = _stub_analyzer()
    loop.pattern_extractor = _stub_analyzer()

    await loop.trigger_once()

    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    data = json.loads(files[0].read_text(encoding="utf-8"))
    assert data["status"] == "approved"
    assert data["confidence"] == 0.9


@pytest.mark.asyncio
async def test_low_confidence_stays_pending(tmp_path) -> None:
    """0.4 < threshold (0.8) → status stays pending, UI surfaces it."""
    loop = EvolutionLoop(proposals_dir=tmp_path)
    loop.skill_promoter = _stub_analyzer(_make_proposal(0.4))
    loop.prompt_evolver = _stub_analyzer()
    loop.perf_analyzer = _stub_analyzer()
    loop.pattern_extractor = _stub_analyzer()

    await loop.trigger_once()

    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    assert json.loads(files[0].read_text(encoding="utf-8"))["status"] == "pending"


@pytest.mark.asyncio
async def test_exactly_at_threshold_is_auto_approved(tmp_path) -> None:
    """Confidence == threshold uses >= comparison, so it auto-approves.
    Pin this — a careless `>` would change behavior at the boundary."""
    loop = EvolutionLoop(proposals_dir=tmp_path)
    loop.skill_promoter = _stub_analyzer(_make_proposal(0.8))
    loop.prompt_evolver = _stub_analyzer()
    loop.perf_analyzer = _stub_analyzer()
    loop.pattern_extractor = _stub_analyzer()

    await loop.trigger_once()

    files = list(tmp_path.glob("*.json"))
    assert json.loads(files[0].read_text(encoding="utf-8"))["status"] == "approved"


@pytest.mark.asyncio
async def test_disable_flag_keeps_everything_pending(
    tmp_path, _fresh_engine,
) -> None:
    """Operator can turn off the auto-approve entirely — everything
    goes back to needing manual click. Pin the kill-switch behavior."""
    _fresh_engine.set("evolution.auto_approve.enabled", False, persist=False)
    loop = EvolutionLoop(proposals_dir=tmp_path)
    loop.skill_promoter = _stub_analyzer(_make_proposal(0.9), _make_proposal(0.5, "b"))
    loop.prompt_evolver = _stub_analyzer()
    loop.perf_analyzer = _stub_analyzer()
    loop.pattern_extractor = _stub_analyzer()

    await loop.trigger_once()

    for f in tmp_path.glob("*.json"):
        assert json.loads(f.read_text(encoding="utf-8"))["status"] == "pending"


@pytest.mark.asyncio
async def test_threshold_override_changes_cutoff(
    tmp_path, _fresh_engine,
) -> None:
    """Raising the threshold to 0.95 makes a 0.9 proposal go to
    pending instead of approved — confirms the flag actually drives
    the cutoff at runtime."""
    _fresh_engine.set("evolution.auto_approve.threshold", 0.95, persist=False)
    loop = EvolutionLoop(proposals_dir=tmp_path)
    loop.skill_promoter = _stub_analyzer(_make_proposal(0.9))
    loop.prompt_evolver = _stub_analyzer()
    loop.perf_analyzer = _stub_analyzer()
    loop.pattern_extractor = _stub_analyzer()

    await loop.trigger_once()

    files = list(tmp_path.glob("*.json"))
    assert json.loads(files[0].read_text(encoding="utf-8"))["status"] == "pending"


@pytest.mark.asyncio
async def test_auto_approve_pending_backfill(tmp_path) -> None:
    """Backfill pass walks existing pending proposals on disk and
    flips the high-confidence ones to approved. Pin counts so a
    careless refactor doesn't double-process or skip cases."""
    loop = EvolutionLoop(proposals_dir=tmp_path)
    # Manually seed three pending proposals + one already-approved
    # (must be skipped by the pending-only filter).
    for conf, status, suffix in [
        (0.95, "pending", "hi"),
        (0.4, "pending", "lo"),
        (0.85, "pending", "hi2"),
        (0.99, "approved", "already"),  # already approved — don't re-touch
    ]:
        p = _make_proposal(conf, suffix)
        data = {
            "id": p.id, "type": p.type, "description": p.description,
            "target": p.target, "diff": p.diff,
            "confidence": p.confidence, "evidence": p.evidence,
            "created_at": p.created_at, "status": status,
        }
        (tmp_path / f"{p.id}.json").write_text(json.dumps(data), encoding="utf-8")

    counts = await loop.auto_approve_pending()
    assert counts == {"approved": 2, "kept_pending": 1, "skipped_errors": 0}

    statuses: dict[str, int] = {}
    for f in tmp_path.glob("*.json"):
        s = json.loads(f.read_text(encoding="utf-8"))["status"]
        statuses[s] = statuses.get(s, 0) + 1
    # 2 newly approved + 1 already approved + 1 kept pending.
    assert statuses == {"approved": 3, "pending": 1}


@pytest.mark.asyncio
async def test_auto_approve_pending_noop_when_disabled(
    tmp_path, _fresh_engine,
) -> None:
    """Disabling the feature gates the backfill too — operator can't
    accidentally clear the pile by hitting the backfill endpoint
    when they explicitly turned the feature off."""
    _fresh_engine.set("evolution.auto_approve.enabled", False, persist=False)
    loop = EvolutionLoop(proposals_dir=tmp_path)
    p = _make_proposal(0.99)
    (tmp_path / f"{p.id}.json").write_text(
        json.dumps({
            "id": p.id, "type": p.type, "description": p.description,
            "target": p.target, "diff": p.diff, "confidence": p.confidence,
            "evidence": [], "created_at": 0, "status": "pending",
        }),
        encoding="utf-8",
    )
    counts = await loop.auto_approve_pending()
    assert counts == {"approved": 0, "kept_pending": 0, "skipped_errors": 0}
    # Original stays pending.
    pending = await loop.list_pending()
    assert len(pending) == 1


@pytest.mark.asyncio
async def test_auto_approve_pending_handles_corrupt_files(tmp_path) -> None:
    """Junk JSON in the proposals dir mustn't crash the sweep —
    count toward skipped_errors and move on."""
    loop = EvolutionLoop(proposals_dir=tmp_path)
    (tmp_path / "good.json").write_text(
        json.dumps({
            "id": "g1", "type": "skill_promote", "description": "d",
            "target": "t", "diff": "", "confidence": 0.95,
            "evidence": [], "created_at": 0, "status": "pending",
        }),
        encoding="utf-8",
    )
    (tmp_path / "bad.json").write_text("{not valid json", encoding="utf-8")

    counts = await loop.auto_approve_pending()
    assert counts["approved"] == 1
    assert counts["skipped_errors"] == 1


@pytest.mark.asyncio
async def test_mixed_batch_splits_by_confidence(tmp_path) -> None:
    """Realistic case: one batch with high + low confidence proposals
    should split — high go to approved, low stay pending. UI only
    shows the pending ones (list_pending filters)."""
    loop = EvolutionLoop(proposals_dir=tmp_path)
    loop.skill_promoter = _stub_analyzer(
        _make_proposal(0.9, "hi"),
        _make_proposal(0.4, "lo"),
        _make_proposal(0.85, "hi2"),
    )
    loop.prompt_evolver = _stub_analyzer()
    loop.perf_analyzer = _stub_analyzer()
    loop.pattern_extractor = _stub_analyzer()

    await loop.trigger_once()

    status_counts = {"approved": 0, "pending": 0}
    for f in tmp_path.glob("*.json"):
        status_counts[json.loads(f.read_text(encoding="utf-8"))["status"]] += 1
    assert status_counts == {"approved": 2, "pending": 1}

    # list_pending should only surface the low-confidence one.
    pending = await loop.list_pending()
    assert len(pending) == 1
    assert pending[0].confidence == 0.4
