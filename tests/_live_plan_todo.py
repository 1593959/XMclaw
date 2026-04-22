"""Live demo — Plan + Todo workflow end-to-end on MiniMax.

Asks the running agent to solve a multi-step task and INSTRUCTS IT to
use ``todo_write`` to track progress. Verifies on the wire that:

  1. The first turn emits a TODO_UPDATED event (initial plan).
  2. Subsequent turns emit more TODO_UPDATED events with status flips
     from pending -> in_progress -> done.
  3. The final assistant text references the plan.

Designed to exercise the full server-side todo stack: tool dispatch,
TODO_UPDATED bus event, per-session isolation, the pending/in_progress/
done status enum. Separately useful as a "does the model cooperate with
the todo pattern?" probe.

Run with the daemon already up on port 8765 (default). Chinese + UTF-8
output friendly.
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
SESSION = "live-plan-todo-" + str(int(time.time()))


async def _drain_until_terminal(ws) -> dict:
    """Collect every frame until the LLM emits a terminal (no-tool-call)
    text response. Return per-type event counts + the final text."""
    counts: dict[str, int] = {}
    todo_snapshots: list[list[dict]] = []
    tool_calls: list[dict] = []
    final_text: str | None = None
    for _ in range(80):
        raw = await ws.recv()
        evt = json.loads(raw)
        t, p = evt["type"], evt.get("payload") or {}
        counts[t] = counts.get(t, 0) + 1
        if t == "todo_updated":
            todo_snapshots.append(p.get("items", []))
        elif t == "tool_call_emitted":
            tool_calls.append({"name": p.get("name"), "args": p.get("args")})
        elif t == "llm_response":
            if p.get("ok") and p.get("tool_calls_count", 0) == 0:
                final_text = p.get("content", "")
                break
        elif t == "anti_req_violation":
            final_text = f"[VIOLATION] {p.get('message')}"
            break
    return {
        "counts": counts,
        "todo_snapshots": todo_snapshots,
        "tool_calls": tool_calls,
        "final": final_text or "",
    }


def _fmt_todos(items: list[dict]) -> str:
    if not items:
        return "  (empty)"
    glyphs = {"pending": "○", "in_progress": "◐", "done": "●"}
    return "\n".join(
        f"  {glyphs.get(t.get('status'), '?')} {t.get('content', '')}"
        for t in items
    )


async def main() -> None:
    token_path = Path.home() / ".xmclaw" / "v2" / "pairing_token.txt"
    token = token_path.read_text(encoding="utf-8").strip() if token_path.exists() else None
    url = f"ws://127.0.0.1:{PORT}/agent/v2/{SESSION}"
    if token:
        url += f"?token={token}"

    # A task that NEEDS planning to do well. Instructions force the
    # todo_write pattern so we can verify TODO_UPDATED fires.
    turn1 = (
        "我想你帮我做一个小任务:在 C:/Users/15978/Desktop 下建一个名为 "
        "`plan_demo` 的子目录,里面写 3 个文件:"
        "README.md(一行标题)、app.py(打印 hello)、notes.txt(空白即可). "
        "请先调用 todo_write 建 4 条待办(创目录、写 README、写 app、写 notes), "
        "然后逐步完成,每完成一项立刻再调 todo_write 把状态改为 done。"
        "最后告诉我都做完了。"
    )

    print(f"=== live plan+todo demo: session={SESSION} ===\n")
    print(f"USER> {turn1}\n")

    async with websockets.connect(url, max_size=None) as ws:
        # Drain the session_lifecycle create frame.
        await ws.recv()
        await ws.send(json.dumps({"type": "user", "content": turn1}))
        result = await _drain_until_terminal(ws)

    # ── report ──
    print("── 事件统计 ──")
    for k, v in sorted(result["counts"].items()):
        print(f"  {k}: {v}")
    print("")
    print(f"── 工具调用序列 ({len(result['tool_calls'])}) ──")
    for i, tc in enumerate(result["tool_calls"], 1):
        args_str = json.dumps(tc["args"] or {}, ensure_ascii=False)
        if len(args_str) > 100:
            args_str = args_str[:100] + "…"
        print(f"  {i:2d}. {tc['name']:<14} {args_str}")
    print("")
    print(f"── Todo 快照序列 ({len(result['todo_snapshots'])} 次更新) ──")
    for i, snap in enumerate(result["todo_snapshots"], 1):
        print(f"  snapshot {i}:")
        print(_fmt_todos(snap))
    print("")
    print("── 最终助手回复 ──")
    print(result["final"][:500])
    print("")

    # ── pass/fail criteria ──
    failures = []
    if result["counts"].get("todo_updated", 0) < 2:
        failures.append(
            f"expected >=2 TODO_UPDATED events, got "
            f"{result['counts'].get('todo_updated', 0)} "
            f"(the agent should re-call todo_write as each step completes)"
        )
    if result["todo_snapshots"]:
        last = result["todo_snapshots"][-1]
        done_count = sum(1 for t in last if t.get("status") == "done")
        if done_count < len(last):
            failures.append(
                f"final snapshot has {done_count}/{len(last)} done -- "
                f"expected all done by end"
            )
    created_dir = Path.home() / "Desktop" / "plan_demo"
    if not created_dir.exists():
        failures.append(f"expected directory {created_dir} to exist")
    else:
        for name in ("README.md", "app.py", "notes.txt"):
            if not (created_dir / name).exists():
                failures.append(f"expected file {created_dir / name} to exist")

    print("── 结果 ──")
    if failures:
        for f in failures:
            print(f"  [FAIL] {f}")
        sys.exit(1)
    print("  [ok] 所有检查都通过。")


if __name__ == "__main__":
    asyncio.run(main())
