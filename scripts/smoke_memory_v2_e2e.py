"""End-to-end smoke test: realistic user message -> daemon -> L1 facts.

Run against a live daemon (port 8766 by default). Verifies the
two-layer extraction (regex + LLM) catches everything from a
real-world business-onboarding message.

Usage:
    python scripts/smoke_memory_v2_e2e.py
    python scripts/smoke_memory_v2_e2e.py --port 8766

Exits non-zero on any verification failure so it can wire into CI
smoke gates later.
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import websockets

# Force stdout/stderr to UTF-8 on Windows so emoji + CJK don't crash
# the print() chain. cp936 / gbk default eats non-BMP and unicode marks.
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")


# ── Test corpus — realistic onboarding-style messages ────────────


SCENARIOS = [
    {
        "id": "onboarding-payment-shop",
        "user_message": (
            "我是干陪玩店的，公司名叫 NimbusGames，"
            "网站是 https://pw310.wxselling.com/admin，"
            "后台账号 admin，密码 admin888，"
            "客服热线 400-800-1234，企业邮箱 contact@nimbus.cn，"
            "微信号 nimbus_cs，我老婆叫小红负责运营，"
            "技术栈是 Python + FastAPI + PostgreSQL，"
            "代码在 C:\\Users\\me\\nimbus\\src，"
            "预算 5 万人民币，截止日期 6月底前，"
            "目标月流水破 10 万。"
            "记住这些信息，以后都默认按陪玩店业务理解。"
        ),
        "expected_patterns": [
            "url", "credential", "cred_pair", "goal",
            "remember_directive", "phone", "email", "social",
            "stack", "path", "deadline", "money", "relationship",
            "org",
        ],
        # Substring matches that SHOULD appear in at least one fact text
        "must_contain_some": [
            "pw310.wxselling.com",
            "admin",
            "5 万",
            "400-800-1234",
            "contact@nimbus.cn",
            "nimbus_cs",
            "Python",
            "陪玩店",
        ],
    },
    {
        "id": "soft-preferences",
        "user_message": (
            "我不喜欢太啰嗦的回答，回复尽量精简。"
            "我用 Windows 11，喜欢用 PowerShell，不要再让我装 bash。"
            "我习惯每天早上 9 点处理邮件，"
            "永远别在下午 6 点之后给我提醒。"
        ),
        "expected_patterns": [
            "correction",  # 不喜欢 / 不要再 / 永远别
            "constraint",  # 永远别
            "preference",  # 喜欢用 PowerShell
            "identity",    # Windows 11
            "datetime",    # 每天早上 9 点 / 下午 6 点
        ],
        "must_contain_some": [
            "PowerShell",
            "Windows 11",
            "永远",
        ],
    },
    {
        "id": "team-and-deadlines",
        "user_message": (
            "我同事 Alice 负责前端，我老板 Bob 想看 demo。"
            "Q3 末之前必须上线，每周三复盘会议下午 3 点。"
            "GitHub 仓库 acme-team/nimbus，Telegram @nimbus_dev。"
        ),
        "expected_patterns": [
            "relationship",  # 我同事 Alice / 我老板 Bob
            "constraint",    # 必须上线
            "deadline",      # Q3 末
            "datetime",      # 每周三 下午 3 点
            "social",        # github / telegram
        ],
        "must_contain_some": [
            "Alice",
            "Bob",
            "Q3",
            "acme-team",
        ],
    },
]


# ── Helpers ──────────────────────────────────────────────────────


def get_pairing_token() -> str:
    token_path = Path.home() / ".xmclaw" / "v2" / "pairing_token.txt"
    if token_path.is_file():
        return token_path.read_text(encoding="utf-8").strip()
    return ""


async def daemon_is_up(port: int, token: str) -> bool:
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            r = await client.get(
                f"http://127.0.0.1:{port}/api/v2/status",
                params={"token": token} if token else None,
            )
            return r.status_code == 200
        except httpx.HTTPError as exc:
            print(f"daemon_is_up: httpx error {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            return False
        except Exception as exc:
            print(f"daemon_is_up: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            return False


async def memory_v2_status(port: int, token: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(
            f"http://127.0.0.1:{port}/api/v2/memory/v2/status",
            params={"token": token} if token else None,
        )
        return r.json()


async def list_facts(port: int, token: str, q: str = "") -> list[dict[str, Any]]:
    params: dict[str, Any] = {"limit": 200}
    if token:
        params["token"] = token
    if q:
        params["q"] = q
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(
            f"http://127.0.0.1:{port}/api/v2/memory/v2/facts",
            params=params,
        )
        return r.json().get("facts", [])


async def post_fact_extract(
    port: int, token: str, message: str,
) -> list[dict[str, Any]]:
    """For testing the extractor directly (no LLM round-trip) — we
    call the WS chat path to drive the full pipeline.

    Skipped here because the WS path requires a live agent + LLM
    call which has variable latency. Instead, we drive the extractor
    by POSTing directly to /memory/v2/facts after extracting locally.

    NOT USED in this smoke script — we want the real daemon hook to
    fire, so we send a real chat message via WS instead. See
    ``send_chat_via_ws`` below.
    """
    raise NotImplementedError


async def send_chat_via_ws(
    port: int, token: str, session_id: str, message: str,
    *, timeout_s: float = 90.0,
) -> dict[str, Any]:
    """Open WS, send one user frame, wait for the assistant turn to
    complete (or timeout). Returns the captured event stream summary."""
    url = f"ws://127.0.0.1:{port}/agent/v2/{session_id}"
    if token:
        url += f"?token={token}"
    events: list[dict[str, Any]] = []
    deadline = time.time() + timeout_s
    async with websockets.connect(url, max_size=None) as ws:
        await ws.send(json.dumps({"type": "user", "content": message}))
        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
            except asyncio.TimeoutError:
                # No events for 5s after the user message — the turn
                # has likely settled.
                break
            try:
                ev = json.loads(raw)
                events.append(ev)
                # llm_response signals end of turn for the simple
                # path; tool loops emit multiple llm_responses, so
                # we keep waiting for quiet.
            except json.JSONDecodeError:
                continue
    return {"count": len(events), "events": events[-5:]}


# ── Scenario runner ──────────────────────────────────────────────


async def run_scenario(
    scen: dict[str, Any], port: int, token: str,
) -> dict[str, Any]:
    session_id = f"smoke-v2-{scen['id']}-{int(time.time())}"
    before = await list_facts(port, token)
    before_ids = {f["id"] for f in before}

    print(f"\n=== {scen['id']} ===")
    print(f"  msg: {scen['user_message'][:80]}...")
    print(f"  facts before: {len(before)}")

    # Send via WS — drives the full agent_loop path including
    # Phase 3b regex hook + Phase 3.2 async LLM extraction.
    t0 = time.time()
    chat = await send_chat_via_ws(port, token, session_id, scen["user_message"])
    elapsed = time.time() - t0
    print(f"  chat round-trip: {elapsed:.1f}s, {chat['count']} events")

    # Layer 2 LLM extraction is a background task — give it a moment.
    await asyncio.sleep(10.0)

    after = await list_facts(port, token)
    new_facts = [f for f in after if f["id"] not in before_ids]
    new_texts = [f["text"] for f in new_facts]
    print(f"  facts after: {len(after)} (+{len(new_facts)} new)")

    # Validate: check the entire fact store, not just new — facts
    # land idempotently on repeat, so a substring may be on an
    # older row with bumped evidence_count rather than a new row.
    all_texts = [f["text"] for f in after]
    missing_substrings = [
        s for s in scen["must_contain_some"]
        if not any(s in t for t in all_texts)
    ]

    # Diagnostic dump
    if missing_substrings:
        print("  WARN missing substrings:", missing_substrings)
        print("  new facts dump:")
        for f in new_facts[:20]:
            print(f"    - [{f['kind']}/{f['scope']}] {f['text'][:100]}")
    else:
        print(f"  OK all must_contain_some present in store "
              f"(across {len(new_facts)} new + existing)")

    return {
        "id": scen["id"],
        "elapsed_s": elapsed,
        "new_fact_count": len(new_facts),
        "new_facts": new_facts,
        "missing_substrings": missing_substrings,
        "passed": len(missing_substrings) == 0,
    }


async def main_async(port: int) -> int:
    token = get_pairing_token()
    if not await daemon_is_up(port, token):
        print(f"daemon not reachable at 127.0.0.1:{port}", file=sys.stderr)
        return 1

    status = await memory_v2_status(port, token)
    if not status.get("enabled"):
        print("memory_v2 not enabled — set "
              "cognition.memory_v2.enabled=true in daemon/config.json",
              file=sys.stderr)
        return 1

    print(f"daemon healthy, memory_v2 enabled, "
          f"embedder={status.get('embedder_name')} "
          f"dim={status.get('embedder_dim')} "
          f"current_fact_count={status.get('fact_count')}")

    results = []
    for scen in SCENARIOS:
        r = await run_scenario(scen, port, token)
        results.append(r)

    # ── Summary ──
    passed = sum(1 for r in results if r["passed"])
    total = len(results)
    print()
    print(f"=== summary: {passed}/{total} scenarios passed ===")
    for r in results:
        flag = "✓" if r["passed"] else "✗"
        print(f"  {flag} {r['id']}: +{r['new_fact_count']} facts, "
              f"{r['elapsed_s']:.1f}s, "
              f"missing={r['missing_substrings'] or '-'}")

    return 0 if passed == total else 1


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    sys.exit(asyncio.run(main_async(args.port)))


if __name__ == "__main__":
    main()
