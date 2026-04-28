"""BuiltinFileMemoryProvider — wraps the persona MEMORY.md / USER.md
files as a :class:`MemoryProvider`.

Hermes' ``BuiltinMemoryProvider`` does this: the persona files are
addressable through the same provider interface as any pluggable
backend, so the agent_loop / manager don't special-case them. We
mirror it so XMclaw's manager is uniform — built-in is just the
first provider in the list, distinguished by its ``name`` only.

What this provider does:
  * ``put`` — appends a bullet to ``<file>``, with the existing
    write-time dedup logic (delegates to ``builtin.py``'s
    ``_append_under_section``)
  * ``query`` — substring search across the canonical files, returns
    matching lines as ``MemoryItem`` rows
  * ``system_prompt_block`` — empty (the persona ASSEMBLER already
    injects the file content; we don't double-inject)

What it does NOT do:
  * Embeddings — that's the SqliteVecMemory provider's job
  * Knowledge graph / entity resolution — out of scope; future
    plugin providers (hindsight etc.) handle that
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from xmclaw.providers.memory.base import Layer, MemoryItem, MemoryProvider


class BuiltinFileMemoryProvider(MemoryProvider):
    """Always-on file-backed memory provider. Reads/writes the active
    persona profile's MEMORY.md and USER.md."""

    name = "builtin"

    def __init__(
        self,
        persona_dir_provider: "object | None" = None,
    ) -> None:
        """``persona_dir_provider``: callable returning the active
        profile dir (``~/.xmclaw/persona/profiles/<active>/``). When
        None, falls back to the canonical default path."""
        self._persona_dir_provider = persona_dir_provider

    def _fs_lock(self, path):
        """B-65: shared module-level lock store so DreamCompactor
        + BuiltinTools writers see the same mutex for the same path."""
        from xmclaw.utils.fs_locks import get_lock
        return get_lock(path)

    def _persona_dir(self) -> Path:
        try:
            if self._persona_dir_provider is not None:
                v = self._persona_dir_provider()
                if v is not None:
                    return Path(str(v))
        except Exception:  # noqa: BLE001
            pass
        from xmclaw.utils.paths import persona_dir as _persona_dir_default
        return _persona_dir_default().parent / "profiles" / "default"

    # ── MemoryProvider API ───────────────────────────────────────

    async def put(self, layer: Layer, item: MemoryItem) -> str:
        """Append the item.text to MEMORY.md (or USER.md if metadata
        flags it). Goes through the persona-write append helper so we
        get the same dedup + char-cap handling as the agent's
        ``remember`` / ``learn_about_user`` tools."""
        from xmclaw.providers.tool.builtin import (
            _append_under_section,
            PERSONA_CHAR_CAPS,
            enforce_char_cap,
        )

        target_file = "USER.md" if (item.metadata or {}).get("kind") == "user_fact" else "MEMORY.md"
        section = (item.metadata or {}).get("category") or (item.metadata or {}).get("section") or "General"
        section_header = f"## {section.strip().lstrip('#').strip()}"
        bullet = "- " + time.strftime("%Y-%m-%d") + ": " + item.text.replace("\n", " ").strip()

        pdir = self._persona_dir()
        pdir.mkdir(parents=True, exist_ok=True)
        path = pdir / target_file
        # B-64: serialise so concurrent puts don't lose appends. Same
        # pattern as BuiltinTools._append_persona (B-63).
        async with self._fs_lock(path):
            existing = path.read_text(encoding="utf-8") if path.is_file() else ""
            new_text = _append_under_section(
                existing,
                section_header=section_header,
                bullet=bullet,
                placeholder_title=f"{target_file} — agent-curated",
            )
            cap = PERSONA_CHAR_CAPS.get(target_file)
            if cap is not None and len(new_text) > cap:
                new_text = enforce_char_cap(new_text, cap)
            from xmclaw.utils.fs_locks import atomic_write_text
            atomic_write_text(path, new_text)
        return item.id or uuid.uuid4().hex

    async def query(
        self,
        layer: Layer,
        *,
        text: str | None = None,
        embedding: list[float] | None = None,
        k: int = 10,
        filters: dict[str, Any] | None = None,
    ) -> list[MemoryItem]:
        """Substring search across MEMORY.md + USER.md. Embeddings
        ignored — this provider is keyword-only by design."""
        if not text and not filters:
            # Nothing to match against; return all bullets, newest-first
            return self._all_bullets(k)
        needle = (text or "").strip().lower()
        if not needle:
            return self._all_bullets(k)
        hits: list[MemoryItem] = []
        pdir = self._persona_dir()
        for fname in ("MEMORY.md", "USER.md"):
            path = pdir / fname
            if not path.is_file():
                continue
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            section_header = ""
            for line in content.splitlines():
                stripped = line.strip()
                if stripped.startswith("## "):
                    section_header = stripped.lstrip("# ").strip()
                    continue
                if not stripped.startswith("-"):
                    continue
                if needle in stripped.lower():
                    hits.append(MemoryItem(
                        id=f"{fname}:{stripped[:40]}",
                        layer=layer,
                        text=stripped.lstrip("-").strip(),
                        metadata={
                            "file": fname,
                            "section": section_header,
                            "kind": "persona_bullet",
                        },
                        ts=path.stat().st_mtime,
                    ))
                    if len(hits) >= k:
                        return hits
        return hits

    async def forget(self, item_id: str) -> None:
        """No-op — the file format doesn't have stable item ids
        suitable for direct deletion. Use the Memory page to edit."""

    async def sync_turn(
        self, *, session_id: str, agent_id: str,
        user_content: str, assistant_content: str,
    ) -> None:
        """B-40 (CoPaw parity): append a one-line entry to the daily
        episodic log at ``<persona_dir>/memory/YYYY-MM-DD.md``.

        Gives the agent a per-day chronology distinct from the
        always-summarised MEMORY.md (which gets char-capped + LRU-
        evicted). Daily logs are kept indefinitely — pruning is the
        agent's responsibility (future Auto-Dream cron will compact
        old days into MEMORY.md). The base class default ``sync_turn``
        wrote to MemoryItem-via-put which appended bullets to
        MEMORY.md; that double-tapped the curated file with raw
        turn text. We override to keep MEMORY.md clean (curated
        only) and put episodic detail in the daily log.
        """
        u = (user_content or "").strip().replace("\n", " ")
        a = (assistant_content or "").strip().replace("\n", " ")
        if not u and not a:
            return
        # Trim to keep daily log readable.
        if len(u) > 400:
            u = u[:397] + "..."
        if len(a) > 400:
            a = a[:397] + "..."
        pdir = self._persona_dir()
        log_dir = pdir / "memory"
        log_dir.mkdir(parents=True, exist_ok=True)
        date = time.strftime("%Y-%m-%d")
        ts = time.strftime("%H:%M:%S")
        log_path = log_dir / f"{date}.md"
        header = f"# 对话日志 {date}\n\n"
        entry = (
            f"## {ts} · session={session_id[-8:] if session_id else '?'}\n\n"
            f"**User:** {u}\n\n"
            f"**Assistant:** {a}\n\n"
        )
        # B-64: lock per-day-log path. Two sessions ending the same
        # second on the same day would otherwise race-overwrite the
        # daily log file.
        from xmclaw.utils.fs_locks import atomic_write_text
        async with self._fs_lock(log_path):
            try:
                if log_path.is_file():
                    existing = log_path.read_text(encoding="utf-8", errors="replace")
                    # Keep existing content; append new entry below.
                    if not existing.startswith("# "):
                        existing = header + existing
                    atomic_write_text(log_path, existing.rstrip() + "\n\n" + entry)
                else:
                    atomic_write_text(log_path, header + entry)
            except OSError:
                # Disk-full / permission — best-effort, never block the turn.
                pass

    # ── extended hooks (used by MemoryManager) ───────────────────

    def system_prompt_block(self) -> str:
        """Empty — the persona assembler already reads MEMORY.md /
        USER.md directly when building the system prompt. We don't
        want to double-inject."""
        return ""

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        """No tool schemas — the agent's existing
        ``remember`` / ``learn_about_user`` / ``update_persona`` tools
        ARE this provider's tool surface, registered separately in
        ``BuiltinTools``. Avoid duplicating."""
        return []

    # ── internal ─────────────────────────────────────────────────

    def _all_bullets(self, k: int) -> list[MemoryItem]:
        out: list[MemoryItem] = []
        pdir = self._persona_dir()
        for fname in ("MEMORY.md", "USER.md"):
            path = pdir / fname
            if not path.is_file():
                continue
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
                mtime = path.stat().st_mtime
            except OSError:
                continue
            section_header = ""
            for line in content.splitlines():
                stripped = line.strip()
                if stripped.startswith("## "):
                    section_header = stripped.lstrip("# ").strip()
                    continue
                if not stripped.startswith("-"):
                    continue
                out.append(MemoryItem(
                    id=f"{fname}:{stripped[:40]}",
                    layer="long",
                    text=stripped.lstrip("-").strip(),
                    metadata={
                        "file": fname,
                        "section": section_header,
                        "kind": "persona_bullet",
                    },
                    ts=mtime,
                ))
        out.sort(key=lambda m: -(m.ts or 0))
        return out[:k]
