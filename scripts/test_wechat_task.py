"""One-shot driver for the WeChat 魔丸群 e2e test.

Sends one user message to the v2 daemon over WebSocket, streams the
event log to stdout with timestamps, and exits when the agent's
``llm_response`` arrives without further tool calls (turn finished) or
after a hard deadline. Used by the dev to verify that the image_read
prompt-bloat fix actually unblocks the multi-hop computer-use chain.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from pathlib import Path


PAIRING_TOKEN_PATH = Path.home() / ".xmclaw" / "v2" / "pairing_token.txt"


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--deadline-s", type=float, default=600.0)
    ap.add_argument("--quiet-after-s", type=float, default=120.0,
                    help="Exit if no event for this long")
    args = ap.parse_args()

    token = PAIRING_TOKEN_PATH.read_text(encoding="utf-8").strip()
    session_id = f"wechat-test-{uuid.uuid4().hex[:8]}"
    url = (
        f"ws://127.0.0.1:8765/agent/v2/{session_id}"
        f"?token={token}"
    )
    print(f"[driver] session={session_id}")
    print(f"[driver] prompt={args.prompt!r}")

    import websockets

    try:
        ws = await websockets.connect(
            url,
            open_timeout=60.0,
            additional_headers={"Origin": "http://127.0.0.1:8765"},
        )
    except Exception as exc:
        print(f"[driver] ws connect failed: {exc}")
        return 1

    t0 = time.time()
    last_event_t = time.time()
    final_text = ""
    tool_calls: list[str] = []
    hop = 0

    async def reader():
        nonlocal last_event_t, final_text, hop
        async for raw in ws:
            last_event_t = time.time()
            try:
                ev = json.loads(raw)
            except Exception:
                continue
            etype = ev.get("type")
            payload = ev.get("payload") or {}
            elapsed = time.time() - t0
            stamp = f"[{elapsed:6.1f}s]"

            if etype == "llm_request":
                hop = payload.get("hop", hop)
                mc = payload.get("messages_count")
                tc = payload.get("tools_count")
                print(f"{stamp} LLM_REQUEST hop={hop} msgs={mc} tools={tc}")
            elif etype == "llm_response":
                print(f"{stamp} LLM_RESPONSE hop={hop} "
                      f"prompt_tok={payload.get('prompt_tokens')} "
                      f"comp_tok={payload.get('completion_tokens')} "
                      f"stop_reason={payload.get('stop_reason')}")
            elif etype == "tool_call_emitted":
                name = payload.get("name", "?")
                tool_calls.append(name)
                args_str = json.dumps(payload.get("args") or {}, ensure_ascii=False)[:120]
                print(f"{stamp} TOOL_CALL_EMITTED {name} args={args_str}")
            elif etype == "tool_invocation_finished":
                name = payload.get("name", "?")
                ok = payload.get("ok")
                latency = payload.get("latency_ms")
                content = str(payload.get("result") or payload.get("error") or "")
                preview = content[:160].replace("\n", " ")
                print(f"{stamp} TOOL_FINISHED {name} ok={ok} "
                      f"latency={latency}ms preview={preview!r}")
            elif etype == "inner_monologue":
                kind = payload.get("kind")
                if kind in ("mode_routed", "plan_first_decomposed",
                            "goal_anchor_injected", "step_verdict"):
                    print(f"{stamp} {kind.upper()}: "
                          f"{json.dumps(payload, ensure_ascii=False)[:200]}")
            elif etype == "anti_req_violation":
                print(f"{stamp} ANTI_REQ_VIOLATION: "
                      f"{json.dumps(payload, ensure_ascii=False)[:200]}")
            elif etype == "llm_chunk":
                # Surface streamed final text but rate-limit.
                pass
            elif etype == "agent_text_emitted" or etype == "text_stream":
                txt = payload.get("text") or payload.get("delta") or ""
                if txt:
                    final_text += txt
            elif etype == "turn_completed":
                print(f"{stamp} TURN_COMPLETED")
                return
            elif etype == "session_lifecycle":
                pass
            elif etype in (
                "tool_invocation_started", "user_message",
            ):
                pass
            else:
                if etype:
                    print(f"{stamp} {etype}: "
                          f"{json.dumps(payload, ensure_ascii=False)[:120]}")

    async def watchdog():
        while True:
            await asyncio.sleep(2.0)
            elapsed = time.time() - t0
            quiet = time.time() - last_event_t
            if elapsed > args.deadline_s:
                print(f"[driver] HARD DEADLINE {args.deadline_s}s reached")
                return
            if quiet > args.quiet_after_s:
                print(f"[driver] QUIET PERIOD {quiet:.0f}s exceeded")
                return

    # Send the user message.
    await ws.send(json.dumps({"type": "user", "content": args.prompt}))
    print(f"[driver] message sent at t=0, watching events...")

    reader_task = asyncio.create_task(reader())
    watchdog_task = asyncio.create_task(watchdog())
    done, pending = await asyncio.wait(
        {reader_task, watchdog_task},
        return_when=asyncio.FIRST_COMPLETED,
    )
    for p in pending:
        p.cancel()
    try:
        await ws.close()
    except Exception:
        pass

    elapsed = time.time() - t0
    print()
    print(f"[driver] DONE elapsed={elapsed:.1f}s hops={hop} tool_calls={len(tool_calls)}")
    print(f"[driver] tool_call_sequence: {tool_calls}")
    print(f"[driver] final_text:")
    print((final_text or "(empty)")[:2000])
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
