"""Real-model end-to-end dialog smoke test for XMclaw v2 daemon.

Run after `xmclaw start` while a real LLM API key is configured in
``daemon/config.json``. The script fetches a pairing token, opens the
agent WebSocket, sends a real user message, and prints **every**
BehavioralEvent received until the turn finishes — so the operator
can audit the entire transcript (LLM_RESPONSE content + any tool calls).

Designed for manual auditing, **not** the automated suite. We don't
import this from pytest because the suite must stay hermetic and
network-free.

Frame protocol (from xmclaw/daemon/app.py):
  -> {"type": "user", "content": "<text>", "ultrathink": false}
  <- BehavioralEvent JSON frames (USER_MESSAGE, LLM_REQUEST,
     LLM_RESPONSE, TOOL_CALL_EMITTED, TOOL_INVOCATION_STARTED,
     TOOL_INVOCATION_FINISHED, COST_TICK, SESSION_LIFECYCLE, ...)

A turn is "finished" when we see an LLM_RESPONSE whose payload has no
tool_calls (tool_calls_count == 0) and ok is true. The agent loop
might do multiple hops, so we keep listening until that final hop.
"""
from __future__ import annotations

import asyncio
import io
import json
import sys
import time
import uuid
from pathlib import Path

import httpx
import websockets

# Force UTF-8 stdout so Chinese characters survive on Windows consoles.
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
else:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace", line_buffering=True)


DAEMON = "http://127.0.0.1:8765"
WS_BASE = "ws://127.0.0.1:8765"
LOG_DIR = Path(__file__).parent / "_artifacts"
LOG_DIR.mkdir(parents=True, exist_ok=True)


def _log(label: str, msg: str) -> None:
    sys.stdout.write(msg)
    sys.stdout.flush()
    with (LOG_DIR / f"{label}.log").open("a", encoding="utf-8") as fh:
        fh.write(msg)


async def fetch_token() -> str:
    async with httpx.AsyncClient(timeout=5) as cli:
        resp = await cli.get(f"{DAEMON}/api/v2/pair")
        resp.raise_for_status()
        return resp.json()["token"]


async def run_turn(prompt: str, *, label: str, idle_timeout_s: float = 60) -> dict:
    token = await fetch_token()
    sid = uuid.uuid4().hex[:12]
    url = f"{WS_BASE}/agent/v2/{sid}?token={token}"

    # Wipe per-label log so reruns are clean.
    (LOG_DIR / f"{label}.log").unlink(missing_ok=True)
    (LOG_DIR / f"{label}.events.jsonl").unlink(missing_ok=True)

    _log(label, f"\n{'='*72}\n[{label}]  sid={sid}\n"
                f"User → {prompt!r}\n{'-'*72}\n")

    transcript: list[str] = []
    tool_events: list[dict] = []
    last_resp_payload: dict | None = None
    started = time.perf_counter()

    async with websockets.connect(url, max_size=4_000_000) as ws:
        await ws.send(json.dumps({"type": "user", "content": prompt}))

        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=idle_timeout_s)
            except asyncio.TimeoutError:
                _log(label, f"\n!! idle for >{idle_timeout_s}s, breaking\n")
                break

            try:
                evt = json.loads(raw)
            except json.JSONDecodeError:
                _log(label, f"\n!! non-JSON frame: {raw!r}\n")
                continue

            with (LOG_DIR / f"{label}.events.jsonl").open("a",
                                                          encoding="utf-8") as fh:
                fh.write(raw + "\n")

            etype = evt.get("type", "?").upper()
            payload = evt.get("payload", {})

            if etype == "LLM_RESPONSE":
                ok = payload.get("ok")
                tc = payload.get("tool_calls_count", 0)
                content = payload.get("content", "")
                last_resp_payload = payload
                _log(label, f"\n<< LLM_RESPONSE ok={ok} hop={payload.get('hop')} "
                            f"tool_calls={tc} latency={payload.get('latency_ms', 0):.0f}ms "
                            f"tokens=p{payload.get('prompt_tokens')}/c{payload.get('completion_tokens')}\n")
                if content:
                    transcript.append(content)
                    _log(label, f"   ┌── content ({len(content)} chars) ──┐\n")
                    for line in content.splitlines() or [content]:
                        _log(label, f"   │ {line}\n")
                    _log(label, f"   └─────────────────────────┘\n")
                if ok and tc == 0:
                    break  # turn complete
            elif etype == "LLM_REQUEST":
                _log(label, f">> LLM_REQUEST hop={payload.get('hop')} "
                            f"messages={payload.get('messages_count')} "
                            f"tools={payload.get('tools_count')}\n")
            elif etype == "TOOL_CALL_EMITTED":
                tool_events.append(evt)
                _log(label, f">> TOOL_CALL_EMITTED name={payload.get('name')!r} "
                            f"id={payload.get('call_id')} "
                            f"args={json.dumps(payload.get('args', {}), ensure_ascii=False)[:200]}\n")
            elif etype == "TOOL_INVOCATION_FINISHED":
                tool_events.append(evt)
                ok = payload.get("ok")
                # The agent loop names the result payload field
                # ``result`` (see xmclaw/daemon/agent_loop.py) — older
                # drafts called it ``content`` so we fall back.
                result = payload.get("result", payload.get("content"))
                result_str = (
                    result if isinstance(result, str)
                    else json.dumps(result, ensure_ascii=False)
                )
                preview = result_str[:300] if result_str else ""
                _log(label, f"<< TOOL_INVOCATION_FINISHED name={payload.get('name')!r} "
                            f"ok={ok} latency={payload.get('latency_ms', 0):.0f}ms\n"
                            f"   result preview: {preview}\n")
            elif etype == "ANTI_REQ_VIOLATION":
                _log(label, f"\n!! VIOLATION: {json.dumps(payload, ensure_ascii=False)}\n")
            elif etype == "COST_TICK":
                _log(label, f".. COST_TICK spent=${payload.get('spent_usd', 0):.6f} "
                            f"budget=${payload.get('budget_usd', 0):.4f}\n")
            elif etype == "SESSION_LIFECYCLE":
                _log(label, f".. SESSION_LIFECYCLE phase={payload.get('phase')}\n")
            elif etype in {"USER_MESSAGE", "TOOL_INVOCATION_STARTED"}:
                _log(label, f".. {etype}\n")
            else:
                _log(label, f".. {etype}: "
                            f"{json.dumps(payload, ensure_ascii=False)[:160]}\n")

    elapsed = time.perf_counter() - started
    full = "".join(transcript)
    summary = {
        "label": label,
        "sid": sid,
        "elapsed_s": round(elapsed, 1),
        "tool_events": len(tool_events),
        "final_text_len": len(full),
        "final_text_preview": full[:500],
        "last_resp": last_resp_payload,
    }
    _log(label, f"\n{'-'*72}\n"
                f"[{label}] elapsed={elapsed:.1f}s  text_len={len(full)}  "
                f"tool_events={len(tool_events)}\n")
    return summary


async def main() -> int:
    prompts = [
        ("simple_intro",
         "用三句中文介绍 XMclaw 这个开源 AI agent 项目的核心理念，不要用工具，直接回答即可。"),
        ("complex_task",
         "请用 read_file 工具读取 README.md 的前 50 行，然后用中文总结："
         "(1) 这个项目解决什么问题；(2) 它的差异化卖点是什么；"
         "(3) 你看到一个潜在风险或缺陷。"),
    ]
    summaries = []
    for label, p in prompts:
        try:
            summary = await run_turn(p, label=label)
            summaries.append(summary)
        except Exception as exc:  # noqa: BLE001
            _log("global", f"\n!! [{label}] failed: {type(exc).__name__}: {exc}\n")
            summaries.append({"label": label, "error": str(exc)})

    print("\n" + "="*72)
    print("OVERALL SUMMARY")
    print("="*72)
    for s in summaries:
        print(json.dumps(s, ensure_ascii=False)[:400])
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
