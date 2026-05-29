"""B-299 end-to-end chain probe.

Drives realistic user-style WS traffic against the running daemon and
observes whether the B-294→299 chain actually closes:

  1. Does the LLM invoke ``skill_browse`` for vague / CJK queries
     where prefilter would have dropped all skill_* tools?
  2. Does the LLM directly hit a registered skill_* when the prefilter
     surfaces a strong keyword match?
  3. Does multi-turn context accumulate (turn N reuses what turn N-1
     learned)?
  4. Does GRADER_VERDICT actually flow → state.json grows arms?

Buckets:

* A. 模糊 CJK 查询 — token-overlap-zero against English skill descs
* B. 明确关键词 — strong prefilter match expected
* C. 复杂多步任务 — needs read+analyze+write
* D. 多轮连续 — same session, 3 turns building on each other

Each scenario uses a fresh session_id to keep results comparable
(except bucket D which deliberately reuses).

Output:
  scripts/probe_b299_chain.json — per-scenario raw data
  scripts/probe_b299_chain.md   — human-readable summary table

Run:
  python scripts/probe_b299_chain.py [--timeout 60]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

import websockets

TOKEN_PATH = Path.home() / ".xmclaw" / "v2" / "pairing_token.txt"
EVOLUTION_DIR = Path.home() / ".xmclaw" / "v2" / "evolution" / "evo-main"
WS_BASE = "ws://127.0.0.1:8766/agent/v2"


# ── scenarios ──────────────────────────────────────────────────────


# Bucket A: vague CJK queries that hit the pre-B-299 zero-skill case
BUCKET_A = [
    ("vague_doc",         "帮我看看怎么写文档好"),
    ("vague_test",        "我想给项目加一些测试"),
    ("vague_optimize",    "代码看着有点乱,想优化一下"),
    ("vague_review",      "你能帮我看一下代码质量吗"),
]

# Bucket B: explicit English keywords — should hit prefilter strongly
BUCKET_B = [
    ("kw_git_commit",     "help me write a good git commit message"),
    ("kw_deploy",         "I need to deploy this to vercel"),
    ("kw_changelog",      "generate a changelog from recent commits"),
]

# Bucket C: complex multi-step. Needs file_read + analysis + write
BUCKET_C = [
    ("complex_review",    "看一下 README.md 写得怎么样,提 3 条改进建议"),
    ("complex_audit",     "检查 xmclaw/skills/ 下面有没有 import 方向违规"),
]

# Bucket D: multi-turn same session, vague→specific progression
BUCKET_D_SESSION = "b299-multiturn-{ts}"
BUCKET_D = [
    ("d_turn1",           "我在写一个 Python 项目"),
    ("d_turn2",           "想加点 type hints"),
    ("d_turn3",           "你能帮我吗"),
]


# ── helpers ────────────────────────────────────────────────────────


def _load_token() -> str:
    return TOKEN_PATH.read_text(encoding="utf-8").strip()


def _read_state_arms() -> int:
    """Read evo-main/state.json arms count, return 0 if missing."""
    p = EVOLUTION_DIR / "state.json"
    if not p.exists():
        return 0
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return len(d.get("arms") or [])
    except Exception:  # noqa: BLE001
        return 0


async def probe_turn(
    ws: websockets.WebSocketClientProtocol,
    prompt: str,
    *,
    timeout: float,
) -> dict:
    """Send one user message on an OPEN ws and capture the response.

    Returns a per-turn observation dict. Does NOT close the ws — the
    caller decides whether to reuse (multi-turn) or hang up.
    """
    skills_invoked: list[str] = []
    tools_invoked: list[str] = []
    browse_calls: list[dict] = []
    browse_results: list[dict] = []
    llm_chunks: list[str] = []
    final_response = ""
    grader_scores: list[float] = []
    grader_skill_ids: list[str] = []

    await ws.send(json.dumps({"type": "user", "content": prompt}))
    start = time.time()
    saw_done = False
    try:
        while True:
            remaining = timeout - (time.time() - start)
            if remaining <= 0:
                break
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            try:
                ev = json.loads(raw)
            except json.JSONDecodeError:
                continue
            t = ev.get("type")
            p = ev.get("payload") or {}
            if t == "tool_call_emitted":
                # B-300 followup: ``tool_invocation_started`` strips
                # args for security/size reasons, but
                # ``tool_call_emitted`` carries the LLM's full call
                # payload — read browse query from there.
                tn = p.get("name") or "?"
                if tn == "skill_browse":
                    args = p.get("args") or {}
                    browse_calls.append({
                        "query": str(args.get("query", ""))[:80],
                        "top_k": args.get("top_k"),
                    })
            elif t == "tool_invocation_started":
                tn = p.get("name") or "?"
                tools_invoked.append(tn)
                if tn.startswith("skill_") and tn != "skill_browse":
                    skills_invoked.append(tn)
            elif t == "tool_invocation_finished":
                if p.get("name") == "skill_browse":
                    content = p.get("content") or {}
                    matches = content.get("matches", []) if isinstance(content, dict) else []
                    browse_results.append({
                        "match_count": len(matches),
                        "first_3_names": [
                            m.get("tool_name") for m in matches[:3]
                        ] if matches else [],
                    })
            elif t == "llm_chunk":
                llm_chunks.append(p.get("delta") or p.get("text") or "")
            elif t == "llm_response":
                final_response = (
                    p.get("text") or p.get("content") or final_response
                )
                # llm_response with no further tool_calls usually means
                # the agent is done. We rely on a short post-response
                # quiet window to confirm.
                saw_done = True
            elif t == "grader_verdict":
                s = p.get("score")
                if isinstance(s, (int, float)):
                    grader_scores.append(float(s))
                sid = p.get("skill_id")
                if sid:
                    grader_skill_ids.append(sid)
            elif t == "agent_idle":
                # Some agents emit this when fully done.
                break
            elif t == "session_lifecycle" and p.get("phase") == "destroyed":
                break

            # End-of-turn heuristic: saw llm_response, then 3s of quiet
            if saw_done:
                try:
                    raw2 = await asyncio.wait_for(ws.recv(), timeout=3.0)
                    # got more — keep going
                    try:
                        ev = json.loads(raw2)
                    except json.JSONDecodeError:
                        continue
                    # stash and let the loop process via re-receive
                    # (cheaper to break here and let the caller continue)
                    saw_done = False  # got more events — reset
                    # Re-process this event by inlining (light dup)
                    t = ev.get("type")
                    p = ev.get("payload") or {}
                    if t == "tool_invocation_started":
                        tn = p.get("name") or "?"
                        tools_invoked.append(tn)
                        if tn == "skill_browse":
                            browse_calls.append({
                                "query": (p.get("args") or {}).get("query", "")[:80],
                            })
                        if tn.startswith("skill_") and tn != "skill_browse":
                            skills_invoked.append(tn)
                    elif t == "llm_chunk":
                        llm_chunks.append(
                            p.get("delta") or p.get("text") or ""
                        )
                    elif t == "llm_response":
                        final_response = p.get("text") or final_response
                        saw_done = True
                    elif t == "grader_verdict":
                        s = p.get("score")
                        if isinstance(s, (int, float)):
                            grader_scores.append(float(s))
                    continue
                except asyncio.TimeoutError:
                    # 3s quiet → turn done
                    break
    except websockets.exceptions.ConnectionClosed:
        pass

    elapsed = time.time() - start
    if not final_response:
        final_response = "".join(llm_chunks)

    return {
        "prompt": prompt,
        "elapsed_s": round(elapsed, 1),
        "tool_count": len(tools_invoked),
        "tools": tools_invoked,
        "browse_calls": browse_calls,
        "browse_results": browse_results,
        "skills_invoked": skills_invoked,
        "grader_scores": grader_scores,
        "grader_n": len(grader_scores),
        "grader_avg": (
            round(sum(grader_scores) / len(grader_scores), 2)
            if grader_scores else None
        ),
        "grader_skill_ids": grader_skill_ids,
        "response_chars": len(final_response),
        "response_excerpt": final_response[:200].replace("\n", " "),
    }


async def run_single(
    name: str, prompt: str, *, session_prefix: str, timeout: float,
) -> dict:
    """Run one prompt on a fresh session."""
    sid = f"{session_prefix}-{name}-{int(time.time() * 1000) % 10**8}"
    token = _load_token()
    url = f"{WS_BASE}/{sid}?token={token}"
    try:
        async with websockets.connect(url, ping_interval=None) as ws:
            obs = await probe_turn(ws, prompt, timeout=timeout)
            obs["scenario"] = name
            obs["session_id"] = sid
            return obs
    except Exception as exc:  # noqa: BLE001
        return {
            "scenario": name,
            "prompt": prompt,
            "session_id": sid,
            "error": f"ws_error: {exc}",
        }


async def run_multiturn(
    bucket: list[tuple[str, str]], *, session_prefix: str, timeout: float,
) -> list[dict]:
    """Run a sequence of prompts on the SAME session."""
    sid = session_prefix.format(ts=int(time.time() * 1000) % 10**8)
    token = _load_token()
    url = f"{WS_BASE}/{sid}?token={token}"
    out: list[dict] = []
    try:
        async with websockets.connect(url, ping_interval=None) as ws:
            for name, prompt in bucket:
                obs = await probe_turn(ws, prompt, timeout=timeout)
                obs["scenario"] = name
                obs["session_id"] = sid
                out.append(obs)
                # short pause so the daemon's run-turn settles
                await asyncio.sleep(0.5)
    except Exception as exc:  # noqa: BLE001
        out.append({
            "scenario": "multiturn_error",
            "session_id": sid,
            "error": f"ws_error: {exc}",
        })
    return out


# ── main + report ──────────────────────────────────────────────────


def _fmt_summary(results: dict) -> str:
    """Render a markdown table of observations."""
    lines: list[str] = []
    lines.append("# B-299 Chain probe")
    lines.append("")
    lines.append(f"- arms before: **{results['arms_before']}**, "
                 f"after: **{results['arms_after']}** "
                 f"(delta {results['arms_after'] - results['arms_before']:+d})")
    lines.append(f"- duration: **{results['total_elapsed_s']:.1f}s**")
    lines.append("")

    for bucket_name, bucket_results in results["buckets"].items():
        lines.append(f"## {bucket_name}")
        lines.append("")
        lines.append("| 场景 | 用时 | 工具数 | 调过 skill_browse | 调过真 skill_* | grader 均分 | 回复摘要 |")
        lines.append("|---|---|---|---|---|---|---|")
        for r in bucket_results:
            if r.get("error"):
                lines.append(f"| {r['scenario']} | — | — | — | — | — | ⚠ {r['error']} |")
                continue
            browse = "✓" if r.get("browse_calls") else "✗"
            real_skills = len(r.get("skills_invoked", []))
            real_skill_str = f"{real_skills} ({r['skills_invoked'][0][7:25]}...)" if real_skills else "✗"
            avg = r.get("grader_avg", "—")
            excerpt = r.get("response_excerpt", "")[:60]
            lines.append(
                f"| `{r['scenario']}` | {r['elapsed_s']}s | "
                f"{r.get('tool_count', 0)} | {browse} | {real_skill_str} | "
                f"{avg} | {excerpt}... |"
            )
        lines.append("")

        # Per-bucket browse detail when relevant
        browse_detail = [
            r for r in bucket_results
            if r.get("browse_calls")
        ]
        if browse_detail:
            lines.append("### skill_browse 调用细节")
            lines.append("")
            for r in browse_detail:
                for i, call in enumerate(r["browse_calls"]):
                    res = (r["browse_results"][i]
                           if i < len(r.get("browse_results", []))
                           else {})
                    lines.append(
                        f"- `{r['scenario']}` query={call['query']!r} "
                        f"→ {res.get('match_count', '?')} matches "
                        f"(top 3: {res.get('first_3_names', [])})"
                    )
            lines.append("")

    return "\n".join(lines)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=float, default=60.0,
                        help="per-turn timeout seconds")
    parser.add_argument("--bucket", choices=["a", "b", "c", "d", "all"],
                        default="all")
    args = parser.parse_args()

    overall_start = time.time()
    arms_before = _read_state_arms()

    buckets: dict[str, list[dict]] = {}

    pfx = f"b299-probe-{int(time.time() * 1000) % 10**6}"

    if args.bucket in ("a", "all"):
        print("Bucket A: 模糊 CJK 查询...", flush=True)
        bucket_a = []
        for name, prompt in BUCKET_A:
            print(f"  - {name}: {prompt[:30]}...", flush=True)
            r = await run_single(
                name, prompt,
                session_prefix=pfx, timeout=args.timeout,
            )
            bucket_a.append(r)
            print(f"    {r.get('elapsed_s', '?')}s, "
                  f"tools={r.get('tool_count', 0)}, "
                  f"browse_calls={len(r.get('browse_calls', []))}",
                  flush=True)
        buckets["A — 模糊 CJK 查询"] = bucket_a

    if args.bucket in ("b", "all"):
        print("Bucket B: 明确关键词...", flush=True)
        bucket_b = []
        for name, prompt in BUCKET_B:
            print(f"  - {name}: {prompt[:30]}...", flush=True)
            r = await run_single(
                name, prompt,
                session_prefix=pfx, timeout=args.timeout,
            )
            bucket_b.append(r)
            print(f"    {r.get('elapsed_s', '?')}s, "
                  f"tools={r.get('tool_count', 0)}, "
                  f"skills={r.get('skills_invoked', [])[:3]}",
                  flush=True)
        buckets["B — 明确关键词 (英文)"] = bucket_b

    if args.bucket in ("c", "all"):
        print("Bucket C: 复杂多步...", flush=True)
        bucket_c = []
        for name, prompt in BUCKET_C:
            print(f"  - {name}: {prompt[:30]}...", flush=True)
            r = await run_single(
                name, prompt,
                session_prefix=pfx, timeout=args.timeout,
            )
            bucket_c.append(r)
            print(f"    {r.get('elapsed_s', '?')}s, "
                  f"tools={r.get('tool_count', 0)}",
                  flush=True)
        buckets["C — 复杂多步任务"] = bucket_c

    if args.bucket in ("d", "all"):
        print("Bucket D: 多轮连续...", flush=True)
        bucket_d = await run_multiturn(
            BUCKET_D, session_prefix=BUCKET_D_SESSION,
            timeout=args.timeout,
        )
        for r in bucket_d:
            print(f"  - {r.get('scenario')}: "
                  f"{r.get('elapsed_s', '?')}s, "
                  f"tools={r.get('tool_count', 0)}",
                  flush=True)
        buckets["D — 多轮连续 (同 session)"] = bucket_d

    # Wait briefly so the trigger's debounce can settle (the trigger
    # only fires evaluate() 30s after the last verdict, but state.json
    # is written on every ingest — so this is mostly to let the bus
    # drain).
    await asyncio.sleep(2.0)
    arms_after = _read_state_arms()

    results = {
        "arms_before": arms_before,
        "arms_after": arms_after,
        "total_elapsed_s": time.time() - overall_start,
        "buckets": buckets,
    }

    out_md = Path("scripts/probe_b299_chain.md")
    out_json = Path("scripts/probe_b299_chain.json")
    out_md.write_text(_fmt_summary(results), encoding="utf-8")
    out_json.write_text(
        json.dumps(results, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("\n=== summary ===", flush=True)
    print(f"arms before: {arms_before}, after: {arms_after} "
          f"(delta {arms_after - arms_before:+d})", flush=True)
    print(f"total elapsed: {results['total_elapsed_s']:.1f}s", flush=True)
    print(f"\nWritten: {out_md}, {out_json}", flush=True)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()) or 0)
