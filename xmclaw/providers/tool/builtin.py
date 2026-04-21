"""Built-in tools — ``file_read`` and ``file_write``.

Phase 2.5: first real ToolProvider. These two tools are the minimum
needed to drive the Phase 1 go/no-go pipeline with actual side effects
(so the honest grader's ``check_side_effect_observable`` exercises a
real fs observation, not a stub).

Security posture for Phase 2.5:
  * ``allowed_dirs`` allowlist — if set, any path outside collapses to
    a PermissionError before touching disk. If unset (default), the
    tool trusts its caller (suitable for unit tests, NOT for production).
  * Phase 3 supersedes this with manifest-driven sandboxing (resource
    ceilings, per-skill permission declarations, process isolation).
    The allowlist in this module is a deliberately thin guard-rail.

Both tools return structured ``ToolResult``:
  * ``file_read``: content = file's UTF-8 text; side_effects = () (pure read)
  * ``file_write``: content = {"path": str, "bytes": int};
    side_effects = (str(path),) so the grader can verify the file exists.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from xmclaw.core.ir import ToolCall, ToolResult, ToolSpec
from xmclaw.providers.tool.base import ToolProvider

_FILE_READ_SPEC = ToolSpec(
    name="file_read",
    description="Read a UTF-8 text file and return its contents as a string.",
    parameters_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute path to the file to read.",
            },
        },
        "required": ["path"],
    },
)

_FILE_WRITE_SPEC = ToolSpec(
    name="file_write",
    description=(
        "Write UTF-8 text to a file, creating parent directories if needed. "
        "Overwrites existing files."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute path to the file to write.",
            },
            "content": {
                "type": "string",
                "description": "Text content to write.",
            },
        },
        "required": ["path", "content"],
    },
)


class BuiltinTools(ToolProvider):
    """Filesystem read/write with optional allowlist-based path guard."""

    def __init__(self, allowed_dirs: list[Path | str] | None = None) -> None:
        self._allowed = (
            [Path(d).resolve() for d in allowed_dirs] if allowed_dirs else None
        )

    def list_tools(self) -> list[ToolSpec]:
        return [_FILE_READ_SPEC, _FILE_WRITE_SPEC]

    async def invoke(self, call: ToolCall) -> ToolResult:
        t0 = time.perf_counter()
        try:
            if call.name == "file_read":
                return await self._file_read(call, t0)
            if call.name == "file_write":
                return await self._file_write(call, t0)
            return ToolResult(
                call_id=call.id,
                ok=False,
                content=None,
                error=f"unknown tool: {call.name!r}",
                latency_ms=(time.perf_counter() - t0) * 1000.0,
            )
        except PermissionError as exc:
            return ToolResult(
                call_id=call.id,
                ok=False,
                content=None,
                error=f"permission denied: {exc}",
                latency_ms=(time.perf_counter() - t0) * 1000.0,
            )
        except FileNotFoundError as exc:
            return ToolResult(
                call_id=call.id,
                ok=False,
                content=None,
                error=f"file not found: {exc}",
                latency_ms=(time.perf_counter() - t0) * 1000.0,
            )
        except Exception as exc:  # noqa: BLE001 — surface as structured failure
            return ToolResult(
                call_id=call.id,
                ok=False,
                content=None,
                error=f"{type(exc).__name__}: {exc}",
                latency_ms=(time.perf_counter() - t0) * 1000.0,
            )

    # ── tool bodies ──

    async def _file_read(self, call: ToolCall, t0: float) -> ToolResult:
        raw_path = call.args.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            return ToolResult(
                call_id=call.id, ok=False, content=None,
                error="missing or empty 'path' argument",
                latency_ms=(time.perf_counter() - t0) * 1000.0,
            )
        path = Path(raw_path)
        self._check_allowed(path)
        content = path.read_text(encoding="utf-8")
        return ToolResult(
            call_id=call.id,
            ok=True,
            content=content,
            side_effects=(),   # pure read
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _file_write(self, call: ToolCall, t0: float) -> ToolResult:
        raw_path = call.args.get("path")
        text = call.args.get("content")
        if not isinstance(raw_path, str) or not raw_path:
            return ToolResult(
                call_id=call.id, ok=False, content=None,
                error="missing or empty 'path' argument",
                latency_ms=(time.perf_counter() - t0) * 1000.0,
            )
        if not isinstance(text, str):
            return ToolResult(
                call_id=call.id, ok=False, content=None,
                error=f"'content' must be string, got {type(text).__name__}",
                latency_ms=(time.perf_counter() - t0) * 1000.0,
            )
        path = Path(raw_path)
        self._check_allowed(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return ToolResult(
            call_id=call.id,
            ok=True,
            content={"path": str(path), "bytes": len(text.encode("utf-8"))},
            side_effects=(str(path.resolve()),),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    # ── allowlist ──

    def _check_allowed(self, path: Path) -> None:
        if self._allowed is None:
            return
        resolved = path.resolve()
        for allowed in self._allowed:
            try:
                resolved.relative_to(allowed)
                return
            except ValueError:
                continue
        raise PermissionError(
            f"path {resolved} is outside the allowlist {self._allowed}"
        )
