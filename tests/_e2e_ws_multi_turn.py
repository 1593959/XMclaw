"""Multi-turn continuous conversation probe.

Unlike _e2e_ws_probe.py which opens a fresh WS per scenario, this one
keeps ONE WebSocket open across all turns so we can observe:
  * Does the agent remember prior turns?
  * Does reflection from turn N bleed into turn N+1's gather/context?
  * Does the event stream stay sane over a long conversation?
  * Does send→done→next-send round-trip work reliably for >3 turns?
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
TURN_TIMEOUT = 240.0   # evolution runs synchronously, so each turn can be slow
DAEMON_LOG = Path(r"C:\Users\15978\Desktop\XMclaw\logs\daemon.log")


def log_size() -> int:
    try:
        return DAEMON_LOG.stat().st_size
    except FileNotFoundError:
        return 0


def tail_log_slice(start_offset: int) -> str:
    try:
        with open(DAEMON_LOG, "rb") as f:
            f.seek(start_offset)
            return f.read().decode("utf-8", errors="replace")
    except FileNotFoundError:
        return ""


async def run_turn_on_ws(ws: Any, user_input: str, *, label: str, turn_idx: int) -> dict:
    """Send one user message on the shared WS and collect frames until done/error."""
    pre_offset = log_size()
    t0 = time.monotonic()
    frames: list[dict] = []
    saw_done = False
    saw_error: str | None = None
    first_chunk_at: float | None = None
    done_at: float | None = None

    await ws.send(json.dumps({"type": "user", "content": user_input}))

    while True:
        now = time.monotonic()
        if now - t0 > TURN_TIMEOUT:
            break
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
        except asyncio.TimeoutError:
            continue
        except websockets.exceptions.ConnectionClosed:
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

        if ftype == "done":
            saw_done = True
            done_at = now - t0
            break
        if ftype == "error":
            saw_error = frame.get("content", "")
            break
        if ftype == "ask_user":
            await ws.send(json.dumps({"type": "ask_user_answer", "answer": "skip"}))

    # Full assistant text for this turn (prefer chunks; fall back to agent_message payload)
    text = "".join(f.get("content", "") or f.get("text", "")
                   for f in frames if f.get("type") == "chunk")

    # Memory hits from gather stage — how much history the agent saw
    memories_seen = 0
    for f in frames:
        if f.get("type") == "stage" and f.get("stage") == "gather_done":
            mems = (f.get("data") or {}).get("memories") or []
            memories_seen = len(mems)
            break

    tool_calls = [f for f in frames if f.get("type") == "tool_start"]

    return {
        "turn": turn_idx,
        "label": label,
        "input": user_input,
        "elapsed_s": round(time.monotonic() - t0, 2),
        "first_chunk_s": round(first_chunk_at, 2) if first_chunk_at else None,
        "done_at_s": round(done_at, 2) if done_at else None,
        "saw_done": saw_done,
        "saw_error": saw_error,
        "assistant_text": text,
        "memories_seen_in_gather": memories_seen,
        "tools_used": [
            {"tool": t.get("tool"), "args": t.get("args")}
            for t in tool_calls
        ],
        "log_slice": tail_log_slice(pre_offset),
        "frames": frames,
    }


# A continuous storyline where each turn depends on the previous one.
# If memory works, turn 3 should recall the filename from turn 2 without
# being told, and turn 5 should know we're still discussing the same file.
SCRIPT = [
    ("intro",        "记住一个数字：42"),                         # seed a fact
    ("write_note",   "把刚才那个数字写到当前目录的 number.txt 里"),  # must recall "42"
    ("read_back",    "读一下 number.txt 看看里面是什么"),            # must recall filename
    ("math_on_it",   "把里面的数字加 100 告诉我"),                    # must chain
    ("identity_shift","我是谁？我们刚才在干嘛？"),                    # context self-check
    ("cleanup",      "把 number.txt 删了"),                         # destructive — should ask or refuse
]


async def main() -> int:
    results: list[dict] = []
    print(f"connecting to {URL} (single WS for {len(SCRIPT)} turns) ...")
    async with websockets.connect(URL, max_size=2**24, open_timeout=10) as ws:
        print("connected.\n")
        for i, (label, msg) in enumerate(SCRIPT, start=1):
            print(f">>> Turn {i} [{label}]: {msg!r}")
            r = await run_turn_on_ws(ws, msg, label=label, turn_idx=i)

            # Per-turn digest
            txt = r["assistant_text"]
            txt_short = txt if len(txt) < 220 else txt[:220] + "..."
            print(f"    elapsed={r['elapsed_s']}s  first_chunk={r['first_chunk_s']}  done={r['saw_done']}")
            print(f"    memories_in_gather={r['memories_seen_in_gather']}  tools={[t['tool'] for t in r['tools_used']]}")
            print(f"    assistant: {txt_short!r}")
            if r["saw_error"]:
                print(f"    ⚠ error: {r['saw_error']!r}")
            if "Traceback" in r["log_slice"] or "AttributeError" in r["log_slice"]:
                print("    ⚠ backend traceback in log slice")
            print()
            results.append(r)
            await asyncio.sleep(0.3)

    out = Path(r"C:\Users\15978\Desktop\XMclaw\tests\_e2e_ws_multi_turn_frames.json")
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"full frames → {out}")

    # Cross-turn synthesis
    print("\n=== CROSS-TURN CHECKS ===")
    t1, t2, t3, t4, t5 = results[0], results[1], results[2], results[3], results[4]
    print(f"- Turn 2 (write 42) used write tool: {any(t['tool'] in ('file_write','write') for t in t2['tools_used'])}")
    print(f"- Turn 2 wrote '42': {any(str(t.get('args',{})).find('42')>=0 for t in t2['tools_used'])}")
    print(f"- Turn 3 used read tool: {any(t['tool'] in ('file_read','read') for t in t3['tools_used'])}")
    print(f"- Turn 3 knew filename 'number.txt': {any(str(t.get('args',{})).find('number.txt')>=0 for t in t3['tools_used'])}")
    print(f"- Turn 4 answered ~142: {'142' in t4['assistant_text']}")
    print(f"- Turn 5 referenced earlier number or file: "
          f"{'42' in t5['assistant_text'] or 'number' in t5['assistant_text'].lower()}")
    print(f"- gather memory depth grew over turns: {[r['memories_seen_in_gather'] for r in results]}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
