"""Live probe: multi-step tool task.

Asks the agent to:
  1. Write file A
  2. Read file A and transform its content
  3. Write file B with the transformed content
  4. Read file B and confirm the transformation

This verifies multiple tool_call hops in one turn + history across turns.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import websockets

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        pass

PORT = int(os.environ.get("PROBE_PORT", "8765"))
SESSION = "live-tool-multi-" + str(int(time.time()))


async def _drain_turn(ws, label: str) -> dict:
    tool_calls: list[str] = []
    texts: list[str] = []
    hops = 0
    for _ in range(60):
        raw = await ws.recv()
        evt = json.loads(raw)
        t, p = evt["type"], evt["payload"]
        if t == "tool_call_emitted":
            tool_calls.append(f"{p.get('name')}({json.dumps(p.get('args', {}))[:80]})")
        elif t == "tool_invocation_finished":
            print(f"  {label}: {p.get('name')} -> ok={p.get('ok')}")
        elif t == "llm_response":
            hops += 1
            if p.get("content"):
                texts.append(p["content"])
            if p.get("ok") and p.get("tool_calls_count", 0) == 0:
                break
        elif t == "anti_req_violation":
            texts.append(f"[VIOLATION] {p.get('message')}")
            break
    return {"tool_calls": tool_calls, "texts": texts, "hops": hops}


async def main() -> None:
    token = (Path.home() / ".xmclaw" / "v2" / "pairing_token.txt").read_text().strip()
    url = f"ws://127.0.0.1:{PORT}/agent/v2/{SESSION}?token={token}"

    sandbox = Path.home() / ".xmclaw" / "v2" / "probe_sandbox"
    sandbox.mkdir(parents=True, exist_ok=True)
    src = sandbox / "multi_src.txt"
    dst = sandbox / "multi_dst.txt"
    for f in (src, dst):
        if f.exists():
            f.unlink()

    tasks = [
        (
            "turn 1 (write)",
            f"Write the text 'hello world' to the file {src}. "
            f"Then confirm you wrote it.",
        ),
        (
            "turn 2 (read+transform+write)",
            f"Read {src}, convert the content to uppercase, "
            f"and write the uppercase version to {dst}.",
        ),
        (
            "turn 3 (verify)",
            f"Read {dst} and tell me what it contains. "
            f"Also remind me what we've done so far.",
        ),
    ]

    async with websockets.connect(url, max_size=None) as ws:
        await ws.recv()

        for label, prompt in tasks:
            print(f"\n=== {label} ===")
            print(f"USER> {prompt}")
            await ws.send(json.dumps({"type": "user", "content": prompt}))
            result = await _drain_turn(ws, label)
            print(f"  hops={result['hops']}")
            print(f"  tool_calls: {result['tool_calls']}")
            for txt in result["texts"][-1:]:  # final assistant text
                print(f"  ASST> {txt[:300]}")

    print("\n=== sandbox state ===")
    for f in (src, dst):
        if f.exists():
            print(f"  {f.name}: {f.read_text(encoding='utf-8')!r}")
        else:
            print(f"  {f.name}: MISSING")


if __name__ == "__main__":
    asyncio.run(main())
