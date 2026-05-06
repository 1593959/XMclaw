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


# P0 wrap-up: rough cost-per-million-tokens table for the most common
# model families. Adapted from xmclaw_port/insights.py — heuristic
# only, real pricing varies by provider tier and changes over time.
# When a model isn't in the table we fall back to a generic "small
# OSS model" rate so a number is always shown.
_MODEL_COST_PER_MTOK_USD: tuple[tuple[str, float, float], ...] = (
    # (substring_match, input_per_mtok, output_per_mtok)
    ("gpt-4o",     2.50,  10.00),
    ("gpt-4",     30.00,  60.00),
    ("gpt-3.5",    0.50,   1.50),
    ("o1-",       15.00,  60.00),
    ("o3-",       15.00,  60.00),
    ("claude-3-opus",   15.00, 75.00),
    ("claude-opus",     15.00, 75.00),
    ("claude-3-sonnet",  3.00, 15.00),
    ("claude-sonnet",    3.00, 15.00),
    ("claude-3-haiku",   0.25,  1.25),
    ("claude-haiku",     0.25,  1.25),
    ("claude",           3.00, 15.00),
    ("gemini-1.5-pro",   1.25,  5.00),
    ("gemini-pro",       0.50,  1.50),
    ("kimi",             0.30,  1.20),
    ("moonshot",         0.30,  1.20),
    ("qwen",             0.30,  1.20),
    ("glm",              0.30,  1.20),
    ("minimax",          0.20,  0.80),
    ("deepseek",         0.14,  0.28),
    ("llama",            0.20,  0.60),
)
_DEFAULT_COST_PER_MTOK = (0.50, 1.50)


def _estimate_cost_usd(model: str, in_tok: int, out_tok: int) -> float:
    """Heuristic per-call cost in USD. Best-effort — see comment above."""
    name = (model or "").lower()
    rates = _DEFAULT_COST_PER_MTOK
    for key, in_rate, out_rate in _MODEL_COST_PER_MTOK_USD:
        if key in name:
            rates = (in_rate, out_rate)
            break
    return (in_tok * rates[0] + out_tok * rates[1]) / 1_000_000.0


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
                "total_failed_calls": 0,    # P0 wrap-up
                "total_cost_usd": 0,        # P0 wrap-up
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
            "top_errors": [],     # P0 wrap-up
        })

    daily_in: dict[str, int] = defaultdict(int)
    daily_out: dict[str, int] = defaultdict(int)
    daily_calls: dict[str, int] = defaultdict(int)
    model_in: dict[str, int] = defaultdict(int)
    model_out: dict[str, int] = defaultdict(int)
    model_calls: dict[str, int] = defaultdict(int)
    total_in = total_out = total_calls = 0
    # P0 wrap-up: cost rollup + error-type aggregation.
    daily_cost: dict[str, float] = defaultdict(float)
    model_cost: dict[str, float] = defaultdict(float)
    total_cost = 0.0
    total_failed_calls = 0
    error_count: dict[str, int] = defaultdict(int)
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
            # Failed turns count toward call total + error rollup but
            # have no useful tokens.
            total_failed_calls += 1
            err = payload.get("error") or "unknown"
            # Trim long stack traces — first line is usually enough to
            # identify the error class for aggregation.
            err_short = str(err).splitlines()[0][:120] if err else "unknown"
            error_count[err_short] += 1
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
        # P0 wrap-up: per-call cost using the heuristic table.
        cost = _estimate_cost_usd(model, pt, ct)
        daily_cost[date]   += cost
        model_cost[model]  += cost
        total_cost         += cost
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
            "cost_usd":      round(daily_cost[d], 4),
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
                "cost_usd":      round(model_cost[m], 4),
            }
            for m in model_in.keys()
        ],
        key=lambda r: r["input_tokens"] + r["output_tokens"],
        reverse=True,
    )
    # P0 wrap-up: top-N error types (by frequency) for the Trace
    # / Analytics page. Helps spot recurring failure modes.
    top_errors = sorted(
        [{"error": err, "count": n} for err, n in error_count.items()],
        key=lambda r: r["count"],
        reverse=True,
    )[:10]
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
            "total_failed_calls":      total_failed_calls,
            "total_cost_usd":          round(total_cost, 4),
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
        # P0 wrap-up: cost rollups + error-type aggregation.
        "top_errors": top_errors,
    })


@router.get("/report.md")
async def get_analytics_markdown(days: int = _DEFAULT_DAYS):
    """Markdown export of the same analytics report.

    Useful when piping the numbers into a bug report, weekly digest,
    or pasting into a chat. Reuses ``get_analytics`` so the JSON +
    markdown views never drift.
    """
    from starlette.responses import PlainTextResponse
    resp = await get_analytics(days=days)
    # ``JSONResponse.body`` is the serialised bytes; decode + reparse.
    data = json.loads(bytes(resp.body).decode("utf-8"))

    s = data.get("summary", {})
    lines: list[str] = [
        f"# XMclaw Analytics ({data.get('period_days', days)} days)",
        f"_Generated: {strftime('%Y-%m-%d %H:%M UTC', gmtime(time()))}_",
        "",
        "## Overview",
        f"- **Calls (success)**: {s.get('total_calls', 0)}",
        f"- **Calls (failed)**: {s.get('total_failed_calls', 0)}",
        f"- **Input tokens**: {s.get('total_prompt_tokens', 0):,}",
        f"- **Output tokens**: {s.get('total_completion_tokens', 0):,}",
        f"- **Est. cost (USD, heuristic)**: ${s.get('total_cost_usd', 0):.4f}",
        f"- **Models used**: {s.get('models_used', 0)}",
        "",
        "## Models",
        "| Model | Calls | Input | Output | Cost (USD) |",
        "|-------|-------|-------|--------|------------|",
    ]
    for m in data.get("models", []):
        lines.append(
            f"| {m.get('model', '?')} | {m.get('calls', 0)} | "
            f"{m.get('input_tokens', 0):,} | {m.get('output_tokens', 0):,} | "
            f"${m.get('cost_usd', 0):.4f} |"
        )
    lines += ["", "## Tools", "| Tool | Calls | Errors | Error rate |",
              "|------|-------|--------|------------|"]
    for t in data.get("tools", []):
        lines.append(
            f"| {t.get('name', '?')} | {t.get('calls', 0)} | "
            f"{t.get('errors', 0)} | {t.get('error_rate', 0)*100:.1f}% |"
        )
    lines += ["", "## Platforms", "| Platform | Calls | Input | Output |",
              "|----------|-------|-------|--------|"]
    for p in data.get("platforms", []):
        lines.append(
            f"| {p.get('platform', '?')} | {p.get('calls', 0)} | "
            f"{p.get('input_tokens', 0):,} | {p.get('output_tokens', 0):,} |"
        )
    if data.get("top_errors"):
        lines += ["", "## Top errors", "| Error | Count |",
                  "|-------|-------|"]
        for e in data["top_errors"]:
            lines.append(f"| {e.get('error', '?')} | {e.get('count', 0)} |")
    if data.get("top_sessions"):
        lines += ["", "## Top sessions (by total tokens)",
                  "| Session | Platform | Calls | Total tokens |",
                  "|---------|----------|-------|--------------|"]
        for sess in data["top_sessions"]:
            sid = sess.get("session_id", "?")
            sid_short = sid[:36] + "…" if len(sid) > 36 else sid
            lines.append(
                f"| {sid_short} | {sess.get('platform', '?')} | "
                f"{sess.get('calls', 0)} | {sess.get('total_tokens', 0):,} |"
            )
    return PlainTextResponse("\n".join(lines), media_type="text/markdown")
