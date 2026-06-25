from __future__ import annotations

import json
import time
from typing import Any

from xmclaw.core.ir import ToolCall, ToolResult
from xmclaw.providers.tool._helpers import _fail


class BuiltinToolsArtifactMixin:
    """Agent-facing artifact ledger tools."""

    async def _artifact_ledger(self, call: ToolCall, t0: float) -> ToolResult:
        store = getattr(self, "_artifact_ledger_store", None)
        if store is None:
            return _fail(call, t0, "artifact_ledger not configured")

        query = str(call.args.get("query") or "").strip()
        session_id = str(call.args.get("session_id") or "").strip()
        if not session_id:
            try:
                from xmclaw.core.agent_context import get_current_session_id
                session_id = get_current_session_id() or ""
            except Exception:  # noqa: BLE001
                session_id = ""
        artifact_type = str(call.args.get("artifact_type") or "").strip() or None
        target_drive = str(call.args.get("target_drive") or "").strip() or None
        include_global = bool(call.args.get("include_global") or False)
        try:
            limit = int(call.args.get("limit") or 10)
        except (TypeError, ValueError):
            limit = 10
        limit = max(1, min(limit, 50))

        skip_reasons: list[str] = []
        try:
            candidates = store.search(
                query=query,
                session_id=session_id or None,
                artifact_type=artifact_type,
                target_drive=target_drive,
                limit=limit,
            )
        except AttributeError:
            candidates = store.list_recent(session_id=session_id or None, limit=limit)
        except Exception as exc:  # noqa: BLE001
            return _fail(call, t0, f"artifact_ledger query failed: {exc}")

        fallback_used = False
        if not candidates and include_global and session_id:
            fallback_used = True
            try:
                candidates = store.search(
                    query=query,
                    session_id=None,
                    artifact_type=artifact_type,
                    target_drive=target_drive,
                    limit=limit,
                )
            except AttributeError:
                candidates = store.list_recent(session_id=None, limit=limit)
            except Exception as exc:  # noqa: BLE001
                return _fail(call, t0, f"artifact_ledger global query failed: {exc}")

        if not session_id:
            skip_reasons.append("no current session_id; searched globally")
        if not candidates:
            skip_reasons.append("no artifact matched the provided filters")
        if artifact_type and not candidates:
            skip_reasons.append(f"artifact_type filter may be too narrow: {artifact_type}")
        if target_drive and not candidates:
            skip_reasons.append(f"target_drive filter may be too narrow: {target_drive}")

        payload: dict[str, Any] = {
            "ok": True,
            "query": query,
            "session_id": session_id,
            "filters": {
                "artifact_type": artifact_type,
                "target_drive": target_drive,
                "limit": limit,
                "include_global": include_global,
            },
            "fallback_used": fallback_used,
            "candidates": [_artifact_candidate(row) for row in candidates],
            "skip_reasons": skip_reasons,
            "recommended_action": (
                "use the top candidate path/url before launching a broad filesystem search"
                if candidates else
                "fall back to the relevant tool history, download folder, or targeted file search"
            ),
        }
        return ToolResult(
            call_id=call.id,
            ok=True,
            content=json.dumps(payload, ensure_ascii=False, indent=2),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )


def _artifact_candidate(row: dict[str, Any]) -> dict[str, Any]:
    location = row.get("path") or row.get("url") or row.get("name") or ""
    return {
        "id": row.get("id", ""),
        "name": row.get("name", ""),
        "artifact_type": row.get("artifact_type", "file"),
        "location": location,
        "path": row.get("path", ""),
        "url": row.get("url", ""),
        "exists": bool(row.get("exists")),
        "target_drive": row.get("target_drive", ""),
        "tool_name": row.get("tool_name", ""),
        "source": row.get("source", ""),
        "created_at": row.get("created_at", 0.0),
        "why": _why(row),
        "recommended_action": (
            f"inspect or open {location}" if location else "inspect artifact metadata"
        ),
    }


def _why(row: dict[str, Any]) -> str:
    parts = []
    if row.get("tool_name"):
        parts.append(f"produced by {row['tool_name']}")
    if row.get("target_drive"):
        parts.append(f"drive {row['target_drive']}")
    if row.get("source"):
        parts.append(f"source {row['source']}")
    return "; ".join(parts) or "recent artifact ledger entry"
