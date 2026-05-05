"""B-200 smoke test — 20 fuzzy scenarios × 2 rounds.

Sends ambiguous prompts to the running daemon over WS and captures
what the agent actually does — which skills it picks, which tools it
calls, whether it leverages memory_search / lessons / preferences,
whether it ever hits the B-199 refuse-without-trying anti-pattern,
whether it ever fires propose_curriculum_edit for self-evolution.

The point is dogfooding the architecture from B-197/198/199/200
together. No skill names in the prompts; the agent has to route.
Round B re-runs identical prompts to test cross-session continuity
(does Round B see what Round A learned?).

Output:
* probe_b200_smoke.md  — human-readable per-scenario report
* probe_b200_smoke.json — structured data for re-analysis
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


# 20 scenarios. Categories interleaved so consecutive runs aren't
# clustered. Names short for table output.
SCENARIOS: list[tuple[str, str]] = [
    # 用户画像 / preference recall
    ("name_recall",     "你怎么称呼我？"),
    ("lang_pref",       "我们用什么语言交流？"),
    ("project_recall",  "我现在在做什么项目？"),
    ("tool_pref",       "我喜欢用什么 lint / 测试工具？"),
    # Lesson / failure-mode recall
    ("past_failure",    "你最近有没有犯过什么值得记下来的错？"),
    ("avoid_pattern",   "我应该怎么样跟你提问比较高效？"),
    # Skills routing
    ("brainstorm",      "我有个新主意，能帮我先发散再收敛吗？"),
    ("commit_help",     "刚改了一些代码，提交了吧。"),
    ("refactor_hint",   "看看 xmclaw/utils/log.py 能不能写得更优雅。"),
    ("plan_long",       "把'XMclaw 加 RAG 检索'拆成几步开发计划。"),
    # B-199 refuse-without-trying — try the impossible-looking
    ("send_email",      "把这次对话整理成邮件发给我邮箱。"),
    ("ring_phone",      "你能给我打个电话提醒我喝水吗？"),
    ("compress_chat",   "把咱们今天的对话打包成 zip 发给我。"),
    ("change_wallpaper","把我桌面壁纸换成北京天气图。"),
    # Memory recall — should hit memory_search
    ("recall_decision", "之前我们关于 SQLite 的决策是什么来着？"),
    ("recall_cron",     "上次 cron 任务踩过什么坑？"),
    # Cross-session continuity
    ("yesterday",       "昨天我们最后聊到哪了？"),
    ("identity_test",   "你叫什么名字？"),
    # Curriculum self-modification probe
    ("self_review",     "review 一下你最近的回答，有什么规则你觉得该加进 LEARNING.md？"),
    # Tool exposure
    ("tool_inventory",  "你都能做哪些事？给个 5 条核心能力列表就行。"),
]


def _load_token() -> str:
    return TOKEN_PATH.read_text(encoding="utf-8").strip()


async def probe_one(
    name: str, prompt: str, *, session_prefix: str, timeout: float = 90.0,
) -> dict:
    """Send one user message. Capture every interesting event until
    the bus reports the turn is finished or timeout hits."""
    sid = f"{session_prefix}-{name}-{int(time.time() * 1000) % 10**8}"
    token = _load_token()
    url = f"{WS_BASE}/{sid}?token={token}"

    skills_invoked: list[str] = []
    tools_invoked: list[str] = []
    llm_chunks: list[str] = []
    final_response = ""
    grader_scores: list[float] = []
    propose_calls: list[dict] = []
    mem_search_calls: list[dict] = []
    start = time.time()
    crashed = False
    crash_reason = ""

    try:
        async with websockets.connect(url, ping_interval=None) as ws:
            await ws.send(json.dumps({"type": "user", "content": prompt}))
            try:
                while True:
                    remaining = timeout - (time.time() - start)
                    if remaining <= 0:
                        break
                    try:
                        raw = await asyncio.wait_for(
                            ws.recv(), timeout=remaining,
                        )
                    except asyncio.TimeoutError:
                        break
                    try:
                        ev = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    t = ev.get("type")
                    p = ev.get("payload") or {}
                    if t == "skill_exec_started":
                        skills_invoked.append(p.get("skill_id") or "?")
                    elif t == "tool_invocation_started":
                        tn = p.get("name") or "?"
                        tools_invoked.append(tn)
                        if tn == "memory_search":
                            mem_search_calls.append({
                                "query": (p.get("args") or {}).get("query", "")[:80],
                                "kind": (p.get("args") or {}).get("kind"),
                            })
                        if tn == "propose_curriculum_edit":
                            propose_calls.append(p.get("args") or {})
                    elif t == "llm_chunk":
                        llm_chunks.append(p.get("delta") or p.get("text") or "")
                    elif t == "llm_response":
                        final_response = (
                            p.get("text") or p.get("content")
                            or final_response
                        )
                    elif t == "grader_verdict":
                        s = p.get("score")
                        if isinstance(s, (int, float)):
                            grader_scores.append(float(s))
                    elif t == "anti_req_violation":
                        crashed = True
                        crash_reason = p.get("message", "")
            except websockets.exceptions.ConnectionClosed:
                pass
    except Exception as exc:  # noqa: BLE001
        crashed = True
        crash_reason = f"ws_error: {exc}"

    elapsed = time.time() - start
    if not final_response:
        final_response = "".join(llm_chunks)

    # Heuristic: did agent refuse-without-trying?
    refuse_phrases = (
        "我没办法", "我不会", "我不能", "我做不到", "无法直接",
    )
    refused = any(p in final_response for p in refuse_phrases)
    investigated = any(t in tools_invoked for t in (
        "memory_search", "sqlite_query", "list_dir", "file_read",
        "grep_files", "web_search",
    ))

    return {
        "scenario": name,
        "prompt": prompt,
        "session_id": sid,
        "elapsed_s": round(elapsed, 1),
        "skills": skills_invoked,
        "tools": tools_invoked,
        "tool_count": len(tools_invoked),
        "memory_search_calls": mem_search_calls,
        "propose_curriculum_calls": propose_calls,
        "response_chars": len(final_response),
        "response_excerpt": final_response[:300].replace("\n", " "),
        "response_full": final_response,
        "grader_avg": (
            round(sum(grader_scores) / len(grader_scores), 2)
            if grader_scores else None
        ),
        "grader_n": len(grader_scores),
        "refused_without_investigation": refused and not investigated,
        "crashed": crashed,
        "crash_reason": crash_reason,
    }


def render_round(label: str, reports: list[dict]) -> str:
    """Render one round's table + per-scenario brief."""
    out: list[str] = []
    out.append(f"## Round {label}\n")
    out.append("| # | scenario | elapsed | skills | tools | refused-w/o-try | mem_search | propose | grader |")
    out.append("|--:|---------|--------:|-------:|------:|-----------------|-----------:|--------:|-------:|")
    for i, r in enumerate(reports, 1):
        skills_n = len(r.get("skills") or [])
        tools_n = r.get("tool_count", 0)
        ref = "⚠️" if r.get("refused_without_investigation") else ""
        ms = len(r.get("memory_search_calls") or [])
        prop = len(r.get("propose_curriculum_calls") or [])
        grader = r.get("grader_avg")
        gtxt = f"{grader} (n={r.get('grader_n', 0)})" if grader else "-"
        out.append(
            f"| {i:2d} | `{r['scenario']:14s}` | "
            f"{r['elapsed_s']:>5.1f}s | "
            f"{skills_n:>2d} | {tools_n:>2d} | "
            f"{ref:^15s} | {ms:>2d} | {prop:>2d} | {gtxt:>10s} |"
        )
    return "\n".join(out)


async def run_round(label: str, prefix: str, timeout: float) -> list[dict]:
    out: list[dict] = []
    for i, (name, prompt) in enumerate(SCENARIOS, 1):
        print(f"[round {label} {i:2d}/{len(SCENARIOS)}] {name}: {prompt[:60]}...",
              flush=True)
        r = await probe_one(name, prompt, session_prefix=prefix, timeout=timeout)
        out.append(r)
        ms = len(r.get("memory_search_calls") or [])
        ref = "REFUSED" if r.get("refused_without_investigation") else "ok"
        prop = "PROPOSE!" if r.get("propose_curriculum_calls") else ""
        print(
            f"   -> {r['elapsed_s']}s  skills={len(r['skills'])}"
            f"  tools={r['tool_count']}  mem_search={ms}"
            f"  {ref}  {prop}",
            flush=True,
        )
    return out


async def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=float, default=90.0)
    ap.add_argument("--report", default="probe_b200_smoke.md")
    args = ap.parse_args()

    print("=== Round A ===", flush=True)
    round_a = await run_round("A", "probe-b200a", args.timeout)
    print("\n=== Round B (same prompts, fresh sessions) ===\n", flush=True)
    round_b = await run_round("B", "probe-b200b", args.timeout)

    # Aggregate
    def _agg(reports: list[dict]) -> dict:
        skills_counter: Counter = Counter()
        tools_counter: Counter = Counter()
        for r in reports:
            for s in r.get("skills", []):
                skills_counter[s] += 1
            for t in r.get("tools", []):
                tools_counter[t] += 1
        return {
            "scenarios": len(reports),
            "total_elapsed_s": round(sum(r["elapsed_s"] for r in reports), 1),
            "refused_without_try_count": sum(
                1 for r in reports if r.get("refused_without_investigation")
            ),
            "memory_search_total_calls": sum(
                len(r.get("memory_search_calls") or []) for r in reports
            ),
            "memory_search_scenarios": sum(
                1 for r in reports if r.get("memory_search_calls")
            ),
            "propose_curriculum_total": sum(
                len(r.get("propose_curriculum_calls") or []) for r in reports
            ),
            "skills_invoked_top": skills_counter.most_common(10),
            "tools_invoked_top": tools_counter.most_common(15),
            "grader_avg_overall": round(
                sum(
                    r["grader_avg"] for r in reports
                    if r.get("grader_avg") is not None
                ) / max(1, sum(
                    1 for r in reports if r.get("grader_avg") is not None
                )),
                2,
            ),
        }

    agg_a = _agg(round_a)
    agg_b = _agg(round_b)

    md = [
        "# B-200 smoke test report",
        "",
        f"_{len(SCENARIOS)} scenarios × 2 rounds = {len(SCENARIOS) * 2} turns_",
        "",
        "## Round-level aggregate",
        "",
        "| metric | round A | round B |",
        "|--------|--------:|--------:|",
        f"| total elapsed s | {agg_a['total_elapsed_s']} | {agg_b['total_elapsed_s']} |",
        f"| refused-without-trying ⚠️ | {agg_a['refused_without_try_count']} | {agg_b['refused_without_try_count']} |",
        f"| memory_search calls (total) | {agg_a['memory_search_total_calls']} | {agg_b['memory_search_total_calls']} |",
        f"| memory_search calls (scenarios) | {agg_a['memory_search_scenarios']} | {agg_b['memory_search_scenarios']} |",
        f"| propose_curriculum_edit calls | {agg_a['propose_curriculum_total']} | {agg_b['propose_curriculum_total']} |",
        f"| grader avg | {agg_a['grader_avg_overall']} | {agg_b['grader_avg_overall']} |",
        "",
        "## Skills invoked (top)",
        "",
        f"Round A: {agg_a['skills_invoked_top']}",
        f"Round B: {agg_b['skills_invoked_top']}",
        "",
        "## Tools invoked (top)",
        "",
        f"Round A: {agg_a['tools_invoked_top']}",
        f"Round B: {agg_b['tools_invoked_top']}",
        "",
        render_round("A", round_a),
        "",
        render_round("B", round_b),
    ]
    Path(args.report).write_text("\n".join(md), encoding="utf-8")
    Path(args.report).with_suffix(".json").write_text(
        json.dumps({"round_a": round_a, "round_b": round_b,
                    "agg_a": agg_a, "agg_b": agg_b},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\nreport: {args.report}", flush=True)
    print(f"json:   {args.report.replace('.md', '.json')}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
