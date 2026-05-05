"""B-200 / Phase 5 — CLI implementations for ``xmclaw curriculum``.

Reads / mutates curriculum-edit proposals stored in memory.db with
``kind=curriculum_proposal``. Approval applies the proposal by
rewriting the manual portion of the target persona file (currently
LEARNING.md only) and re-rendering disk via PersonaStore.

Run path is intentionally daemon-independent: the CLI talks straight
to memory.db + persona files, so users can review proposals even
when the daemon is stopped. (When the daemon IS running, an
approved edit lands at the top of the next system prompt — no
restart needed since the assembler reads disk.)
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import typer

from xmclaw.utils.paths import data_dir, persona_dir


def _memory_db_path() -> Path:
    return data_dir() / "v2" / "memory.db"


def _resolve_active_profile_dir() -> Path:
    """Mirror ``daemon.factory._resolve_persona_profile_dir`` enough
    to find the active profile from config without spinning up the
    full lifespan."""
    cfg_path = Path("daemon/config.json")
    if cfg_path.is_file():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            persona = cfg.get("persona") if isinstance(cfg, dict) else None
            if isinstance(persona, dict):
                pid = persona.get("profile_id")
                if isinstance(pid, str) and pid.strip():
                    return persona_dir().parent / "profiles" / pid.strip()
        except (json.JSONDecodeError, OSError):
            pass
    return persona_dir().parent / "profiles" / "default"


def _connect() -> sqlite3.Connection | None:
    db = _memory_db_path()
    if not db.is_file():
        return None
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    return con


def _fetch_proposals(
    con: sqlite3.Connection, *, status: str | None = None,
) -> list[dict[str, Any]]:
    """Pull curriculum_proposal rows from memory.db. Each row's
    metadata JSON is unpacked so callers don't need to parse."""
    cur = con.cursor()
    cur.execute(
        "SELECT id, text, metadata, ts FROM memory_items "
        "WHERE json_extract(metadata, '$.kind') = 'curriculum_proposal' "
        "ORDER BY ts DESC",
    )
    rows: list[dict[str, Any]] = []
    for r in cur.fetchall():
        try:
            md = json.loads(r["metadata"]) if r["metadata"] else {}
        except json.JSONDecodeError:
            md = {}
        if status and status != "all" and md.get("status") != status:
            continue
        rows.append({
            "id": r["id"],
            "text": r["text"] or "",
            "metadata": md,
            "ts": r["ts"],
        })
    return rows


def _update_proposal_status(
    con: sqlite3.Connection,
    proposal_id: str,
    *,
    new_status: str,
    user_reason: str = "",
) -> bool:
    """Atomic UPDATE on the metadata JSON to set status + decided_ts."""
    cur = con.cursor()
    row = cur.execute(
        "SELECT metadata FROM memory_items WHERE id = ?", (proposal_id,),
    ).fetchone()
    if row is None:
        return False
    try:
        md = json.loads(row["metadata"]) if row["metadata"] else {}
    except json.JSONDecodeError:
        md = {}
    md["status"] = new_status
    md["decided_ts"] = time.time()
    if user_reason:
        md["user_reason"] = user_reason
    cur.execute(
        "UPDATE memory_items SET metadata = ? WHERE id = ?",
        (json.dumps(md, ensure_ascii=False), proposal_id),
    )
    con.commit()
    return True


def run_curriculum_list(status: str) -> int:
    if status not in ("pending", "approved", "rejected", "all"):
        typer.echo(
            f"  [x]  unknown status {status!r}; expected pending|approved|"
            "rejected|all",
            err=True,
        )
        return 2
    con = _connect()
    if con is None:
        typer.echo(
            "  [x]  memory.db not found — daemon never started or "
            "wrong profile."
        )
        return 1
    try:
        rows = _fetch_proposals(con, status=status)
    finally:
        con.close()

    if not rows:
        typer.echo(f"  no {status} curriculum proposals")
        return 0

    typer.echo(f"  {len(rows)} {status} curriculum proposal(s):\n")
    for row in rows:
        md = row["metadata"]
        ts = md.get("proposed_ts", row["ts"])
        when = (
            time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
            if ts else "unknown"
        )
        target = md.get("target_file", "?")
        section = md.get("section", "?")
        op = md.get("operation", "?")
        text_preview = (row["text"] or "")[:120].replace("\n", " ")
        rationale_preview = (md.get("rationale") or "")[:120].replace("\n", " ")
        st = md.get("status", "?")
        typer.echo("  ─────────────────────────────────────────")
        typer.echo(f"  id:        {row['id']}")
        typer.echo(f"  status:    {st}")
        typer.echo(f"  proposed:  {when}")
        typer.echo(f"  target:    {target}")
        typer.echo(f"  section:   {section}")
        typer.echo(f"  operation: {op}")
        typer.echo(f"  content:   {text_preview}")
        typer.echo(f"  rationale: {rationale_preview}")
    typer.echo("  ─────────────────────────────────────────\n")
    typer.echo(
        "  Run `xmclaw curriculum show <id>` for full text. "
        "`approve <id>` to apply; `reject <id>` to dismiss."
    )
    return 0


def run_curriculum_show(proposal_id: str) -> int:
    con = _connect()
    if con is None:
        typer.echo("  [x]  memory.db not found")
        return 1
    try:
        cur = con.cursor()
        row = cur.execute(
            "SELECT id, text, metadata, ts FROM memory_items WHERE id = ?",
            (proposal_id,),
        ).fetchone()
        if row is None:
            typer.echo(f"  [x]  proposal {proposal_id!r} not found")
            return 1
        try:
            md = json.loads(row["metadata"]) if row["metadata"] else {}
        except json.JSONDecodeError:
            md = {}
        typer.echo(f"  id:           {row['id']}")
        typer.echo(f"  status:       {md.get('status', '?')}")
        typer.echo(f"  target_file:  {md.get('target_file', '?')}")
        typer.echo(f"  section:      {md.get('section', '?')}")
        typer.echo(f"  operation:    {md.get('operation', '?')}")
        typer.echo(f"  proposed_by:  {md.get('proposed_by', '?')}")
        proposed_ts = md.get("proposed_ts", row["ts"])
        if proposed_ts:
            typer.echo(
                f"  proposed_ts:  "
                f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(proposed_ts))}"
            )
        decided_ts = md.get("decided_ts")
        if decided_ts:
            typer.echo(
                f"  decided_ts:   "
                f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(decided_ts))}"
            )
        if md.get("user_reason"):
            typer.echo(f"  user_reason:  {md['user_reason']}")
        typer.echo("")
        typer.echo("  ── proposed bullet ──")
        for line in (row["text"] or "").splitlines():
            typer.echo(f"    {line}")
        typer.echo("")
        typer.echo("  ── rationale ──")
        for line in (md.get("rationale") or "").splitlines():
            typer.echo(f"    {line}")
        evidence = md.get("evidence") or []
        if evidence:
            typer.echo("")
            typer.echo("  ── evidence ──")
            for ev in evidence:
                typer.echo(f"    - {ev}")
    finally:
        con.close()
    return 0


def _apply_proposal(proposal_id: str, *, md: dict[str, Any], text: str) -> tuple[bool, str]:
    """Apply an approved proposal by updating LEARNING.md's manual
    section. Goes through PersonaStore so the disk render and DB
    truth stay aligned. Returns ``(ok, message)``.
    """
    target_file = md.get("target_file", "")
    operation = md.get("operation", "")
    section = md.get("section", "")

    if target_file != "LEARNING.md":
        return False, f"unsupported target_file: {target_file!r}"
    if operation != "add_principle":
        return False, f"unsupported operation: {operation!r}"
    if not section:
        return False, "missing section"

    # Use the same SqliteVecMemory + PersonaStore the daemon uses so
    # the post-apply disk render matches what the agent will see.
    from xmclaw.providers.memory.sqlite_vec import SqliteVecMemory
    from xmclaw.providers.memory.base import MemoryItem
    from xmclaw.providers.tool.builtin import _append_under_section
    from xmclaw.core.persona.store import PersonaStore

    pdir = _resolve_active_profile_dir()
    pdir.mkdir(parents=True, exist_ok=True)
    db_path = _memory_db_path()
    if not db_path.is_file():
        return False, f"memory.db not found at {db_path}"

    mem = SqliteVecMemory(str(db_path))
    store = PersonaStore(mem, pdir, item_factory=MemoryItem)

    async def _do() -> tuple[bool, str]:
        existing = await store.read_manual(target_file)
        bullet = text.strip()
        if not bullet.startswith("- "):
            bullet = f"- {bullet}"
        new_manual = _append_under_section(
            existing,
            section_header=section,
            bullet=bullet,
            placeholder_title=f"{target_file} — agent-curated",
        )
        if new_manual == existing:
            return False, "no-op (bullet already present or section missing)"
        try:
            await store.set_manual(target_file, new_manual)
        except Exception as exc:  # noqa: BLE001
            return False, f"set_manual failed: {exc}"
        return True, "applied"

    try:
        return asyncio.run(_do())
    finally:
        try:
            mem.close()
        except Exception:  # noqa: BLE001
            pass


def run_curriculum_approve(proposal_id: str) -> int:
    con = _connect()
    if con is None:
        typer.echo("  [x]  memory.db not found")
        return 1
    try:
        cur = con.cursor()
        row = cur.execute(
            "SELECT id, text, metadata FROM memory_items WHERE id = ?",
            (proposal_id,),
        ).fetchone()
        if row is None:
            typer.echo(f"  [x]  proposal {proposal_id!r} not found")
            return 1
        try:
            md = json.loads(row["metadata"]) if row["metadata"] else {}
        except json.JSONDecodeError:
            md = {}
        if md.get("status") != "pending":
            typer.echo(
                f"  [x]  proposal already {md.get('status', '?')}, "
                f"refusing to re-apply"
            )
            return 1
    finally:
        con.close()

    ok, message = _apply_proposal(proposal_id, md=md, text=row["text"] or "")
    if not ok:
        typer.echo(f"  [x]  apply failed: {message}")
        return 1

    # Mark the proposal as approved so subsequent list / show / agent
    # ``list_curriculum_proposals`` reflect the new state.
    con = _connect()
    if con is None:
        typer.echo("  [x]  memory.db gone after apply (race?) — manual fixup needed")
        return 1
    try:
        ok = _update_proposal_status(con, proposal_id, new_status="approved")
    finally:
        con.close()
    if not ok:
        typer.echo("  [x]  approved but failed to mark status")
        return 1

    typer.echo(f"  [ok] proposal {proposal_id} applied + marked approved")
    typer.echo(
        "  rendered LEARNING.md updated; agent will see the new "
        "principle on the next turn (no restart needed — "
        "system prompt cache is mtime-keyed)."
    )
    return 0


def run_curriculum_reject(proposal_id: str, reason: str) -> int:
    con = _connect()
    if con is None:
        typer.echo("  [x]  memory.db not found")
        return 1
    try:
        cur = con.cursor()
        row = cur.execute(
            "SELECT metadata FROM memory_items WHERE id = ?", (proposal_id,),
        ).fetchone()
        if row is None:
            typer.echo(f"  [x]  proposal {proposal_id!r} not found")
            return 1
        try:
            md = json.loads(row["metadata"]) if row["metadata"] else {}
        except json.JSONDecodeError:
            md = {}
        if md.get("status") != "pending":
            typer.echo(
                f"  [x]  proposal already {md.get('status', '?')}, "
                f"refusing to overwrite"
            )
            return 1
        ok = _update_proposal_status(
            con, proposal_id, new_status="rejected", user_reason=reason,
        )
    finally:
        con.close()
    if not ok:
        typer.echo("  [x]  failed to mark rejected")
        return 1
    typer.echo(f"  [ok] proposal {proposal_id} marked rejected")
    if reason:
        typer.echo(f"  reason recorded: {reason}")
    typer.echo(
        "  agent will see the rejection (and reason) on next "
        "list_curriculum_proposals call."
    )
    return 0
