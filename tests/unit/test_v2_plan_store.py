"""Epic #26 Phase C (2026-05-19): PlanStore persistence ledger.

Pins:
  * start() inserts a row with status=executing
  * update_progress + finalise stamp the lifecycle
  * mark_orphaned() flips lingering executing rows on boot
  * list_recent / get / counts_by_status query paths
"""
from __future__ import annotations

from pathlib import Path

import pytest

from xmclaw.cognition.plan_store import (
    PLAN_STATUS_COMPLETED,
    PLAN_STATUS_EXECUTING,
    PLAN_STATUS_FAILED,
    PLAN_STATUS_ORPHANED,
    PlanStore,
)


@pytest.fixture
def store(tmp_path: Path) -> PlanStore:
    s = PlanStore(tmp_path / "plans.db")
    yield s
    s.close()


def test_start_creates_executing_row(store: PlanStore) -> None:
    store.start("p1", goal_id="g1", n_steps=3, budget_usd=2.0)
    row = store.get("p1")
    assert row is not None
    assert row["plan_id"] == "p1"
    assert row["goal_id"] == "g1"
    assert row["status"] == PLAN_STATUS_EXECUTING
    assert row["n_steps"] == 3
    assert row["n_completed"] == 0
    assert row["budget_usd"] == 2.0
    assert row["finished_at"] is None


def test_start_is_idempotent(store: PlanStore) -> None:
    """Same plan_id called twice → INSERT OR IGNORE keeps the first
    row + doesn't crash. Lets dispatcher retry execute_plan after a
    restart without conflicting on the PK."""
    store.start("p1", goal_id="g1", n_steps=2)
    store.start("p1", goal_id="something_else", n_steps=99)
    row = store.get("p1")
    assert row["goal_id"] == "g1"  # first wins
    assert row["n_steps"] == 2


def test_update_progress_tracks_completed(store: PlanStore) -> None:
    store.start("p1", n_steps=3)
    store.update_progress("p1", n_completed=1, spent_usd=0.05)
    row = store.get("p1")
    assert row["n_completed"] == 1
    assert row["spent_usd"] == 0.05
    assert row["status"] == PLAN_STATUS_EXECUTING


def test_finalise_completed_stamps_terminal(store: PlanStore) -> None:
    store.start("p1", n_steps=2)
    store.finalise(
        "p1", status=PLAN_STATUS_COMPLETED,
        spent_usd=0.12, n_completed=2,
    )
    row = store.get("p1")
    assert row["status"] == PLAN_STATUS_COMPLETED
    assert row["finished_at"] is not None
    assert row["spent_usd"] == 0.12
    assert row["n_completed"] == 2
    assert row["error"] is None


def test_finalise_failed_records_error(store: PlanStore) -> None:
    store.start("p1", n_steps=2)
    store.finalise(
        "p1", status=PLAN_STATUS_FAILED,
        error="step s1 failed: boom",
    )
    row = store.get("p1")
    assert row["status"] == PLAN_STATUS_FAILED
    assert "step s1 failed" in row["error"]


def test_finalise_invalid_status_rejected(store: PlanStore) -> None:
    """Random status strings can't get into the DB — guards
    downstream consumers (UI / cognitive_daemon) from unknown values."""
    store.start("p1", n_steps=1)
    store.finalise("p1", status="weird_status")
    # Status unchanged.
    assert store.get("p1")["status"] == PLAN_STATUS_EXECUTING


def test_mark_orphaned_flips_executing_rows(store: PlanStore) -> None:
    """Boot-sweep: any plan still in ``executing`` from a previous
    daemon run gets flipped. Completed/failed plans are untouched."""
    store.start("p1", n_steps=1)
    store.start("p2", n_steps=1)
    store.finalise("p2", status=PLAN_STATUS_COMPLETED)

    n = store.mark_orphaned()
    assert n == 1  # only p1
    assert store.get("p1")["status"] == PLAN_STATUS_ORPHANED
    assert store.get("p2")["status"] == PLAN_STATUS_COMPLETED


def test_list_recent_orders_newest_first(store: PlanStore) -> None:
    import time as _time
    store.start("p1", n_steps=1)
    _time.sleep(0.01)
    store.start("p2", n_steps=1)
    _time.sleep(0.01)
    store.start("p3", n_steps=1)
    rows = store.list_recent(limit=10)
    ids = [r["plan_id"] for r in rows]
    assert ids == ["p3", "p2", "p1"]


def test_list_recent_filters_by_status(store: PlanStore) -> None:
    store.start("p1", n_steps=1)
    store.finalise("p1", status=PLAN_STATUS_COMPLETED)
    store.start("p2", n_steps=1)
    store.start("p3", n_steps=1)
    store.finalise("p3", status=PLAN_STATUS_FAILED, error="bad")

    completed = store.list_recent(status=PLAN_STATUS_COMPLETED)
    assert [r["plan_id"] for r in completed] == ["p1"]

    executing = store.list_recent(status=PLAN_STATUS_EXECUTING)
    assert [r["plan_id"] for r in executing] == ["p2"]


def test_counts_by_status(store: PlanStore) -> None:
    store.start("p1", n_steps=1)
    store.start("p2", n_steps=1)
    store.start("p3", n_steps=1)
    store.finalise("p1", status=PLAN_STATUS_COMPLETED)
    store.finalise("p2", status=PLAN_STATUS_FAILED, error="x")

    counts = store.counts_by_status()
    assert counts.get(PLAN_STATUS_EXECUTING) == 1
    assert counts.get(PLAN_STATUS_COMPLETED) == 1
    assert counts.get(PLAN_STATUS_FAILED) == 1
