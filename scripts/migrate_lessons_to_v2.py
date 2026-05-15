"""Migrate ``memory.db`` kind=lesson rows into v2 facts (LanceDB).

One-shot backfill. The new dual-write path in
``_write_facts_to_memory`` covers future lessons going forward; this
script walks the existing ``~/.xmclaw/v2/memory.db`` rows and re-plays
them through ``MemoryService.remember()`` so they enter the v2 dedup
pipeline (write-time merge collapses paraphrases automatically).

Idempotent: re-running merges hits the same ids in v2 (compute_id is
deterministic on kind+scope+text), so a second pass just bumps
evidence_count instead of double-inserting.

Drops:
  - ``bucket`` (workflow / tool_quirks / failure_modes / values /
    rules) — v2 Fact has no first-class bucket field; the dedup
    point of view is "same text ⇒ same fact regardless of which
    extractor bucket landed it". memory.db rows are NOT deleted —
    persona MD render path keeps reading them.
  - ``layer`` mapped: ``long_term`` → ``long_term``; everything
    else (``working`` / ``short_term``) → ``working``. v2 doesn't
    have a short-term layer; auto-promotion will lift to long_term
    when evidence_count crosses the threshold.

Usage::

    python scripts/migrate_lessons_to_v2.py            # dry run
    python scripts/migrate_lessons_to_v2.py --execute  # write

Requires the daemon to be RUNNING (talks to ``/api/v2/memory/v2/facts``
POST) so the live MemoryService handles embedding + dedup — avoids
having to construct the service stack standalone.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

import httpx

# Windows GBK stdout doesn't encode CJK — force utf-8 so the report
# is readable from a stock cmd / PowerShell.
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


DEFAULT_BASE = "http://127.0.0.1:8765"
DEFAULT_DB = Path.home() / ".xmclaw" / "v2" / "memory.db"
DEFAULT_TOKEN_FILE = Path.home() / ".xmclaw" / "v2" / "pairing_token.txt"


def _load_token(token_file: Path) -> str:
    return token_file.read_text(encoding="utf-8").strip()


def _scan_lessons(db_path: Path) -> list[dict]:
    con = sqlite3.connect(str(db_path))
    try:
        cur = con.cursor()
        rows = cur.execute(
            "SELECT id, layer, text, metadata, ts, evidence_count, confidence "
            "FROM memory_items"
        ).fetchall()
    finally:
        con.close()

    out = []
    for rid, layer, text, md, ts, ev, conf in rows:
        try:
            m = json.loads(md) if isinstance(md, str) else (md or {})
        except (json.JSONDecodeError, TypeError):
            m = {}
        if not isinstance(m, dict):
            continue
        if m.get("kind") != "lesson":
            continue
        if not isinstance(text, str) or not text.strip():
            continue
        out.append({
            "id": rid,
            "layer": layer,
            "text": text.strip(),
            "metadata": m,
            "ts": ts,
            "evidence_count": ev or 1,
            "confidence": conf or 0.7,
        })
    return out


def _post_fact(
    *, base: str, token: str, text: str, confidence: float, timeout: float,
) -> tuple[bool, dict | None]:
    r = httpx.post(
        f"{base}/api/v2/memory/v2/facts",
        json={
            "text": text,
            "kind": "lesson",
            "scope": "project",
            "confidence": confidence,
        },
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        timeout=timeout,
    )
    if r.status_code != 200:
        return False, {"status_code": r.status_code, "body": r.text[:200]}
    try:
        return True, r.json()
    except Exception:  # noqa: BLE001
        return True, None


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Backfill memory.db kind=lesson rows into v2 facts",
    )
    ap.add_argument("--db", type=Path, default=DEFAULT_DB,
                    help=f"memory.db path (default: {DEFAULT_DB})")
    ap.add_argument("--token-file", type=Path, default=DEFAULT_TOKEN_FILE,
                    help="pairing token file")
    ap.add_argument("--base", default=DEFAULT_BASE,
                    help="daemon base URL")
    ap.add_argument("--execute", action="store_true",
                    help="actually write to v2; default is dry-run")
    ap.add_argument("--timeout", type=float, default=30.0)
    args = ap.parse_args()

    if not args.db.exists():
        print(f"[error] memory.db not found at {args.db}")
        return 1

    lessons = _scan_lessons(args.db)
    print(f"scanned {args.db}: found {len(lessons)} lesson row(s)")
    if not lessons:
        return 0

    buckets = Counter(
        (l["metadata"].get("bucket") or "(none)") for l in lessons
    )
    print("\nbucket distribution:")
    for b, c in buckets.most_common():
        print(f"  {b}: {c}")

    print("\nsample (first 3):")
    for l in lessons[:3]:
        print(f"  [{l['metadata'].get('bucket')}] {l['text'][:80]!r}")

    if not args.execute:
        print(
            f"\n[dry-run] {len(lessons)} lesson(s) would be POSTed to "
            f"{args.base}/api/v2/memory/v2/facts. Re-run with --execute."
        )
        return 0

    token = _load_token(args.token_file)
    print(f"\n[execute] posting to {args.base} …")
    ok_count = 0
    fail_count = 0
    t0 = time.perf_counter()
    for i, l in enumerate(lessons, 1):
        ok, payload = _post_fact(
            base=args.base, token=token,
            text=l["text"], confidence=float(l["confidence"]),
            timeout=args.timeout,
        )
        if ok:
            ok_count += 1
            new_id = (
                payload.get("created", {}).get("id", "?")
                if isinstance(payload, dict) else "?"
            )
            print(f"  [{i}/{len(lessons)}] ok → {str(new_id)[:32]}")
        else:
            fail_count += 1
            print(f"  [{i}/{len(lessons)}] FAIL: {payload}")
    elapsed = time.perf_counter() - t0
    print(
        f"\n=== done: {ok_count} ok / {fail_count} failed "
        f"in {elapsed:.1f}s ==="
    )
    print(
        "v2 write-time near-dup merge will have collapsed any "
        "duplicate texts — check /api/v2/memory/v2/facts?kind=lesson "
        "for the deduplicated row count."
    )
    return 0 if fail_count == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
