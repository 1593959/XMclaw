"""Tool-chain logic probe — does the agent COMPOSE tools correctly?

Each scenario requires >=2 tool calls where tool B's args depend on tool
A's result. If the agent just hallucinates B's args without reading A's
output, the scenario fails — that's the signal we want.

Also includes error-recovery and parallel-dispatch scenarios.
"""
from __future__ import annotations

import asyncio
import io
import json
import sys
import time
from pathlib import Path

import websockets

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

URL = "ws://127.0.0.1:8766/agent/default"
TURN_TIMEOUT = 240.0


async def run_turn(user_input: str, *, label: str) -> dict:
    t0 = time.monotonic()
    frames = []
    saw_done = False
    saw_error = None
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

    starts = [f for f in frames if f.get("type") == "tool_start"]
    results = [f for f in frames if f.get("type") == "tool_result"]
    text = "".join(f.get("content", "") or "" for f in frames if f.get("type") == "chunk")

    return {
        "label": label,
        "input": user_input,
        "elapsed_s": round(time.monotonic() - t0, 2),
        "saw_done": saw_done,
        "saw_error": saw_error,
        "tools_called": [
            {"name": s.get("tool") or s.get("name"),
             "args": s.get("args") or s.get("arguments")}
            for s in starts
        ],
        "tool_results": [
            {"name": r.get("tool") or r.get("name"),
             "ok": r.get("ok"),
             "preview": str(r.get("result") or r.get("content") or "")[:200]}
            for r in results
        ],
        "assistant_text": text,
    }


SCENARIOS = [
    # Chain: grep must find the file, then file_read must open THAT path.
    ("chain_grep_then_read",
     ["grep", "file_read"],
     "在 xmclaw/core 里用 grep 搜 `class TaskClassifier` 定位文件，"
     "然后用 file_read 把那个文件的前 30 行原文读给我看。"),

    # Chain: write file, then bash `cat` reads what you just wrote.
    ("chain_write_then_bash",
     ["file_write", "bash"],
     "把字符串 'chain-probe' 写到当前目录下的 _chain.txt，"
     "然后用 bash 运行 `cat _chain.txt` 把输出贴给我。"),

    # Chain: compute via code_exec, then write the result.
    ("chain_exec_then_write",
     ["code_exec", "file_write"],
     "先用 code_exec 算出 2**20 的值，"
     "再用 file_write 把结果数字写到当前目录的 _pow.txt。"),

    # Parallel: read two files in one turn. Good agents send tool_use blocks in parallel.
    ("parallel_two_reads",
     ["file_read", "file_read"],
     "同时读 README.md 的前 3 行和 pyproject.toml 的前 3 行告诉我。"),

    # Error recovery: ask for a file that doesn't exist. Agent should NOT loop forever;
    # it should either gracefully report failure or fall back to glob/grep.
    ("error_recovery_missing_file",
     [],
     "用 file_read 读 DOES_NOT_EXIST_AAA.txt 并告诉我里面写了什么。如果读不到就说清楚。"),

    # Identity introspection: should read SOUL.md on its own initiative.
    ("identity_introspect",
     ["file_read"],
     "自我介绍一下：你的定位是什么？你可以读 agents/default/SOUL.md 和 agents/default/PROFILE.md。"),

    # Long chain: glob → pick a file → read → summarize. 3+ hops.
    ("long_chain_summarize",
     ["glob", "file_read"],
     "在 xmclaw/core 里 glob 出所有 *.py，挑 task_classifier.py，"
     "读它的前 40 行然后一句话告诉我这个模块在干嘛。"),
]


async def main() -> int:
    results = []
    for label, expected_tools, msg in SCENARIOS:
        print(f"\n>>> [{label}] expect tools={expected_tools}")
        print(f"    input: {msg!r}")
        r = await run_turn(msg, label=label)
        r["expected_tools"] = expected_tools
        called = [t["name"] for t in r["tools_called"]]
        r["expected_all_called"] = all(t in called for t in expected_tools) if expected_tools else True
        print(f"    elapsed={r['elapsed_s']}s done={r['saw_done']} error={r['saw_error']!r}")
        print(f"    tools_called={called}")
        print(f"    expected_satisfied={r['expected_all_called']}")
        for tr in r["tool_results"][:6]:
            print(f"    result[{tr['name']}] ok={tr['ok']} preview={tr['preview']!r}")
        txt = r["assistant_text"]
        print(f"    assistant: {(txt[:220] + '…') if len(txt) > 220 else txt!r}")
        results.append(r)
        await asyncio.sleep(0.5)

    print("\n\n=== TOOL-CHAIN SUMMARY ===")
    print(f"{'label':<32}{'expected':<24}{'called':<28}{'ok?':<6}")
    for r in results:
        called_names = [t["name"] for t in r["tools_called"]]
        print(f"{r['label']:<32}{','.join(r['expected_tools']):<24}"
              f"{','.join(called_names)[:26]:<28}{str(r['expected_all_called']):<6}")

    out = Path(r"C:\Users\15978\Desktop\XMclaw\tests\_e2e_tool_chain_frames.json")
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str),
                   encoding="utf-8")
    print(f"\nfull frames → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
