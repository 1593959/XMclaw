"""Migrate memory.db kind=lesson + kind=persona_manual rows → v2 facts.

Wave-27 Phase 3c (2026-05-16): one-shot upgrade utility for users
who accumulated persona content in the LEGACY memory.db before
Phase 3a/b moved it to v2. Without this migration, upgrading
users would see their old lessons / hand-edited persona files
disappear from the new render path.

Idempotent: running this twice doesn't double-insert. The v2
write path uses deterministic ids:

  * lesson rows  → ``Fact.compute_id(kind, scope, text)``
                   so re-running merges into the same row.
  * persona_manual → ``persona_manual:session:<sha1(basename)[:12]>``
                   so re-running OVERWRITES with the latest body.

Usage::

    # Dry-run (recommended first):
    python scripts/migrate_memory_db_to_v2.py

    # Actually write:
    python scripts/migrate_memory_db_to_v2.py --execute

The daemon must be RUNNING — the script POSTs via the HTTP API so
the live MemoryService handles embedding + dedup.
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


if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


DEFAULT_BASE = "http://127.0.0.1:8765"
DEFAULT_DB = Path.home() / ".xmclaw" / "v2" / "memory.db"
DEFAULT_TOKEN_FILE = Path.home() / ".xmclaw" / "v2" / "pairing_token.txt"


# ── memory.db schema probe ───────────────────────────────────────


def _scan_rows(db_path: Path) -> tuple[list[dict], list[dict]]:
    """Return ``(lesson_rows, persona_manual_rows)`` from memory.db.

    Skips rows with empty / malformed metadata. Lesson rows carry
    a ``bucket`` field (workflow / tool_quirks / etc.); persona
    manual rows carry a ``file`` field (basename).
    """
    con = sqlite3.connect(str(db_path))
    try:
        cur = con.cursor()
        rows = cur.execute(
            "SELECT id, layer, text, metadata, ts, "
            "evidence_count, confidence "
            "FROM memory_items"
        ).fetchall()
    finally:
        con.close()

    lessons: list[dict] = []
    manuals: list[dict] = []
    for rid, layer, text, md, ts, ev, conf in rows:
        if not isinstance(text, str) or not text.strip():
            continue
        try:
            meta = json.loads(md) if isinstance(md, str) else (md or {})
        except (json.JSONDecodeError, TypeError):
            meta = {}
        if not isinstance(meta, dict):
            continue
        kind = meta.get("kind")
        if kind == "lesson":
            bucket = meta.get("bucket")
            if not bucket or not isinstance(bucket, str):
                # Lesson without bucket → can't route; skip rather
                # than guess.
                continue
            lessons.append({
                "memory_id": rid,
                "text": text.strip(),
                "bucket": bucket,
                "evidence_count": ev or 1,
                "confidence": conf or 0.7,
                "ts": ts,
            })
        elif kind == "persona_manual":
            basename = meta.get("file")
            if not basename or not isinstance(basename, str):
                continue
            manuals.append({
                "memory_id": rid,
                "text": text.strip(),
                "basename": basename,
                "ts": ts,
            })
    return lessons, manuals


# ── HTTP helpers ─────────────────────────────────────────────────


def _headers(token_file: Path) -> dict[str, str]:
    tok = token_file.read_text(encoding="utf-8").strip()
    return {
        "Authorization": f"Bearer {tok}",
        "Content-Type": "application/json",
    }


def _post_fact(
    *, base: str, headers: dict, text: str, bucket: str,
    confidence: float, timeout: float = 30.0,
) -> tuple[bool, dict | None]:
    """POST a lesson fact via /api/v2/memory/v2/facts.

    Sends ``kind=lesson, scope=project`` with the bucket as
    metadata. The router routes the POST to MemoryService.remember()
    which handles dedup + bucket persistence.
    """
    r = httpx.post(
        f"{base}/api/v2/memory/v2/facts",
        json={
            "text": text,
            "kind": "lesson",
            "scope": "project",
            "confidence": confidence,
            "bucket": bucket,
        },
        headers=headers, timeout=timeout,
    )
    if r.status_code != 200:
        return False, {"status_code": r.status_code, "body": r.text[:200]}
    try:
        return True, r.json()
    except Exception:  # noqa: BLE001
        return True, None


def _put_persona_manual(
    *, base: str, headers: dict, basename: str, body: str,
    timeout: float = 30.0,
) -> tuple[bool, dict | None]:
    """PUT manual content via /api/v2/profiles/active/{basename}.

    The route (Phase 3b) already routes through v2 when wired,
    calling upsert_persona_manual + render_persona_file.
    """
    r = httpx.put(
        f"{base}/api/v2/profiles/active/{basename}",
        json={"content": body},
        headers=headers, timeout=timeout,
    )
    if r.status_code != 200:
        return False, {"status_code": r.status_code, "body": r.text[:200]}
    try:
        return True, r.json()
    except Exception:  # noqa: BLE001
        return True, None


# ── Main ─────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Migrate memory.db persona rows → v2 facts",
    )
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--token-file", type=Path, default=DEFAULT_TOKEN_FILE)
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument(
        "--execute", action="store_true",
        help="Actually POST to v2. Default is dry-run.",
    )
    ap.add_argument("--timeout", type=float, default=30.0)
    args = ap.parse_args()

    if not args.db.exists():
        print(f"[error] memory.db not found at {args.db}")
        return 1

    lessons, manuals = _scan_rows(args.db)
    print(f"scanned {args.db}:")
    print(f"  {len(lessons)} lesson row(s)")
    print(f"  {len(manuals)} persona_manual row(s)")

    if not lessons and not manuals:
        print("nothing to migrate — exiting.")
        return 0

    # Print a per-bucket breakdown for visibility.
    if lessons:
        buckets = Counter(l["bucket"] for l in lessons)
        print("\nlesson buckets:")
        for b, c in buckets.most_common():
            print(f"  {b:18}: {c}")
    if manuals:
        files = Counter(m["basename"] for m in manuals)
        print("\npersona_manual files:")
        for f, c in files.most_common():
            print(f"  {f:18}: {c}")

    if not args.execute:
        print(
            f"\n[dry-run] would migrate {len(lessons)} lessons + "
            f"{len(manuals)} manual sections. Re-run with --execute."
        )
        return 0

    if not args.token_file.exists():
        print(f"[error] token file missing: {args.token_file}")
        return 1
    headers = _headers(args.token_file)

    print(f"\n[execute] posting to {args.base} …")
    ok_count = 0
    fail_count = 0
    t0 = time.perf_counter()

    for i, l in enumerate(lessons, 1):
        ok, payload = _post_fact(
            base=args.base, headers=headers,
            text=l["text"], bucket=l["bucket"],
            confidence=float(l["confidence"]),
            timeout=args.timeout,
        )
        if ok:
            ok_count += 1
            print(f"  [lesson {i}/{len(lessons)}] ok bucket={l['bucket']!r}")
        else:
            fail_count += 1
            print(f"  [lesson {i}/{len(lessons)}] FAIL: {payload}")

    # For persona_manual we MUST collapse duplicates: legacy
    # memory.db can hold multiple rows per file (one per save);
    # the new path has exactly one. Pick the most recent ``ts``.
    by_file: dict[str, dict] = {}
    for m in manuals:
        prev = by_file.get(m["basename"])
        if prev is None or m["ts"] > prev["ts"]:
            by_file[m["basename"]] = m

    for i, (basename, row) in enumerate(by_file.items(), 1):
        ok, payload = _put_persona_manual(
            base=args.base, headers=headers,
            basename=basename, body=row["text"],
            timeout=args.timeout,
        )
        if ok:
            ok_count += 1
            print(f"  [manual {i}/{len(by_file)}] ok {basename}")
        else:
            fail_count += 1
            print(f"  [manual {i}/{len(by_file)}] FAIL {basename}: {payload}")

    elapsed = time.perf_counter() - t0
    print(
        f"\n=== done: {ok_count} ok / {fail_count} failed "
        f"in {elapsed:.1f}s ==="
    )
    print(
        "v2 write-time dedup will have collapsed duplicate lesson "
        "texts → check L1 events panel for the deduplicated row count."
    )
    return 0 if fail_count == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
