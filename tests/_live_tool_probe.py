"""Live probe: ask the agent to do a task that REQUIRES a tool.

Prints what the agent does -- hops, tool_calls, content. If tools are
wired: we should see a tool_call_emitted frame. If not: the agent will
either hallucinate or (with our new system prompt) politely say it
can't reach the filesystem.
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
SESSION = "live-tool-" + str(int(time.time()))


async def _read_until_terminal(ws) -> list[dict]:
    """Collect events until we get a terminal LLM_RESPONSE or violation."""
    events: list[dict] = []
    for _ in range(40):
        raw = await ws.recv()
        evt = json.loads(raw)
        events.append(evt)
        t, p = evt["type"], evt["payload"]
        if t == "llm_response" and p.get("ok") and p.get("tool_calls_count", 0) == 0:
            break
        if t == "anti_req_violation":
            break
    return events


async def main() -> None:
    token = (Path.home() / ".xmclaw" / "v2" / "pairing_token.txt").read_text().strip()
    url = f"ws://127.0.0.1:{PORT}/agent/v2/{SESSION}?token={token}"

    test_dir = Path.home() / ".xmclaw" / "v2" / "probe_sandbox"
    test_dir.mkdir(parents=True, exist_ok=True)
    test_file = test_dir / "secret.txt"
    test_file.write_text("The magic word is banana-telescope-42.", encoding="utf-8")

    async with websockets.connect(url, max_size=None) as ws:
        await ws.recv()

        prompt = (
            f"Read the file at {test_file} and tell me the magic word."
        )
        print(f"USER> {prompt}\n")
        await ws.send(json.dumps({"type": "user", "content": prompt}))
        events = await _read_until_terminal(ws)

        print(f"-- {len(events)} events --")
        for evt in events:
            t, p = evt["type"], evt["payload"]
            if t == "llm_response":
                print(
                    f"  llm_response hop={p.get('hop')} ok={p.get('ok')} "
                    f"tool_calls={p.get('tool_calls_count')} "
                    f"content_len={p.get('content_length', 0)}"
                )
                if p.get("content"):
                    print(f"    content: {p['content'][:200]}")
            elif t == "tool_call_emitted":
                print(f"  TOOL_CALL_EMITTED name={p.get('name')} args={p.get('args')}")
            elif t == "tool_invocation_finished":
                print(
                    f"  TOOL_INVOCATION_FINISHED name={p.get('name')} "
                    f"ok={p.get('ok')} error={p.get('error')}"
                )
            elif t == "anti_req_violation":
                print(f"  VIOLATION: {p.get('message')}")
            else:
                print(f"  {t}")


if __name__ == "__main__":
    asyncio.run(main())
