from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from xmclaw.core.ir import ToolCall, ToolResult
from xmclaw.providers.tool._helpers import (
    PERSONA_CHAR_CAPS,
    _PERSONA_BASENAMES_LOOKUP,
    _append_under_section,
    _fail as _fail,
    enforce_char_cap,
)



class BuiltinToolsPersonaMixin:
    """Persona and curriculum tools."""

    async def _update_persona(self, call: ToolCall, t0: float) -> ToolResult:
        """General-purpose persona file editor — append_section / replace
        / delete on any of the 7 canonical files.

        Per user direction (B-14): full self-modification rights, no
        per-file blocklist. The agent is trusted to use sparingly and
        ask before rewriting SOUL.md / IDENTITY.md.
        """
        file_arg = call.args.get("file")
        mode = call.args.get("mode")
        if not isinstance(file_arg, str) or not file_arg.strip():
            return _fail(call, t0, "missing or empty 'file'")
        if mode not in ("append_section", "replace", "delete"):
            return _fail(call, t0, f"invalid 'mode' {mode!r}; expected append_section|replace|delete")

        canonical = _PERSONA_BASENAMES_LOOKUP.get(file_arg.strip().lower())
        if canonical is None:
            return _fail(
                call, t0,
                f"unknown persona file {file_arg!r}; expected one of "
                + ", ".join(_PERSONA_BASENAMES_LOOKUP.values()),
            )

        try:
            pdir_raw = self._persona_dir_provider()
        except Exception as exc:  # noqa: BLE001
            return _fail(call, t0, f"persona dir provider failed: {exc}")
        if pdir_raw is None:
            return _fail(call, t0, "no active persona dir")
        pdir = Path(pdir_raw)
        try:
            pdir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return _fail(call, t0, f"could not create persona dir: {exc}")
        target = pdir / canonical

        # B-198 Phase 3: prefer PersonaStore when wired — DB is truth,
        # disk is rendered cache. The store's set_manual handles
        # atomic write + render-to-disk + auto-section preservation.
        # Falls back to legacy direct-markdown writes when no store
        # provider is configured (tests / B-198-disabled installs).
        store = None
        if self._persona_store_provider is not None:
            try:
                store = self._persona_store_provider()
            except Exception:  # noqa: BLE001
                store = None

        # B-63: serialise concurrent writes through the per-path lock
        # so an in-flight append_section (read-modify-write) doesn't
        # race with a sibling delete or replace.
        async with self._fs_lock(target):
            try:
                if mode == "delete":
                    if store is not None:
                        # In B-198 land, "delete" = clear manual row.
                        # Auto-extracted facts are independent — they
                        # stay (use forget_fact / archive separately).
                        await store.set_manual(canonical, "")
                        written_size = 0
                        summary = f"deleted manual portion of {canonical}"
                    else:
                        if target.is_file():
                            target.unlink()
                            written_size = 0
                            summary = f"deleted {canonical}"
                        else:
                            summary = f"{canonical} did not exist (no-op)"
                            written_size = 0
                elif mode == "replace":
                    content = call.args.get("content")
                    if not isinstance(content, str):
                        return _fail(call, t0, "'content' required for replace mode")
                    if store is not None:
                        # store.set_manual strips the auto section
                        # if the caller round-tripped a render — the
                        # manual row stays clean.
                        await store.set_manual(canonical, content)
                        written_size = len(content.encode("utf-8"))
                        summary = (
                            f"replaced manual portion of {canonical} "
                            f"({written_size} bytes)"
                        )
                    else:
                        from xmclaw.utils.fs_locks import atomic_write_text
                        atomic_write_text(target, content)
                        written_size = len(content.encode("utf-8"))
                        summary = (
                            f"replaced {canonical} ({written_size} bytes)"
                        )
                else:  # append_section
                    section = call.args.get("section")
                    content = call.args.get("content")
                    if not isinstance(section, str) or not section.strip():
                        return _fail(call, t0, "'section' required for append_section mode")
                    if not isinstance(content, str) or not content:
                        return _fail(call, t0, "'content' required for append_section mode")
                    section_clean = section.strip().lstrip("#").strip()
                    section_header = f"## {section_clean}"
                    if store is not None:
                        # Read manual portion, append-under-section in
                        # memory, write back. Auto sections are
                        # preserved (rendered fresh by the store).
                        existing_manual = await store.read_manual(canonical)
                        new_manual = _append_under_section(
                            existing_manual,
                            section_header=section_header,
                            bullet=content,
                            placeholder_title=f"{canonical} — agent-curated",
                        )
                        cap = PERSONA_CHAR_CAPS.get(canonical)
                        if cap is not None and len(new_manual) > cap:
                            new_manual = enforce_char_cap(new_manual, cap)
                        await store.set_manual(canonical, new_manual)
                        written_size = len(new_manual.encode("utf-8"))
                    else:
                        existing = (
                            target.read_text(encoding="utf-8")
                            if target.is_file() else ""
                        )
                        new_text = _append_under_section(
                            existing,
                            section_header=section_header,
                            bullet=content,
                            placeholder_title=f"{canonical} — agent-curated",
                        )
                        cap = PERSONA_CHAR_CAPS.get(canonical)
                        if cap is not None and len(new_text) > cap:
                            new_text = enforce_char_cap(new_text, cap)
                        from xmclaw.utils.fs_locks import atomic_write_text
                        atomic_write_text(target, new_text)
                        written_size = len(new_text.encode("utf-8"))
                    summary = f"appended to {canonical} under {section_header}"
            except OSError as exc:
                return _fail(call, t0, f"write failed: {exc}")
            except ValueError as exc:
                # store.set_manual rejects unknown basenames — already
                # validated above via _PERSONA_BASENAMES_LOOKUP, so
                # surface this as an internal error rather than a
                # bad-input fail.
                return _fail(call, t0, f"persona_store rejected: {exc}")

        # Sidecar log so the Memory UI can show "agent wrote this" badges.
        snippet = ""
        if mode == "append_section":
            snippet = (call.args.get("content") or "")[:200]
        elif mode == "replace":
            snippet = (call.args.get("content") or "")[:200]
        elif mode == "delete":
            snippet = "(deleted)"
        self._record_agent_write(
            pdir, canonical,
            call.args.get("section") if mode == "append_section" else None,
            snippet,
        )

        # Trigger system-prompt rebuild on success so the agent's NEXT
        # turn sees its own edit.
        if self._persona_writeback is not None:
            try:
                self._persona_writeback(canonical)
            except Exception:  # noqa: BLE001
                pass

        return ToolResult(
            call_id=call.id, ok=True,
            content={
                "file": str(target),
                "mode": mode,
                "summary": summary,
                "bytes": written_size,
            },
            side_effects=(str(target.resolve()),),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    def _record_agent_write(
        self, pdir: Path, basename: str, section: str | None, snippet: str,
    ) -> None:
        """Record this write to a sidecar log so the Memory page can
        show "agent wrote this" badges. JSONL one row per write.
        Best-effort — sidecar failures don't fail the main write."""
        try:
            sidecar = pdir / ".agent_writes.jsonl"
            entry = {
                "ts": time.time(),
                "file": basename,
                "section": section,
                "snippet": snippet[:200],
            }
            with sidecar.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass

    # ── B-200 / Phase 5: curriculum-edit proposal flow ──────────────

    async def _propose_curriculum_edit(
        self, call: ToolCall, t0: float,
    ) -> ToolResult:
        """B-200: file a curriculum (LEARNING.md) edit proposal that
        requires user approval before applying.

        Stores the proposal as ``kind=curriculum_proposal`` in
        memory.db. The user runs ``xmclaw curriculum approve <id>`` /
        ``reject`` to act on it; approve invokes the same store
        write path as ``update_persona`` after applying the edit.
        """
        target_file = str(call.args.get("target_file") or "").strip()
        operation = str(call.args.get("operation") or "").strip()
        section = str(call.args.get("section") or "").strip()
        content = str(call.args.get("content") or "").strip()
        rationale = str(call.args.get("rationale") or "").strip()
        evidence = call.args.get("evidence") or []

        if target_file != "LEARNING.md":
            return _fail(call, t0, "v0 supports target_file=LEARNING.md only")
        if operation != "add_principle":
            return _fail(call, t0, "v0 supports operation=add_principle only")
        if not section:
            return _fail(call, t0, "missing 'section'")
        if not content:
            return _fail(call, t0, "missing 'content'")
        if not rationale or len(rationale) < 20:
            return _fail(
                call, t0,
                "rationale must be at least 20 chars (lazy rationale = "
                "guaranteed rejection)",
            )
        if not isinstance(evidence, list):
            evidence = []

        if self._persona_store_provider is None:
            return _fail(call, t0, "persona_store not wired")
        store = self._persona_store_provider()
        if store is None:
            return _fail(call, t0, "persona_store unavailable at call time")

        # Verify the section actually exists in the current file —
        # propose-anchor must be real or the apply step has nowhere
        # to land the bullet.
        try:
            current_manual = await store.read_manual(target_file)
        except Exception as exc:  # noqa: BLE001
            return _fail(call, t0, f"read LEARNING.md failed: {exc}")
        if section.startswith("##"):
            section_norm = section
        else:
            section_norm = f"## {section.lstrip('# ').strip()}"
        if section_norm not in current_manual:
            return _fail(
                call, t0,
                f"section {section_norm!r} not found in {target_file}; "
                f"copy a section heading verbatim from the file you "
                f"read at turn start",
            )

        proposal_id = "curriculum_proposal:" + uuid.uuid4().hex
        now = time.time()
        metadata: dict[str, Any] = {
            "kind": "curriculum_proposal",
            "target_file": target_file,
            "operation": operation,
            "section": section_norm,
            "content": content,
            "rationale": rationale,
            "evidence": list(evidence),
            "status": "pending",
            "proposed_by": call.session_id or "agent",
            "proposed_ts": now,
        }

        try:
            await store.add_fact(
                kind="curriculum_proposal",
                text=content,
                metadata=metadata,
                layer="long",
            )
        except Exception as exc:  # noqa: BLE001
            return _fail(call, t0, f"proposal write failed: {exc}")

        return ToolResult(
            call_id=call.id, ok=True,
            content={
                "proposal_id": proposal_id,
                "status": "pending",
                "target_file": target_file,
                "operation": operation,
                "section": section_norm,
                "review_cmd": "xmclaw curriculum list",
                "approve_cmd": f"xmclaw curriculum approve {proposal_id}",
                "note": (
                    "Proposal queued for user review. "
                    "Will appear in your system prompt only after "
                    "user runs `xmclaw curriculum approve`."
                ),
            },
            side_effects=(),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _list_curriculum_proposals(
        self, call: ToolCall, t0: float,
    ) -> ToolResult:
        """List recent curriculum-edit proposals + their status."""
        status_filter = str(call.args.get("status") or "pending").strip()
        if status_filter not in ("pending", "approved", "rejected", "all"):
            return _fail(call, t0, f"unknown status filter: {status_filter!r}")

        if self._persona_store_provider is None:
            return _fail(call, t0, "persona_store not wired")
        store = self._persona_store_provider()
        if store is None:
            return _fail(call, t0, "persona_store unavailable at call time")

        # Reach into the store's underlying provider to query —
        # PersonaStore doesn't expose "list rows of kind X" yet, but
        # we can use the same memory_provider it's holding.
        mem = getattr(store, "_mem", None)
        if mem is None:
            return _fail(call, t0, "persona_store has no memory provider")

        try:
            hits = await mem.query(
                "long", text=None, k=50,
                filters={"kind": "curriculum_proposal"},
            )
        except Exception as exc:  # noqa: BLE001
            return _fail(call, t0, f"proposal query failed: {exc}")

        rows: list[dict[str, Any]] = []
        for h in hits:
            md = getattr(h, "metadata", {}) or {}
            row_status = md.get("status", "pending")
            if status_filter != "all" and row_status != status_filter:
                continue
            rows.append({
                "id": getattr(h, "id", ""),
                "target_file": md.get("target_file"),
                "operation": md.get("operation"),
                "section": md.get("section"),
                "content_preview": (h.text or "")[:200],
                "rationale_preview": (md.get("rationale") or "")[:200],
                "status": row_status,
                "proposed_ts": md.get("proposed_ts"),
                "decided_ts": md.get("decided_ts"),
                "user_reason": md.get("user_reason"),
            })
            if len(rows) >= 20:
                break

        return ToolResult(
            call_id=call.id, ok=True,
            content={
                "filter": status_filter,
                "count": len(rows),
                "proposals": rows,
            },
            side_effects=(),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _append_persona(
        self, call: ToolCall, t0: float, *,
        basename: str, section: str, entry: str, placeholder_title: str,
        inner_timeout_s: float = 30.0,
    ) -> ToolResult:
        """Idempotent-ish append: locate or create the ``## section``
        block, append a ``- YYYY-MM-DD: entry`` bullet under it.

        We don't try to be too clever about merging — duplicate entries
        on different days are fine (the date prefix shows when it was
        learned). Heavy de-dup would risk dropping useful context.

        B-63: the read-modify-write block is serialised by a per-path
        asyncio.Lock so concurrent agent + dream cron + multi-agent
        ``remember`` calls don't race + lose appends.

        Epic #27 sweep #9 (2026-05-19): inner timeout of 30s. Pre-fix
        the only timeout was the hop_loop's 180s outer wall-clock —
        a wedged PersonaStore.set_manual (embedding pipeline stalled,
        vec_db locked, etc.) blocked the user-visible tool result
        for a full 3 minutes per call. daemon.log showed 56 such
        timeouts/day. Now we wrap the body in asyncio.wait_for(30s)
        and return a clean error if the backend doesn't answer —
        agent gets a recoverable signal instead of a 3-minute freeze.
        """
        import asyncio as _asyncio
        try:
            return await _asyncio.wait_for(
                self._append_persona_inner(
                    call, t0,
                    basename=basename,
                    section=section,
                    entry=entry,
                    placeholder_title=placeholder_title,
                ),
                timeout=inner_timeout_s,
            )
        except _asyncio.TimeoutError:
            return _fail(
                call, t0,
                f"persona write timed out after {int(inner_timeout_s)}s "
                f"(backend slow / locked). Retry, or check the memory "
                f"subsystem health via /api/v2/memory/v2/status. "
                f"Your input is NOT saved; ask the user to confirm.",
            )

    async def _append_persona_inner(
        self, call: ToolCall, t0: float, *,
        basename: str, section: str, entry: str, placeholder_title: str,
    ) -> ToolResult:
        """Inner body of _append_persona — wrapped by asyncio.wait_for
        in the public method. Kept separate so the timeout logic
        doesn't intermix with the read-modify-write flow."""
        from datetime import date as _date
        try:
            pdir_raw = self._persona_dir_provider()
        except Exception as exc:  # noqa: BLE001
            return _fail(call, t0, f"persona dir provider failed: {exc}")
        if pdir_raw is None:
            return _fail(call, t0, "no active persona dir")
        pdir = Path(pdir_raw)
        try:
            pdir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return _fail(call, t0, f"could not create persona dir: {exc}")
        target = pdir / basename

        # B-198 Phase 3: prefer PersonaStore when wired — write goes
        # through the manual row, store renders disk after. Fallback
        # to legacy direct-markdown writes when no store provider.
        store = None
        if self._persona_store_provider is not None:
            try:
                store = self._persona_store_provider()
            except Exception:  # noqa: BLE001
                store = None

        evicted = 0
        async with self._fs_lock(target):
            today = _date.today().isoformat()
            bullet = f"- {today}: {entry}"
            section_header = f"## {section}"

            if store is not None:
                # Read manual portion, append-under-section in memory,
                # write back. Auto-extracted sections are preserved
                # (rendered from facts on next read).
                try:
                    existing_manual = await store.read_manual(basename)
                except Exception as exc:  # noqa: BLE001
                    return _fail(call, t0, f"store read failed: {exc}")
                new_text = _append_under_section(
                    existing_manual,
                    section_header=section_header,
                    bullet=bullet,
                    placeholder_title=placeholder_title,
                )
                cap = PERSONA_CHAR_CAPS.get(basename)
                if cap is not None and len(new_text) > cap:
                    before_len = len(new_text)
                    new_text = enforce_char_cap(new_text, cap)
                    evicted = before_len - len(new_text)
                try:
                    # B-65 deadlock fix: hold fs_lock only for the DB write;
                    # skip render_to_disk inside the lock since it acquires
                    # the SAME get_lock(path) singleton and asyncio.Lock is
                    # not re-entrant.
                    await store.set_manual(basename, new_text, render=False)
                except Exception as exc:  # noqa: BLE001
                    # Wave 26 fix-5: broadened from (OSError, ValueError)
                    # to catch sqlite3.IntegrityError too. Pre-fix, vec0's
                    # UNIQUE constraint failure bubbled out of the except
                    # block and never returned a ToolResult — the dispatcher
                    # eventually emitted a generic error frame but the
                    # ``tool_invocation_finished`` event for THIS call_id
                    # never fired, leaving the UI's ToolCard stuck at
                    # ``running`` forever. Catching broadly here ensures
                    # the user sees an error instead of a frozen spinner.
                    return _fail(call, t0, f"store write failed: {exc}")
                # Render to disk AFTER releasing fs_lock — same lock
                # is used by render_to_disk via get_lock(path).
                try:
                    await store.render_to_disk(basename)
                except Exception as exc:  # noqa: BLE001
                    # render_to_disk failure is non-fatal — the DB row is
                    # already committed; disk cache will refresh on next
                    # read or daemon tick.
                    pass
            else:
                try:
                    existing = (
                        target.read_text(encoding="utf-8")
                        if target.is_file() else ""
                    )
                except OSError as exc:
                    return _fail(call, t0, f"read failed: {exc}")
                new_text = _append_under_section(
                    existing,
                    section_header=section_header,
                    bullet=bullet,
                    placeholder_title=placeholder_title,
                )
                # B-25: enforce char cap (LRU eviction) — Hermes parity.
                cap = PERSONA_CHAR_CAPS.get(basename)
                if cap is not None and len(new_text) > cap:
                    before_len = len(new_text)
                    new_text = enforce_char_cap(new_text, cap)
                    evicted = before_len - len(new_text)

                from xmclaw.utils.fs_locks import atomic_write_text
                try:
                    atomic_write_text(target, new_text)
                except OSError as exc:
                    return _fail(call, t0, f"write failed: {exc}")

        # Sidecar log: this write came from the agent (vs. user via
        # Memory page). Powers the diff badge in the UI.
        self._record_agent_write(pdir, basename, section, entry)

        # Trigger system-prompt rebuild so the agent's NEXT turn sees the
        # entry in its system prompt (closes the "wrote and then forgot
        # immediately" feedback gap).
        if self._persona_writeback is not None:
            try:
                self._persona_writeback(basename)
            except Exception:  # noqa: BLE001 — writeback failure must
                # not roll back the write itself
                pass

        return ToolResult(
            call_id=call.id, ok=True,
            content={
                "file": str(target),
                "section": section,
                "appended": bullet,
                "bytes": len(new_text.encode("utf-8")),
                "evicted_chars": evicted,
            },
            side_effects=(str(target.resolve()),),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    # ── allowlist ─────────────────────────────────────────────────────

