from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from xmclaw.core.ir import ToolCall, ToolResult
from xmclaw.providers.tool._helpers import _fail as _fail

# Module-level question state — lives here so builtin.py can import it
# without creating a circular dependency (builtin imports this mixin,
# not the other way around).
_PENDING_QUESTIONS: dict[str, asyncio.Future] = {}
_PENDING_QUESTION_PAYLOADS: dict[str, dict] = {}


class BuiltinToolsUserMixin:
    """User interaction tools: ask_user_question, agent_status."""

    async def _ask_user_question(self, call: ToolCall, t0: float) -> ToolResult:
        """B-92: stop the turn, publish AGENT_ASKED_QUESTION, block on a
        Future until the WS handler resolves it with the user's answer.

        Cross-boundary plumbing: the future lives in the module-level
        :data:`_PENDING_QUESTIONS` dict so both this tool (which awaits
        it) and ``daemon/app.py`` 's WS handler (which resolves it on
        the answer_question client frame) share the same identity.

        Timeout caps the wait at 600 seconds — past that we return
        ``ok=False`` so the agent can recover and proceed with its
        best guess instead of hanging indefinitely.
        """
        question = str(call.args.get("question") or "").strip()
        options = call.args.get("options") or []
        multi = bool(call.args.get("multi_select"))
        allow_other = bool(call.args.get("allow_other", True))
        if not question:
            return _fail(call, t0, "missing 'question'")
        if not isinstance(options, list) or not options:
            return _fail(call, t0, "options must be a non-empty list")
        # Normalise options to {label, value, description?} dicts.
        norm_options: list[dict[str, str]] = []
        for i, o in enumerate(options):
            if not isinstance(o, dict):
                return _fail(call, t0, f"options[{i}] must be an object")
            label = str(o.get("label") or "").strip()
            value = str(o.get("value") or "").strip()
            if not label or not value:
                return _fail(call, t0, f"options[{i}] needs both 'label' and 'value'")
            entry = {"label": label, "value": value}
            desc = o.get("description")
            if isinstance(desc, str) and desc.strip():
                entry["description"] = desc.strip()
            norm_options.append(entry)

        question_id = uuid.uuid4().hex
        loop = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()
        _PENDING_QUESTIONS[question_id] = future
        # B-99: payload snapshot for the reconnect-recovery endpoint.
        # Front-end calls ``GET /api/v2/pending_questions`` on WS open
        # so a browser refresh while an ask is in flight rebuilds the
        # card instead of stranding the future.
        _PENDING_QUESTION_PAYLOADS[question_id] = {
            "question_id": question_id,
            "question": question,
            "options": norm_options,
            "multi_select": multi,
            "allow_other": allow_other,
            "tool_call_id": call.id,
            # Lets cancel_pending_questions() scope Stop to one session.
            "session_id": call.session_id or "",
        }

        # Publish AGENT_ASKED_QUESTION via the bus the daemon factory
        # supplies. Same indirection pattern persona-writeback uses
        # (_LAST_APP_STATE) so this tool stays decoupled from the
        # daemon module.
        try:
            from xmclaw.daemon import app as _app_mod
            state = getattr(_app_mod, "_LAST_APP_STATE", None)
            bus = getattr(state, "bus", None) if state is not None else None
        except Exception:  # noqa: BLE001
            bus = None
        if bus is not None:
            try:
                from xmclaw.core.bus import EventType, make_event
                # B-237: use the REAL session_id (set by AgentLoop on
                # the ToolCall before invoke). Pre-B-237 this was
                # hardcoded to ``"_question"`` — a placeholder that
                # never matches the front-end's WS session
                # subscription, so the event silently dropped on the
                # gateway floor. The QuestionCard only became visible
                # after page refresh because the rehydrate path
                # (``GET /api/v2/pending_questions``) is HTTP and
                # session-agnostic. Live path was broken since B-92.
                # Fall back to ``"_question"`` only for defensive
                # callers that build a ToolCall without a session_id.
                sid = call.session_id or "_question"
                ev = make_event(
                    session_id=sid,
                    agent_id="main",
                    type=EventType.AGENT_ASKED_QUESTION,
                    payload={
                        "question_id": question_id,
                        "question": question,
                        "options": norm_options,
                        "multi_select": multi,
                        "allow_other": allow_other,
                        "tool_call_id": call.id,
                    },
                )
                await bus.publish(ev)
            except Exception:  # noqa: BLE001 — telemetry path; never block
                pass

        try:
            # No timeout — the user may take as long as they need.
            # The future is resolved when the WS handler receives an
            # answer_question frame or the session is cancelled.
            answer = await future
        except asyncio.CancelledError:
            return _fail(
                call, t0,
                "question was cancelled — proceed with your best guess "
                "or ask again differently",
            )
        finally:
            _PENDING_QUESTIONS.pop(question_id, None)
            _PENDING_QUESTION_PAYLOADS.pop(question_id, None)

        # ``answer`` is a string for single-select, list for multi-select,
        # or a free-text "Other" string. Caller (the LLM) sees it as
        # plain text in the tool result.
        if isinstance(answer, list):
            return ToolResult(
                call_id=call.id, ok=True,
                content=", ".join(str(a) for a in answer),
                side_effects=(),
                latency_ms=(time.perf_counter() - t0) * 1000.0,
            )
        return ToolResult(
            call_id=call.id, ok=True,
            content=str(answer),
            side_effects=(),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _send_media(self, call: ToolCall, t0: float) -> ToolResult:
        """Send a local media file to the chat UI as a viewable attachment."""
        import shutil
        from pathlib import Path

        path_str = str(call.args.get("path") or "").strip()
        if not path_str:
            return _fail(call, t0, "missing 'path'")

        src = Path(path_str)
        if not src.is_file():
            return _fail(call, t0, f"file not found: {path_str}")

        # Copy to the uploads directory so /api/v2/media/<basename>
        # can serve it. Use the original basename so the URL is stable.
        from xmclaw.utils.paths import data_dir
        uploads_dir = Path(data_dir()) / "v2" / "uploads"
        uploads_dir.mkdir(parents=True, exist_ok=True)
        dst = uploads_dir / src.name

        # If a file with the same name already exists, append a counter.
        counter = 1
        original_dst = dst
        while dst.exists():
            stem = original_dst.stem
            suffix = original_dst.suffix
            dst = uploads_dir / f"{stem}_{counter}{suffix}"
            counter += 1

        try:
            shutil.copy2(str(src), str(dst))
        except Exception as exc:  # noqa: BLE001
            return _fail(call, t0, f"copy failed: {exc}")

        # Determine MIME from extension.
        ext = dst.suffix.lower()
        mime_map = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif": "image/gif",
            ".webp": "image/webp",
            ".bmp": "image/bmp",
            ".mp3": "audio/mpeg",
            ".wav": "audio/wav",
            ".ogg": "audio/ogg",
            ".m4a": "audio/mp4",
            ".mp4": "video/mp4",
            ".webm": "video/webm",
            ".mov": "video/quicktime",
            ".mkv": "video/x-matroska",
            ".avi": "video/x-msvideo",
            ".m4v": "video/mp4",
            # 文档类（2026-06-14）：之前缺这些 → mime=None + kind 误判 image
            # → 前端当图渲染 "<img src=xlsx>" 必裂（用户报"商品定价利润率表
            # .xlsx 加载失败"）。补 mime + 归类 document，前端给文件卡。
            ".pdf": "application/pdf",
            ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ".xls": "application/vnd.ms-excel",
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ".doc": "application/msword",
            ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            ".csv": "text/csv",
            ".txt": "text/plain",
            ".md": "text/markdown",
            ".json": "application/json",
            ".zip": "application/zip",
        }
        mime = mime_map.get(ext)

        # Determine kind for the attachment metadata. Default is now
        # "document" (not "image") — an unknown extension is far more
        # likely a file than an image, and a wrong "image" kind makes the
        # UI render a broken <img>. Only the explicit media extensions
        # below get image/video/audio.
        _IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".svg")
        if ext in _IMAGE_EXTS:
            kind = "image"
        elif ext in (".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v"):
            kind = "video"
        elif ext in (".mp3", ".wav", ".ogg", ".m4a"):
            kind = "audio"
        else:
            kind = "document"

        return ToolResult(
            call_id=call.id, ok=True,
            content=f"Media sent: {dst.name}",
            side_effects=(str(dst),),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
            metadata={
                "attachments": [
                    {
                        "kind": kind,
                        "path": str(dst),
                        "mime": mime,
                        "bytes_size": dst.stat().st_size,
                    }
                ]
            },
        )

    async def _agent_status(self, call: ToolCall, t0: float) -> ToolResult:
        """B-49: self-introspection. Reads daemon state via the same
        ``_LAST_APP_STATE`` holder factory.py uses for persona-writeback,
        so works without forcing every BuiltinTools instance to carry
        an explicit app.state reference."""
        out: dict[str, Any] = {}
        # 1) Memory layer — providers + indexer.
        if self._memory_manager is not None:
            providers = []
            for p in getattr(self._memory_manager, "providers", []):
                providers.append({
                    "name": getattr(p, "name", "?"),
                    "kind": "builtin" if getattr(p, "name", "") == "builtin" else "external",
                })
            out["memory"] = {
                "wired": True,
                "providers": providers,
                "embedder": (
                    {"name": getattr(self._embedder, "name", "?"),
                     "dim": getattr(self._embedder, "dim", 0)}
                    if self._embedder is not None else None
                ),
            }
        else:
            out["memory"] = {"wired": False}

        # 2) Daemon-side state via _LAST_APP_STATE.
        state = None
        try:
            from xmclaw.daemon import app as _app_mod
            state = getattr(_app_mod, "_LAST_APP_STATE", None)
        except Exception:  # noqa: BLE001
            state = None

        if state is not None:
            # Indexer
            idx = getattr(state, "memory_indexer", None)
            if idx is not None:
                out["indexer"] = {
                    "wired": True,
                    "running": getattr(idx, "is_running", False),
                    "watched_paths_count": sum(1 for _ in getattr(idx, "_watched_paths", lambda: [])()),
                    "known_paths_count": len(getattr(idx, "_known_paths", set()) or set()),
                    "poll_interval_s": getattr(idx, "_poll_s", None),
                }
            else:
                out["indexer"] = {"wired": False}

            # Epic #24 Phase 1: removed auto_evo subsystem status —
            # `app.state.auto_evo_process` no longer exists. Phase 2
            # will surface the EvolutionAgent observer's running state
            # here through `app.state.evolution_observer` instead.

            # Bus event count proxy via the events DB row count when
            # the daemon's running. Cheap query.
            try:
                import sqlite3 as _sql
                from xmclaw.utils.paths import data_dir
                events_db = data_dir() / "v2" / "events.db"
                if events_db.is_file():
                    con = _sql.connect(f"file:{events_db}?mode=ro", uri=True, timeout=2)
                    try:
                        n = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
                        out["events_db"] = {"row_count": int(n)}
                    finally:
                        con.close()
            except Exception:  # noqa: BLE001
                out["events_db"] = {"row_count": None}

        # 3) Cron — singleton, always reachable.
        try:
            from xmclaw.core.scheduler.cron import default_cron_store
            store = default_cron_store()
            jobs = store.list_jobs()
            next_at = min((j.next_run_at for j in jobs if j.enabled and j.next_run_at), default=None)
            out["cron"] = {
                "job_count": len(jobs),
                "enabled_count": sum(1 for j in jobs if j.enabled),
                "next_run_at": next_at,
            }
        except Exception:  # noqa: BLE001
            out["cron"] = {"wired": False}

        # 4) Workspace + persona dirs (resolved lazily).
        try:
            if self._workspace_root_provider is not None:
                v = self._workspace_root_provider()
                out["workspace_root"] = str(v) if v else None
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._persona_dir_provider is not None:
                v = self._persona_dir_provider()
                out["persona_dir"] = str(v) if v else None
        except Exception:  # noqa: BLE001
            pass

        return ToolResult(
            call_id=call.id, ok=True,
            content=out,
            side_effects=(),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

