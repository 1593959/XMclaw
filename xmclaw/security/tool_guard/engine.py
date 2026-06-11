"""ToolGuardEngine — orchestrates guardians and returns a ToolGuardResult."""
from __future__ import annotations

import time
from typing import Any

from .base import BaseToolGuardian
from .computer_use_guardian import MUTATING_GUI_TOOLS
from .models import GuardFinding, ToolGuardResult


# Tools that are *always* checked regardless of the guarded-tools scope.
# FilePathToolGuardian is registered here so sensitive files stay protected
# even for tools not explicitly listed in ``guarded_tools``.
_ALWAYS_RUN_GUARDIANS: set[str] = {"file_path"}

# Default tools that trigger full guardian scanning.
# B-340 (audit pass-2 #4): the canonical XMclaw tool name is ``bash``
# (see ``xmclaw/providers/tool/_specs.py:_BASH_SPEC``). Pre-B-340 this
# set listed ``execute_shell_command`` (a name borrowed from the
# the upstream agent rule set) which never matched any real tool dispatch — so
# the entire shell-evasion + dangerous-shell-rules + file-guardian
# path was dead for the most dangerous tool we ship. Browser tool
# names match their ``ToolSpec`` definitions in ``browser.py``.
# ``apply_patch`` and ``file_delete`` join the set because they
# mutate files just as much as ``file_write``.
_DEFAULT_GUARDED_TOOLS: set[str] = {
    "bash",
    "file_read",
    "file_write",
    "file_delete",
    "apply_patch",
    "browser_open",
    "browser_click",
    "browser_fill",
}

# Phase 9 M2.2: GUI 操作类 computer-use 工具进 guarded 集合。此前这批
# "agent 直接驱动用户鼠标键盘"的工具不在集合里 → 只跑 always-run 的
# file_path guardian → ComputerUseActionGuardian 永远不会被咨询。
# 名单收口在 computer_use_guardian.MUTATING_GUI_TOOLS（单一来源）。
_DEFAULT_GUARDED_TOOLS |= set(MUTATING_GUI_TOOLS)

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
            except Exception:  # noqa: BLE001
                # Guardian failure must not block the tool call,
                # but MUST be logged so operators can detect
                # silent security degradation (audit 2026-06-11).
                from xmclaw.utils.log import get_logger as _gl
                _gl(__name__).warning(
                    "tool_guard.guardian_failed guardian=%s tool=%s",
                    g.name, tool_name, exc_info=True,
                )
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
