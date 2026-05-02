"""Black-box behavioural probe.

Connects to the running daemon's WS endpoint, sends a vague user
message that does NOT name any specific skill / tool, and records
which skills the agent autonomously picked, which tools it ran, and
how long each turn took. The point is to test the agent's
*choosing* behaviour — anti-gimmick: we don't whisper hints.

Usage::
    python scripts/probe_blackbox.py "帮我提交一下"

Or batch-mode (4 default scenarios)::
    python scripts/probe_blackbox.py --batch
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import Counter
from pathlib import Path

import websockets

TOKEN_PATH = Path.home() / ".xmclaw" / "v2" / "pairing_token.txt"
WS_BASE = "ws://127.0.0.1:8765/agent/v2"

# Black-box scenarios — vague prompts the agent has to figure out.
DEFAULT_SCENARIOS = [
    ("commit",  "帮我把当前改动提交了"),
    ("ui_ux",   "帮我设计一下登录页的 UI 思路"),
    ("filesys", "看一下我桌面上有什么文件"),
    ("recall",  "我之前跟你说过我偏好什么编程语言?"),
]


def _load_token() -> str:
    return TOKEN_PATH.read_text(encoding="utf-8").strip()


async def probe_one(name: str, prompt: str, timeout: float = 90.0) -> dict:
    """Send one user message, collect events for ``timeout`` seconds
    or until the bus reports the turn finished (final LLM_RESPONSE
    with no pending tool call). Returns a structured report."""
    sid = f"probe-{name}-{int(time.time())}"
    token = _load_token()
    url = f"{WS_BASE}/{sid}?token={token}"

    skills_invoked: list[str] = []
    tools_invoked: list[tuple[str, dict]] = []
    llm_chunks: list[str] = []
    final_response: str = ""
    grader_scores: list[float] = []
    start = time.time()
    last_event_ts = start
    crashed = False
    crash_reason = ""

    async with websockets.connect(url, ping_interval=None) as ws:
        await ws.send(json.dumps({"type": "user", "content": prompt}))
        try:
            while True:
                remaining = timeout - (time.time() - start)
                if remaining <= 0:
                    break
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                except asyncio.TimeoutError:
                    break
                last_event_ts = time.time()
                try:
                    ev = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                t = ev.get("type")
                p = ev.get("payload") or {}
                if t == "skill_exec_started":
                    skills_invoked.append(p.get("skill_id") or "?")
                elif t == "tool_invocation_started":
                    tools_invoked.append((p.get("name") or "?", p))
                elif t == "llm_chunk":
                    llm_chunks.append(p.get("text") or p.get("delta") or "")
                elif t == "llm_response":
                    final_response = (
                        p.get("text") or p.get("content") or final_response
                    )
                    # turn naturally ends after final LLM response with
                    # no further pending tool calls — but bus continues
                    # so we wait for trailing events briefly.
                elif t == "grader_verdict":
                    s = p.get("score")
                    if isinstance(s, (int, float)):
                        grader_scores.append(float(s))
                elif t == "anti_req_violation":
                    crashed = True
                    crash_reason = p.get("message", "")
                # Heuristic stop: 8s of bus silence after at least one
                # llm_response = turn settled.
                if final_response and (
                    time.time() - last_event_ts > 8.0
                ):
                    break
        except websockets.ConnectionClosed:
            pass

    elapsed = time.time() - start
    return {
        "scenario": name,
        "prompt": prompt,
        "elapsed_s": round(elapsed, 1),
        "skills": skills_invoked,
        "tools": [t[0] for t in tools_invoked],
        "tool_args_first": tools_invoked[0][1] if tools_invoked else None,
        "grader_avg": round(
            sum(grader_scores) / len(grader_scores), 2,
        ) if grader_scores else None,
        "grader_n": len(grader_scores),
        "response_chars": len(final_response),
        "response_excerpt": (final_response or "".join(llm_chunks))[:200].replace("\n", " "),
        "crashed": crashed,
        "crash_reason": crash_reason,
    }


def render_report(reports: list[dict]) -> str:
    out: list[str] = []
    out.append("=" * 80)
    out.append("BLACK-BOX BEHAVIOURAL PROBE — agent autonomy without hints")
    out.append("=" * 80)
    for r in reports:
        out.append("")
        out.append(f"### {r['scenario']:10s}  ({r['elapsed_s']}s)")
        out.append(f"  prompt:      {r['prompt']}")
        if r["crashed"]:
            out.append(f"  ❌ crashed:  {r['crash_reason'][:200]}")
            continue
        skills_str = ", ".join(r["skills"]) if r["skills"] else "(none)"
        tools_str = ", ".join(r["tools"]) if r["tools"] else "(none)"
        out.append(f"  skills:      {skills_str}")
        out.append(f"  tools:       {tools_str}")
        if r["grader_avg"] is not None:
            out.append(
                f"  grader:      avg={r['grader_avg']} (n={r['grader_n']})"
            )
        out.append(f"  resp chars:  {r['response_chars']}")
        out.append(f"  excerpt:     {r['response_excerpt']}")
    out.append("")
    out.append("=" * 80)
    out.append("AGGREGATE")
    out.append("=" * 80)
    all_skills: Counter = Counter()
    all_tools: Counter = Counter()
    n_used_skill = 0
    for r in reports:
        if r["skills"]:
            n_used_skill += 1
        all_skills.update(r["skills"])
        all_tools.update(r["tools"])
    out.append(
        f"scenarios using a skill autonomously: "
        f"{n_used_skill} / {len(reports)}"
    )
    if all_skills:
        out.append(
            "skills picked: "
            + ", ".join(f"{s}×{n}" for s, n in all_skills.most_common())
        )
    if all_tools:
        out.append(
            "tools called: "
            + ", ".join(f"{t}×{n}" for t, n in all_tools.most_common(10))
        )
    return "\n".join(out)


async def main() -> int:
    # Force UTF-8 stdout on Windows GBK terminals so Chinese / emoji
    # in agent responses don't crash the print.
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass

    ap = argparse.ArgumentParser()
    ap.add_argument("prompt", nargs="?", help="custom prompt (else --batch).")
    ap.add_argument("--batch", action="store_true", help="run default scenarios.")
    ap.add_argument("--timeout", type=float, default=90.0)
    ap.add_argument("--report-file", type=str, default=None)
    args = ap.parse_args()

    if args.prompt and not args.batch:
        reports = [await probe_one("custom", args.prompt, timeout=args.timeout)]
    else:
        reports = []
        for name, prompt in DEFAULT_SCENARIOS:
            print(f"[probe] running {name!r}: {prompt}", flush=True)
            r = await probe_one(name, prompt, timeout=args.timeout)
            reports.append(r)
            print(f"  done in {r['elapsed_s']}s, "
                  f"{len(r['skills'])} skill(s), {len(r['tools'])} tool(s)",
                  flush=True)
        print()

    text = render_report(reports)
    if args.report_file:
        Path(args.report_file).write_text(text, encoding="utf-8")
        print(f"report written to {args.report_file}")
    else:
        print(text)
    # Always dump structured data alongside the human report for re-analysis.
    json_path = (
        Path(args.report_file).with_suffix(".json")
        if args.report_file else Path("probe_blackbox_last.json")
    )
    json_path.write_text(
        json.dumps(reports, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"json dump: {json_path}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
