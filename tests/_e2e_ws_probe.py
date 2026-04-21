"""Ad-hoc end-to-end WebSocket probe for the live XMclaw daemon.

Open a FRESH WebSocket per turn so frames can't bleed across scenarios,
capture a byte-offset slice of logs/daemon.log bracketing each turn so
backend errors are attributable, and print a compact per-turn report.

Throwaway harness for the v2 conversation-loop audit, not a pytest.
"""
from __future__ import annotations

import asyncio
import io
import json
import sys
import time
from pathlib import Path
from typing import Any

import websockets

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

URL = "ws://127.0.0.1:8766/agent/default"
TURN_TIMEOUT = 180.0  # seconds — evolution can take >60s on simple turns
DAEMON_LOG = Path(r"C:\Users\15978\Desktop\XMclaw\logs\daemon.log")


def tail_log_slice(start_offset: int) -> str:
    """Read daemon.log from start_offset to EOF."""
    try:
        with open(DAEMON_LOG, "rb") as f:
            f.seek(start_offset)
            return f.read().decode("utf-8", errors="replace")
    except FileNotFoundError:
        return ""


def log_size() -> int:
    try:
        return DAEMON_LOG.stat().st_size
    except FileNotFoundError:
        return 0


async def run_turn(user_input: str, *, label: str) -> dict:
    """Open a fresh WS, send one user message, drain until done/error/timeout."""
    pre_offset = log_size()
    t0 = time.monotonic()
    frames: list[dict] = []
    saw_done = False
    saw_error: str | None = None
    first_chunk_at: float | None = None
    reflect_done_at: float | None = None
    hang_reason: str | None = None

    async with websockets.connect(URL, max_size=2**24, open_timeout=10) as ws:
        await ws.send(json.dumps({"type": "user", "content": user_input}))

        while True:
            now = time.monotonic()
            if now - t0 > TURN_TIMEOUT:
                hang_reason = f"hit hard timeout {TURN_TIMEOUT}s"
                break

            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            except asyncio.TimeoutError:
                continue
            except websockets.exceptions.ConnectionClosed:
                hang_reason = "server closed WS"
                break

            try:
                frame = json.loads(raw)
            except json.JSONDecodeError:
                frames.append({"type": "__malformed__", "raw": raw[:200]})
                continue

            frames.append(frame)
            ftype = frame.get("type")

            if ftype == "chunk" and first_chunk_at is None:
                first_chunk_at = now - t0

            if ftype == "stage":
                name = frame.get("name") or frame.get("payload", {}).get("name")
                if name == "reflect_done":
                    reflect_done_at = now

            if ftype == "done":
                saw_done = True
                break
            if ftype == "error":
                saw_error = frame.get("content", "")
                break
            if ftype == "ask_user":
                await ws.send(json.dumps({"type": "ask_user_answer", "answer": "skip"}))

    log_slice = tail_log_slice(pre_offset)
    return {
        "label": label,
        "input": user_input,
        "elapsed_s": round(time.monotonic() - t0, 2),
        "first_chunk_s": round(first_chunk_at, 2) if first_chunk_at else None,
        "reflect_done_s": round(reflect_done_at - t0, 2) if reflect_done_at else None,
        "saw_done": saw_done,
        "saw_error": saw_error,
        "hang_reason": hang_reason,
        "frames": frames,
        "log_slice": log_slice,
    }


def summarize(r: dict) -> None:
    print(f"\n=== [{r['label']}] ===")
    print(f"  input      : {r['input']!r}")
    print(f"  elapsed    : {r['elapsed_s']}s  first_chunk={r['first_chunk_s']}  reflect_done={r['reflect_done_s']}")
    print(f"  result     : done={r['saw_done']} error={r['saw_error']!r} hang={r['hang_reason']!r}")

    counts: dict[str, int] = {}
    for f in r["frames"]:
        counts[f.get("type", "?")] = counts.get(f.get("type", "?"), 0) + 1
    print(f"  frame types: {counts}")

    stages = [f for f in r["frames"] if f.get("type") == "stage"]
    if stages:
        print("  stages:")
        for s in stages:
            name = s.get("name") or s.get("payload", {}).get("name")
            print(f"    - {name}")

    text = "".join(f.get("content", "") or f.get("text", "")
                   for f in r["frames"] if f.get("type") == "chunk")
    if text:
        print(f"  chunks text: {(text if len(text) < 200 else text[:200] + '...')!r}")
    else:
        print("  chunks text: <none>")

    # agent_message frames carry the whole assistant turn text too
    agent_msgs = [f for f in r["frames"] if f.get("type") == "agent_message"]
    for m in agent_msgs[:2]:
        p = m.get("payload") or {}
        msg = p.get("message") or p.get("text") or m.get("content")
        if msg:
            print(f"  agent_msg  : {(msg if len(msg) < 200 else msg[:200] + '...')!r}")

    starts = [f for f in r["frames"] if f.get("type") in ("tool_start", "tool_call")]
    results = [f for f in r["frames"] if f.get("type") == "tool_result"]
    if starts or results:
        print(f"  tools      : starts={len(starts)} results={len(results)}")
        for f in starts:
            p = f.get("payload") or {}
            name = f.get("name") or p.get("name") or f.get("tool")
            args = f.get("arguments") or p.get("arguments") or f.get("args")
            print(f"    start : {name} args={str(args)[:120]}")
        for f in results:
            p = f.get("payload") or {}
            name = f.get("name") or p.get("name") or f.get("tool")
            content = f.get("content") or p.get("content") or f.get("result")
            print(f"    result: {name} content_len={len(str(content or ''))}")

    refls = [f for f in r["frames"] if f.get("type") in ("reflection", "reflection_complete")]
    for rf in refls:
        p = rf.get("payload") or rf
        print(f"  reflection : status={p.get('status')} summary={str(p.get('summary'))[:140]!r}")

    # Highlight any traceback in the daemon-log slice captured around this turn
    log = r.get("log_slice", "")
    for keyword in ("AttributeError", "PLAN_MODE_PROMPT", "Traceback", "agent_run_error", "agent_loop_error"):
        if keyword in log:
            # Pull a small snippet around the first hit
            idx = log.find(keyword)
            start = max(0, idx - 100)
            end = min(len(log), idx + 400)
            snippet = log[start:end].replace("\n", " | ")
            print(f"  ⚠ LOG[{keyword}]: ...{snippet}...")
            break


SCENARIOS = [
    ("chat:hello",         "你好"),
    ("chat:identity",      "你是谁"),
    ("task:read_readme",   "读一下 README.md 的前 10 行"),
    ("plan_mode:list_py",  "[PLAN MODE]帮我列出 xmclaw/core 目录下的所有 .py 文件"),
    ("task:multi_tool",    "写个 python hello world 到当前目录的 hi.py 并运行"),
]


async def main() -> int:
    results: list[dict] = []
    for label, msg in SCENARIOS:
        print(f"\n>>> Running {label}: {msg!r}")
        r = await run_turn(msg, label=label)
        summarize(r)
        results.append(r)
        # Brief pause so eventbus can quiesce between clean WS sessions
        await asyncio.sleep(1.0)

    out = Path(r"C:\Users\15978\Desktop\XMclaw\tests\_e2e_ws_probe_frames.json")
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\nfull frames written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
