"""Memory v3 Phase 1 — backfill ``bucket=""`` facts to ``"misc"``.

Pre-v3, ``MemoryService.remember()`` accepted ``bucket=""`` and the
persona renderer silently skipped those facts (no bucket → no .md
section). After v3 phase 1, empty bucket is coerced to ``"misc"`` at
write time, but **existing** LanceDB rows still carry the empty
string. This script does the one-time backfill.

What it does
============

1. List every Fact in the v2 store.
2. For each fact whose ``bucket`` is None/empty/whitespace, rewrite
   it to ``"misc"`` via the vector backend's update path.
3. Re-render every persona MD file that ``misc`` lands in
   (``MEMORY.md`` ## Other facts (recent)) so the freshly-bucketed
   facts immediately surface to the agent.
4. Print a summary: total scanned / backfilled / per-kind breakdown.

Usage
=====

  python scripts/migrate_dark_facts_to_misc.py [--dry-run]
                                                [--profile <id>]

``--dry-run``: report what WOULD be backfilled, write nothing.
``--profile``: persona profile id (defaults to config's
              ``persona.profile_id`` or ``"default"``).

Idempotent: safe to re-run. Subsequent runs find 0 dark facts and
exit clean.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path


async def _run(dry_run: bool, profile_id: str | None) -> int:
    # Late imports — keep CLI startup fast for --help.
    from xmclaw.daemon.factory import (
        _resolve_persona_profile_dir, build_memory_v2_service,
    )
    from xmclaw.utils.paths import config_path

    # Load config so build_memory_v2_service uses the same backend
    # paths the daemon does.
    cfg_path = config_path()
    if not cfg_path.is_file():
        print(f"no config at {cfg_path}", file=sys.stderr)
        return 2
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))

    svc = await build_memory_v2_service(cfg)
    if svc is None:
        print(
            "memory v2 is not enabled in config — nothing to migrate",
            file=sys.stderr,
        )
        return 0

    # Pull every fact (cap at 100K to avoid runaway scans on weird
    # corrupted stores — adjust if you actually have more).
    listing = await svc._vec.search(None, where=None, limit=100_000)
    total = len(listing)
    dark = [
        f for f in listing
        if not (getattr(f, "bucket", "") or "").strip()
    ]
    print(f"scanned {total} facts; {len(dark)} have empty bucket")

    if not dark:
        print("nothing to do — store is already v3-clean")
        return 0

    # Breakdown by kind so the operator can sanity-check what gets
    # bucketed (e.g. if every "identity" fact has empty bucket
    # something's wrong with the new extractor).
    from collections import Counter
    kind_breakdown = Counter(getattr(f, "kind", "?") for f in dark)
    print("dark facts by kind:")
    for k, n in kind_breakdown.most_common():
        print(f"  {k}: {n}")

    if dry_run:
        print("\n--dry-run; not writing anything")
        return 0

    # Backfill — update bucket in place. We DON'T use remember()
    # because that would re-embed + recompute id; we just need the
    # bucket column flipped.
    fixed = 0
    for f in dark:
        try:
            f.bucket = "misc"
            await svc._vec.upsert(f)
            fixed += 1
        except Exception as exc:  # noqa: BLE001
            print(
                f"  ! upsert failed for fid={getattr(f, 'id', '?')}: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
    print(f"backfilled {fixed}/{len(dark)} facts to bucket=misc")

    # Re-render the affected persona files so the freshly bucketed
    # facts show up in the next agent turn without waiting for a
    # natural write.
    pdir = _resolve_persona_profile_dir(cfg)
    if profile_id:
        # Override: caller passed an explicit profile.
        pdir = pdir.parent / profile_id
    from xmclaw.core.persona.v2_renderer import render_all_persona_files
    report = await render_all_persona_files(svc, pdir)
    print(f"re-rendered persona files: {report}")

    return 0


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument(
        "--profile", default=None,
        help="persona profile id (defaults to config's persona.profile_id)",
    )
    args = p.parse_args()
    rc = asyncio.run(_run(dry_run=args.dry_run, profile_id=args.profile))
    sys.exit(rc)


if __name__ == "__main__":
    main()
