"""Data models for the tool-guard security layer."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class GuardSeverity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"
    SAFE = "safe"


class GuardThreatCategory(str, Enum):
    COMMAND_INJECTION = "command_injection"
    DATA_EXFILTRATION = "data_exfiltration"
    HARDCODED_SECRETS = "hardcoded_secrets"
    OBFUSCATION = "obfuscation"
    PROMPT_INJECTION = "prompt_injection"
    SOCIAL_ENGINEERING = "social_engineering"
    SUPPLY_CHAIN = "supply_chain"
    UNAUTHORIZED_TOOL_USE = "unauthorized_tool_use"
    PATH_TRAVERSAL = "path_traversal"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class GuardFinding:
    rule_id: str
    category: str
    severity: GuardSeverity
    title: str
    description: str
    tool_name: str
    param_name: str = ""
    matched_value: str = ""
    remediation: str = ""
    guardian: str = ""


@dataclass(frozen=True)
class ToolGuardResult:
    tool_name: str
    params: dict[str, Any]
    findings: list[GuardFinding] = field(default_factory=list)
    guard_duration_seconds: float = 0.0
    guardians_used: list[str] = field(default_factory=list)

    @property
    def is_safe(self) -> bool:
        return not self.findings or all(
            f.severity in (GuardSeverity.LOW, GuardSeverity.INFO, GuardSeverity.SAFE)
            for f in self.findings
        )

    @property
    def max_severity(self) -> GuardSeverity | None:
        if not self.findings:
            return GuardSeverity.SAFE
        order = {
            GuardSeverity.SAFE: 0,
            GuardSeverity.INFO: 1,
            GuardSeverity.LOW: 2,
            GuardSeverity.MEDIUM: 3,
            GuardSeverity.HIGH: 4,
            GuardSeverity.CRITICAL: 5,
        }
        return max(self.findings, key=lambda f: order.get(f.severity, 0)).severity
