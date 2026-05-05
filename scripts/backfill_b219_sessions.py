"""B-219 backfill: rebuild sessions.db.session_history from events.db.

Symptom: user opened chat after daemon restart and saw ONLY 1 recent
session ("chat-e9d9c5aa"). The other 14+ chat-* sessions (chat-4fbd1d07
with 1034 events, chat-59bb7a7a with 801, …) still existed in events.db
but were missing from sessions.db.session_history.

Root cause: SessionStore.save() only fires on ``run_turn`` finalisation;
crashed turns / abrupt daemon kills / pre-store sessions never landed in
sessions.db. events.db has the canonical event log either way.

Fix: walk events.db for every session_id matching chat-*/feishu:*, replay
user_message + assistant llm_response events into a Message[] list,
write back into sessions.db via the SessionStore API. Idempotent — only
fills missing rows; existing entries are kept.

Usage::

    python scripts/backfill_b219_sessions.py [--overwrite]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--overwrite", action="store_true",
        help="Replace existing sessions.db rows too (default: skip).",
    )
    args = parser.parse_args()

    from xmclaw.daemon.session_store import SessionStore
    from xmclaw.providers.llm.base import Message
    from xmclaw.utils.paths import data_dir

    events_path = data_dir() / "v2" / "events.db"
    sessions_path = data_dir() / "v2" / "sessions.db"
    if not events_path.is_file():
        print(f"ERROR: events.db missing at {events_path}")
        return 2

    store = SessionStore(sessions_path)

    # Find every session_id that has at least 1 user_message in events.db.
    edb = sqlite3.connect(events_path)
    edb.row_factory = sqlite3.Row
    cur = edb.execute("""
        SELECT session_id, MAX(ts) AS last_ts, COUNT(*) AS n
        FROM events
        WHERE session_id LIKE 'chat-%' OR session_id LIKE 'feishu:%'
        GROUP BY session_id
        ORDER BY last_ts DESC
    """)
    sids = [(r["session_id"], r["last_ts"], r["n"]) for r in cur]
    if not sids:
        print("nothing to backfill — no chat-*/feishu:* sessions in events.db")
        edb.close()
        return 0

    # Existing rows in sessions.db
    existing_cur = sqlite3.connect(sessions_path).execute(
        "SELECT session_id FROM session_history"
    )
    existing_ids: set[str] = {r[0] for r in existing_cur}

    print(f"events.db has {len(sids)} chat sessions")
    print(f"sessions.db has {len(existing_ids)} existing rows")
    print()

    written = 0
    skipped = 0
    empty = 0
    for sid, last_ts, n_events in sids:
        if not args.overwrite and sid in existing_ids:
            skipped += 1
            continue

        # Replay user_message + llm-emitted text into a Message list.
        # Key insight: ``llm_response.content`` is empty on tool-only
        # hops; the actual visible text comes from llm_chunk events
        # (streamed). Aggregate chunks by correlation_id to recover
        # what the user saw on each hop. Then concatenate hops within
        # a turn (between user_messages) into one assistant Message.
        cur = edb.execute("""
            SELECT ts, type, payload, correlation_id FROM events
            WHERE session_id=?
              AND type IN ('user_message', 'llm_chunk', 'llm_response')
            ORDER BY ts ASC
        """, (sid,))
        msgs: list[Message] = []
        # Buffer for the assistant text being accumulated since last
        # user_message. Hops within a turn concatenate into one
        # Message so the chat sidebar preview reads naturally.
        chunks_by_corr: dict[str, list[str]] = {}
        for row in cur:
            try:
                p = json.loads(row["payload"])
            except json.JSONDecodeError:
                continue
            t = row["type"]
            if t == "user_message":
                # Flush the assistant buffer (if any) before starting
                # a fresh user turn.
                if chunks_by_corr:
                    flat = "\n\n".join(
                        "".join(parts) for parts in chunks_by_corr.values()
                        if any(part.strip() for part in parts)
                    )
                    if flat.strip():
                        msgs.append(Message(role="assistant", content=flat))
                    chunks_by_corr = {}
                content = p.get("content") or ""
                if content.strip():
                    msgs.append(Message(role="user", content=content))
            elif t == "llm_chunk":
                delta = p.get("delta") or ""
                if delta:
                    cid = row["correlation_id"] or "default"
                    chunks_by_corr.setdefault(cid, []).append(delta)
            elif t == "llm_response":
                # Some hops emit final text only on llm_response (no
                # streaming). Add as a fallback chunk row.
                content = p.get("content") or ""
                if content.strip() and p.get("ok"):
                    cid = row["correlation_id"] or "default"
                    # Avoid double-counting if chunks already have it.
                    existing = "".join(chunks_by_corr.get(cid, []))
                    if content not in existing:
                        chunks_by_corr.setdefault(cid, []).append(content)
        # Tail flush: assistant buffer from the LAST turn.
        if chunks_by_corr:
            flat = "\n\n".join(
                "".join(parts) for parts in chunks_by_corr.values()
                if any(part.strip() for part in parts)
            )
            if flat.strip():
                msgs.append(Message(role="assistant", content=flat))
        if not msgs:
            empty += 1
            continue

        ts_str = time.strftime("%m-%d %H:%M", time.localtime(last_ts))
        store.save(sid, msgs)
        written += 1
        print(f"  [ok] {sid:50} msgs={len(msgs):>3}  last={ts_str}")

    edb.close()
    print()
    print(f"backfill done: wrote {written}, skipped (already present) {skipped}, "
          f"empty (no msg events) {empty}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
