"""Trace export + replay reconstruction (#6, 2026-06-15).

Top-tier harnesses let you export a full run trace and replay it offline
for debugging. XMclaw already persists every ``BehavioralEvent`` (the
SqliteEventBus durable log + the in-memory ``session_logs`` buffer) and
replays it to the UI on reconnect. This module adds the two missing
pieces:

* **Export** — a session's events as JSONL (one jsonable event per line),
  the portable on-disk trace format.
* **Replay** — :func:`reconstruct_timeline` walks an exported trace and
  produces a compact human-readable timeline (turns, tool calls + their
  results, steering, errors, cancellations) WITHOUT a live daemon.

Both operate on jsonable event dicts (``{"type", "payload", "ts", ...}``),
so they work identically on an exported file, a DB query result, or the
in-memory buffer. Field lookups are tolerant — payload shapes have drifted
across phases, so we try the known aliases and degrade gracefully rather
than KeyError on an old trace.
"""
from __future__ import annotations

import json
from typing import Any


def events_to_jsonl(events: list[dict[str, Any]]) -> str:
    """Serialise jsonable events to JSONL (one compact JSON object/line)."""
    return "\n".join(json.dumps(e, ensure_ascii=False) for e in events)


def jsonl_to_events(text: str) -> list[dict[str, Any]]:
    """Parse a JSONL trace back into event dicts. Blank/garbage lines are
    skipped so a partially-written export still loads."""
    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _short(val: Any, n: int = 160) -> str:
    if val is None:
        return ""
    s = val if isinstance(val, str) else json.dumps(val, ensure_ascii=False)
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def reconstruct_timeline(events: list[dict[str, Any]]) -> str:
    """Walk a trace and return a compact, human-readable timeline.

    Renders the load-bearing event types; everything else is ignored so
    the output stays signal-dense for debugging.
    """
    lines: list[str] = []
    for e in events:
        if not isinstance(e, dict):
            continue
        t = str(e.get("type") or "")
        p = e.get("payload") if isinstance(e.get("payload"), dict) else {}

        if t == "user_message":
            channel = p.get("channel")
            tag = "USER·steer" if channel == "steering" else "USER"
            lines.append(f"{tag}: {_short(p.get('content'), 200)}")
        elif t == "llm_response":
            txt = p.get("text") or p.get("content") or ""
            if txt:
                lines.append(f"  ASSISTANT: {_short(txt, 200)}")
            for tc in (p.get("tool_calls") or []):
                if isinstance(tc, dict):
                    lines.append(
                        f"  → call {tc.get('name', '?')}({_short(tc.get('args'), 80)})"
                    )
        elif t == "tool_invocation_started":
            lines.append(f"    ⋯ running {p.get('name', p.get('tool_name', '?'))}")
        elif t == "tool_invocation_finished":
            name = p.get("name") or p.get("tool_name") or "?"
            ok = p.get("ok")
            body = p.get("result") if p.get("result") is not None else p.get("error")
            lines.append(f"    tool[{name}] ok={ok} {_short(body)}")
        elif t == "anti_req_violation":
            lines.append(f"  ⚠ {p.get('kind', 'violation')}: {_short(p.get('message'))}")
        elif t == "session_lifecycle":
            phase = p.get("phase")
            if phase in ("turn_cancelled", "cancel_requested"):
                lines.append(f"  ⛔ {phase}")
            elif phase:
                lines.append(f"  ·· {phase}")
        elif t == "agent_asked_question":
            lines.append(f"  ❓ {_short(p.get('question'), 160)}")
        elif t == "user_answered_question":
            lines.append(f"  ✔ answered: {_short(p.get('value'), 80)}")
    return "\n".join(lines)


__all__ = [
    "events_to_jsonl",
    "jsonl_to_events",
    "reconstruct_timeline",
]
