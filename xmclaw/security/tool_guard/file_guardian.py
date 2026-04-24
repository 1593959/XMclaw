"""FilePathToolGuardian — blocks access to sensitive files and directories."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from .base import BaseToolGuardian
from .models import GuardFinding, GuardSeverity

# Known file-tool parameter names that may carry paths.
_PATH_PARAM_NAMES = {"file_path", "path", "target", "destination", "src", "dst"}

# Regex to extract bare paths from a shell command string.
_SHELL_PATH_RE = re.compile(r"[\s;|&]([/~.$][^\s;|&\"'`]+)")


class FilePathToolGuardian(BaseToolGuardian):
    """Protects sensitive files/directories from tool access.

    Configurable via ``security.file_guard.sensitive_files`` — a list of
    absolute or home-relative paths.  Any tool whose parameters reference
    one of these paths (exact match or inside a prefix directory) gets a
    ``CRITICAL`` finding.
    """

    def __init__(self, sensitive_files: list[str] | None = None) -> None:
        self._sensitive_files: set[Path] = set()
        self._sensitive_dirs: set[Path] = set()
        if sensitive_files:
            for raw in sensitive_files:
                p = Path(os.path.expanduser(raw)).resolve()
                self._sensitive_files.add(p)
                self._sensitive_dirs.add(p)
                if p.is_dir():
                    self._sensitive_dirs.add(p)

    @property
    def name(self) -> str:
        return "file_path"

    def guard(self, tool_name: str, params: dict[str, Any]) -> list[GuardFinding]:
        findings: list[GuardFinding] = []
        # 1. Direct path parameters
        for key, val in params.items():
            if key in _PATH_PARAM_NAMES and isinstance(val, str):
                path = Path(os.path.expanduser(val)).resolve()
                if self._is_sensitive(path):
                    findings.append(
                        GuardFinding(
                            rule_id="FILE_GUARD_SENSITIVE_PATH",
                            category="path_traversal",
                            severity=GuardSeverity.CRITICAL,
                            title="Sensitive file access blocked",
                            description=f"Tool attempted to access protected path: {path}",
                            tool_name=tool_name,
                            param_name=key,
                            matched_value=str(path),
                            remediation="Remove the sensitive path from the tool call or add it to allowed_dirs",
                            guardian=self.name,
                        )
                    )
        # 2. Shell commands — extract paths from the command string
        if tool_name == "execute_shell_command":
            cmd = params.get("command", "")
            if isinstance(cmd, str):
                for m in _SHELL_PATH_RE.finditer(cmd):
                    raw_path = m.group(1)
                    try:
                        path = Path(os.path.expanduser(raw_path)).resolve()
                    except (OSError, ValueError):
                        continue
                    if self._is_sensitive(path):
                        findings.append(
                            GuardFinding(
                                rule_id="FILE_GUARD_SENSITIVE_PATH",
                                category="path_traversal",
                                severity=GuardSeverity.CRITICAL,
                                title="Sensitive file access blocked via shell command",
                                description=f"Shell command references protected path: {path}",
                                tool_name=tool_name,
                                param_name="command",
                                matched_value=raw_path,
                                remediation="Avoid referencing sensitive paths in shell commands",
                                guardian=self.name,
                            )
                        )
        return findings

    def _is_sensitive(self, path: Path) -> bool:
        # Exact match
        if path in self._sensitive_files:
            return True
        # Prefix match (path is inside a sensitive dir)
        for d in self._sensitive_dirs:
            try:
                path.relative_to(d)
                return True
            except ValueError:
                continue
        return False
