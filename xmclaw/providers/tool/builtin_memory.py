from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from xmclaw.core.ir import ToolCall, ToolResult
from xmclaw.providers.tool._helpers import (
    _PERSONA_BASENAMES_LOOKUP as _PERSONA_BASENAMES_LOOKUP,
    _fail as _fail,
)

_VALID_TODO_STATUSES = {"pending", "in_progress", "done"}

# 2026-05-29 cleanup: pre-compile the read-side regexes used by
# ``_memory_get`` so they're not re-parsed per tool call. Python's
# internal pattern cache makes the practical cost negligible, but
# module-level constants match the codebase convention and let the
# pattern serve as a published contract for the fid marker format.
_FID_MARKER_RE = re.compile(r"<!--\s*fid:([0-9a-fA-F]{4,})\s*-->")
_LINES_RANGE_RE = re.compile(r"^\s*(\d+)\s*-\s*(\d+)\s*$")


class BuiltinToolsMemoryMixin:
    """Memory, todo, note, and journal tools."""

    def _todo_key(self, call: ToolCall) -> str:
        # ToolCall.session_id is populated by AgentLoop. Anonymous callers
        # (e.g. direct unit tests) share the "_default" bucket.
        return call.session_id or "_default"

    async def _todo_write(self, call: ToolCall, t0: float) -> ToolResult:
        items = call.args.get("items")
        if not isinstance(items, list):
            return _fail(call, t0, "'items' must be a list")
        cleaned: list[dict[str, str]] = []
        for i, raw in enumerate(items):
            if not isinstance(raw, dict):
                return _fail(
                    call, t0,
                    f"item {i} must be an object with content + status",
                )
            content = raw.get("content")
            status = raw.get("status", "pending")
            if not isinstance(content, str) or not content.strip():
                return _fail(call, t0, f"item {i}: content must be non-empty string")
            if status not in _VALID_TODO_STATUSES:
                return _fail(
                    call, t0,
                    f"item {i}: status {status!r} must be one of "
                    f"{sorted(_VALID_TODO_STATUSES)}",
                )
            cleaned.append({"content": content.strip(), "status": status})

        sid = self._todo_key(call)
        self._todos[sid] = cleaned
        if self._todo_listener is not None:
            try:
                self._todo_listener(sid, list(cleaned))
            except Exception:  # noqa: BLE001 -- listener must never sink a tool call
                pass

        done = sum(1 for t in cleaned if t["status"] == "done")
        prog = sum(1 for t in cleaned if t["status"] == "in_progress")
        summary = f"saved {len(cleaned)} todos ({done} done, {prog} in progress)"
        return ToolResult(
            call_id=call.id, ok=True,
            content=summary,
            side_effects=(),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _todo_read(self, call: ToolCall, t0: float) -> ToolResult:
        sid = self._todo_key(call)
        items = self._todos.get(sid, [])
        if not items:
            body = "(no todos yet)"
        else:
            def _glyph(s: str) -> str:
                return {"pending": "[ ]", "in_progress": "[~]", "done": "[x]"}.get(s, "[?]")
            body = "\n".join(
                f"{i+1}. {_glyph(t['status'])} {t['content']}"
                for i, t in enumerate(items)
            )
        return ToolResult(
            call_id=call.id, ok=True, content=body,
            side_effects=(),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _update_focus(self, call: ToolCall, t0: float) -> ToolResult:
        """Wave-27 fix-8 / C: agent self-declares its current focus.

        The recorded text gets injected into the GoalAnchor block on
        the next hop's anchor refresh — survives compression and
        context shuffling because the anchor is regenerated each
        time. Empty / blank text clears the slot (use when finishing
        a phase and not yet starting a new one).
        """
        focus = call.args.get("focus")
        if not isinstance(focus, str):
            return _fail(call, t0, "'focus' must be a string")
        # Lazy import — keeps this module independent of cognition/.
        from xmclaw.cognition.goal_anchor import set_session_focus
        sid = call.session_id or "_default"
        focus_clean = focus.strip()
        set_session_focus(sid, focus_clean)
        if focus_clean:
            summary = f"focus set: {focus_clean[:80]}"
            if len(focus_clean) > 80:
                summary += "…"
        else:
            summary = "focus cleared"
        return ToolResult(
            call_id=call.id, ok=True, content=summary,
            side_effects=(),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    # ── self-modifying memory tools ───────────────────────────────────

    async def _remember(self, call: ToolCall, t0: float) -> ToolResult:
        category = call.args.get("category")
        note = call.args.get("note")
        if not isinstance(category, str) or not category.strip():
            return _fail(call, t0, "missing or empty 'category'")
        if not isinstance(note, str) or not note.strip():
            return _fail(call, t0, "missing or empty 'note'")
        return await self._append_persona(
            call, t0,
            basename="MEMORY.md",
            section=category.strip(),
            entry=note.strip(),
            placeholder_title="MEMORY.md — what I want to remember next time",
        )

    async def _memory_pin(self, call: ToolCall, t0: float) -> ToolResult:
        """B-53: pin a fact to MEMORY.md's ``## Pinned`` section. Same
        write path as ``remember``, just under a section the dream
        prompt is told to preserve verbatim."""
        content = call.args.get("content")
        if not isinstance(content, str) or not content.strip():
            return _fail(call, t0, "missing or empty 'content'")
        return await self._append_persona(
            call, t0,
            basename="MEMORY.md",
            section="Pinned",
            entry=content.strip(),
            placeholder_title="MEMORY.md — what I want to remember next time",
        )

    async def _learn_about_user(self, call: ToolCall, t0: float) -> ToolResult:
        section = call.args.get("section")
        fact = call.args.get("fact")
        if not isinstance(section, str) or not section.strip():
            return _fail(call, t0, "missing or empty 'section'")
        if not isinstance(fact, str) or not fact.strip():
            return _fail(call, t0, "missing or empty 'fact'")
        return await self._append_persona(
            call, t0,
            basename="USER.md",
            section=section.strip(),
            entry=fact.strip(),
            placeholder_title="USER.md — who I'm working with",
        )

    async def _schedule_followup(self, call: ToolCall, t0: float) -> ToolResult:
        """Create a cron job — agent's self-scheduling primitive.

        Wraps :class:`xmclaw.core.scheduler.cron.CronStore` so the agent
        can set its own reminders without learning the full
        ``/api/v2/cron`` REST surface. ``run_once=True`` is implemented
        by appending a deletion clause to the prompt — the future agent
        deletes its own job after firing.
        """
        name = call.args.get("name")
        schedule = call.args.get("schedule")
        prompt = call.args.get("prompt")
        run_once = bool(call.args.get("run_once", False))
        if not isinstance(name, str) or not name.strip():
            return _fail(call, t0, "missing or empty 'name'")
        if not isinstance(schedule, str) or not schedule.strip():
            return _fail(call, t0, "missing or empty 'schedule'")
        if not isinstance(prompt, str) or not prompt.strip():
            return _fail(call, t0, "missing or empty 'prompt'")

        # B-37: run_once is now a real CronJob field — CronStore.mark_fired
        # deletes the job after firing instead of rescheduling. No more
        # "future agent please delete yourself" breadcrumbs.
        full_prompt = prompt.strip()

        try:
            from xmclaw.core.scheduler.cron import CronJob, default_cron_store
            store = default_cron_store()
            import uuid as _uuid
            job = CronJob(
                id=_uuid.uuid4().hex,
                name=name.strip(),
                schedule=schedule.strip(),
                prompt=full_prompt,
                run_once=run_once,
            )
            saved = store.add(job)
        except Exception as exc:  # noqa: BLE001
            return _fail(call, t0, f"schedule failed: {exc}")

        return ToolResult(
            call_id=call.id, ok=True,
            content={
                "job_id": saved.id,
                "name": saved.name,
                "schedule": saved.schedule,
                "next_run_at": saved.next_run_at,
                "run_once": run_once,
            },
            side_effects=(),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _note_write(self, call: ToolCall, t0: float) -> ToolResult:
        """B-45: agent-driven write to ~/.xmclaw/memory/*.md.

        Lands in the Web UI's Notes tab + gets vector-indexed by
        the next indexer tick. Used by the agent to record workflows,
        lessons learned, accumulated reference — first-class evolution
        surface alongside MEMORY.md.
        """
        from xmclaw.utils.paths import file_memory_dir

        name = str(call.args.get("name") or "").strip()
        content = call.args.get("content")
        mode = str(call.args.get("mode") or "replace").lower()
        description = str(call.args.get("description") or "").strip()
        tags_raw = call.args.get("tags")
        tags: list[str] = []
        if isinstance(tags_raw, list):
            tags = [str(t).strip() for t in tags_raw if str(t).strip()]
        if not name:
            return _fail(call, t0, "missing 'name'")
        if not isinstance(content, str):
            return _fail(call, t0, "'content' must be a string")
        if mode not in ("replace", "append"):
            return _fail(call, t0, f"unknown mode {mode!r}")

        # Strip path components for safety, ensure .md.
        safe = name.replace("\\", "/").split("/")[-1].strip()
        if not safe or safe.startswith("."):
            return _fail(call, t0, f"invalid note name {name!r}")
        if not safe.endswith(".md"):
            safe = safe + ".md"

        mdir = file_memory_dir()
        try:
            mdir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return _fail(call, t0, f"mkdir failed: {exc}")
        path = mdir / safe

        # B-93: build YAML-style frontmatter when description/tags
        # passed. Only on replace mode — append preserves whatever
        # frontmatter the file already had.
        def _build_frontmatter() -> str:
            if not description and not tags:
                return ""
            lines = ["---"]
            if description:
                # Escape any literal \"---\" inside the description
                # so it can't terminate the block early.
                clean = description.replace("---", "—")
                lines.append(f"description: {clean}")
            if tags:
                lines.append("tags: [" + ", ".join(tags) + "]")
            lines.append("---")
            lines.append("")  # blank line before body
            return "\n".join(lines) + "\n"

        # B-64: lock the file so concurrent note_write calls (or note +
        # daemon-side editor write via /api/v2/memory POST) don't race
        # on the read-modify-write append path.
        from xmclaw.utils.fs_locks import atomic_write_text
        async with self._fs_lock(path):
            try:
                if mode == "append" and path.is_file():
                    existing = path.read_text(encoding="utf-8", errors="replace")
                    sep = "\n\n---\n\n" if existing.strip() else ""
                    atomic_write_text(
                        path,
                        existing.rstrip() + sep + content.strip() + "\n",
                    )
                else:
                    body = _build_frontmatter() + content
                    atomic_write_text(path, body)
            except OSError as exc:
                return _fail(call, t0, f"write failed: {exc}")

        return ToolResult(
            call_id=call.id, ok=True,
            content={
                "name": safe,
                "path": str(path),
                "mode": mode,
                "size": path.stat().st_size,
            },
            side_effects=(str(path),),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _journal_append(self, call: ToolCall, t0: float) -> ToolResult:
        """B-45: append a dated entry to ~/.xmclaw/memory/journal/<date>.md.

        Web UI's Journal tab reads from the same path. Each entry gets
        a horizontal rule separator + an HH:MM:SS timestamp. Optional
        ``title`` becomes a ## heading for table-of-contents-style
        scanning later.
        """
        from xmclaw.utils.paths import file_memory_dir
        import re as _re

        content = call.args.get("content")
        date = str(call.args.get("date") or "").strip() or time.strftime("%Y-%m-%d")
        title = str(call.args.get("title") or "").strip()

        if not isinstance(content, str) or not content.strip():
            return _fail(call, t0, "missing 'content'")
        # Reject malformed dates rather than silently writing to a
        # weird filename — agent sometimes hands us "today" as the
        # literal string.
        if not _re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            return _fail(
                call, t0,
                f"date must be YYYY-MM-DD (got {date!r})",
            )

        jdir = file_memory_dir() / "journal"
        try:
            jdir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return _fail(call, t0, f"mkdir failed: {exc}")

        path = jdir / f"{date}.md"
        ts = time.strftime("%H:%M:%S")
        block_parts: list[str] = []
        if title:
            block_parts.append(f"## {title}")
        block_parts.append(f"_{ts}_")
        block_parts.append(content.strip())
        block = "\n\n".join(block_parts)

        # B-64: same RMW lock as note_write — concurrent agent +
        # cron append on the same daily file would otherwise lose
        # entries.
        from xmclaw.utils.fs_locks import atomic_write_text
        async with self._fs_lock(path):
            try:
                if path.is_file():
                    existing = path.read_text(encoding="utf-8", errors="replace")
                    if not existing.startswith("# "):
                        existing = f"# 日记 {date}\n\n" + existing
                    atomic_write_text(
                        path,
                        existing.rstrip() + "\n\n---\n\n" + block + "\n",
                    )
                else:
                    atomic_write_text(
                        path,
                        f"# 日记 {date}\n\n" + block + "\n",
                    )
            except OSError as exc:
                return _fail(call, t0, f"write failed: {exc}")

        return ToolResult(
            call_id=call.id, ok=True,
            content={
                "date": date,
                "path": str(path),
                "size": path.stat().st_size,
                "title": title or None,
            },
            side_effects=(str(path),),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _recall_user_preferences(
        self, call: ToolCall, t0: float,
    ) -> ToolResult:
        """Epic #24 Phase 4.2: read USER.md auto-extracted preferences.

        Parses the ``## Auto-extracted preferences`` section written
        by ProfileExtractor. Each line follows the
        ``ProfileDelta.render_line()`` shape::

            - [auto · {kind} · conf={confidence:.2f} · session={sid}] {text}

        Optional ``topic`` substring filter (case-insensitive) +
        ``kind`` exact filter + ``limit`` cap. Returns [] cleanly
        when no auto-extracted entries exist yet.
        """
        import re as _re_pref

        topic = (call.args.get("topic") or "").strip().lower()
        kind = (call.args.get("kind") or "").strip().lower()
        limit_raw = call.args.get("limit", 10)
        try:
            limit = max(1, min(50, int(limit_raw)))
        except (TypeError, ValueError):
            return _fail(call, t0, f"limit must be integer (got {limit_raw!r})")

        if self._persona_dir_provider is None:
            return _fail(
                call, t0,
                "recall_user_preferences not configured (no persona dir)",
            )
        try:
            persona_root = Path(self._persona_dir_provider())
        except Exception as exc:  # noqa: BLE001
            return _fail(call, t0, f"persona dir resolution failed: {exc}")

        user_md = persona_root / "USER.md"
        if not user_md.is_file():
            return ToolResult(
                call_id=call.id, ok=True,
                content={
                    "entries": [],
                    "note": "USER.md not yet created — no extracted preferences",
                },
                side_effects=(),
                latency_ms=(time.perf_counter() - t0) * 1000.0,
            )

        try:
            text = user_md.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return _fail(call, t0, f"USER.md read failed: {exc}")

        # Locate the section. ProfileExtractor writes / appends below
        # the heading "## Auto-extracted preferences".
        heading = "## Auto-extracted preferences"
        idx = text.find(heading)
        if idx < 0:
            return ToolResult(
                call_id=call.id, ok=True,
                content={
                    "entries": [],
                    "note": "USER.md has no `## Auto-extracted "
                            "preferences` section yet — ProfileExtractor "
                            "hasn't flushed any deltas",
                },
                side_effects=(),
                latency_ms=(time.perf_counter() - t0) * 1000.0,
            )

        section = text[idx + len(heading):]
        # Stop at the next top-level heading so we don't bleed into
        # whatever the user / hand-curated content put after.
        nxt = section.find("\n## ")
        if nxt > 0:
            section = section[:nxt]

        # Match lines emitted by ProfileDelta.render_line(). Tolerant
        # of whitespace + accepts both ASCII and CJK middle dots
        # (·) so future renderer tweaks don't silently break the
        # parser.
        pattern = _re_pref.compile(
            r"^\s*-\s*\[auto\s*[·.]\s*([^·.\]]+?)\s*[·.]\s*conf=([\d.]+)\s*"
            r"[·.]\s*session=([^\]]+?)\]\s*(.+)\s*$"
        )
        entries: list[dict[str, Any]] = []
        for line in section.splitlines():
            m = pattern.match(line)
            if m is None:
                continue
            entry_kind = m.group(1).strip().lower()
            try:
                conf = float(m.group(2))
            except ValueError:
                continue
            entry_session = m.group(3).strip()
            entry_text = m.group(4).strip()
            if kind and entry_kind != kind:
                continue
            if topic and topic not in entry_text.lower():
                continue
            entries.append({
                "kind": entry_kind,
                "text": entry_text,
                "confidence": round(conf, 3),
                "session": entry_session,
            })
            if len(entries) >= limit:
                break

        return ToolResult(
            call_id=call.id, ok=True,
            content={"entries": entries, "matched": len(entries)},
            side_effects=(),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _journal_recall(self, call: ToolCall, t0: float) -> ToolResult:
        """Epic #24 Phase 2.5: read past session journals.

        Loads ``JournalReader`` lazily so a config without persona/
        memory wiring still surfaces the tool cleanly. Filters:

        * ``limit`` (1-50, default 5)
        * ``days_back`` (default 30) drops entries older than that
        * ``contains`` substring filter on tool name list

        Returns one dict per matching entry with the journal fields
        the agent typically wants to reason about (session_id,
        ts_end ISO, duration_s, turn_count, tool names, grader avg).
        """
        from xmclaw.core.journal import JournalReader

        limit_raw = call.args.get("limit", 5)
        days_back_raw = call.args.get("days_back", 30)
        contains = (call.args.get("contains") or "").strip().lower()

        try:
            limit = max(1, min(50, int(limit_raw)))
        except (TypeError, ValueError):
            return _fail(call, t0, f"limit must be integer (got {limit_raw!r})")
        try:
            days_back = max(1, int(days_back_raw))
        except (TypeError, ValueError):
            return _fail(
                call, t0,
                f"days_back must be integer (got {days_back_raw!r})",
            )

        reader = JournalReader()
        # Pull a couple extra so the days_back / contains filters have
        # room to drop without ending up under the requested limit.
        candidates = reader.recent(limit=max(limit * 4, 20))
        if not candidates:
            return ToolResult(
                call_id=call.id, ok=True,
                content={
                    "entries": [],
                    "note": "journal directory empty — no prior sessions yet",
                },
                side_effects=(),
                latency_ms=(time.perf_counter() - t0) * 1000.0,
            )

        cutoff = time.time() - days_back * 86400
        out: list[dict] = []
        for entry in candidates:
            if entry.ts_end < cutoff:
                continue
            tool_names = [tc.name for tc in entry.tool_calls]
            if contains and not any(
                contains in (n or "").lower() for n in tool_names
            ):
                continue
            out.append({
                "session_id": entry.session_id,
                "ts_end_iso": time.strftime(
                    "%Y-%m-%d %H:%M:%S",
                    time.localtime(entry.ts_end),
                ),
                "duration_s": round(entry.duration_s, 1),
                "turn_count": entry.turn_count,
                "tool_names": tool_names,
                "tool_errors": sum(
                    1 for tc in entry.tool_calls if not tc.ok
                ),
                "grader_avg": (
                    round(entry.grader_avg_score, 3)
                    if entry.grader_avg_score is not None else None
                ),
                "grader_play_count": entry.grader_play_count,
                "anti_req_violations": entry.anti_req_violations,
            })
            if len(out) >= limit:
                break

        return ToolResult(
            call_id=call.id, ok=True,
            content={"entries": out, "matched": len(out)},
            side_effects=(),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _read_conversation_history(
        self, call: ToolCall, t0: float,
    ) -> ToolResult:
        """Browse current session history chronologically.

        Loads from the wired ``session_store`` (SQLite) so the tool
        works even if the in-memory cache was lost (daemon restart,
        /new, etc.).
        """
        if self._session_store is None:
            return _fail(
                call, t0,
                "read_conversation_history not configured "
                "(no session_store wired)",
            )

        offset_raw = call.args.get("offset", 0)
        limit_raw = call.args.get("limit", 10)
        direction = str(call.args.get("direction") or "newest").lower().strip()

        try:
            offset = max(0, int(offset_raw))
        except (TypeError, ValueError):
            return _fail(call, t0, f"offset must be integer (got {offset_raw!r})")
        try:
            limit = max(1, min(50, int(limit_raw)))
        except (TypeError, ValueError):
            return _fail(call, t0, f"limit must be integer (got {limit_raw!r})")
        if direction not in ("newest", "oldest"):
            return _fail(
                call, t0,
                f"direction must be 'newest' or 'oldest' (got {direction!r})",
            )

        sid = call.session_id or "_default"
        # B-PERF: offload SQLite read to thread so the event loop
        # isn't blocked on disk I/O inside the tool invoke path.
        import asyncio as _asyncio
        history = await _asyncio.to_thread(self._session_store.load, sid)
        if history is None:
            return ToolResult(
                call_id=call.id, ok=True,
                content={
                    "entries": [],
                    "note": "no persisted history for this session yet",
                },
                side_effects=(),
                latency_ms=(time.perf_counter() - t0) * 1000.0,
            )

        total = len(history)
        if direction == "newest":
            start = max(0, total - limit - offset)
            end = total - offset
            slice_ = history[start:end]
            slice_.reverse()
        else:
            start = offset
            end = min(total, offset + limit)
            slice_ = history[start:end]

        entries: list[dict[str, Any]] = []
        for m in slice_:
            content = m.content or ""
            if isinstance(content, list):
                content = "".join(
                    p.get("text", "") for p in content if isinstance(p, dict)
                )
            preview = str(content)[:280]
            if len(str(content)) > 280:
                preview += "…"
            entries.append({
                "role": m.role,
                "preview": preview,
                "has_tool_calls": bool(m.tool_calls),
                "tool_call_id": m.tool_call_id,
            })

        return ToolResult(
            call_id=call.id, ok=True,
            content={
                "entries": entries,
                "total_messages": total,
                "returned": len(entries),
                "offset": offset,
                "direction": direction,
            },
            side_effects=(),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    # 2026-05-26: agent-callable curation handlers. See _specs.py
    # for the user-facing tool descriptions. Each handler resolves
    # the v2 MemoryService via the same _LAST_APP_STATE indirection
    # _memory_compact uses, so the tool stays usable from worktrees
    # / agents that never reach into factory.py.

    @staticmethod
    def _resolve_memory_v2_service() -> "Any":
        """Best-effort lookup of the running daemon's MemoryService.

        Returns ``None`` when the daemon isn't up or the v2 store
        wasn't wired (in-process tests, --no-memory boot, ...).
        """
        try:
            from xmclaw.daemon import app as _app_mod
            state = getattr(_app_mod, "_LAST_APP_STATE", None)
        except Exception:  # noqa: BLE001
            return None
        if state is None:
            return None
        return getattr(state, "memory_v2_service", None)

    async def _memory_forget(self, call: ToolCall, t0: float) -> ToolResult:
        svc = self._resolve_memory_v2_service()
        if svc is None:
            return _fail(
                call, t0,
                "memory_forget unavailable: v2 memory service not wired",
            )
        query = str(call.args.get("query") or "").strip()
        if not query:
            return _fail(call, t0, "missing or empty 'query'")
        max_matches = max(1, min(10, int(call.args.get("max_matches") or 3)))
        reason = str(call.args.get("reason") or "").strip() or None

        # 2026-05-29 cleanup: share the recall→forget loop with the
        # v3 multi-action ``memory(action='forget')`` path. The
        # legacy wire format used ``"id"`` instead of ``"fid"``;
        # remap here so existing chat history / UI doesn't break.
        forgotten_internal = await self._forget_by_query(
            svc, query=query, max_matches=max_matches, reason=reason,
        )
        forgotten = [
            {"id": f["fid"], "text": f["text"], "distance": f["distance"]}
            for f in forgotten_internal
        ]
        return ToolResult(
            call_id=call.id, ok=True,
            content={
                "query": query,
                "forgotten_count": len(forgotten),
                "forgotten": forgotten,
                "reason": reason,
            },
            error=None,
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _memory_correct(self, call: ToolCall, t0: float) -> ToolResult:
        svc = self._resolve_memory_v2_service()
        if svc is None:
            return _fail(
                call, t0,
                "memory_correct unavailable: v2 memory service not wired",
            )
        old_text = str(call.args.get("old_text") or "").strip()
        new_text = str(call.args.get("new_text") or "").strip()
        if not old_text:
            return _fail(call, t0, "missing or empty 'old_text'")
        if not new_text:
            return _fail(call, t0, "missing or empty 'new_text'")
        kind = call.args.get("kind") or None
        scope = call.args.get("scope") or None

        result = await svc.correct(
            old_text=old_text,
            new_text=new_text,
            kind=str(kind) if kind else None,
            scope=str(scope) if scope else None,
        )
        return ToolResult(
            call_id=call.id, ok=True,
            content=result,
            error=None,
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _memory_dedup(self, call: ToolCall, t0: float) -> ToolResult:
        svc = self._resolve_memory_v2_service()
        if svc is None:
            return _fail(
                call, t0,
                "memory_dedup unavailable: v2 memory service not wired",
            )
        kind = call.args.get("kind") or None
        scope = call.args.get("scope") or None
        bucket = call.args.get("bucket") or None
        dry_run = bool(call.args.get("dry_run", True))
        # 2026-05-29: mode="llm" runs paraphrase-level semantic dedup
        # (catches "空消息超3轮停止" said 7 different ways that cosine
        # clustering misses). mode="vector" (default) is the fast
        # embedding-cosine pass.
        mode = str(call.args.get("mode") or "vector").lower()
        if mode == "llm":
            result = await svc.llm_dedup_scope(
                kind=str(kind) if kind else None,
                scope=str(scope) if scope else None,
                bucket=str(bucket) if bucket else None,
                dry_run=dry_run,
            )
        else:
            result = await svc.dedup_scope(
                kind=str(kind) if kind else None,
                scope=str(scope) if scope else None,
                bucket=str(bucket) if bucket else None,
                dry_run=dry_run,
            )
        return ToolResult(
            call_id=call.id, ok=True,
            content=result,
            error=None,
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    def _schedule_commitment_cron(
        self,
        *,
        fid: str,
        text: str,
        due_ts: float,
    ) -> dict[str, Any]:
        """2026-05-28 memory v3 phase 4.3: schedule a one-shot cron
        that fires the commitment back as a proactive prompt at
        ``due_ts``.

        Always returns the same dict shape so the caller doesn't
        have to branch on key presence::

            {
              "scheduled":      bool,
              "cron_id":        str | None,
              "fires_at":       float | None,
              "skipped_reason": str | None,
            }

        Never raises.

        Implementation note (2026-05-29 cleanup): ``CronStore.add``
        only calls ``parse_schedule`` when ``next_run_at == 0``, so
        we pre-fill ``next_run_at=due_ts`` directly on the
        ``CronJob`` and use a sentinel ``schedule`` string that the
        store never re-parses (``run_once=True`` deletes the job
        after firing, so the schedule string is effectively dead
        metadata). This avoids inventing a ``"@once <ts>"`` syntax
        that ``parse_schedule`` doesn't accept — earlier draft hit
        the silent-fallback path that schedules everything 1 hour
        from now.
        """
        import time as _time
        skipped = {
            "scheduled": False,
            "cron_id": None,
            "fires_at": None,
            "skipped_reason": None,
        }
        try:
            from xmclaw.core.scheduler.cron import CronJob, default_cron_store
        except Exception as exc:  # noqa: BLE001
            return {**skipped, "skipped_reason": f"cron module unavailable: {exc}"}
        try:
            store = default_cron_store()
        except Exception as exc:  # noqa: BLE001
            return {**skipped, "skipped_reason": f"cron store unavailable: {exc}"}

        now = _time.time()
        delta_s = max(0.0, float(due_ts) - now)
        if delta_s < 1.0:
            return {**skipped, "skipped_reason": "due_ts is in the past or now"}

        import uuid as _uuid
        cron_id = f"commitment-{fid[:8]}-{_uuid.uuid4().hex[:6]}"
        # Cron prompt fires as if the user said this; AgentLoop
        # treats it like any other turn — including auto-recall,
        # so the agent gets the full context (fid + bucket).
        prompt = (
            f"[Commitment due — fid:{fid}] {text}\n\n"
            f"Resolve this commitment. When fully handled, call "
            f"``memory(action='forget', old_fid='{fid}', reason='commitment fulfilled')``."
        )
        # See class docstring above: bypass parse_schedule by
        # pre-filling next_run_at. The ``schedule`` string is kept
        # human-readable for the cron list UI; it's never re-parsed
        # because ``run_once=True`` removes the job on first fire.
        job = CronJob(
            id=cron_id,
            name=f"commitment {fid[:8]}",
            schedule=f"one-shot @ {int(due_ts)}",
            prompt=prompt,
            enabled=True,
            run_once=True,
            next_run_at=float(due_ts),
        )
        try:
            store.add(job)
        except Exception as exc:  # noqa: BLE001
            return {**skipped, "skipped_reason": f"cron add failed: {exc}"}
        return {
            "scheduled": True,
            "cron_id": cron_id,
            "fires_at": float(due_ts),
            "skipped_reason": None,
        }

    async def _forget_by_query(
        self,
        svc: Any,
        *,
        query: str,
        max_matches: int,
        reason: str | None,
    ) -> list[dict[str, Any]]:
        """2026-05-29 cleanup: shared recall-then-forget loop used by
        both ``_memory_forget`` (legacy single-purpose tool) and
        ``_memory_multi_action(action='forget', query=...)``. Pre-
        cleanup the loop was implemented in both places — drift
        risk on any future change to forget semantics. Returns the
        ``forgotten`` list (one dict per successful forget)."""
        hits = await svc.recall(
            query, k=max_matches,
            min_confidence=0.0,
            include_relations=False,
            include_superseded=False,
        )
        forgotten: list[dict[str, Any]] = []
        for h in hits:
            if await svc.forget(fact_id=h.fact.id, reason=reason):
                forgotten.append({
                    "fid": h.fact.id,
                    "text": (h.fact.text or "")[:200],
                    "distance": round(float(h.distance), 3),
                })
        return forgotten

    def _build_due_marker(self, text: str, due_ts: Any) -> str:
        """Inline ``[due:YYYY-MM-DDTHH:MMZ]`` marker for commitments.
        Returns the original text unchanged if ``due_ts`` can't be
        parsed. The marker is intentionally schema-neutral — see the
        ``due_ts`` reservation note in the Phase 4.5 follow-up."""
        try:
            iso = time.strftime(
                "%Y-%m-%dT%H:%MZ", time.gmtime(float(due_ts)),
            )
            return f"[due:{iso}] {text}"
        except (TypeError, ValueError):
            return text

    async def _memory_multi_action(
        self, call: ToolCall, t0: float,
    ) -> ToolResult:
        """2026-05-28 memory v3 phase 4.1 — single tool, 4 actions.

        Dispatches to the existing MemoryService primitives via the
        same indirection (``_resolve_memory_v2_service``). The
        legacy single-purpose tools (``remember`` / ``memory_pin`` /
        ``memory_correct`` / ``memory_forget``) keep working
        unchanged for backward compat.

        Bucket inference / validation lives in
        ``xmclaw.memory.v2.buckets`` (registry). Unknown buckets
        coerce to ``misc`` at service-level so the agent never gets
        a "wrong bucket name" error — the fact still lands, just
        in the catch-all section of MEMORY.md.
        """
        action = (call.args.get("action") or "").strip()
        if action not in ("add", "replace", "forget", "pin"):
            return _fail(
                call, t0,
                f"action must be add/replace/forget/pin, got {action!r}",
            )

        svc = self._resolve_memory_v2_service()
        if svc is None:
            return _fail(
                call, t0,
                f"memory({action!r}) unavailable: v2 memory service not wired",
            )

        from xmclaw.memory.v2.buckets import resolve as _resolve_bucket

        text = (call.args.get("text") or "").strip()
        bucket = (call.args.get("bucket") or "").strip()
        scope = (call.args.get("scope") or "user").strip()
        kind = (call.args.get("kind") or "").strip()
        confidence = float(call.args.get("confidence") or 0.85)
        due_ts = call.args.get("due_ts")
        reason = (call.args.get("reason") or "").strip() or None

        # ── action: add / pin ─────────────────────────────────────
        # Pin = add with a confidence floor. v3 phase 4.1 note: this
        # is a soft pin — dedup/compact see it as a high-priority
        # survivor, but ``forget(fid)`` will still remove it. True
        # hard-pin needs a ``pinned`` column on the Fact model
        # (Phase 4.5 schema bump, paired with ``due_ts``).
        if action in ("add", "pin"):
            if not text:
                return _fail(call, t0, f"memory({action}) requires 'text'")
            bdef = _resolve_bucket(bucket)
            effective_kind = kind or bdef.default_kind
            if bdef.tag == "commitment" and not due_ts:
                return _fail(
                    call, t0,
                    f"memory({action} bucket=commitment) requires 'due_ts'.",
                )
            text_to_store = (
                self._build_due_marker(text, due_ts)
                if bdef.tag == "commitment" and due_ts else text
            )
            effective_conf = (
                max(0.95, confidence) if action == "pin" else confidence
            )
            gateway = getattr(self, "_memory_gateway", None)
            if gateway is not None:
                from xmclaw.memory.v2.gateway_models import Observation
                fact = await gateway.ingest(
                    Observation(
                        source="tool_invoked",
                        content=text_to_store,
                        turn_id=getattr(call, "id", "") or "tool",
                        timestamp=time.time(),
                        metadata={
                            "kind_hint": effective_kind,
                            "scope_hint": scope,
                            "bucket_hint": bdef.tag,
                            "confidence_hint": effective_conf,
                            "source_event_id": getattr(call, "id", None),
                        },
                    ),
                    context={"tool_call": True},
                )
            else:
                fact = await svc.remember(
                    text_to_store,
                    kind=effective_kind,
                    scope=scope,
                    confidence=effective_conf,
                    bucket=bdef.tag,
                    source_event_id=getattr(call, "id", None),
                    provenance="tool_invoked",
                )
            cron_info: dict[str, Any] | None = None
            if action == "add" and bdef.tag == "commitment" and due_ts:
                try:
                    cron_info = self._schedule_commitment_cron(
                        fid=fact.id, text=text, due_ts=float(due_ts),
                    )
                except Exception as exc:  # noqa: BLE001
                    cron_info = {
                        "scheduled": False, "cron_id": None,
                        "fires_at": None,
                        "skipped_reason": f"{type(exc).__name__}: {exc}",
                    }
            return ToolResult(
                call_id=call.id, ok=True,
                content={
                    "action": action,
                    "fid": fact.id,
                    "bucket": fact.bucket,
                    "rendered_to": [bdef.target_file],
                    "section": bdef.section,
                    "confidence": fact.confidence,
                    "cron": cron_info,
                },
                error=None,
                latency_ms=(time.perf_counter() - t0) * 1000.0,
            )

        # ── action: replace ───────────────────────────────────────
        # Both old_fid and old_text paths now flow through
        # ``service.correct`` — single supersede pipeline, no
        # duplicate forget+remember code, SUPERSEDES edge always
        # created.
        if action == "replace":
            if not text:
                return _fail(call, t0, "memory(replace) requires 'text' (new value)")
            old_fid = (call.args.get("old_fid") or "").strip()
            old_text = (call.args.get("old_text") or "").strip()
            if not old_fid and not old_text:
                return _fail(
                    call, t0,
                    "memory(replace) requires 'old_fid' OR 'old_text'",
                )
            result = await svc.correct(
                old_text=old_text,
                new_text=text,
                old_fact_id=old_fid or None,
                kind=kind or None,
                scope=scope or None,
                bucket=bucket or None,
            )
            return ToolResult(
                call_id=call.id, ok=True,
                content={
                    "action": "replace",
                    "via": "old_fid" if old_fid else "old_text",
                    **result,
                },
                error=None,
                latency_ms=(time.perf_counter() - t0) * 1000.0,
            )

        # ── action: forget ────────────────────────────────────────
        # (action == "forget" — the only branch left after the
        # validation gate at the top of the method)
        old_fid = (call.args.get("old_fid") or "").strip()
        query = (call.args.get("query") or "").strip()
        if old_fid:
            ok = await svc.forget(fact_id=old_fid, reason=reason)
            return ToolResult(
                call_id=call.id, ok=True,
                content={
                    "action": "forget",
                    "via": "old_fid",
                    "forgotten": [{"fid": old_fid, "ok": ok}],
                },
                error=None,
                latency_ms=(time.perf_counter() - t0) * 1000.0,
            )
        if not query:
            return _fail(
                call, t0,
                "memory(forget) requires 'old_fid' OR 'query'",
            )
        max_matches = max(1, min(10, int(call.args.get("max_matches") or 3)))
        forgotten = await self._forget_by_query(
            svc, query=query, max_matches=max_matches, reason=reason,
        )
        return ToolResult(
            call_id=call.id, ok=True,
            content={
                "action": "forget",
                "via": "query",
                "query": query,
                "forgotten_count": len(forgotten),
                "forgotten": forgotten,
                "reason": reason,
            },
            error=None,
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _memory_get(self, call: ToolCall, t0: float) -> ToolResult:
        """2026-05-28 memory v3 phase 4.2 — read a persona MD file
        verbatim, optionally narrowed by section or line range.

        The output preserves ``<!-- fid:xxx -->`` markers so the
        agent can grab fids straight out of the file and feed them
        into ``memory(action='replace'/'forget', old_fid=...)``.

        Canonical-name resolution: the shared
        ``_PERSONA_BASENAMES_LOOKUP`` table from ``_helpers`` is the
        single source of truth — the same one ``update_persona``
        uses, so a file the agent can edit via one tool is also
        readable via the other. The empty-but-known fallback uses
        ``buckets.known_files()`` so adding a new persona file
        anywhere in the registry automatically expands what
        ``memory_get`` recognises.
        """
        file_arg = (call.args.get("file") or "").strip()
        if not file_arg:
            return _fail(call, t0, "memory_get requires 'file'")
        try:
            pdir = self._persona_dir_provider() if (
                self._persona_dir_provider is not None
            ) else None
        except Exception as exc:  # noqa: BLE001
            return _fail(
                call, t0,
                f"persona dir lookup failed: {type(exc).__name__}: {exc}",
            )
        if pdir is None:
            return _fail(
                call, t0,
                "memory_get unavailable: persona profile dir not configured",
            )
        # Canonical name from the shared lookup. Falls back to the
        # raw arg if the user passed a non-persona-managed file the
        # daemon happens to have under the profile dir.
        canonical = _PERSONA_BASENAMES_LOOKUP.get(
            file_arg.lower().removesuffix(".md"),
            _PERSONA_BASENAMES_LOOKUP.get(file_arg.lower(), file_arg),
        )
        target = Path(pdir) / canonical
        if not target.is_file():
            # Known persona file but not rendered yet — return empty
            # content with a helpful note rather than an error.
            from xmclaw.memory.v2.buckets import known_files
            known = {n.lower() for n in known_files()} | {"bootstrap.md"}
            if canonical.lower() in known:
                return ToolResult(
                    call_id=call.id, ok=True,
                    content={
                        "file": canonical,
                        "content": "",
                        "fids_present": [],
                        "note": "file doesn't exist on disk yet (no facts in any of its buckets)",
                    },
                    error=None,
                    latency_ms=(time.perf_counter() - t0) * 1000.0,
                )
            return _fail(
                call, t0,
                f"file {file_arg!r} not found in persona dir {pdir}",
            )
        try:
            full_text = target.read_text(encoding="utf-8")
        except OSError as exc:
            return _fail(
                call, t0,
                f"read failed: {type(exc).__name__}: {exc}",
            )

        # Section filter — extract everything between this ## header
        # and the next ## (or EOF). The header arg accepts both
        # ``## Foo`` and ``Foo``; we always normalise to the former.
        section = call.args.get("section")
        if isinstance(section, str) and section.strip():
            header = "## " + section.strip().lstrip("#").strip()
            lines_all = full_text.splitlines(keepends=True)
            keep: list[str] = []
            inside = False
            for line in lines_all:
                stripped = line.strip()
                if stripped == header:
                    inside = True
                    keep.append(line)
                    continue
                if inside and stripped.startswith("## "):
                    break
                if inside:
                    keep.append(line)
            full_text = "".join(keep) if keep else (
                f"(section {header!r} not found in {canonical})"
            )

        # Line range — 'start-end' (1-indexed, inclusive).
        line_arg = call.args.get("lines")
        if isinstance(line_arg, str) and line_arg.strip():
            m = _LINES_RANGE_RE.match(line_arg)
            if not m:
                return _fail(
                    call, t0,
                    f"lines must match 'start-end' (1-indexed), got {line_arg!r}",
                )
            start = max(1, int(m.group(1)))
            end = max(start, int(m.group(2)))
            lines_all = full_text.splitlines()
            full_text = "\n".join(lines_all[start - 1: end])

        # Pull fids out of the kept content so the agent has them
        # ready for the next memory(...) call.
        fids_present = _FID_MARKER_RE.findall(full_text)

        return ToolResult(
            call_id=call.id, ok=True,
            content={
                "file": canonical,
                "section": section if isinstance(section, str) else None,
                "lines": line_arg if isinstance(line_arg, str) else None,
                "content": full_text,
                "fids_present": list(dict.fromkeys(fids_present)),
                "chars": len(full_text),
            },
            error=None,
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _memory_inspect(self, call: ToolCall, t0: float) -> ToolResult:
        """2026-05-28: read-only health probe.

        Reports total fact count, per-(scope, kind) breakdown, top
        oldest entries, top largest entries, and an estimated
        duplicate ratio per scope (from a sample of facts vs
        embedding cosine).

        The agent uses this to decide whether to run memory_dedup /
        memory_forget without being asked.
        """
        svc = self._resolve_memory_v2_service()
        if svc is None:
            return _fail(
                call, t0,
                "memory_inspect unavailable: v2 memory service not wired",
            )
        scope = call.args.get("scope") or None
        sample = int(call.args.get("sample_dup_check", 500) or 500)
        sample = max(50, min(2000, sample))

        # List facts to inspect. Cap the scan so big stores don't
        # block the turn — 5K is plenty for the breakdown.
        try:
            facts = await svc.recall(
                None,
                k=5000,
                scopes=[scope] if scope else None,
                min_confidence=0.0,
                include_relations=False,
                include_superseded=False,
            )
        except Exception as exc:  # noqa: BLE001
            return _fail(
                call, t0,
                f"memory_inspect scan failed: "
                f"{type(exc).__name__}: {exc}",
            )

        # Aggregate counts by (scope, kind).
        breakdown: dict[str, dict[str, int]] = {}
        oldest: list[tuple[float, str, str]] = []  # (ts, scope, text)
        largest: list[tuple[int, str, str]] = []   # (size, scope, text)
        for h in facts:
            f = h.fact
            s = str(getattr(f, "scope", "?") or "?")
            k = str(getattr(f, "kind", "?") or "?")
            breakdown.setdefault(s, {}).setdefault(k, 0)
            breakdown[s][k] += 1
            ts = float(getattr(f, "ts_last", 0) or 0)
            oldest.append((ts, s, (f.text or "")[:120]))
            largest.append((len(f.text or ""), s, (f.text or "")[:120]))

        oldest.sort(key=lambda x: x[0])
        largest.sort(key=lambda x: x[0], reverse=True)

        # Estimate duplicate ratio per scope by sampling and
        # cosine-clustering at the same 0.86 threshold dedup uses.
        from math import sqrt
        dup_ratios: dict[str, dict[str, float | int]] = {}
        for s, by_kind in breakdown.items():
            sample_set = [
                h for h in facts
                if str(getattr(h.fact, "scope", "?") or "?") == s
            ][:sample]
            if len(sample_set) < 2:
                continue
            clusters: list[list[Any]] = []
            for h in sample_set:
                emb = h.fact.embedding
                if not emb:
                    clusters.append([h])
                    continue
                placed = False
                for cluster in clusters:
                    ref = cluster[0].fact.embedding
                    if not ref:
                        continue
                    dot = sum(a * b for a, b in zip(emb, ref))
                    na = sqrt(sum(a * a for a in emb))
                    nb = sqrt(sum(b * b for b in ref))
                    cos = dot / (na * nb) if (na and nb) else 0.0
                    if cos >= 0.86:
                        cluster.append(h)
                        placed = True
                        break
                if not placed:
                    clusters.append([h])
            n_dup_clusters = sum(1 for c in clusters if len(c) > 1)
            n_excess = sum(len(c) - 1 for c in clusters if len(c) > 1)
            ratio = (
                n_excess / len(sample_set)
                if sample_set else 0.0
            )
            dup_ratios[s] = {
                "sample_size": len(sample_set),
                "dup_clusters": n_dup_clusters,
                "excess_facts": n_excess,
                "dup_ratio": round(ratio, 3),
            }

        # Recommendation hint — gives the agent an explicit signal.
        recommendations: list[str] = []
        for s, stats in dup_ratios.items():
            ratio_v = stats.get("dup_ratio") or 0.0
            if isinstance(ratio_v, (int, float)) and ratio_v >= 0.15:
                recommendations.append(
                    f"memory_dedup(scope={s!r}, dry_run=true) — "
                    f"{stats['excess_facts']} excess in {stats['sample_size']}-sample"
                )

        return ToolResult(
            call_id=call.id, ok=True,
            content={
                "total_facts": len(facts),
                "scope_filter": scope,
                "breakdown": breakdown,
                "oldest_5": [
                    {"ts": ts, "scope": s, "text": t}
                    for ts, s, t in oldest[:5]
                ],
                "largest_5": [
                    {"chars": n, "scope": s, "text": t}
                    for n, s, t in largest[:5]
                ],
                "dup_estimate": dup_ratios,
                "recommendations": recommendations or [
                    "no action needed — store looks tidy.",
                ],
            },
            error=None,
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _memory_graph_neighbors(
        self, call: ToolCall, t0: float,
    ) -> ToolResult:
        """B-240: graph walk from a known fact_id."""
        svc = self._resolve_memory_v2_service()
        if svc is None:
            return _fail(
                call, t0,
                "memory_graph_neighbors unavailable: v2 memory service not wired",
            )
        fact_id = str(call.args.get("fact_id") or "").strip()
        if not fact_id:
            return _fail(call, t0, "missing or empty 'fact_id'")

        relation_types = call.args.get("relation_types")
        if isinstance(relation_types, str):
            relation_types = [relation_types]
        elif not isinstance(relation_types, (list, tuple)):
            relation_types = None

        max_hops = max(1, min(3, int(call.args.get("max_hops") or 1)))

        try:
            edges = await svc.neighbors(
                fact_id,
                relation_types=relation_types,
                max_hops=max_hops,
            )
        except Exception as exc:  # noqa: BLE001
            return _fail(call, t0, f"graph_neighbors failed: {exc}")

        # Hydrate target facts in parallel (cap to avoid flooding).
        limit = 20
        trimmed = edges[:limit]
        import asyncio as _asyncio

        async def _hydrate(rel: Any, target_id: str) -> dict[str, Any]:
            try:
                fact = await svc.get(target_id)
                text = (fact.text or "")[:200] if fact is not None else ""
            except Exception:  # noqa: BLE001
                text = ""
            return {
                "relation": rel.relation,
                "target_fact_id": target_id,
                "strength": round(float(rel.strength or 0.0), 3),
                "text_preview": text,
            }

        items = await _asyncio.gather(*[
            _hydrate(rel, tid) for rel, tid in trimmed
        ])

        return ToolResult(
            call_id=call.id, ok=True,
            content={
                "fact_id": fact_id,
                "max_hops": max_hops,
                "relation_types": relation_types,
                "total_edges": len(edges),
                "returned": len(items),
                "neighbors": items,
            },
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _memory_compact(self, call: ToolCall, t0: float) -> ToolResult:
        """B-52: trigger Auto-Dream now (instead of waiting for the
        daily cron). Reaches the running compactor via the same
        ``_LAST_APP_STATE`` holder factory.py uses for persona-writeback.
        Refuses cleanly when no LLM is configured."""
        try:
            from xmclaw.daemon import app as _app_mod
            state = getattr(_app_mod, "_LAST_APP_STATE", None)
        except Exception:  # noqa: BLE001
            state = None
        if state is None:
            return _fail(call, t0, "daemon not started (no app.state available)")
        compactor = getattr(state, "dream_compactor", None)
        if compactor is None:
            return _fail(
                call, t0,
                "memory_compact unavailable: no LLM configured for dream",
            )
        result = await compactor.dream()
        return ToolResult(
            call_id=call.id, ok=bool(result.get("ok")),
            content=result,
            error=None if result.get("ok") else result.get("error"),
            side_effects=(result.get("memory_path") or "",) if result.get("ok") else (),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

