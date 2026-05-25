"""One-shot cleanup of identity-test pollution from memory.

Background
==========

The user reported (2026-05-26) that earlier test runs left a batch of
fictional identity facts in memory — 张伟 / LT凌天电竞 / 陪玩店 /
NimbusBot / LT-Command — none of which are the real user. These
facts had been re-rendered into USER.md / SOUL.md / MEMORY.md as
ground truth, and the agent was opening turns with greetings like
"哥, 你的 LT 凌天的状态已加载" based on them.

The structural fix (agent-callable curation tools + post-correction
hook + render-time dedup) shipped alongside this script. But the
existing poisoned rows still live in LanceDB; without explicit
cleanup, the renderer can still surface them until they age out
naturally.

This script scans the v2 facts store for the known pollution
keywords and soft-deletes matches via
``MemoryService.forget(fact_id, reason=...)`` — same path the
agent's ``memory_forget`` tool uses. Soft delete keeps a tombstone
so the change is reversible if the script fired on something it
shouldn't have.

Usage
=====

    python scripts/cleanup_identity_pollution.py                # dry run
    python scripts/cleanup_identity_pollution.py --commit       # actually forget

Dry-run reports which facts would be forgotten without writing.
``--commit`` performs the soft delete. Either way the script
re-renders the persona files at the end so MD content stays in sync
with LanceDB.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any


# Keywords lifted from the user-reported pollution. Each keyword is
# matched as a substring (case-insensitive). False positives are
# possible — review the dry-run output before passing --commit.
_POLLUTION_KEYWORDS = (
    "张伟",
    "LT凌天",
    "凌天电竞",
    "陪玩店",
    "NimbusBot",
    "LT-Command",
)


async def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--commit", action="store_true",
        help="Actually forget the matched facts (default: dry run only).",
    )
    parser.add_argument(
        "--keywords", nargs="+", default=list(_POLLUTION_KEYWORDS),
        help="Override the default pollution keyword list.",
    )
    parser.add_argument(
        "--profile", default="default",
        help="Persona profile directory under ~/.xmclaw/persona/profiles/",
    )
    args = parser.parse_args()

    # Open the v2 memory service against the standard daemon paths.
    # This mirrors how the daemon boots it — no daemon needs to be
    # running.
    from xmclaw.utils.paths import data_dir
    from xmclaw.memory.v2.service import MemoryService
    from xmclaw.memory.v2.backend_lancedb import LanceDbFactsBackend

    db_path = data_dir() / "v2" / "lancedb"
    if not db_path.exists():
        print(f"no v2 store at {db_path} — nothing to clean", flush=True)
        return 0

    backend = LanceDbFactsBackend(db_path=db_path)
    await backend.open()
    svc = MemoryService(vec_backend=backend, embedder=None)

    # Find every fact whose text matches any keyword.
    print(
        f"scanning v2 facts for pollution keywords: "
        f"{', '.join(args.keywords)}",
        flush=True,
    )
    candidates: list[Any] = []
    seen_ids: set[str] = set()
    for kw in args.keywords:
        # recall() in pure-filter mode (query=None) lists by ts_last
        # so we capture both old + recent matches. k=1000 is well
        # above any realistic poisoned-row count.
        hits = await svc.recall(
            query=None,
            k=1000,
            min_confidence=0.0,
            include_relations=False,
            include_superseded=False,
        )
        for h in hits:
            text = (h.fact.text or "")
            if kw.lower() in text.lower() and h.fact.id not in seen_ids:
                candidates.append(h)
                seen_ids.add(h.fact.id)

    if not candidates:
        print("nothing matched — store is clean.", flush=True)
        return 0

    print(f"\n{len(candidates)} candidate fact(s) match pollution keywords:\n")
    for h in candidates:
        f = h.fact
        marker = "DRY" if not args.commit else "FORGET"
        print(
            f"  [{marker}] kind={f.kind} scope={f.scope} bucket={f.bucket or '-'}\n"
            f"          conf={f.confidence:.2f} ev={f.evidence_count}\n"
            f"          {f.text[:200]}\n"
        )

    if not args.commit:
        print(
            "\nDry-run only. Re-run with --commit to forget these facts.",
            flush=True,
        )
        return 0

    # Commit: soft-delete each match via the service-level forget API.
    forgotten = 0
    for h in candidates:
        ok = await svc.forget(
            fact_id=h.fact.id,
            reason="scripts/cleanup_identity_pollution.py — "
                   "user reported these as test-leftover, not real identity",
        )
        if ok:
            forgotten += 1
    print(f"\nforgot {forgotten} fact(s).", flush=True)

    # Re-render persona files so the MD reflects the new state.
    persona_root = (
        Path.home() / ".xmclaw" / "persona" / "profiles" / args.profile
    )
    if persona_root.is_dir():
        from xmclaw.core.persona.v2_renderer import render_all_persona_files
        result = await render_all_persona_files(svc, persona_root)
        rewritten = sum(1 for v in result.values() if v)
        print(
            f"re-rendered persona dir {persona_root}: "
            f"{rewritten} file(s) updated",
            flush=True,
        )
    else:
        print(
            f"persona dir not found at {persona_root} — skipping re-render",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
