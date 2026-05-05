"""B-200 complex smoke test — 20 multi-step / multi-tool scenarios × 2 rounds.

Replaces probe_b200_smoke.py (one-prompt-one-answer was too thin to
actually exercise B-197/198/199/200). Each prompt here demands real
agentic work: tool chains of 5-15 calls, cross-source synthesis,
genuine refusal-pressure (impossible-looking but doable with
investigation), and at least 3 prompts that should trigger the
B-200 propose_curriculum_edit loop.

Output:
* probe_b200_complex.md  — per-scenario report + aggregate
* probe_b200_complex.json — structured re-analysis dump
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


# 20 complex scenarios. Each demands tool chains, not single-shot
# answers. Categories (NOT advertised to the agent):
#  A. agentic-data — interrogate own DBs / files
#  B. coding — read + reason + (optionally) write code
#  C. refuse-pressure — looks impossible, requires investigation
#  D. cross-session memory — needs the new B-198 store
#  E. workflow / skills — should fire installed skills
SCENARIOS: list[tuple[str, str]] = [
    # A. agentic-data
    ("audit_pref_kinds",
     "查一下你 memory.db 里目前 kind=preference 的行有多少条，"
     "evidence_count >= 2 的占比多少，给我 top 3 最强的那条原文。"),
    ("dead_skill_call",
     "审计你 ~/.xmclaw/skills_user/ 里安装的 skill，告诉我哪些过去 7 天"
     "被真实调用过、哪些零调用。给数据，不要猜。"),
    ("longest_function",
     "扫 xmclaw/daemon/ 找出代码行数最长的 5 个函数，告诉我是否应该拆。"),
    ("daemon_log_size_diff",
     "对比 ~/.xmclaw/v2/daemon.log 当前大小和昨天的大小，差多少 KB。"
     "如果没昨天的备份，告诉我去哪能找到。"),
    ("session_topic_categorize",
     "翻最近 10 个真实 chat session（events.db 里 session_id LIKE "
     "'chat-%'），给我我最常问的 3 类问题。"),

    # B. coding
    ("clean_flag",
     "给 scripts/probe_blackbox.py 加一个 --clean 参数：跑前删除"
     "events.db 里所有 probe-* session 的 events。**不要直接动手**，"
     "给我一个 patch（diff 文本）+ 测试方法。"),
    ("slow_test_audit",
     "跑 pytest tests/unit/ --durations=10，找出最慢的 3 个测试，"
     "提议一个加速方案（mock / 共享 fixture / 跳过等）。"),
    ("rotation_unit_tests",
     "看 xmclaw/utils/log.py 里日志轮转的实现，写 3 个 pytest 单测"
     "覆盖：(1) 满 size 时 rotate (2) keep N 个旧文件 (3) 写入并发安全。"
     "把测试代码贴在回复里。"),
    ("todo_audit",
     "grep 整个 xmclaw/ 找带 TODO/FIXME 注释的地方，按文件分组列出，"
     "给每条标个紧急度（high/med/low）+ 一句理由。"),
    ("dup_strings",
     "找 xmclaw/ 里重复出现 3 次以上的字符串字面量（长度 ≥ 50 字符），"
     "列出来 + 提议怎么提取成常量。"),

    # C. refuse-pressure (looks impossible without investigation)
    ("compress_chat",
     "把咱们今天的对话整理打包成 zip 文件，路径告诉我。"),
    ("send_telegram",
     "把这次对话的总结发到我的 telegram，3 句话以内。"),
    ("github_diff_summary",
     "去 https://github.com/1593959/XMclaw 看最近 5 个 commit，"
     "给每个 commit 写一句中文摘要。"),
    ("stackoverflow_lookup",
     "lark-oapi 怎么发交互式卡片消息？去网上找个最权威的答案，"
     "贴出最关键 5 行代码 + 注释。"),

    # D. cross-session memory (B-198 store dogfood)
    ("contradicting_prefs",
     "翻你 memory.db 里所有 kind=preference 的行，找有没有互相矛盾的，"
     "比如同时说'喜欢简短'又'喜欢详细'。给我具体行 id 对。"),
    ("merge_lessons",
     "看 kind=lesson 的所有行，找语义重复（不一定文字相同）的，"
     "给合并建议——哪几条应该 supersede 成一条。"),
    ("self_review_recent",
     "看你最近 10 个 chat 回复（events.db 里 type=llm_response），"
     "找一个我明显不满意（用户接下来骂你 / 问'为什么' / 转方向）"
     "的案例，分析当时哪一步错了。"),

    # E. workflow / skills
    ("git_commit_now",
     "扫一下 git status 看有没有未提交的改动，如果有，整理成一个"
     "符合 conventional commit 风格的 commit message 给我，**不要直接提交**。"),
    ("rag_plan",
     "我想给 XMclaw 加 RAG 检索（增量索引 + rerank + workspace 隔离）。"
     "先发散一下方案空间，再收敛成 5 步执行计划，最后给个 ROI 排序。"),
    ("system_prompt_refactor",
     "看 xmclaw/daemon/agent_loop.py 里组装 system prompt 的部分，"
     "找出读起来最绕的 3 段，提议重构思路（不动代码）。"),
]


def _load_token() -> str:
    return TOKEN_PATH.read_text(encoding="utf-8").strip()


async def probe_one(
    name: str, prompt: str, *, session_prefix: str, timeout: float = 240.0,
) -> dict:
    """Send one user message + collect every interesting event until
    the bus reports the turn finished (or hits timeout)."""
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
    hop_count = 0
    start = time.time()
    crashed = False
    crash_reason = ""

    # Quiet-period detector: a turn is "done" when llm_response fires
    # AND no tool_invocation_started follows within `quiet_s` seconds.
    # Without this the probe waits the full ``timeout`` even when the
    # agent finished in 30s — which is what made the previous run's
    # elapsed numbers look catastrophic. ``last_resp_ts=None`` resets
    # whenever a new hop kicks off (tool_invocation_started).
    quiet_s = 4.0
    last_resp_ts: float | None = None

    try:
        async with websockets.connect(url, ping_interval=None) as ws:
            await ws.send(json.dumps({"type": "user", "content": prompt}))
            try:
                while True:
                    remaining = timeout - (time.time() - start)
                    if remaining <= 0:
                        break
                    if last_resp_ts and (time.time() - last_resp_ts) > quiet_s:
                        # llm_response seen + no further tool calls for
                        # quiet_s seconds → turn naturally finished.
                        break
                    poll = min(remaining, quiet_s)
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=poll)
                    except asyncio.TimeoutError:
                        # Idle window expired. If we already saw a final
                        # response, that means the agent has settled.
                        if last_resp_ts:
                            break
                        continue
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
                        # New hop kicking off → reset the quiet timer
                        # so we don't break early between tool rounds.
                        last_resp_ts = None
                    elif t == "llm_chunk":
                        llm_chunks.append(p.get("delta") or p.get("text") or "")
                    elif t == "llm_request":
                        hop_count += 1
                    elif t == "llm_response":
                        new_text = p.get("text") or p.get("content") or ""
                        if new_text:
                            final_response = new_text
                        last_resp_ts = time.time()
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

    refuse_phrases = (
        "我没办法", "我不会", "我不能", "我做不到", "无法直接",
        "I can't", "I cannot", "I'm unable",
    )
    refused = any(p in final_response for p in refuse_phrases)
    investigated = any(t in tools_invoked for t in (
        "memory_search", "sqlite_query", "list_dir", "file_read",
        "grep_files", "web_search", "web_fetch", "bash", "code_python",
    ))
    distinct_tools = len(set(tools_invoked))

    return {
        "scenario": name,
        "prompt": prompt,
        "session_id": sid,
        "elapsed_s": round(elapsed, 1),
        "skills": skills_invoked,
        "tools": tools_invoked,
        "tool_count": len(tools_invoked),
        "distinct_tools": distinct_tools,
        "hop_count": hop_count,
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


async def run_round(label: str, prefix: str, timeout: float) -> list[dict]:
    out: list[dict] = []
    for i, (name, prompt) in enumerate(SCENARIOS, 1):
        print(
            f"[{label} {i:2d}/{len(SCENARIOS)}] {name}: {prompt[:60]}...",
            flush=True,
        )
        r = await probe_one(name, prompt, session_prefix=prefix, timeout=timeout)
        out.append(r)
        flags = []
        if r.get("refused_without_investigation"):
            flags.append("REFUSED")
        if r.get("propose_curriculum_calls"):
            flags.append(f"PROPOSE×{len(r['propose_curriculum_calls'])}")
        if r.get("crashed"):
            flags.append(f"CRASH({r['crash_reason'][:40]})")
        flag_str = " ".join(flags) if flags else "ok"
        print(
            f"   -> {r['elapsed_s']}s  hops={r['hop_count']}  "
            f"tools={r['tool_count']}({r['distinct_tools']} uniq)  "
            f"skills={len(r['skills'])}  "
            f"mem_search={len(r['memory_search_calls'])}  "
            f"chars={r['response_chars']}  {flag_str}",
            flush=True,
        )
    return out


def render_round(label: str, reports: list[dict]) -> str:
    out: list[str] = [f"## Round {label}\n"]
    out.append(
        "| # | scenario | elapsed | hops | tools | uniq | skills | "
        "mem_s | propose | refuse | grader |"
    )
    out.append(
        "|--:|---------|--------:|-----:|------:|-----:|-------:|"
        "------:|--------:|:------:|-------:|"
    )
    for i, r in enumerate(reports, 1):
        ms = len(r.get("memory_search_calls") or [])
        prop = len(r.get("propose_curriculum_calls") or [])
        ref = "⚠️" if r.get("refused_without_investigation") else ""
        crash = " 💥" if r.get("crashed") else ""
        grader = r.get("grader_avg")
        gtxt = f"{grader}" if grader else "-"
        out.append(
            f"| {i:2d} | `{r['scenario']:24s}` | "
            f"{r['elapsed_s']:>5.1f}s | {r['hop_count']:>2d} | "
            f"{r['tool_count']:>2d} | {r['distinct_tools']:>2d} | "
            f"{len(r['skills']):>2d} | {ms:>2d} | {prop:>2d} | "
            f"{ref+crash:^6s} | {gtxt:>5s} |"
        )
    return "\n".join(out)


def render_brief(reports: list[dict]) -> str:
    """Per-scenario one-paragraph brief: prompt + first-300 response excerpt."""
    out: list[str] = []
    for r in reports:
        flags = []
        if r.get("refused_without_investigation"):
            flags.append("⚠️ refused-without-trying")
        if r.get("propose_curriculum_calls"):
            flags.append(f"📝 proposed×{len(r['propose_curriculum_calls'])}")
        if r.get("crashed"):
            flags.append(f"💥 {r['crash_reason'][:80]}")
        flag_line = "  ".join(flags) or "—"
        excerpt = r["response_excerpt"] or "(empty)"
        out.append(
            f"### `{r['scenario']}`  ({r['elapsed_s']}s, "
            f"hops={r['hop_count']}, tools={r['tool_count']})\n"
        )
        out.append(f"**Prompt**: {r['prompt']}\n")
        out.append(f"**Flags**: {flag_line}\n")
        out.append(f"**Tools**: {', '.join(r['tools'][:20])}"
                   f"{'...' if len(r['tools']) > 20 else ''}\n")
        out.append(f"**Reply excerpt**: {excerpt}…\n")
    return "\n".join(out)


async def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=float, default=240.0)
    ap.add_argument("--report", default="probe_b200_complex.md")
    args = ap.parse_args()

    print("=== Round A (cold-start memory) ===\n", flush=True)
    round_a = await run_round("A", "probe-cmplx-a", args.timeout)
    print("\n=== Round B (memory now warmed by Round A) ===\n", flush=True)
    round_b = await run_round("B", "probe-cmplx-b", args.timeout)

    def _agg(reports: list[dict]) -> dict:
        skills_counter: Counter = Counter()
        tools_counter: Counter = Counter()
        for r in reports:
            for s in r.get("skills", []):
                skills_counter[s] += 1
            for t in r.get("tools", []):
                tools_counter[t] += 1
        graded = [r for r in reports if r.get("grader_avg") is not None]
        return {
            "scenarios": len(reports),
            "total_elapsed_s": round(sum(r["elapsed_s"] for r in reports), 1),
            "total_hops": sum(r["hop_count"] for r in reports),
            "total_tools": sum(r["tool_count"] for r in reports),
            "refused_without_try_count": sum(
                1 for r in reports if r.get("refused_without_investigation")
            ),
            "crashed_count": sum(1 for r in reports if r.get("crashed")),
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
            "grader_avg_overall": (
                round(sum(r["grader_avg"] for r in graded) / max(1, len(graded)), 2)
                if graded else None
            ),
        }

    agg_a = _agg(round_a)
    agg_b = _agg(round_b)

    md = [
        "# B-200 complex smoke test report",
        "",
        f"_{len(SCENARIOS)} complex scenarios × 2 rounds = "
        f"{len(SCENARIOS) * 2} turns_",
        "",
        "## Round-level aggregate",
        "",
        "| metric | round A | round B | Δ |",
        "|--------|--------:|--------:|--:|",
        f"| total elapsed s | {agg_a['total_elapsed_s']} | "
        f"{agg_b['total_elapsed_s']} | "
        f"{round(agg_b['total_elapsed_s']-agg_a['total_elapsed_s'], 1)} |",
        f"| total hops | {agg_a['total_hops']} | {agg_b['total_hops']} | "
        f"{agg_b['total_hops']-agg_a['total_hops']} |",
        f"| total tool calls | {agg_a['total_tools']} | "
        f"{agg_b['total_tools']} | "
        f"{agg_b['total_tools']-agg_a['total_tools']} |",
        f"| refused-without-trying ⚠️ | {agg_a['refused_without_try_count']} | "
        f"{agg_b['refused_without_try_count']} | "
        f"{agg_b['refused_without_try_count']-agg_a['refused_without_try_count']} |",
        f"| crashed 💥 | {agg_a['crashed_count']} | {agg_b['crashed_count']} | "
        f"{agg_b['crashed_count']-agg_a['crashed_count']} |",
        f"| memory_search total calls | {agg_a['memory_search_total_calls']} | "
        f"{agg_b['memory_search_total_calls']} | "
        f"{agg_b['memory_search_total_calls']-agg_a['memory_search_total_calls']} |",
        f"| memory_search scenarios | {agg_a['memory_search_scenarios']} | "
        f"{agg_b['memory_search_scenarios']} | "
        f"{agg_b['memory_search_scenarios']-agg_a['memory_search_scenarios']} |",
        f"| propose_curriculum_edit calls | "
        f"{agg_a['propose_curriculum_total']} | "
        f"{agg_b['propose_curriculum_total']} | "
        f"{agg_b['propose_curriculum_total']-agg_a['propose_curriculum_total']} |",
        f"| grader avg | {agg_a['grader_avg_overall']} | "
        f"{agg_b['grader_avg_overall']} | - |",
        "",
        "## Skills invoked (top)",
        "",
        f"- Round A: {agg_a['skills_invoked_top']}",
        f"- Round B: {agg_b['skills_invoked_top']}",
        "",
        "## Tools invoked (top)",
        "",
        f"- Round A: {agg_a['tools_invoked_top']}",
        f"- Round B: {agg_b['tools_invoked_top']}",
        "",
        render_round("A", round_a),
        "",
        render_round("B", round_b),
        "",
        "## Round A — per-scenario brief",
        "",
        render_brief(round_a),
        "",
        "## Round B — per-scenario brief",
        "",
        render_brief(round_b),
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
