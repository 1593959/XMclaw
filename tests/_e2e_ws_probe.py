"""Ad-hoc end-to-end WebSocket probe for the live XMclaw daemon.

Drives /agent/{agent_id} exactly like the web UI does, captures every
frame with timing, and prints a compact per-turn report so we can see
what the pipeline actually emits for each class of user input.

Not a pytest — throw-away harness for the v2 conversation-loop audit.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from typing import Any

import websockets

URL = "ws://127.0.0.1:8766/agent/default"
TURN_TIMEOUT = 90.0  # seconds; generous so slow LLM turns still finish


async def run_turn(ws: Any, user_input: str, *, label: str) -> dict:
    """Send one user message and collect frames until `done`/`error`/timeout."""
    t0 = time.monotonic()
    frames: list[dict] = []
    saw_done = False
    saw_error: str | None = None
    first_chunk_at: float | None = None

    await ws.send(json.dumps({"type": "user", "content": user_input}))

    while True:
        try:
            remaining = TURN_TIMEOUT - (time.monotonic() - t0)
            if remaining <= 0:
                print(f"  ! TIMEOUT after {TURN_TIMEOUT}s")
                break
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        except asyncio.TimeoutError:
            print(f"  ! TIMEOUT after {TURN_TIMEOUT}s")
            break

        try:
            frame = json.loads(raw)
        except json.JSONDecodeError:
            frames.append({"type": "__malformed__", "raw": raw[:200]})
            continue

        frames.append(frame)
        ftype = frame.get("type")

        if ftype == "chunk" and first_chunk_at is None:
            first_chunk_at = time.monotonic() - t0

        if ftype == "done":
            saw_done = True
            break
        if ftype == "error":
            saw_error = frame.get("content", "")
            break
        if ftype == "ask_user":
            # Don't get stuck — send a canned answer so the harness keeps moving
            await ws.send(json.dumps({"type": "ask_user_answer", "answer": "skip"}))

    elapsed = time.monotonic() - t0
    return {
        "label": label,
        "input": user_input,
        "elapsed_s": round(elapsed, 2),
        "first_chunk_s": round(first_chunk_at, 2) if first_chunk_at else None,
        "saw_done": saw_done,
        "saw_error": saw_error,
        "frames": frames,
    }


def summarize(report: dict) -> None:
    """Print a per-turn digest."""
    print(f"\n=== [{report['label']}] ===")
    print(f"  input      : {report['input']!r}")
    print(f"  elapsed    : {report['elapsed_s']}s  (first chunk @ {report['first_chunk_s']}s)")
    print(f"  done/error : done={report['saw_done']} error={report['saw_error']!r}")

    # Roll up frame types
    counts: dict[str, int] = {}
    for f in report["frames"]:
        t = f.get("type", "?")
        counts[t] = counts.get(t, 0) + 1
    print(f"  frame types: {counts}")

    # Pull out the interesting stuff
    stages = [f for f in report["frames"] if f.get("type") == "stage"]
    if stages:
        print("  stages:")
        for s in stages:
            name = s.get("name") or s.get("payload", {}).get("name") or s.get("stage") or s
            status = s.get("status") or s.get("payload", {}).get("status")
            print(f"    - {name!s:<30} {status or ''}")

    # Assistant text — concat chunks, trim
    text = "".join(f.get("content", "") or f.get("text", "")
                   for f in report["frames"] if f.get("type") == "chunk")
    if text:
        clipped = text if len(text) < 300 else text[:300] + "…"
        print(f"  assistant  : {clipped!r}")
    else:
        print("  assistant  : <empty>")

    # Tool calls / results
    tstarts = [f for f in report["frames"] if f.get("type") in ("tool_start", "tool_call")]
    tresults = [f for f in report["frames"] if f.get("type") == "tool_result"]
    if tstarts or tresults:
        print(f"  tools      : starts={len(tstarts)} results={len(tresults)}")
        for f in tstarts:
            print(f"    start : {f.get('name') or f.get('tool')} args={str(f.get('arguments') or f.get('args') or f.get('payload',{}).get('arguments'))[:120]}")
        for f in tresults:
            print(f"    result: {f.get('name') or f.get('tool')} ok={f.get('ok')} content_len={len(str(f.get('content') or f.get('result') or ''))}")

    # Reflection
    refls = [f for f in report["frames"] if f.get("type") in ("reflection", "reflection_complete")]
    for r in refls:
        p = r.get("payload") or r
        print(f"  reflection : status={p.get('status') or r.get('status')}  summary={str(p.get('summary') or r.get('summary'))[:120]!r}")

    # Any errors in non-done frames?
    errs = [f for f in report["frames"] if f.get("type") == "error"]
    for e in errs:
        print(f"  ERROR      : {e.get('content')!r}")


SCENARIOS = [
    ("chat:hello",      "你好"),
    ("chat:identity",   "你是谁"),
    ("chat:capabilities","你都能干什么"),
    ("qa:date",         "今天几号"),
    ("qa:math",         "1+1等于几"),
    ("task:read_file",  "读一下 README.md 的前 10 行"),
    ("task:write_run",  "写个 python hello world 到当前目录的 hi.py 并运行"),
    ("plan_mode",       "[PLAN MODE]帮我列出 xmclaw/core 目录下的所有 .py 文件"),
    ("memory:followup", "刚才那个 hi.py 文件再给我看一遍"),
]


async def main() -> int:
    results: list[dict] = []
    print(f"connecting to {URL} ...")
    async with websockets.connect(URL, max_size=2**24, open_timeout=10) as ws:
        print("connected.")
        for label, msg in SCENARIOS:
            report = await run_turn(ws, msg, label=label)
            summarize(report)
            results.append(report)
            # small gap between turns so the server can quiesce
            await asyncio.sleep(0.5)

    # Dump full frames to a file for offline inspection
    out = r"C:\Users\15978\Desktop\XMclaw\tests\_e2e_ws_probe_frames.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2, default=str)
    print(f"\nfull frames written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
