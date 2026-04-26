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
        })

    daily_in: dict[str, int] = defaultdict(int)
    daily_out: dict[str, int] = defaultdict(int)
    daily_calls: dict[str, int] = defaultdict(int)
    model_in: dict[str, int] = defaultdict(int)
    model_out: dict[str, int] = defaultdict(int)
    model_calls: dict[str, int] = defaultdict(int)
    total_in = total_out = total_calls = 0

    try:
        conn = sqlite3.connect(str(db))
        try:
            rows = conn.execute(
                """
                SELECT ts, payload FROM events
                 WHERE type = 'llm_response'
                   AND ts >= ?
                """,
                (cutoff,),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.OperationalError:
        rows = []

    for ts, raw in rows:
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
    })
