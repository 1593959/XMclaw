"""Cleanup: delete all facts injected by the smoke test script.

Targets facts whose source_event_id starts with ``smoke-v2-`` (every
fact produced by ``scripts/smoke_memory_v2_e2e.py``) plus three
direct API POSTs made during manual testing.

Safe to run repeatedly — idempotent on rows that no longer exist.
"""
from __future__ import annotations

import asyncio
import io
import sys
from pathlib import Path

import httpx

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

TOKEN = (Path.home() / ".xmclaw" / "v2" / "pairing_token.txt").read_text(
    encoding="utf-8",
).strip()
BASE = "http://127.0.0.1:8765/api/v2/memory/v2"

DIRECT_POST_TEXTS = {
    "陪玩店业务: pw310.wxselling.com",
    "用户偏好简短回复",
    "决定用 LanceDB 不用 sqlite-vec",
}


async def list_all(c: httpx.AsyncClient) -> list[dict]:
    r = await c.get(
        f"{BASE}/facts",
        params={"token": TOKEN, "limit": 500},
    )
    if r.status_code != 200:
        return []
    data = r.json()
    return data.get("facts") or []


async def main() -> None:
    async with httpx.AsyncClient(timeout=30.0) as c:
        total_deleted = 0
        rounds = 0
        # Loop because list endpoint may cap at 500 and we might
        # have more than that (unlikely but defensive).
        while True:
            facts = await list_all(c)
            if not facts:
                print(f"No facts returned. Total deleted: {total_deleted}.")
                break

            print(f"\nRound {rounds + 1}: pulled {len(facts)} facts")
            to_delete = [
                f for f in facts
                if (f.get("source_event_id") or "").startswith("smoke-v2-")
                or (f.get("text") or "") in DIRECT_POST_TEXTS
            ]
            print(f"  marked for deletion: {len(to_delete)}")
            if not to_delete:
                print(f"\nDONE. Cleanup complete. Total deleted: {total_deleted}.")
                break

            round_deleted = 0
            for f in to_delete:
                fid = f["id"]
                try:
                    r = await c.delete(
                        f"{BASE}/facts/{fid}",
                        params={"token": TOKEN},
                    )
                    if r.status_code == 200:
                        round_deleted += 1
                except Exception as exc:
                    print(f"  err deleting {fid[:40]}: {exc}")
            total_deleted += round_deleted
            print(f"  deleted this round: {round_deleted}")

            if round_deleted == 0:
                print(f"\nNo progress — stopping. Total deleted: {total_deleted}.")
                break

            rounds += 1
            if rounds > 5:
                print(f"\nMax rounds reached. Total deleted: {total_deleted}.")
                break

        # Final tally
        r = await c.get(
            f"{BASE}/status", params={"token": TOKEN},
        )
        if r.status_code == 200:
            j = r.json()
            print(f"\nFinal fact_count in store: {j.get('fact_count')}")


asyncio.run(main())
