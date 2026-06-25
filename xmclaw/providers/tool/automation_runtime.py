"""Shared runtime contracts for browser and desktop automation tools."""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Any

from xmclaw.utils.paths import v2_workspace_dir


def stable_json_hash(value: Any) -> str | None:
    """Return a stable SHA-256 for JSON-like data, or ``None`` on failure."""
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    except Exception:  # noqa: BLE001
        return None
    return hashlib.sha256(payload).hexdigest()


def safe_trace_id(value: str | None) -> str:
    raw = (value or "default").strip() or "default"
    return "".join(c if c.isalnum() or c in ("-", "_", ".") else "_" for c in raw)[:120]


class AutomationTraceRecorder:
    """Append-only JSONL trace with lightweight replay support."""

    def __init__(self, surface: str) -> None:
        self.surface = safe_trace_id(surface)
        self._paths: dict[str, Path] = {}

    def path_for(self, session_id: str | None) -> Path:
        sid = safe_trace_id(session_id)
        if sid not in self._paths:
            root = v2_workspace_dir() / "automation_traces" / self.surface
            root.mkdir(parents=True, exist_ok=True)
            suffix = f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
            self._paths[sid] = root / f"{sid}-{suffix}.jsonl"
        return self._paths[sid]

    def append(
        self,
        session_id: str | None,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> str:
        path = self.path_for(session_id)
        row = {
            "ts": time.time(),
            "surface": self.surface,
            "event_type": event_type,
            "session_id": session_id or "default",
            "payload": payload or {},
        }
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        return str(path)

    def read_tail(self, session_id: str | None, limit: int = 80) -> list[dict[str, Any]]:
        path = self.path_for(session_id)
        if not path.exists():
            return []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception:  # noqa: BLE001
            return []
        events: list[dict[str, Any]] = []
        for line in lines[-max(1, min(int(limit), 500)):]:
            try:
                events.append(json.loads(line))
            except Exception:  # noqa: BLE001
                events.append({"event_type": "trace_parse_error", "raw": line})
        return events
