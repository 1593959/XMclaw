"""One-shot migration: strip GOAL-ANCHOR phantom user messages from
the live sessions.db.

Companion to commit e2ec59e (drop GOAL-ANCHOR scaffolding from
persisted history). That commit stops NEW pollution from being
written; this script heals EXISTING damage.

Backups the DB before touching it. Safe to re-run — idempotent
(messages matching the marker are already gone after the first
pass).

Usage (after stopping the daemon):

    python scripts/one_shot_strip_goal_anchor.py            # apply
    python scripts/one_shot_strip_goal_anchor.py --dry-run  # show plan
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import time


def _default_db_path() -> str:
    return os.path.join(
        os.path.expanduser("~"), ".xmclaw", "v2", "sessions.db",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--db", default=_default_db_path())
    args = parser.parse_args()

    src = args.db
    if not os.path.exists(src):
        print(f"sessions.db not found at {src}", file=sys.stderr)
        return 1

    con = sqlite3.connect(src)
    try:
        rows = con.execute(
            "SELECT session_id, history_json FROM session_history "
            "WHERE history_json LIKE '%[GOAL-ANCHOR]%'",
        ).fetchall()
        print(f"sessions with GOAL-ANCHOR pollution: {len(rows)}")
        if not rows:
            print("nothing to do")
            return 0

        total_before, total_after = 0, 0
        plan: list[tuple[str, str, int]] = []
        for sid, hjson in rows:
            msgs = json.loads(hjson)
            kept = [
                m for m in msgs
                if not (
                    m.get("role") == "user"
                    and (m.get("content") or "").lstrip().startswith(
                        "[GOAL-ANCHOR]",
                    )
                )
            ]
            new_json = json.dumps(kept, ensure_ascii=False)
            before_bytes, after_bytes = len(hjson), len(new_json)
            print(
                f"  {sid}: msgs {len(msgs)}->{len(kept)} "
                f"bytes {before_bytes}->{after_bytes} "
                f"(-{before_bytes - after_bytes})",
            )
            total_before += before_bytes
            total_after += after_bytes
            plan.append((sid, new_json, len(kept)))

        saved = total_before - total_after
        pct = (saved / total_before * 100) if total_before else 0
        print(
            f"\nTOTAL: {total_before} -> {total_after} bytes "
            f"(saved {saved}, {pct:.1f}%)",
        )

        if args.dry_run:
            print("(dry-run only — no writes performed)")
            return 0

        bak = src + ".before-anchor-cleanup-" + time.strftime(
            "%Y%m%d-%H%M%S",
        ) + ".bak"
        shutil.copy2(src, bak)
        print(f"\nbacked up to {bak} ({os.path.getsize(bak)} bytes)")

        now = time.time()
        for sid, new_json, count in plan:
            con.execute(
                "UPDATE session_history SET history_json=?, "
                "message_count=?, updated_at=? WHERE session_id=?",
                (new_json, count, now, sid),
            )
        con.commit()
        print(f"updated {len(plan)} rows")
        con.execute("VACUUM")
        print(f"vacuumed; final db size: {os.path.getsize(src)} bytes")
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
