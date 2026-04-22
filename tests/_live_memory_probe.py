"""Live smoke: open a WS to the running daemon and drive a 5-turn
conversation over real MiniMax. Prints each assistant reply so we can
eyeball whether the agent tracks context across turns.

Not a pytest -- this is a manual ad-hoc harness. Run:
    python tests/_live_memory_probe.py

Pre-req: `xmclaw start --port 8765 --no-auth` (or start without no-auth
and supply a token via env XMC_V2_PAIRING_TOKEN).
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
SESSION = "live-memory-" + str(int(time.time()))


async def _read_assistant_reply(ws) -> tuple[str, int, float]:
    """Consume events until a terminal LLM_RESPONSE with content arrives.
    Returns (text, hops, latency_ms_total)."""
    hops_seen = 0
    total_latency = 0.0
    while True:
        raw = await ws.recv()
        evt = json.loads(raw)
        t = evt["type"]
        p = evt["payload"]
        if t == "llm_response":
            hops_seen += 1
            total_latency += p.get("latency_ms", 0.0)
            # Terminal = ok + (content present OR no tool calls)
            if p.get("ok") and p.get("tool_calls_count", 0) == 0:
                return p.get("content", ""), hops_seen, total_latency
        elif t == "anti_req_violation":
            return f"[VIOLATION] {p.get('message')}", hops_seen, total_latency


async def main() -> None:
    token_path = Path.home() / ".xmclaw" / "v2" / "pairing_token.txt"
    if not token_path.exists():
        print(f"ERROR: no pairing token at {token_path}")
        print("Run `xmclaw start --port 8765` first.")
        sys.exit(1)
    token = token_path.read_text(encoding="utf-8").strip()
    url = f"ws://127.0.0.1:{PORT}/agent/v2/{SESSION}?token={token}"

    # Five-turn conversation: identity, recall, math, recall-from-math,
    # complex synthesis. If any turn fails to reference earlier context
    # the test is considered failed (but we just print for the human).
    turns = [
        "Hi. My name is Riley and I work on a distributed database at ClawCorp.",
        "What did I just tell you my name was?",
        "Compute 13 * 17 for me.",
        "What was the answer to my math question, and tie it back to what you know about me so far?",
        "Summarize everything you know about me in one bullet list.",
    ]

    print(f"=== live memory probe: port={PORT} session={SESSION} ===\n")
    async with websockets.connect(url, max_size=None) as ws:
        # Drain the session_lifecycle frame.
        await ws.recv()

        for i, user_text in enumerate(turns, 1):
            print(f"[turn {i} USER] {user_text}")
            await ws.send(json.dumps({"type": "user", "content": user_text}))
            t0 = time.time()
            reply, hops, latency = await _read_assistant_reply(ws)
            wall = (time.time() - t0) * 1000.0
            print(f"[turn {i} ASSISTANT hops={hops} wall={wall:.0f}ms] {reply}")
            print()

    print("=== probe complete -- review the replies above for context fidelity ===")


if __name__ == "__main__":
    asyncio.run(main())
