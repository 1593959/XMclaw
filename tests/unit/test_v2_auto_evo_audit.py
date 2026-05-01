"""B-123 — pin /learned_skills auto-disable audit trail.

When B-36 auto-parks a misbehaving skill it emits SKILL_OUTCOME with
``verdict="auto_disabled"`` + ``consecutive_errors``. The endpoint now
surfaces those as ``auto_disabled_ts`` + ``auto_disabled_streak`` so
the UI can label "user clicked暂停" vs "agent auto-停 after 3 errors".
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from starlette.responses import JSONResponse

from xmclaw.daemon.routers.auto_evo import learned_skills


# ── helpers ────────────────────────────────────────────────────────


def _seed_events_db(db_path: Path, rows: list[tuple[str, float, dict]]) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path))
    try:
        con.execute(
            "CREATE TABLE events ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "type TEXT, ts REAL, payload TEXT)"
        )
        for typ, ts, payload in rows:
            con.execute(
                "INSERT INTO events (type, ts, payload) VALUES (?, ?, ?)",
                (typ, ts, json.dumps(payload)),
            )
        con.commit()
    finally:
        con.close()


class _StubLoader:
    def __init__(self, root: Path, skills: list[dict]) -> None:
        self.skills_root = root
        self._skills = skills

    def list_for_api(self, *, include_disabled: bool = False) -> list[dict]:
        return [dict(s) for s in self._skills]


async def _call_endpoint() -> dict:
    resp = await learned_skills(include_disabled=True)
    assert isinstance(resp, JSONResponse)
    return json.loads(resp.body.decode("utf-8"))


# ── tests ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_auto_disabled_event_surfaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A SKILL_OUTCOME with verdict=auto_disabled gets surfaced as
    auto_disabled_ts + auto_disabled_streak fields on the row."""
    db = tmp_path / "v2" / "events.db"
    _seed_events_db(db, [
        ("skill_outcome", 1700_000_000.0, {
            "skill_id": "auto_repair_v1",
            "verdict": "auto_disabled",
            "consecutive_errors": 3,
        }),
    ])

    monkeypatch.setattr(
        "xmclaw.utils.paths.data_dir", lambda: tmp_path,
    )
    monkeypatch.setattr(
        "xmclaw.daemon.learned_skills.default_learned_skills_loader",
        lambda: _StubLoader(tmp_path / "skills", [
            {"skill_id": "auto_repair_v1", "title": "Auto Repair v1",
             "disabled": True},
        ]),
    )

    body = await _call_endpoint()
    sk = body["skills"][0]
    assert sk["skill_id"] == "auto_repair_v1"
    assert sk["auto_disabled_ts"] == pytest.approx(1700_000_000.0)
    assert sk["auto_disabled_streak"] == 3


@pytest.mark.asyncio
async def test_only_latest_auto_disable_surfaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When two auto_disabled events exist for the same skill, only
    the most recent one's ts/streak survives in the response."""
    db = tmp_path / "v2" / "events.db"
    _seed_events_db(db, [
        ("skill_outcome", 1600_000_000.0, {
            "skill_id": "x",
            "verdict": "auto_disabled",
            "consecutive_errors": 3,
        }),
        ("skill_outcome", 1700_000_000.0, {
            "skill_id": "x",
            "verdict": "auto_disabled",
            "consecutive_errors": 5,
        }),
    ])

    monkeypatch.setattr("xmclaw.utils.paths.data_dir", lambda: tmp_path)
    monkeypatch.setattr(
        "xmclaw.daemon.learned_skills.default_learned_skills_loader",
        lambda: _StubLoader(tmp_path / "skills", [
            {"skill_id": "x", "title": "x", "disabled": True},
        ]),
    )

    body = await _call_endpoint()
    sk = body["skills"][0]
    assert sk["auto_disabled_ts"] == pytest.approx(1700_000_000.0)
    assert sk["auto_disabled_streak"] == 5


@pytest.mark.asyncio
async def test_no_auto_disable_means_no_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A skill that was never auto-disabled must NOT have
    auto_disabled_ts in its response — UI distinguishes manual暂停
    from auto-停 by presence/absence of this field."""
    db = tmp_path / "v2" / "events.db"
    _seed_events_db(db, [
        ("skill_outcome", 1700_000_000.0, {
            "skill_id": "x",
            "verdict": "success",
        }),
    ])

    monkeypatch.setattr("xmclaw.utils.paths.data_dir", lambda: tmp_path)
    monkeypatch.setattr(
        "xmclaw.daemon.learned_skills.default_learned_skills_loader",
        lambda: _StubLoader(tmp_path / "skills", [
            {"skill_id": "x", "title": "x", "disabled": True},
        ]),
    )

    body = await _call_endpoint()
    sk = body["skills"][0]
    assert "auto_disabled_ts" not in sk
    assert "auto_disabled_streak" not in sk
    # success outcome is still tallied normally.
    assert sk["outcomes"]["success"] == 1


@pytest.mark.asyncio
async def test_auto_disable_does_not_skew_outcome_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Critical: the auto_disabled verdict must NOT count as 'error'
    in the success/partial/error tally — it's a meta-event, not a
    real grader verdict, and folding it in would corrupt the success
    rate that drives the UI's color-coded badge."""
    db = tmp_path / "v2" / "events.db"
    _seed_events_db(db, [
        ("skill_outcome", 1.0, {"skill_id": "x", "verdict": "success"}),
        ("skill_outcome", 2.0, {"skill_id": "x", "verdict": "success"}),
        ("skill_outcome", 3.0, {
            "skill_id": "x",
            "verdict": "auto_disabled",
            "consecutive_errors": 3,
        }),
    ])

    monkeypatch.setattr("xmclaw.utils.paths.data_dir", lambda: tmp_path)
    monkeypatch.setattr(
        "xmclaw.daemon.learned_skills.default_learned_skills_loader",
        lambda: _StubLoader(tmp_path / "skills", [
            {"skill_id": "x", "title": "x", "disabled": True},
        ]),
    )

    body = await _call_endpoint()
    sk = body["skills"][0]
    assert sk["outcomes"] == {"success": 2, "partial": 0, "error": 0}
    assert sk["auto_disabled_streak"] == 3
