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


class GuardianAction(str, Enum):
    """What GuardedToolProvider does when the scan returns a given severity.

    - ``ALLOW`` — pass through to the inner tool. Findings are silently
      discarded (for MEDIUM/LOW/INFO by default — noisy rules would
      otherwise tax every tool call).
    - ``APPROVE`` — create a pending :class:`ApprovalService` entry and
      return ``NEEDS_APPROVAL:<request_id>`` so the LLM / user can
      approve explicitly.
    - ``DENY`` — block immediately with the findings summary. Used for
      smoking-gun matches (``CRITICAL`` by default) where no human
      approval is meaningful.
    """
    ALLOW = "allow"
    APPROVE = "approve"
    DENY = "deny"


@dataclass(frozen=True)
class GuardianPolicy:
    """Per-severity action mapping.

    The default preserves the original hard-coded behavior:

    - ``CRITICAL`` → ``DENY``   (block, no approval)
    - ``HIGH``     → ``APPROVE`` (pending approval)
    - ``MEDIUM``   → ``ALLOW``  (pass through)
    - ``LOW``      → ``ALLOW``
    - ``INFO``     → ``ALLOW``

    Users tighten by moving entries up the ladder (e.g. set MEDIUM to
    ``APPROVE`` to gate medium findings, or HIGH to ``DENY`` to skip
    the approval dance entirely). Loosen by moving down (e.g. HIGH
    to ``ALLOW`` for dev environments) — not recommended.

    ``SAFE`` and ``INFO`` always pass through; they exist in the enum
    for completeness but are never looked up.
    """
    critical: GuardianAction = GuardianAction.DENY
    high: GuardianAction = GuardianAction.APPROVE
    medium: GuardianAction = GuardianAction.ALLOW
    low: GuardianAction = GuardianAction.ALLOW
    info: GuardianAction = GuardianAction.ALLOW

    def action_for(self, severity: GuardSeverity) -> GuardianAction:
        """Look up the action for *severity*. Unknown severities
        default to ``ALLOW`` (fail open) — the caller should treat
        ``SAFE`` the same way."""
        if severity == GuardSeverity.CRITICAL:
            return self.critical
        if severity == GuardSeverity.HIGH:
            return self.high
        if severity == GuardSeverity.MEDIUM:
            return self.medium
        if severity == GuardSeverity.LOW:
            return self.low
        if severity == GuardSeverity.INFO:
            return self.info
        return GuardianAction.ALLOW

    @classmethod
    def from_config(cls, cfg: dict[str, Any] | None) -> "GuardianPolicy":
        """Parse a dict like ``{"critical": "deny", "high": "approve"}``.

        - Missing keys fall back to the class default.
        - Unknown severities or actions raise :class:`ValueError` with
          a message that lists the valid set — factory should surface
          this at startup rather than silently falling back.
        - ``None`` input returns the default policy (the common case
          when the user's config has no ``policy`` section).
        """
        if not cfg:
            return cls()
        valid_actions = {a.value for a in GuardianAction}
        valid_severities = {"critical", "high", "medium", "low", "info"}
        kwargs: dict[str, GuardianAction] = {}
        for key, raw in cfg.items():
            if key not in valid_severities:
                raise ValueError(
                    f"unknown severity {key!r} in guardians.policy; "
                    f"valid: {sorted(valid_severities)}"
                )
            if not isinstance(raw, str) or raw.lower() not in valid_actions:
                raise ValueError(
                    f"unknown action {raw!r} for severity {key!r}; "
                    f"valid: {sorted(valid_actions)}"
                )
            kwargs[key] = GuardianAction(raw.lower())
        return cls(**kwargs)


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
