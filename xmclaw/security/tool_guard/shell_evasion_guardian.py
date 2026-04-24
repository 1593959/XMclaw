"""ShellEvasionGuardian — detects obfuscation / evasion in shell commands."""
from __future__ import annotations

import re
from typing import Any

from .base import BaseToolGuardian
from .models import GuardFinding, GuardSeverity

# Ordered checks — each is a (id, title, pattern) tuple.
_EVASION_CHECKS: list[tuple[str, str, str]] = [
    (
        "EVASION_COMMAND_SUBST",
        "Command substitution detected",
        r"\$\(|`[^`]+`|\$\{[^}]*\}",
    ),
    (
        "EVASION_ANSIC_QUOTE",
        "ANSI-C quote $'...' detected",
        r"\$'[^']*'",
    ),
    (
        "EVASION_BACKSLASH_SPACE",
        "Backslash-escaped whitespace",
        r"\\\s",
    ),
    (
        "EVASION_BACKSLASH_OPERATOR",
        "Backslash-escaped shell operator",
        r"\\[;|&<>()$`\"']",
    ),
    (
        "EVASION_HIDDEN_NEWLINE",
        "Hidden newline / carriage return in command",
        r"[\r\n]",
    ),
]


class ShellEvasionGuardian(BaseToolGuardian):
    r"""Scans ``execute_shell_command`` for evasion techniques that bypass
    simple regex guards (e.g. ``rm`` -> ``r\ m``, ``$(rm)``)."""

    @property
    def name(self) -> str:
        return "shell_evasion"

    def guard(self, tool_name: str, params: dict[str, Any]) -> list[GuardFinding]:
        if tool_name != "execute_shell_command":
            return []
        cmd = params.get("command", "")
        if not isinstance(cmd, str):
            return []

        findings: list[GuardFinding] = []
        for rule_id, title, pattern in _EVASION_CHECKS:
            for m in re.finditer(pattern, cmd):
                findings.append(
                    GuardFinding(
                        rule_id=rule_id,
                        category="command_injection",
                        severity=GuardSeverity.HIGH,
                        title=title,
                        description=f"Matched '{m.group(0)}' at position {m.start()}",
                        tool_name=tool_name,
                        param_name="command",
                        matched_value=m.group(0),
                        remediation="Avoid obfuscated shell syntax; use explicit tool calls instead",
                        guardian=self.name,
                    )
                )
        return findings
