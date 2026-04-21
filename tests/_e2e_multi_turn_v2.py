"""Multi-turn continuous conversation probe v2.

Uses a single WS across all turns, waits for a clean 'done' per turn,
retries on upstream 529 (MiniMax overload), and checks whether earlier
context (a number, a function) actually reaches later turns. Also flags
'extraneous frames' that leak across turn boundaries.
"""
from __future__ import annotations

import asyncio
import io
import json
import sys
import time

import websockets

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

URL = "ws://127.0.0.1:8766/agent/default"
PER_TURN_TIMEOUT = 120.0
RETRY_ON_OVERLOAD = 2

TURNS = [
    ("T1_set",
     "记住我下面会给你一个数字，你之后要回忆它。数字是 4723。只回复 '收到'。"),
    ("T2_recall",
     "我刚才告诉你的数字是多少？只回数字本身，不要带别的文字。"),
    ("T3_write_plan",
     "把接下来的工作计划写到 agents/default/workspace/plan.md（用 file_write）："
     "一行就够：`Next: verify is_prime(97)`。"),
    ("T4_use_plan",
     "不要读任何文件直接告诉我：我上一步让你写到 plan.md 里的内容是什么？"
     "你应该记得，因为你刚写过。"),
    ("T5_summary",
     "一句话：这一轮对话我们做了哪三件事？"),
]


async def one_turn(ws, msg: str, label: str) -> dict:
    t0 = time.monotonic()
    text = ""
    tools: list[str] = []
    done = False
    err: str | None = None
    first_chunk_t: float | None = None
    pre_chunk_frames: list[str] = []
    await ws.send(json.dumps({"type": "user", "content": msg}))
    while time.monotonic() - t0 < PER_TURN_TIMEOUT:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
        except asyncio.TimeoutError:
            continue
        except websockets.exceptions.ConnectionClosed:
            break
        try:
            f = json.loads(raw)
        except Exception:
            continue
        t = f.get("type")
        if t == "chunk":
            if first_chunk_t is None:
                first_chunk_t = time.monotonic() - t0
            text += f.get("content", "") or ""
        elif t == "tool_start":
            tools.append(f.get("tool") or f.get("name"))
        elif t == "done":
            done = True
            break
        elif t == "error":
            err = f.get("content") or ""
            break
        elif not text and t not in ("user_message_event", "stage", "agent_thinking",
                                    "state", "tool_start", "tool_result"):
            pre_chunk_frames.append(t)
    return {
        "label": label,
        "elapsed": round(time.monotonic() - t0, 2),
        "first_chunk": first_chunk_t,
        "done": done,
        "err": err,
        "tools": tools,
        "text": text,
        "pre_chunk_frames": pre_chunk_frames,
    }


async def drain(ws, ms: float = 500) -> int:
    """Drain any leftover frames for `ms` milliseconds. Returns count."""
    deadline = time.monotonic() + ms / 1000
    n = 0
    while time.monotonic() < deadline:
        try:
            await asyncio.wait_for(ws.recv(), timeout=0.2)
            n += 1
        except asyncio.TimeoutError:
            return n
        except websockets.exceptions.ConnectionClosed:
            return n
    return n


async def main() -> int:
    session_t0 = time.monotonic()
    results: list[dict] = []
    async with websockets.connect(URL, max_size=2**24, open_timeout=10) as ws:
        # settle any stage-0 frames
        await drain(ws, ms=300)
        for label, msg in TURNS:
            print(f"\n>>> [{label}] {msg[:80]}{'…' if len(msg) > 80 else ''}")
            r = None
            for attempt in range(RETRY_ON_OVERLOAD + 1):
                r = await one_turn(ws, msg, label)
                if not r["err"] or "529" not in (r["err"] or ""):
                    break
                print(f"    [attempt {attempt + 1}] 529 overloaded — waiting 8s…")
                await asyncio.sleep(8)
                await drain(ws, ms=400)
            print(f"    elapsed={r['elapsed']}s done={r['done']} first_chunk={r['first_chunk']} err={str(r['err'])[:80]!r}")
            print(f"    tools={r['tools']}  pre_chunk_frames={r['pre_chunk_frames']}")
            print(f"    text[:260]={r['text'][:260]!r}")
            results.append(r)
            # Key hygiene: drain before the next turn so we don't
            # contaminate the next turn's frame window.
            leaked = await drain(ws, ms=400)
            if leaked:
                print(f"    [post-turn drain] discarded {leaked} lingering frames")

    # Post-analysis: did recall work?
    print("\n=== CONTINUITY ANALYSIS ===")
    t1 = next((r for r in results if r["label"] == "T1_set"), None)
    t2 = next((r for r in results if r["label"] == "T2_recall"), None)
    t3 = next((r for r in results if r["label"] == "T3_write_plan"), None)
    t4 = next((r for r in results if r["label"] == "T4_use_plan"), None)

    if t2:
        hit = "4723" in (t2["text"] or "")
        print(f"T2 recalls 4723? {hit}  — text={t2['text'][:80]!r}")
    if t3:
        print(f"T3 called file_write? {'file_write' in t3['tools']}")
    if t4:
        hit = "is_prime" in (t4["text"] or "") or "Next:" in (t4["text"] or "")
        print(f"T4 remembers plan content without re-reading? {hit}  — text={t4['text'][:120]!r}")

    total = time.monotonic() - session_t0
    print(f"\n=== session_total={total:.1f}s ===")
    from pathlib import Path
    out = Path(__file__).parent / "_e2e_multi_turn_v2_frames.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str),
                   encoding="utf-8")
    print(f"full frames → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
