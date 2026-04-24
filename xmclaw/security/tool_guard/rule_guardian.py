"""RuleBasedToolGuardian — matches tool parameters against YAML regex rules."""
from __future__ import annotations

from typing import Any

from xmclaw.security.rule_loader import load_rules, scan_with_rules

from .base import BaseToolGuardian
from .models import GuardFinding, GuardSeverity


class RuleBasedToolGuardian(BaseToolGuardian):
    """Loads YAML signature rules and applies them to tool parameters.

    Rules are loaded from ``xmclaw/security/rules/*.yaml`` (ported from
    QwenPaw under Apache-2.0).  Each rule carries regex *patterns* and
    optional *exclude_patterns*; when a pattern hits we emit a
    :class:`GuardFinding` with the severity declared in the rule.
    """

    def __init__(self) -> None:
        self._rules = load_rules()

    @property
    def name(self) -> str:
        return "rule_based"

    def guard(self, tool_name: str, params: dict[str, Any]) -> list[GuardFinding]:
        findings: list[GuardFinding] = []
        # Flatten all string parameter values into a single text block.
        # Rules are already scoped by ``tools`` / ``params`` in YAML, but
        # our rule_loader scans the raw text; we concatenate so that multi-
        # param rules still hit.
        chunks: list[str] = []
        for key, val in params.items():
            if isinstance(val, str):
                chunks.append(f"{key}={val}")
        if not chunks:
            return findings

        text = "\n".join(chunks)
        for rf in scan_with_rules(text, rules=self._rules):
            # Map rule_loader Severity (which lacks CRITICAL) to GuardSeverity
            if rf.severity.value == "high":
                sev = GuardSeverity.HIGH
            elif rf.severity.value == "medium":
                sev = GuardSeverity.MEDIUM
            else:
                sev = GuardSeverity.LOW
            findings.append(
                GuardFinding(
                    rule_id=rf.rule_id,
                    category=rf.category,
                    severity=sev,
                    title=rf.rule_id,
                    description=rf.description,
                    tool_name=tool_name,
                    param_name="",
                    matched_value=rf.matched_text,
                    remediation=rf.remediation,
                    guardian=self.name,
                )
            )
        return findings

    def reload(self) -> None:
        self._rules = load_rules()
