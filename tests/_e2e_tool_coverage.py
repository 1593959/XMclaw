"""Tool-coverage probe — one scenario per tool category, fresh WS per scenario.

We deliberately open a new WS per turn to sidestep the frame-desync bug so
each measurement is clean. The goal isn't pretty UX; it's "did this tool
actually get CALLED by the agent, did it return a result, and did the
assistant use the result?"

Network tools (web_search, web_fetch, browser, github) are flagged
separately because they may fail on a machine without internet / keys —
that's a finding, not a bug in the harness.
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
TURN_TIMEOUT = 180.0


async def run_turn(user_input: str, *, label: str) -> dict:
    t0 = time.monotonic()
    frames: list[dict] = []
    saw_done = False
    saw_error: str | None = None

    try:
        async with websockets.connect(URL, max_size=2**24, open_timeout=10) as ws:
            await ws.send(json.dumps({"type": "user", "content": user_input}))
            while True:
                if time.monotonic() - t0 > TURN_TIMEOUT:
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
                    continue
                frames.append(frame)
                if frame.get("type") == "done":
                    saw_done = True
                    break
                if frame.get("type") == "error":
                    saw_error = frame.get("content", "")
                    break
                if frame.get("type") == "ask_user":
                    await ws.send(json.dumps({"type": "ask_user_answer", "answer": "yes"}))
    except Exception as e:
        saw_error = f"ws_error: {e}"

    text = "".join(f.get("content", "") or f.get("text", "")
                   for f in frames if f.get("type") == "chunk")

    tool_starts = [f for f in frames if f.get("type") == "tool_start"]
    tool_results = [f for f in frames if f.get("type") == "tool_result"]
    tool_errors = [f for f in frames if f.get("type") == "tool_error"]

    return {
        "label": label,
        "input": user_input,
        "elapsed_s": round(time.monotonic() - t0, 2),
        "saw_done": saw_done,
        "saw_error": saw_error,
        "assistant_text": text,
        "tools_called": [
            {"name": t.get("tool") or t.get("name"),
             "args": t.get("args") or t.get("arguments")}
            for t in tool_starts
        ],
        "tool_results": [
            {"name": t.get("tool") or t.get("name"),
             "ok": t.get("ok"),
             "content_preview": str(t.get("result") or t.get("content") or "")[:200]}
            for t in tool_results
        ],
        "tool_errors": [t.get("content") or t.get("error") for t in tool_errors],
    }


# Target tool in each scenario — the agent MUST call this for the scenario to pass.
SCENARIOS = [
    # (label, expected_tool, user_input)
    ("bash",          "bash",          "运行 bash 命令 `echo hello-from-bash` 把输出告诉我"),
    ("glob",          "glob",          "用 glob 工具列出 xmclaw/core 目录下所有 .py 文件"),
    ("grep",          "grep",          "用 grep 在 xmclaw/core 目录里搜 TaskClassifier 这个类名，告诉我在哪个文件"),
    ("file_read",     "file_read",     "读一下 README.md 的前 5 行原文给我"),
    ("file_write",    "file_write",    "把 'probe-ok' 写到当前目录下的 _probe.txt 里"),
    ("file_edit",     "file_edit",     "把 _probe.txt 里的 probe-ok 替换成 probe-done"),
    ("code_exec",     "code_exec",     "用 code_exec 工具跑 Python: print(2**10)，把输出告诉我"),
    ("web_search",    "web_search",    "用 web_search 工具查一下 FastAPI 最新稳定版本号是多少"),
    ("web_fetch",     "web_fetch",     "用 web_fetch 工具抓 https://example.com 看看首页有什么文字"),
    ("memory_search", "memory_search", "用 memory_search 工具在我的记忆库里搜一下之前有没有提过 number.txt"),
    ("identity",      "file_read",     "你叫什么名字？你应该读 agents/default/SOUL.md 来回答我"),
]


async def main() -> int:
    results = []
    for label, expected, msg in SCENARIOS:
        print(f"\n>>> [{label}] expect tool={expected!r}")
        print(f"    input: {msg!r}")
        r = await run_turn(msg, label=label)
        r["expected_tool"] = expected
        r["expected_tool_called"] = any(
            (t.get("name") == expected) for t in r["tools_called"]
        )
        print(f"    elapsed={r['elapsed_s']}s done={r['saw_done']} error={r['saw_error']!r}")
        print(f"    tools_called={[t['name'] for t in r['tools_called']]}")
        print(f"    expected_tool_called={r['expected_tool_called']}")
        for tr in r["tool_results"]:
            print(f"    result[{tr['name']}] ok={tr['ok']} preview={tr['content_preview']!r}")
        for te in r["tool_errors"]:
            print(f"    ⚠ tool_error: {te!r}")
        txt = r["assistant_text"]
        print(f"    assistant: {(txt[:200] + '…') if len(txt) > 200 else txt!r}")
        results.append(r)
        await asyncio.sleep(0.5)

    # Summary
    print("\n\n=== TOOL COVERAGE SUMMARY ===")
    print(f"{'label':<16}{'expected':<16}{'called?':<10}{'done?':<8}{'result ok?':<12}")
    for r in results:
        name = r["expected_tool"]
        ok = any(tr.get("name") == name and tr.get("ok") for tr in r["tool_results"])
        print(f"{r['label']:<16}{name:<16}{str(r['expected_tool_called']):<10}"
              f"{str(r['saw_done']):<8}{str(ok):<12}")

    out = Path(r"C:\Users\15978\Desktop\XMclaw\tests\_e2e_tool_coverage_frames.json")
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str),
                   encoding="utf-8")
    print(f"\nfull frames → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
