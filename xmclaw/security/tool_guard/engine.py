"""ToolGuardEngine — orchestrates guardians and returns a ToolGuardResult."""
from __future__ import annotations

import time
from typing import Any

from .base import BaseToolGuardian
from .models import GuardFinding, ToolGuardResult


# Tools that are *always* checked regardless of the guarded-tools scope.
# FilePathToolGuardian is registered here so sensitive files stay protected
# even for tools not explicitly listed in ``guarded_tools``.
_ALWAYS_RUN_GUARDIANS: set[str] = {"file_path"}

# Default tools that trigger full guardian scanning.
_DEFAULT_GUARDED_TOOLS: set[str] = {
    "execute_shell_command",
    "file_read",
    "file_write",
    "browser_open",
    "browser_click",
    "browser_fill",
}

# Tools that are unconditionally blocked.
_DEFAULT_DENIED_TOOLS: set[str] = set()


class ToolGuardEngine:
    """Orchestrates one or more :class:`BaseToolGuardian` instances.

    Typical usage inside an agent loop::

        engine = ToolGuardEngine(guardians=[...])
        result = engine.guard(tool_name, params)
        if not result.is_safe:
            ... # prompt user or block
    """

    def __init__(
        self,
        guardians: list[BaseToolGuardian] | None = None,
        *,
        guarded_tools: set[str] | None = None,
        denied_tools: set[str] | None = None,
    ) -> None:
        self._guardians = guardians or []
        self._guarded_tools = guarded_tools or set(_DEFAULT_GUARDED_TOOLS)
        self._denied_tools = denied_tools or set(_DEFAULT_DENIED_TOOLS)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_denied(self, tool_name: str) -> bool:
        return tool_name in self._denied_tools

    def is_guarded(self, tool_name: str) -> bool:
        return tool_name in self._guarded_tools

    def guard(
        self,
        tool_name: str,
        params: dict[str, Any],
        *,
        only_always_run: bool = False,
    ) -> ToolGuardResult:
        """Run guardians against *tool_name* + *params*.

        Parameters
        ----------
        only_always_run :
            When ``True``, skip guardians that are not in
            ``_ALWAYS_RUN_GUARDIANS``. Used for tools outside the
            ``guarded_tools`` set — we still protect sensitive paths
            but don't run expensive regex scans.
        """
        start = time.perf_counter()
        findings: list[GuardFinding] = []
        used: list[str] = []

        for g in self._guardians:
            if only_always_run and g.name not in _ALWAYS_RUN_GUARDIANS:
                continue
            try:
                batch = g.guard(tool_name, params)
            except Exception:
                # Guardian failure must not block the tool call.
                # Logged by the caller if desired.
                batch = []
            if batch:
                findings.extend(batch)
                used.append(g.name)

        duration = time.perf_counter() - start
        return ToolGuardResult(
            tool_name=tool_name,
            params=params,
            findings=findings,
            guard_duration_seconds=duration,
            guardians_used=used,
        )

    def reload_rules(self) -> None:
        """Hot-reload every guardian that supports it."""
        for g in self._guardians:
            g.reload()
