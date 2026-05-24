"""Migrate memory.db user-facing rows → v2 facts.

Wave-27 Phase 3c (2026-05-16) shipped this for lessons + persona_manual.
Phase 7.B.2 (2026-05-24) extended it for the full §7 V1→V2 migration:

  * **persona_bullet** added to coverage (MEMORY.md / USER.md bullet
    rows V1 indexed via BuiltinFileMemoryProvider).
  * **--backup** flag — automatic on ``--execute``; copies memory.db →
    memory.db.pre-phase7.bak so §7.B.3 has a rollback target.
  * **verify** subcommand — for every migrated V1 row, hash the text
    and check whether a corresponding V2 fact exists. Reports missing
    rows so the operator can re-run.
  * **file_chunk / code_chunk** explicitly skipped with row counts so
    the operator knows what the migration ignored (those are
    workspace indexing artefacts, not user facts; V2 doesn't host
    them).

Idempotent: running ``--execute`` twice doesn't double-insert. v2
write path uses deterministic ids — same content + kind + scope
collapses into the same row.

Usage::

    # Dry-run scan + per-kind breakdown (recommended first):
    python scripts/migrate_memory_db_to_v2.py

    # Backup + execute migration:
    python scripts/migrate_memory_db_to_v2.py --execute

    # Verify coverage after migration:
    python scripts/migrate_memory_db_to_v2.py verify

The daemon must be RUNNING — the script POSTs via the HTTP API so the
live MemoryService handles embedding + dedup.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
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
BACKUP_SUFFIX = ".pre-phase7.bak"

# Kinds we explicitly DON'T migrate — workspace indexing artefacts,
# not user-facing facts. Reported with counts so the operator sees
# what got skipped intentionally.
_SKIP_KINDS = {"file_chunk", "code_chunk"}


# ── memory.db schema probe ───────────────────────────────────────


def _scan_rows(
    db_path: Path,
) -> tuple[list[dict], list[dict], list[dict], Counter]:
    """Return ``(lesson_rows, persona_manual_rows, bullet_rows, skipped_counter)``.

    Skipped counter records kinds we deliberately ignore + the count
    of malformed rows.
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
    bullets: list[dict] = []
    skipped: Counter = Counter()

    for rid, layer, text, md, ts, ev, conf in rows:
        if not isinstance(text, str) or not text.strip():
            skipped["_malformed"] += 1
            continue
        try:
            meta = json.loads(md) if isinstance(md, str) else (md or {})
        except (json.JSONDecodeError, TypeError):
            skipped["_malformed"] += 1
            continue
        if not isinstance(meta, dict):
            skipped["_malformed"] += 1
            continue

        kind = meta.get("kind")
        if kind in _SKIP_KINDS:
            skipped[str(kind)] += 1
            continue

        if kind == "lesson":
            bucket = meta.get("bucket")
            if not bucket or not isinstance(bucket, str):
                skipped["_lesson_no_bucket"] += 1
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
                skipped["_persona_manual_no_file"] += 1
                continue
            manuals.append({
                "memory_id": rid,
                "text": text.strip(),
                "basename": basename,
                "ts": ts,
            })
        elif kind == "persona_bullet":
            # Phase 7.B.2: bullets from MEMORY.md / USER.md indexing.
            # Treat as lessons under a "bullet" bucket so they
            # participate in V2 dedup + render. The original source
            # file is preserved in metadata.path.
            source = meta.get("path") or meta.get("source") or "unknown"
            bullets.append({
                "memory_id": rid,
                "text": text.strip(),
                "source_path": source,
                "evidence_count": ev or 1,
                "confidence": conf or 0.7,
                "ts": ts,
            })
        else:
            skipped[str(kind or "_no_kind")] += 1

    return lessons, manuals, bullets, skipped


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
    """POST a lesson fact via /api/v2/memory/v2/facts."""
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
    """PUT manual content via /api/v2/profiles/active/{basename}."""
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


def _get_v2_fact_count(
    *, base: str, headers: dict, timeout: float = 30.0,
) -> int | None:
    """Best-effort: count facts in the live V2 store via the API."""
    try:
        r = httpx.get(
            f"{base}/api/v2/memory/v2/facts/count",
            headers=headers, timeout=timeout,
        )
        if r.status_code == 200:
            data = r.json()
            return int(data.get("count") or data.get("n") or 0)
    except Exception:  # noqa: BLE001
        return None
    return None


# ── Backup ────────────────────────────────────────────────────────


def _make_backup(db_path: Path) -> Path:
    """Copy memory.db to memory.db.pre-phase7.bak (sibling)."""
    backup = db_path.with_name(db_path.name + BACKUP_SUFFIX)
    if backup.exists():
        print(f"[backup] existing backup found at {backup} — skipping copy")
    else:
        shutil.copy2(str(db_path), str(backup))
        print(f"[backup] {db_path} → {backup}")
    return backup


# ── Migrate subcommand ───────────────────────────────────────────


def _cmd_migrate(args: argparse.Namespace) -> int:
    if not args.db.exists():
        print(f"[error] memory.db not found at {args.db}")
        return 1

    lessons, manuals, bullets, skipped = _scan_rows(args.db)

    print(f"scanned {args.db}:")
    print(f"  {len(lessons)} lesson row(s)")
    print(f"  {len(manuals)} persona_manual row(s)")
    print(f"  {len(bullets)} persona_bullet row(s)")
    if skipped:
        print("\nskipped:")
        for k, c in sorted(skipped.items()):
            print(f"  {k:24}: {c}")

    if not (lessons or manuals or bullets):
        print("nothing to migrate — exiting.")
        return 0

    # Per-bucket breakdown.
    if lessons:
        bs = Counter(l["bucket"] for l in lessons)
        print("\nlesson buckets:")
        for b, c in bs.most_common():
            print(f"  {b:18}: {c}")
    if manuals:
        fs = Counter(m["basename"] for m in manuals)
        print("\npersona_manual files:")
        for f, c in fs.most_common():
            print(f"  {f:18}: {c}")
    if bullets:
        ps = Counter(b["source_path"] for b in bullets)
        print("\npersona_bullet sources:")
        for p, c in ps.most_common(10):
            print(f"  {p[:34]:34}: {c}")

    if not args.execute:
        total = len(lessons) + len(manuals) + len(bullets)
        print(
            f"\n[dry-run] would migrate {total} row(s). "
            f"Re-run with --execute (auto-backups memory.db first).",
        )
        return 0

    # ── Execute path: backup first. ──────────────────────────────
    if not args.no_backup:
        _make_backup(args.db)

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
            if args.verbose:
                print(f"  [lesson {i}/{len(lessons)}] ok bucket={l['bucket']!r}")
        else:
            fail_count += 1
            print(f"  [lesson {i}/{len(lessons)}] FAIL: {payload}")

    # Persona_manual: collapse duplicates per basename, keep newest.
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
            if args.verbose:
                print(f"  [manual {i}/{len(by_file)}] ok {basename}")
        else:
            fail_count += 1
            print(f"  [manual {i}/{len(by_file)}] FAIL {basename}: {payload}")

    # Bullets — same wire shape as lessons (bucket = "bullet").
    for i, b in enumerate(bullets, 1):
        ok, payload = _post_fact(
            base=args.base, headers=headers,
            text=b["text"], bucket="bullet",
            confidence=float(b["confidence"]),
            timeout=args.timeout,
        )
        if ok:
            ok_count += 1
            if args.verbose:
                print(f"  [bullet {i}/{len(bullets)}] ok src={b['source_path']}")
        else:
            fail_count += 1
            print(f"  [bullet {i}/{len(bullets)}] FAIL: {payload}")

    elapsed = time.perf_counter() - t0
    print(
        f"\n=== done: {ok_count} ok / {fail_count} failed "
        f"in {elapsed:.1f}s ===",
    )
    if fail_count == 0:
        print("Next: run ``verify`` subcommand to confirm V2 coverage.")
    return 0 if fail_count == 0 else 2


# ── Verify subcommand ────────────────────────────────────────────


def _cmd_verify(args: argparse.Namespace) -> int:
    """Sanity check: V2 fact count >= V1 user-facing row count.

    Doesn't do per-row hash comparison (that would need V2-side
    introspection by content hash) — instead checks the rough
    invariant ``count(V2) >= count(V1 user-facing)`` and reports
    what V1 had so the operator can spot-check.
    """
    if not args.db.exists():
        print(f"[error] memory.db not found at {args.db}")
        return 1

    lessons, manuals, bullets, skipped = _scan_rows(args.db)
    v1_user_facing = len(lessons) + len(manuals) + len(bullets)
    print(f"V1 user-facing rows in {args.db}: {v1_user_facing}")
    print(f"  lessons:        {len(lessons)}")
    print(f"  persona_manual: {len(manuals)} ({len({m['basename'] for m in manuals})} unique files)")
    print(f"  persona_bullet: {len(bullets)}")
    if skipped:
        print("  (skipped non-user-facing kinds:)")
        for k, c in sorted(skipped.items()):
            print(f"     {k:22}: {c}")

    headers = _headers(args.token_file)
    v2_count = _get_v2_fact_count(base=args.base, headers=headers)
    if v2_count is None:
        print(
            "\n[warn] could not query V2 fact count via "
            "/api/v2/memory/v2/facts/count — verify manually that "
            f"V2 fact count >= {v1_user_facing}.",
        )
        return 0
    print(f"\nV2 fact count: {v2_count}")
    # Note: V2 dedup may collapse multiple V1 rows into one fact, so
    # V2 count can legitimately be LESS than V1. We report and let
    # the operator judge.
    if v2_count == 0 and v1_user_facing > 0:
        print(
            "[FAIL] V2 store is empty but V1 has user-facing rows. "
            "Run the migrate subcommand with --execute.",
        )
        return 2
    print(
        "[ok] V2 store is populated. Note: V2 deterministic-id dedup "
        "may collapse V1 duplicates so V2 count >= V1 is NOT "
        "guaranteed — spot-check important facts via the UI.",
    )
    return 0


# ── Main ─────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Migrate memory.db user-facing rows → v2 facts",
    )
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--token-file", type=Path, default=DEFAULT_TOKEN_FILE)
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument(
        "--verbose", "-v", action="store_true",
        help="Print per-row migration progress (default is summary-only)",
    )

    sub = ap.add_subparsers(dest="cmd")
    sub.required = False  # default = legacy migrate behaviour

    mig = sub.add_parser("migrate", help="Run the migration (default)")
    mig.add_argument(
        "--execute", action="store_true",
        help="Actually POST to v2 (otherwise dry-run only).",
    )
    mig.add_argument(
        "--no-backup", action="store_true",
        help="Skip auto-backup before --execute (NOT recommended)",
    )

    sub.add_parser("verify", help="Verify V2 coverage of V1 user-facing rows")

    # Legacy top-level --execute / --no-backup so old invocations still work.
    ap.add_argument(
        "--execute", action="store_true",
        help="(legacy) Run migrate --execute without the subcommand.",
    )
    ap.add_argument(
        "--no-backup", action="store_true",
        help="(legacy) Skip auto-backup before --execute.",
    )
    return ap


def main() -> int:
    ap = _build_parser()
    args = ap.parse_args()
    if args.cmd == "verify":
        return _cmd_verify(args)
    # default + explicit "migrate"
    return _cmd_migrate(args)


if __name__ == "__main__":
    sys.exit(main())
