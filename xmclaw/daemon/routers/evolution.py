"""B-301: live evolution-chain status.

Mounted at ``/api/v2/evolution``. Surfaces the things you can ONLY see
from inside the running daemon — `EvolutionAgent._arms` snapshot, the
trigger's fire counter, recent skill_dream cycle results, and (most
importantly) per-skill progress towards the controller's `min_plays`
threshold.

Why this endpoint exists
------------------------

Pre-B-301 the Evolution UI page only had `/api/v2/events` to read from.
That's good for "what proposals fired in the last 7 days" but useless
for "why is nothing happening RIGHT NOW". The bandit aggregate is
in-memory + on disk in `state.json`; the trigger's debounce/cooldown
state is in-memory only; the dream-cycle's last-run results live in a
session journal nobody reads. So users (and Claude itself, in the
sibling conversation that motivated B-301) end up looking in the wrong
places — they go check cron / memory compact / random log files
because the actual chain state isn't surfaced anywhere.

This endpoint does one thing: dump the live state in a single
JSON-serialisable object the UI can render as a status panel.

When the chain isn't wired (echo-mode daemon, ``evolution.enabled=false``,
or wiring failed at boot) the endpoint returns
``{"observer": null, ...}`` rather than 404 — that lets the UI degrade
into a clean "evolution is not configured on this daemon" message.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse

router = APIRouter(prefix="/api/v2/evolution", tags=["evolution"])


# ── thresholds (mirror controller defaults; UI shows progress%) ────


_DEFAULT_MIN_PLAYS = 10  # PromotionThresholds.min_plays default
_DEFAULT_MIN_MEAN = 0.65  # PromotionThresholds.min_mean default


# ── helpers ────────────────────────────────────────────────────────


def _arm_progress(plays: int, mean_score: float) -> dict[str, Any]:
    """Per-arm progress dict for the UI.

    The two gates the controller cares about are ``plays >= min_plays``
    and ``mean_score >= min_mean``. We surface both as fractions so the
    UI can draw a "5/10 plays  • 0.80/0.65 mean" progress bar without
    having to know controller internals.
    """
    play_progress = min(1.0, plays / _DEFAULT_MIN_PLAYS) if _DEFAULT_MIN_PLAYS > 0 else 1.0
    mean_progress = (
        min(1.0, mean_score / _DEFAULT_MIN_MEAN)
        if _DEFAULT_MIN_MEAN > 0 and mean_score is not None else 0.0
    )
    return {
        "plays": plays,
        "plays_required": _DEFAULT_MIN_PLAYS,
        "plays_progress": round(play_progress, 3),
        "mean_score": round(float(mean_score), 4) if mean_score is not None else None,
        "mean_required": _DEFAULT_MIN_MEAN,
        "mean_progress": round(mean_progress, 3),
        "ready_to_propose": (
            plays >= _DEFAULT_MIN_PLAYS
            and mean_score is not None
            and mean_score >= _DEFAULT_MIN_MEAN
        ),
    }


def _read_skill_dream_audit(audit_path: Path, limit: int = 10) -> list[dict[str, Any]]:
    """Tail-read the skill-dream audit JSONL.

    Each line is one accepted-proposal record from
    ``SkillDreamCycle._append_audit``. Newest-last; we reverse for
    UI consumption.
    """
    if not audit_path.exists():
        return []
    try:
        lines = audit_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    # Walk backwards so we get newest first without holding the whole
    # file in memory if it's large.
    for line in reversed(lines[-limit * 4:]):  # over-sample, then trim
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
        if len(out) >= limit:
            break
    return out


# ── routes ────────────────────────────────────────────────────────


@router.get("/snapshot")
async def evolution_snapshot(request: Request) -> JSONResponse:
    """Live status of the evolution chain.

    Response shape::

        {
          "observer": {
            "agent_id": "evo-main",
            "is_running": true,
            "arms": [
              {"skill_id": "...", "version": 1, "plays": 3,
               "mean_score": 0.80, "ewma_mean": 0.80,
               "score_mode": "mean",
               "progress": {"plays": 3, "plays_required": 10,
                            "plays_progress": 0.30,
                            "mean_score": 0.80, "mean_required": 0.65,
                            "mean_progress": 1.0,
                            "ready_to_propose": false}}
            ],
            "ready_to_propose_count": 0,
            "tracked_skill_count": 1
          },
          "trigger": {
            "is_active": true,
            "debounce_s": 30.0,
            "cooldown_s": 300.0,
            "min_new_verdicts": 10,
            "fire_count": 0,
            "verdicts_since_last_fire": 0
          },
          "variant_selector": {
            "is_active": true,
            "exploration_c": 2.0,
            "head_warmup_plays": 5,
            "tracked_arm_count": 1
          },
          "skill_dream": {
            "recent_proposals": [{...audit row}],
            "audit_path": "..."
          }
        }
    """
    state = request.app.state

    # ── observer ────────────────────────────────────────────────
    evo = getattr(state, "evolution_observer", None)
    observer_payload: dict[str, Any] | None
    if evo is None:
        observer_payload = None
    else:
        try:
            evals = evo.snapshot()
        except Exception:  # noqa: BLE001
            evals = []
        arms = []
        ready_count = 0
        for e in evals:
            notes = e.notes or {}
            progress = _arm_progress(e.plays, e.mean_score)
            if progress.get("ready_to_propose"):
                ready_count += 1
            arms.append({
                "skill_id": e.candidate_id,
                "version": e.version,
                "plays": e.plays,
                "mean_score": (
                    round(float(e.mean_score), 4)
                    if e.mean_score is not None else None
                ),
                "ewma_mean": (
                    round(float(notes.get("ewma_mean")), 4)
                    if notes.get("ewma_mean") is not None else None
                ),
                "lifetime_mean": (
                    round(float(notes.get("lifetime_mean")), 4)
                    if notes.get("lifetime_mean") is not None else None
                ),
                "score_mode": notes.get("score_mode"),
                "progress": progress,
            })
        # Sort: ready-to-propose first, then by plays desc — UI sees
        # "what's about to happen" at the top.
        arms.sort(
            key=lambda a: (
                not a["progress"]["ready_to_propose"], -a["plays"],
            ),
        )
        observer_payload = {
            "agent_id": getattr(evo, "_agent_id", "evo-main"),
            "is_running": bool(
                getattr(evo, "is_running", lambda: True)()
                if callable(getattr(evo, "is_running", None))
                else True
            ),
            "arms": arms,
            "tracked_skill_count": len(arms),
            "ready_to_propose_count": ready_count,
        }

    # ── trigger ────────────────────────────────────────────────
    trig = getattr(state, "evolution_evaluation_trigger", None)
    trigger_payload: dict[str, Any] | None = None
    if trig is not None:
        trigger_payload = {
            "is_active": bool(getattr(trig, "is_active", False)),
            "debounce_s": float(getattr(trig, "_debounce_s", 30.0)),
            "cooldown_s": float(getattr(trig, "_cooldown_s", 300.0)),
            "min_new_verdicts": int(getattr(trig, "_min_new_verdicts", 10)),
            "fire_count": int(getattr(trig, "fire_count", 0)),
            "verdicts_since_last_fire": int(
                getattr(trig, "verdicts_since_last_fire", 0),
            ),
        }

    # ── variant selector ───────────────────────────────────────
    vs = getattr(state, "variant_selector", None)
    selector_payload: dict[str, Any] | None = None
    if vs is not None:
        # Snapshot of arm-stats; cheap because in-memory.
        try:
            tracked = len(vs.snapshot()) if hasattr(vs, "snapshot") else 0
        except Exception:  # noqa: BLE001
            tracked = 0
        selector_payload = {
            "is_active": bool(getattr(vs, "is_active", True)),
            "exploration_c": float(getattr(vs, "exploration_c", 2.0)),
            "head_warmup_plays": int(getattr(vs, "head_warmup_plays", 5)),
            "tracked_arm_count": tracked,
        }

    # ── skill_dream ────────────────────────────────────────────
    # Audit file path: agents in xmclaw write to
    # ``<data>/agents/<agent_id>/audit.jsonl`` historically; the
    # canonical SkillDreamCycle path is exposed via app.state.
    skill_dream_payload: dict[str, Any] = {"recent_proposals": []}
    sd = getattr(state, "skill_dream", None) or getattr(
        state, "skill_dream_cycle", None,
    )
    if sd is not None:
        audit = getattr(sd, "_audit_path", None) or getattr(
            sd, "audit_path", None,
        )
        if audit is not None:
            audit_p = Path(audit)
            skill_dream_payload["audit_path"] = str(audit_p)
            skill_dream_payload["recent_proposals"] = (
                _read_skill_dream_audit(audit_p, limit=10)
            )

    # ── server timestamp (for UI age display) ──────────────────
    return JSONResponse({
        "ts": time.time(),
        "observer": observer_payload,
        "trigger": trigger_payload,
        "variant_selector": selector_payload,
        "skill_dream": skill_dream_payload,
    })


# 2026-05-10 P2 (3): Evolution.js used to fan 3 separate
# ``/api/v2/events?types=...&since=...&limit=...`` GETs out (proposals,
# grader verdicts, promotions/rollbacks) and merge them client-side.
# That:
#   * triple-paid the events.db round-trip per page tick (bad as journal grows),
#   * couldn't sort across kinds — proposals can interleave with their
#     verdicts and the UI rendered them as separate buckets without
#     correlating,
#   * meant any future filter (per-skill, per-tier) needed 3 query
#     params plumbed across 3 client calls.
#
# This endpoint is a thin server-side aggregator: one events.db read,
# one merge, returned as 4 buckets the UI already wants. Falls back
# gracefully when the bus isn't SqliteEventBus (test harnesses).


@router.get("/proposals")
async def evolution_proposals(
    request: Request,
    since: float | None = None,
    limit: int = 50,
) -> JSONResponse:
    """Aggregated evolution-chain feed.

    Args:
        since: unix ts to filter from; ``None`` means full history
            (still capped by ``limit`` per bucket)
        limit: max rows per bucket. Default 50, capped at 500.

    Returns:
        ``{
          "ts": float,
          "since": float | null,
          "proposals":  [BehavioralEvent...],
          "verdicts":   [BehavioralEvent...],
          "promotions": [BehavioralEvent...],
          "rollbacks":  [BehavioralEvent...]
        }``
    Each list is newest-first. Promotions / rollbacks share their cap
    (skill_promoted + skill_rolled_back returned in two separate
    lists for UI layout simplicity).
    """
    from xmclaw.core.bus import EventType, SqliteEventBus, event_as_jsonable

    limit = max(1, min(int(limit), 500))
    bus = request.app.state.bus
    if not isinstance(bus, SqliteEventBus):
        return JSONResponse({
            "ts": time.time(), "since": since,
            "proposals": [], "verdicts": [],
            "promotions": [], "rollbacks": [],
            "warning": "event bus is not SqliteEventBus; "
                       "in-memory bus has no historical query path",
        })

    def _q(types: list[EventType]) -> list[dict[str, Any]]:
        try:
            rows = bus.query(
                since=since, types=types, limit=limit, offset=0,
            )
        except Exception:  # noqa: BLE001
            return []
        # Newest-first (events.db returns ascending by default).
        return [event_as_jsonable(r) for r in reversed(rows)]

    return JSONResponse({
        "ts": time.time(),
        "since": since,
        "proposals": _q([EventType.SKILL_CANDIDATE_PROPOSED]),
        "verdicts": _q([EventType.GRADER_VERDICT]),
        "promotions": _q([EventType.SKILL_PROMOTED]),
        "rollbacks": _q([EventType.SKILL_ROLLED_BACK]),
    })
