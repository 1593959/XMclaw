from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from xmclaw.core.ir import ToolCall, ToolResult
from xmclaw.providers.tool._helpers import _fail as _fail



class BuiltinToolsFsMixin:
    """File-system tools: read, write, patch, list, glob, grep, delete."""

    async def _file_read(self, call: ToolCall, t0: float) -> ToolResult:
        """B-57: capped + range-aware file read.

        Three modes, mutually exclusive but resolved by argument
        presence (no explicit mode flag):

        * ``offset`` + ``limit`` set → read line range
        * neither → read up to ``max_bytes`` (default 100KB) from
          the start, append ``[truncated]`` marker if larger
        * Either way: refuse binary-looking files (NUL byte in the
          first 8KB).

        Honors ``allowed_dirs`` sandbox via ``_check_allowed``.
        """
        raw_path = call.args.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            return _fail(call, t0, "missing or empty 'path' argument")
        path = Path(raw_path)
        self._check_allowed(path)
        if not path.exists():
            return _fail(call, t0, f"file not found: {path}")
        if not path.is_file():
            return _fail(call, t0, f"not a file: {path}")

        # Binary heuristic — read first 8KB raw, look for NUL.
        try:
            with path.open("rb") as fh:
                head = fh.read(8192)
        except OSError as exc:
            return _fail(call, t0, f"open failed: {exc}")
        if b"\x00" in head:
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            return _fail(
                call, t0,
                f"file looks binary ({size} bytes, NUL byte in first 8KB) "
                f"— file_read is text-only",
            )

        # Range read (offset + limit) takes precedence.
        offset = call.args.get("offset")
        limit = call.args.get("limit")
        if offset is not None or limit is not None:
            try:
                off_i = int(offset) if offset is not None else 1
                lim_i = int(limit) if limit is not None else 2000
            except (TypeError, ValueError):
                return _fail(call, t0, "offset / limit must be integers")
            off_i = max(1, off_i)
            lim_i = max(1, min(lim_i, 50000))
            try:
                with path.open("r", encoding="utf-8", errors="replace") as fh:
                    lines = []
                    for i, line in enumerate(fh, 1):
                        if i < off_i:
                            continue
                        if len(lines) >= lim_i:
                            break
                        lines.append(line)
            except OSError as exc:
                return _fail(call, t0, f"read failed: {exc}")
            content = "".join(lines)
            return ToolResult(
                call_id=call.id, ok=True, content=content,
                side_effects=(),
                latency_ms=(time.perf_counter() - t0) * 1000.0,
            )

        # Byte-cap mode.
        try:
            max_bytes = int(call.args.get("max_bytes") or 100_000)
        except (TypeError, ValueError):
            max_bytes = 100_000
        max_bytes = max(1024, min(max_bytes, 1_000_000))
        try:
            stat = path.stat()
        except OSError as exc:
            return _fail(call, t0, f"stat failed: {exc}")
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                # Read max_bytes worth — actually char-count not byte
                # since Python decodes. Close enough — UTF-8 char
                # length and byte length are equal for ASCII, ≤4x
                # for CJK; we err on the side of slightly more.
                content = fh.read(max_bytes)
        except OSError as exc:
            return _fail(call, t0, f"read failed: {exc}")
        if stat.st_size > max_bytes:
            content += f"\n\n[truncated, {stat.st_size} total bytes; pass max_bytes or offset/limit for more]"
        return ToolResult(
            call_id=call.id, ok=True, content=content,
            side_effects=(),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _file_write(self, call: ToolCall, t0: float) -> ToolResult:
        raw_path = call.args.get("path")
        text = call.args.get("content")
        if not isinstance(raw_path, str) or not raw_path:
            return _fail(call, t0, "missing or empty 'path' argument")
        if not isinstance(text, str):
            return _fail(
                call, t0,
                f"'content' must be string, got {type(text).__name__}",
            )
        path = Path(raw_path)
        self._check_allowed(path)
        # B-331: visibility signal when the write escapes the
        # configured workspace roots. Doesn't block — sandboxing is
        # a separate UX-design epic.
        self._audit_workspace_containment(path, op="file_write")
        # Sprint 0 Track B: snapshot pre-state for undo. Skipped when
        # no cabinet is wired (test/legacy callers).
        undo_id: str | None = None
        cab = getattr(self, "_undo_cabinet", None)
        if cab is not None:
            try:
                undo_id = cab.record_file_mutation(
                    path=path,
                    action="file_write",
                    args={"bytes": len(text.encode("utf-8"))},
                    session_id=getattr(call, "session_id", None),
                )
            except Exception:  # noqa: BLE001 — never block tool over undo
                undo_id = None
        path.parent.mkdir(parents=True, exist_ok=True)
        from xmclaw.utils.fs_locks import atomic_write_text
        atomic_write_text(path, text)
        # Structured dict for graders and the bus; agent_loop renders
        # it into a readable tool-message string when feeding to the LLM.
        content_dict: dict[str, Any] = {
            "path": str(path),
            "bytes": len(text.encode("utf-8")),
        }
        if undo_id:
            content_dict["undo_id"] = undo_id
        return ToolResult(
            call_id=call.id, ok=True,
            content=content_dict,
            side_effects=(str(path.resolve()),),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _apply_patch(self, call: ToolCall, t0: float) -> ToolResult:
        raw_path = call.args.get("path")
        edits = call.args.get("edits")
        if not isinstance(raw_path, str) or not raw_path:
            return _fail(call, t0, "missing or empty 'path' argument")
        if not isinstance(edits, list) or not edits:
            return _fail(call, t0, "'edits' must be a non-empty list")

        # Pre-validate every edit's shape before touching disk.
        clean: list[tuple[str, str]] = []
        for i, e in enumerate(edits):
            if not isinstance(e, dict):
                return _fail(call, t0, f"edits[{i}] must be an object")
            old_text = e.get("old_text")
            new_text = e.get("new_text")
            if not isinstance(old_text, str) or old_text == "":
                return _fail(call, t0, f"edits[{i}].old_text must be a non-empty string")
            if not isinstance(new_text, str):
                return _fail(call, t0, f"edits[{i}].new_text must be a string")
            clean.append((old_text, new_text))

        path = Path(raw_path)
        self._check_allowed(path)
        # B-331: same workspace-containment audit as file_write.
        self._audit_workspace_containment(path, op="apply_patch")
        if not path.exists() or not path.is_file():
            return _fail(call, t0, f"file does not exist: {path}")
        original = path.read_text(encoding="utf-8")
        text = original

        # Apply edits sequentially. Each old_text must occur exactly once
        # in the *current* text (after prior edits) — so two edits whose
        # search strings overlap are caught here, not silently mis-applied.
        for i, (old_text, new_text) in enumerate(clean):
            count = text.count(old_text)
            if count == 0:
                # B-397 (Sprint 1 stragglers): pre-fix, the error said
                # "file may have changed; re-read it before patching" —
                # the right hint, but real-world LLMs ignored it and
                # repeated the same stale-text edit until max_hops fired
                # (real example: xmclaw-architecture-redesign.md, 40
                # hops, all the same edit). Surface the CURRENT file
                # content + a fuzzy-match suggestion in the error so
                # the LLM has the fresh state inline and can rebase
                # without another file_read round-trip.
                hint = self._stale_match_hint(text, old_text)
                return _fail(
                    call, t0,
                    f"edits[{i}].old_text not found in {path}.\n{hint}",
                )
            if count > 1:
                return _fail(
                    call, t0,
                    f"edits[{i}].old_text occurs {count} times in {path}; "
                    f"include more surrounding context to make it unique",
                )
            text = text.replace(old_text, new_text, 1)

        if text == original:
            return _fail(call, t0, "patch produced no change (every old_text == new_text)")

        # Sprint 0 Track B: snapshot pre-state for undo BEFORE the
        # atomic write. We have the original bytes in ``original`` but
        # the cabinet reads from disk — so we record before the swap.
        undo_id: str | None = None
        cab = getattr(self, "_undo_cabinet", None)
        if cab is not None:
            try:
                undo_id = cab.record_file_mutation(
                    path=path,
                    action="apply_patch",
                    args={"edits_count": len(clean)},
                    session_id=getattr(call, "session_id", None),
                )
            except Exception:  # noqa: BLE001
                undo_id = None

        # Atomic write: temp + replace so a crash mid-write can't truncate.
        tmp = path.with_suffix(path.suffix + ".patch.tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)

        before = len(original.encode("utf-8"))
        after = len(text.encode("utf-8"))
        content_dict: dict[str, Any] = {
            "path": str(path),
            "edits_applied": len(clean),
            "bytes_before": before,
            "bytes_after": after,
            "delta": after - before,
        }
        if undo_id:
            content_dict["undo_id"] = undo_id
        return ToolResult(
            call_id=call.id, ok=True,
            content=content_dict,
            side_effects=(str(path.resolve()),),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    @staticmethod
    def _stale_match_hint(current_text: str, old_text: str) -> str:
        """B-397: when ``old_text`` doesn't match the current file,
        return a hint that gives the LLM enough context to rebase
        without another file_read round-trip.

        Strategy:
          1. If the file is small (≤ 4000 chars), return the whole thing
             — cheaper than guessing.
          2. Otherwise, find the longest substring of ``old_text`` that
             DOES appear in current_text and return ±10 lines of context
             around it. This handles the common case where the edit's
             anchor is right but a few lines drifted (whitespace, prior
             edit replaced part of the chunk, etc).
          3. If nothing of ``old_text`` matches at all, return the first
             80 lines of current_text — enough for the LLM to recognize
             it's looking at the right file and re-anchor.
        """
        max_inline = 4000
        if len(current_text) <= max_inline:
            return (
                "File may have changed since your last read OR the "
                "old_text is from a stale view. The CURRENT file content "
                "is below — re-base your edit and try again WITHOUT "
                "calling file_read first.\n\n"
                "=== CURRENT FILE ===\n"
                f"{current_text}\n"
                "=== END ==="
            )
        # Search for the longest prefix of old_text that occurs in current.
        # Cheap O(n^2) — old_text is bounded by tool args and current_text
        # is bounded by max_inline check above.
        best_anchor = ""
        for length in range(min(len(old_text), 200), 5, -1):
            sub = old_text[:length]
            if sub in current_text:
                best_anchor = sub
                break
        if best_anchor:
            idx = current_text.index(best_anchor)
            # ±10 lines of context.
            before_lines = current_text[:idx].splitlines()[-10:]
            after_chunk = current_text[idx + len(best_anchor):]
            after_lines = after_chunk.splitlines()[:10]
            ctx_lines = (
                before_lines
                + [best_anchor.rstrip(), "<<<< drifted from here >>>>"]
                + after_lines
            )
            ctx = "\n".join(ctx_lines)
            return (
                "File may have changed; partial match found. Context "
                "around where your old_text WOULD have anchored "
                "(±10 lines):\n\n"
                "=== CONTEXT ===\n"
                f"{ctx}\n"
                "=== END ===\n"
                "Re-base your edit on this context and try again."
            )
        # No partial match — show file head.
        head = "\n".join(current_text.splitlines()[:80])
        return (
            "File may have changed and your old_text doesn't appear at "
            "all. First 80 lines of current file:\n\n"
            "=== HEAD ===\n"
            f"{head}\n"
            "=== END ===\n"
            "Re-anchor your edit and try again WITHOUT calling file_read."
        )

    async def _list_dir(self, call: ToolCall, t0: float) -> ToolResult:
        raw_path = call.args.get("path")
        pattern = call.args.get("pattern", "*")
        if not isinstance(raw_path, str) or not raw_path:
            return _fail(call, t0, "missing or empty 'path' argument")
        if not isinstance(pattern, str) or not pattern:
            pattern = "*"
        try:
            limit = int(call.args.get("limit") or 200)
        except (TypeError, ValueError):
            limit = 200
        limit = max(1, min(limit, 5000))
        path = Path(raw_path)
        self._check_allowed(path)
        if not path.exists():
            return _fail(call, t0, f"path does not exist: {path}")
        if not path.is_dir():
            return _fail(call, t0, f"not a directory: {path}")
        # B-58: stream entries one-by-one with an entry-count cap so a
        # huge dir doesn't flood the LLM context. We collect into a
        # list because path.glob's order isn't sorted; sort *all*
        # then truncate vs sort *truncated* — small price for
        # determinism, and a 5000-entry sort is sub-ms.
        try:
            all_entries = sorted(path.glob(pattern))
        except OSError as exc:
            return _fail(call, t0, f"glob failed: {exc}")
        total = len(all_entries)
        truncated = total > limit
        kept = all_entries[:limit]
        lines: list[str] = []
        for entry in kept:
            kind = "l" if entry.is_symlink() else (
                "d" if entry.is_dir() else "f"
            )
            try:
                size = entry.stat().st_size if kind == "f" else 0
            except OSError:
                size = 0
            lines.append(f"{kind} {size:>10} {entry.name}")
        body = "\n".join(lines) if lines else f"(no entries matching {pattern!r})"
        if truncated:
            body += f"\n[truncated, {total - limit} more — pass limit= for all]"
        return ToolResult(
            call_id=call.id, ok=True,
            content=f"{len(lines)} of {total} entries in {path}:\n{body}",
            side_effects=(),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _glob_files(self, call: ToolCall, t0: float) -> ToolResult:
        """B-46: pure-stdlib glob. Cross-platform — works on Windows
        without needing find / fd / ripgrep installed."""
        pattern = str(call.args.get("pattern") or "").strip()
        if not pattern:
            return _fail(call, t0, "missing 'pattern'")
        root_arg = call.args.get("root")
        root = Path(str(root_arg)) if root_arg else self._cwd_default()
        try:
            root = root.resolve()
        except OSError as exc:
            return _fail(call, t0, f"bad root: {exc}")
        self._check_allowed(root)
        if not root.is_dir():
            return _fail(call, t0, f"not a directory: {root}")
        try:
            limit = int(call.args.get("limit") or 200)
        except (TypeError, ValueError):
            limit = 200
        limit = max(1, min(limit, 2000))
        results: list[str] = []
        try:
            # Path.glob handles ``**`` natively when pattern contains it.
            iterator = root.glob(pattern)
            for entry in iterator:
                results.append(str(entry))
                if len(results) >= limit:
                    break
        except (OSError, ValueError) as exc:
            return _fail(call, t0, f"glob failed: {exc}")
        return ToolResult(
            call_id=call.id, ok=True,
            content={
                "root": str(root),
                "pattern": pattern,
                "matches": results,
                "count": len(results),
                "truncated": len(results) >= limit,
            },
            side_effects=(),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _grep_files(self, call: ToolCall, t0: float) -> ToolResult:
        """B-46: regex search across files. Pure stdlib re; iterates
        line-by-line so a huge file doesn't OOM. Bounded by max_hits."""
        import re as _re

        pattern = str(call.args.get("pattern") or "")
        if not pattern:
            return _fail(call, t0, "missing 'pattern'")
        glob_pat = str(call.args.get("glob") or "**/*")
        root_arg = call.args.get("root")
        root = Path(str(root_arg)) if root_arg else self._cwd_default()
        try:
            root = root.resolve()
        except OSError as exc:
            return _fail(call, t0, f"bad root: {exc}")
        self._check_allowed(root)
        if not root.is_dir():
            return _fail(call, t0, f"not a directory: {root}")
        try:
            max_hits = int(call.args.get("max_hits") or 200)
        except (TypeError, ValueError):
            max_hits = 200
        max_hits = max(1, min(max_hits, 2000))
        flags = _re.IGNORECASE if call.args.get("case_insensitive") else 0
        try:
            rx = _re.compile(pattern, flags)
        except _re.error as exc:
            return _fail(call, t0, f"bad regex: {exc}")

        hits: list[dict[str, Any]] = []
        files_scanned = 0
        try:
            for path in root.glob(glob_pat):
                if not path.is_file():
                    continue
                files_scanned += 1
                # Skip obvious binary / large files cheaply.
                try:
                    if path.stat().st_size > 5_000_000:
                        continue
                except OSError:
                    continue
                try:
                    with path.open("r", encoding="utf-8", errors="replace") as fh:
                        for lineno, line in enumerate(fh, 1):
                            if rx.search(line):
                                hits.append({
                                    "path": str(path),
                                    "line": lineno,
                                    "text": line.rstrip("\n")[:300],
                                })
                                if len(hits) >= max_hits:
                                    raise StopIteration
                except OSError:
                    continue
        except StopIteration:
            pass
        return ToolResult(
            call_id=call.id, ok=True,
            content={
                "root": str(root),
                "pattern": pattern,
                "glob": glob_pat,
                "files_scanned": files_scanned,
                "hits": hits,
                "hit_count": len(hits),
                "truncated": len(hits) >= max_hits,
            },
            side_effects=(),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _file_delete(self, call: ToolCall, t0: float) -> ToolResult:
        """B-46: cross-platform file/dir delete. Refuses non-empty dirs
        unless ``recursive=true``. Honours allowed_dirs sandbox.

        B-62: refuses to delete a sandbox root itself, even when the
        path resolves "inside" the sandbox. Otherwise an agent given
        ``allowed_dirs=["/home/proj"]`` could call
        ``file_delete("/home/proj", recursive=True)`` and nuke the
        whole project including .git — sandbox-respecting in name,
        catastrophic in effect.
        """
        import shutil as _shutil

        raw_path = call.args.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            return _fail(call, t0, "missing 'path'")
        path = Path(raw_path)
        try:
            path = path.resolve()
        except OSError as exc:
            return _fail(call, t0, f"bad path: {exc}")
        self._check_allowed(path)
        # B-331: workspace-containment audit. file_delete is destructive
        # so the signal is especially valuable when the agent reaches
        # outside the configured workspace.
        self._audit_workspace_containment(path, op="file_delete")
        # B-62 guard: deny deletion when path IS one of the sandbox
        # roots (not just inside them). Apply only when sandbox is on
        # — without sandbox, there's no notion of "root to protect".
        if self._allowed is not None:
            for root in self._allowed:
                try:
                    if path.samefile(root):
                        return _fail(
                            call, t0,
                            f"refused: {path} is a sandbox root; deleting "
                            f"it would wipe the entire allowlisted area",
                        )
                except OSError:
                    continue
        if not path.exists():
            return _fail(call, t0, f"path does not exist: {path}")
        recursive = bool(call.args.get("recursive", False))
        kind = "dir" if path.is_dir() else "file"
        # Sprint 0 Track B: snapshot the file's bytes before deletion
        # so undo can restore. Directories are NOT recorded yet (would
        # need recursive zip backup) — caller should be warned.
        undo_id: str | None = None
        cab = getattr(self, "_undo_cabinet", None)
        if cab is not None and kind == "file":
            try:
                undo_id = cab.record_file_mutation(
                    path=path,
                    action="file_delete",
                    args={"recursive": False},
                    session_id=getattr(call, "session_id", None),
                )
            except Exception:  # noqa: BLE001
                undo_id = None
        try:
            if path.is_dir():
                if recursive:
                    _shutil.rmtree(path)
                else:
                    # rmdir refuses non-empty
                    path.rmdir()
            else:
                path.unlink()
        except OSError as exc:
            return _fail(call, t0, f"delete failed: {exc}")
        content_dict: dict[str, Any] = {
            "path": str(path),
            "kind": kind,
            "recursive": recursive if kind == "dir" else False,
        }
        if undo_id:
            content_dict["undo_id"] = undo_id
        return ToolResult(
            call_id=call.id, ok=True,
            content=content_dict,
            side_effects=(str(path),),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    # ── Undo (Sprint 0 Track B) ────────────────────────────────────

    async def _undo_list(self, call: ToolCall, t0: float) -> ToolResult:
        cab = getattr(self, "_undo_cabinet", None)
        if cab is None:
            return _fail(call, t0, "undo cabinet not wired")
        within_s = call.args.get("within_s", 60)
        try:
            within = float(within_s)
        except (TypeError, ValueError):
            within = 60.0
        within = max(0.0, min(within, 1800.0))
        records = cab.recent(within_s=within)
        now = time.time()
        return ToolResult(
            call_id=call.id, ok=True,
            content={
                "count": len(records),
                "within_s": within,
                "records": [
                    {
                        "id": r.id,
                        "action": r.action,
                        "path": r.path,
                        "age_s": round(now - r.ts, 1),
                        "pre_existed": r.pre_existed,
                    }
                    for r in records
                ],
            },
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _undo_recent(self, call: ToolCall, t0: float) -> ToolResult:
        cab = getattr(self, "_undo_cabinet", None)
        if cab is None:
            return _fail(call, t0, "undo cabinet not wired")
        action_id = call.args.get("action_id")
        action_filter = call.args.get("action_filter")
        if action_filter is not None and not isinstance(action_filter, str):
            return _fail(call, t0, "action_filter must be a string")
        if action_id is not None and isinstance(action_id, str) and action_id.strip():
            result = cab.undo(action_id.strip())
            return ToolResult(
                call_id=call.id, ok=bool(result.get("applied")),
                content=result,
                error=None if result.get("applied") else result.get("reason"),
                latency_ms=(time.perf_counter() - t0) * 1000.0,
            )
        within_s = call.args.get("within_s", 10)
        try:
            within = float(within_s)
        except (TypeError, ValueError):
            within = 10.0
        within = max(0.0, min(within, 1800.0))
        results = cab.undo_recent(within_s=within, action_filter=action_filter)
        applied = sum(1 for r in results if r.get("applied"))
        return ToolResult(
            call_id=call.id, ok=True,
            content={
                "applied_count": applied,
                "total_attempted": len(results),
                "within_s": within,
                "results": results,
            },
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    def _cwd_default(self) -> Path:
        """Resolve the workspace root for tools that take an optional
        ``root``. Falls back to cwd when no workspace is wired."""
        try:
            if self._workspace_root_provider is not None:
                v = self._workspace_root_provider()
                if v is not None:
                    return Path(str(v))
        except Exception:  # noqa: BLE001
            pass
        return Path(".").resolve()

    # ── bash ──────────────────────────────────────────────────────────

