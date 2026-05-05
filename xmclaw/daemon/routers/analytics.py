"""Analytics API — token usage rollups for the Web UI Analytics page.

Mounted at ``/api/v2/analytics``. Aggregates LLM_RESPONSE events from
``~/.xmclaw/v2/events.db`` into daily / per-model totals for the
chart panels Hermes's AnalyticsPage renders.

Response shape::

    {
      "period_days": 30,
      "summary": {
        "total_prompt_tokens": 12345,
        "total_completion_tokens": 6789,
        "total_calls": 42,
        "models_used": 3,
      },
      "daily": [
        {"date": "2026-04-26", "input_tokens": 890, "output_tokens": 412, "calls": 6},
        ...
      ],
      "models": [
        {"model": "claude-opus-4-7", "input_tokens": 5000, "output_tokens": 2200, "calls": 18},
        ...
      ]
    }
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from time import gmtime, strftime, time

from fastapi import APIRouter
from starlette.responses import JSONResponse

from xmclaw.utils.paths import default_events_db_path

router = APIRouter(prefix="/api/v2/analytics", tags=["analytics"])

_DEFAULT_DAYS = 30
_MAX_DAYS = 365


def _date_str(epoch: float) -> str:
    return strftime("%Y-%m-%d", gmtime(epoch))


def _platform_of(session_id: str | None) -> str:
    """Classify session origin from its id prefix.

    XMclaw session id conventions:
      ``chat-*``                  → web UI
      ``feishu:*``                → 飞书 channel
      ``reflect:*`` / ``dream:*`` → background self-improvement
      ``probe-*`` / ``flow-*``    → probe / e2e tests
      else → "other"
    """
    s = (session_id or "").strip()
    if not s:
        return "other"
    if s.startswith("chat-"):
        return "web"
    if s.startswith("feishu:"):
        return "feishu"
    if s.startswith("reflect:"):
        return "reflect"
    if s.startswith("dream:") or s.startswith("skill-dream"):
        return "dream"
    if s.startswith("probe-") or s.startswith("flow-") or s.startswith("test"):
        return "probe"
    return "other"


@router.get("")
async def get_analytics(days: int = _DEFAULT_DAYS) -> JSONResponse:
    days = max(1, min(int(days), _MAX_DAYS))
    cutoff = time() - days * 86400.0

    db = default_events_db_path()
    if not db.exists():
        return JSONResponse({
            "period_days": days,
            "summary": {
                "total_prompt_tokens": 0,
                "total_completion_tokens": 0,
                "total_calls": 0,
                "models_used": 0,
            },
            "daily": [],
            "models": [],
            "tools": [],          # B-228
            "platforms": [],      # B-228
            "activity": {         # B-228
                "by_weekday": [0] * 7,
                "by_hour": [0] * 24,
            },
            "top_sessions": [],   # B-228
        })

    daily_in: dict[str, int] = defaultdict(int)
    daily_out: dict[str, int] = defaultdict(int)
    daily_calls: dict[str, int] = defaultdict(int)
    model_in: dict[str, int] = defaultdict(int)
    model_out: dict[str, int] = defaultdict(int)
    model_calls: dict[str, int] = defaultdict(int)
    total_in = total_out = total_calls = 0
    # B-228 new aggregates
    platform_in: dict[str, int] = defaultdict(int)
    platform_out: dict[str, int] = defaultdict(int)
    platform_calls: dict[str, int] = defaultdict(int)
    session_in: dict[str, int] = defaultdict(int)
    session_out: dict[str, int] = defaultdict(int)
    session_calls: dict[str, int] = defaultdict(int)
    session_last_ts: dict[str, float] = {}
    weekday_calls = [0] * 7
    hour_calls = [0] * 24

    try:
        conn = sqlite3.connect(str(db))
        try:
            llm_rows = conn.execute(
                """
                SELECT ts, session_id, payload FROM events
                 WHERE type = 'llm_response'
                   AND ts >= ?
                """,
                (cutoff,),
            ).fetchall()
            # B-228: tool invocation rollup. tool_invocation_finished is
            # the canonical "this tool ran" event (started events also
            # exist but finished pairs with the result).
            tool_rows = conn.execute(
                """
                SELECT payload FROM events
                 WHERE type = 'tool_invocation_finished'
                   AND ts >= ?
                """,
                (cutoff,),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.OperationalError:
        llm_rows = []
        tool_rows = []

    for ts, session_id, raw in llm_rows:
        try:
            payload = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if payload.get("ok") is False:
            # Failed turns count toward call total but have no useful tokens.
            continue
        pt = int(payload.get("prompt_tokens") or 0)
        ct = int(payload.get("completion_tokens") or 0)
        model = payload.get("model") or "unknown"
        date = _date_str(float(ts))
        daily_in[date]    += pt
        daily_out[date]   += ct
        daily_calls[date] += 1
        model_in[model]    += pt
        model_out[model]   += ct
        model_calls[model] += 1
        total_in    += pt
        total_out   += ct
        total_calls += 1
        # B-228 new tallies
        platform = _platform_of(session_id)
        platform_in[platform]    += pt
        platform_out[platform]   += ct
        platform_calls[platform] += 1
        if session_id:
            session_in[session_id]    += pt
            session_out[session_id]   += ct
            session_calls[session_id] += 1
            session_last_ts[session_id] = max(
                session_last_ts.get(session_id, 0.0), float(ts),
            )
        # weekday / hour distribution (local time via gmtime — close
        # enough for a usage heatmap; user-facing activity panel only
        # cares about coarse pattern not minute precision).
        tt = gmtime(float(ts))
        # gmtime tm_wday: 0=Mon..6=Sun
        weekday_calls[tt.tm_wday] += 1
        hour_calls[tt.tm_hour] += 1

    # B-228: tools rollup — count + error rate per tool name.
    tool_count: dict[str, int] = defaultdict(int)
    tool_errors: dict[str, int] = defaultdict(int)
    for (raw,) in tool_rows:
        try:
            payload = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        name = payload.get("name") or "unknown"
        tool_count[name] += 1
        if payload.get("ok") is False or payload.get("error"):
            tool_errors[name] += 1

    daily = [
        {
            "date": d,
            "input_tokens":  daily_in[d],
            "output_tokens": daily_out[d],
            "calls":         daily_calls[d],
        }
        for d in sorted(daily_in.keys())
    ]
    models = sorted(
        [
            {
                "model": m,
                "input_tokens":  model_in[m],
                "output_tokens": model_out[m],
                "calls":         model_calls[m],
            }
            for m in model_in.keys()
        ],
        key=lambda r: r["input_tokens"] + r["output_tokens"],
        reverse=True,
    )
    # B-228 — extended dimensions
    tools = sorted(
        [
            {
                "name":   n,
                "calls":  tool_count[n],
                "errors": tool_errors[n],
                "error_rate": (
                    round(tool_errors[n] / tool_count[n], 3)
                    if tool_count[n] else 0.0
                ),
            }
            for n in tool_count.keys()
        ],
        key=lambda r: r["calls"],
        reverse=True,
    )[:20]
    platforms = sorted(
        [
            {
                "platform": p,
                "calls":         platform_calls[p],
                "input_tokens":  platform_in[p],
                "output_tokens": platform_out[p],
            }
            for p in platform_calls.keys()
        ],
        key=lambda r: r["calls"],
        reverse=True,
    )
    top_sessions = sorted(
        [
            {
                "session_id":    sid,
                "calls":         session_calls[sid],
                "input_tokens":  session_in[sid],
                "output_tokens": session_out[sid],
                "total_tokens":  session_in[sid] + session_out[sid],
                "last_ts":       session_last_ts.get(sid, 0.0),
                "platform":      _platform_of(sid),
            }
            for sid in session_calls.keys()
        ],
        key=lambda r: r["total_tokens"],
        reverse=True,
    )[:10]

    return JSONResponse({
        "period_days": days,
        "summary": {
            "total_prompt_tokens":     total_in,
            "total_completion_tokens": total_out,
            "total_calls":             total_calls,
            "models_used":             len(models),
        },
        "daily":  daily,
        "models": models,
        # B-228: extended panels — feed Analytics.js sub-cards.
        "tools": tools,
        "platforms": platforms,
        "activity": {
            "by_weekday": weekday_calls,
            "by_hour":    hour_calls,
        },
        "top_sessions": top_sessions,
    })
