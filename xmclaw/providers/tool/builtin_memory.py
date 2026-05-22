from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from xmclaw.core.ir import ToolCall, ToolResult
from xmclaw.providers.tool._helpers import _fail as _fail

_VALID_TODO_STATUSES = {"pending", "in_progress", "done"}


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
        history = self._session_store.load(sid)
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

