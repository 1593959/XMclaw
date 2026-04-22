"""Replay the exact scenario the user ran through the web UI where the
agent failed at every question. All four should now succeed:

  1. 'How many word files on the Desktop?'  -> list_dir or bash
  2. 'Read C:\\Users\\15978\\Desktop\\XMclaw_DESIGN.md'  -> file_read
     (previously failed permission-denied + hallucinated 'None')
  3. 'Weather in Beijing tomorrow?'  -> web_search / web_fetch
  4. 'How many stars does iopenclaw have on GitHub?'  -> web_fetch

Prints each assistant reply and flags any that mentions 'can't / no
access / 无法 / 没有访问' -- those phrases used to be the default.
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
SESSION = "live-user-scenario-" + str(int(time.time()))


async def _drain(ws) -> dict:
    tool_calls: list[str] = []
    texts: list[str] = []
    tool_errors: list[str] = []
    for _ in range(60):
        raw = await ws.recv()
        evt = json.loads(raw)
        t, p = evt["type"], evt["payload"]
        if t == "tool_call_emitted":
            tool_calls.append(p.get("name"))
        elif t == "tool_invocation_finished":
            if not p.get("ok"):
                tool_errors.append(f"{p.get('name')}: {p.get('error')}")
        elif t == "llm_response":
            if p.get("content"):
                texts.append(p["content"])
            if p.get("ok") and p.get("tool_calls_count", 0) == 0:
                break
        elif t == "anti_req_violation":
            texts.append(f"[VIOLATION] {p.get('message')}")
            break
    return {"tool_calls": tool_calls, "tool_errors": tool_errors, "final": texts[-1] if texts else ""}


REFUSAL_PATTERNS = [
    "没有访问", "没有工具", "无法访问", "无法执行", "不能执行",
    "i can't", "i cannot", "no access", "don't have the ability",
]


async def main() -> None:
    token = (Path.home() / ".xmclaw" / "v2" / "pairing_token.txt").read_text().strip()
    url = f"ws://127.0.0.1:{PORT}/agent/v2/{SESSION}?token={token}"

    # Prepare the exact file the user's chat transcript tried to read.
    desktop = Path.home() / "Desktop"
    target = desktop / "XMclaw_DESIGN.md"
    if not target.exists():
        # Create a synthetic so scenario 2 is actionable.
        target.write_text(
            "# XMclaw Design Notes (probe fixture)\n\n"
            "Three-tier architecture: daemon + agent loop + providers.\n",
            encoding="utf-8",
        )
        created = True
    else:
        created = False

    scenarios = [
        ("1) Desktop word-file count",
         "帮我看一下桌面有几个word文件"),
        ("2) Read the design doc",
         f"{target} 看一下这个里面写的啥"),
        ("3) Tomorrow Beijing weather",
         "明天北京的天气怎么样"),
        ("4) iopenclaw github stars",
         "帮我看一下iopenclaw在github上有多少星了"),
    ]

    print(f"=== live user scenario replay: port={PORT} session={SESSION} ===\n")
    refusals: list[str] = []
    try:
        async with websockets.connect(url, max_size=None) as ws:
            await ws.recv()

            for label, prompt in scenarios:
                print(f"\n--- {label} ---")
                print(f"USER> {prompt}")
                await ws.send(json.dumps({"type": "user", "content": prompt}))
                result = await _drain(ws)
                print(f"  tool_calls: {result['tool_calls']}")
                if result["tool_errors"]:
                    print(f"  tool_errors: {result['tool_errors']}")
                print(f"  ASST> {result['final'][:500]}")
                low = result["final"].lower()
                for bad in REFUSAL_PATTERNS:
                    if bad in low:
                        refusals.append(f"{label}: refusal phrase '{bad}'")
                        break
    finally:
        if created:
            target.unlink(missing_ok=True)

    print("\n=== summary ===")
    if refusals:
        print("REGRESSIONS (agent still refusing):")
        for r in refusals:
            print(f"  - {r}")
        sys.exit(1)
    else:
        print("All 4 scenarios: no refusal phrases detected.")


if __name__ == "__main__":
    asyncio.run(main())
