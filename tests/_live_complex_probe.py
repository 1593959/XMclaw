"""Live probe: 10-turn complex conversation stressing memory.

Mixes identity facts, numeric tracking, hypotheticals, self-reference,
and contradiction-detection. The goal is not a pass/fail gate -- the
output is printed for a human to read and verify that the agent
maintains a coherent persona + fact set across all 10 turns.

Run:
    python tests/_live_complex_probe.py

Requires the daemon running on port 8765 (or PROBE_PORT).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import websockets

# Windows GBK console chokes on emoji / some unicode. Force UTF-8 for
# stdout so probe output doesn't crash mid-turn.
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        pass

PORT = int(os.environ.get("PROBE_PORT", "8765"))
SESSION = "live-complex-" + str(int(time.time()))


async def _read_assistant_reply(ws) -> tuple[str, int]:
    hops = 0
    while True:
        raw = await ws.recv()
        evt = json.loads(raw)
        t = evt["type"]
        p = evt["payload"]
        if t == "llm_response":
            hops += 1
            if p.get("ok") and p.get("tool_calls_count", 0) == 0:
                return p.get("content", ""), hops
        elif t == "anti_req_violation":
            return f"[VIOLATION] {p.get('message')}", hops


async def main() -> None:
    token_path = Path.home() / ".xmclaw" / "v2" / "pairing_token.txt"
    if not token_path.exists():
        print("ERROR: no pairing token; start daemon first")
        sys.exit(1)
    token = token_path.read_text(encoding="utf-8").strip()
    url = f"ws://127.0.0.1:{PORT}/agent/v2/{SESSION}?token={token}"

    # 10-turn sequence designed to break stateless agents:
    #   1  establish identity A
    #   2  establish second fact (numeric)
    #   3  transform the numeric fact (half)
    #   4  establish contradiction-prone fact (favorite color)
    #   5  retrieve from turn 2
    #   6  correct turn 4 (change color)
    #   7  retrieve the CORRECTED color (not the original)
    #   8  cross-reference turn 1 + turn 3
    #   9  hypothetical referring back to turn 1
    #  10  final summary of everything so far
    turns = [
        "Hi, I'm Jordan, a climate-data ML engineer at Glacier Labs.",
        "My team manages 48 petabytes of sensor data.",
        "If we halved our dataset for a test run, how many PB would we use?",
        "My favorite color is amber.",
        "Remind me how much data my team manages.",
        "Actually, scratch that -- my favorite color is slate, not amber.",
        "What color did I say I like?",
        "How does the test-run size from earlier relate to me, Jordan?",
        "If I switched to a non-ML role at Glacier Labs, what skills would transfer? Keep it to 3 bullets.",
        "Give me a 5-bullet summary of every fact I've told you about myself.",
    ]

    print(f"=== complex probe: port={PORT} session={SESSION} ===\n")
    failures: list[str] = []
    async with websockets.connect(url, max_size=None) as ws:
        await ws.recv()

        for i, user_text in enumerate(turns, 1):
            print(f"[{i}] USER> {user_text}")
            await ws.send(json.dumps({"type": "user", "content": user_text}))
            t0 = time.time()
            reply, hops = await _read_assistant_reply(ws)
            wall = (time.time() - t0) * 1000.0
            print(f"[{i}] ASST> ({wall:.0f}ms, {hops} hops) {reply}")
            print()

            low = reply.lower()
            # Light sanity checks -- human should still eyeball output.
            if i == 3 and "24" not in reply:
                failures.append("t3: halving 48 should yield 24 (reply lacks '24')")
            if i == 5 and "48" not in reply:
                failures.append("t5: lost the 48 PB fact")
            if i == 7:
                if "slate" not in low:
                    failures.append("t7: should say slate (post-correction)")
                # Amber might be mentioned in context of correction -- that's OK.
            if i == 10:
                for required in ["jordan", "glacier", "48", "slate"]:
                    if required not in low:
                        failures.append(f"t10: summary missing '{required}'")

    print("=== probe complete ===")
    if failures:
        print("\nSANITY CHECK FAILURES:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    else:
        print("All sanity checks passed.")


if __name__ == "__main__":
    asyncio.run(main())
