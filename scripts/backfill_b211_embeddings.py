"""B-211: backfill missing embeddings on existing memory rows.

Pre-B-211 the persona_store paths wrote rows without embedding:
  * persona_manual: 7/7 rows missing
  * lesson:        16/145 rows missing (migrate_from_disk path)
  * preference:    1/137 rows missing

Going forward the writers auto-embed (B-211 commit). This script
sweeps the existing has_embedding=0 rows once and embeds them.

Idempotent: only touches rows where has_embedding=0. Safe to re-run.

Usage::

    python scripts/backfill_b211_embeddings.py
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from pathlib import Path

# Ensure xmclaw is importable when run from the repo root
_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


def _load_user_config() -> dict:
    """Read daemon/config.json directly — same path the daemon uses."""
    cfg_path = _REPO / "daemon" / "config.json"
    if not cfg_path.is_file():
        raise SystemExit(f"config not found at {cfg_path}")
    return json.loads(cfg_path.read_text(encoding="utf-8"))


async def main() -> int:
    from xmclaw.providers.memory.base import MemoryItem
    from xmclaw.providers.memory.embedding import build_embedding_provider
    from xmclaw.providers.memory.sqlite_vec import SqliteVecMemory
    from xmclaw.utils.paths import data_dir

    cfg = _load_user_config()

    embed_cfg = (
        ((cfg.get("evolution") or {}).get("memory") or {})
        .get("embedding") or {}
    )
    if not embed_cfg or not embed_cfg.get("base_url"):
        print("ERROR: evolution.memory.embedding not configured")
        return 2

    # build_embedding_provider takes the full config dict and reaches
    # into evolution.memory.embedding internally — NOT just the embed
    # section.
    embedder = build_embedding_provider(cfg)
    if embedder is None:
        print("ERROR: build_embedding_provider returned None — check api_key / base_url")
        return 2

    db_path = data_dir() / "v2" / "memory.db"
    if not db_path.is_file():
        print(f"ERROR: memory.db missing at {db_path}")
        return 2

    # Find candidates.
    target_kinds = ("persona_manual", "lesson", "preference")
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    placeholders = ",".join("?" * len(target_kinds))
    cur = db.execute(
        f"""
        SELECT id, layer, text, metadata, ts
        FROM memory_items
        WHERE has_embedding = 0
          AND text IS NOT NULL AND length(trim(text)) > 0
          AND json_extract(metadata, '$.kind') IN ({placeholders})
        """,
        target_kinds,
    )
    rows = list(cur)
    db.close()

    if not rows:
        print("nothing to backfill — every persona_manual / lesson / preference row already has has_embedding=1")
        return 0

    print(f"backfilling {len(rows)} rows with missing embeddings…")

    sv = SqliteVecMemory(
        db_path=db_path,
        embedding_dim=int(embed_cfg.get("dimensions") or 1024),
    )

    by_kind_ok: dict[str, int] = {}
    by_kind_fail: dict[str, int] = {}

    BATCH = 16
    for i in range(0, len(rows), BATCH):
        batch = rows[i : i + BATCH]
        texts = [r["text"][:6000] for r in batch]
        try:
            vecs = await embedder.embed(texts)
        except Exception as exc:  # noqa: BLE001
            print(f"  embed batch {i}-{i+len(batch)-1} FAILED: {type(exc).__name__}: {exc}")
            for r in batch:
                kind = (json.loads(r["metadata"] or "{}").get("kind") or "?")
                by_kind_fail[kind] = by_kind_fail.get(kind, 0) + 1
            continue

        for r, v in zip(batch, vecs):
            kind = (json.loads(r["metadata"] or "{}").get("kind") or "?")
            if not v:
                by_kind_fail[kind] = by_kind_fail.get(kind, 0) + 1
                continue
            try:
                md = json.loads(r["metadata"] or "{}")
                item = MemoryItem(
                    id=r["id"],
                    layer=r["layer"],
                    text=r["text"],
                    metadata=md,
                    embedding=tuple(v),
                    ts=r["ts"] or 0,
                )
                # put() upserts on duplicate id and re-attaches the vector.
                await sv.put(r["layer"], item)
                by_kind_ok[kind] = by_kind_ok.get(kind, 0) + 1
            except Exception as exc:  # noqa: BLE001
                print(f"  put failed for {r['id'][:16]}: {type(exc).__name__}: {exc}")
                by_kind_fail[kind] = by_kind_fail.get(kind, 0) + 1

    print()
    print("backfill complete:")
    for k in sorted(set(by_kind_ok) | set(by_kind_fail)):
        ok = by_kind_ok.get(k, 0)
        bad = by_kind_fail.get(k, 0)
        print(f"  {k:20} ok={ok:>4}  failed={bad:>4}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
