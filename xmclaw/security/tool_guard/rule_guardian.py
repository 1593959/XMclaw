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
        # B-340 (audit pass-2 #4): scan one param at a time and pass
        # ``tool_name`` + ``param_name`` to scan_with_rules, so YAML
        # ``tools:`` / ``params:`` filters actually narrow the rule
        # set. Pre-B-340 we concatenated every string param into one
        # blob which (a) defeated the per-param scope and (b) coupled
        # with the audit-pass-2 finding that ``Rule.tools`` was never
        # consulted at all → shell rules fired on file_write, file
        # rules on bash, etc. Now: a rule scoped to ``[bash]`` only
        # fires on bash; a rule scoped to ``[command]`` only fires
        # against the command param. Per-rule de-dup inside
        # scan_with_rules still kicks in within one param scan, so a
        # multi-pattern rule won't double-report on one param.
        seen_ids: set[str] = set()
        for key, val in params.items():
            if not isinstance(val, str):
                continue
            for rf in scan_with_rules(
                val,
                rules=self._rules,
                tool_name=tool_name,
                param_name=key,
            ):
                # de-dup across params so a rule firing on multiple
                # params (rare — most are scoped to one) still emits
                # exactly one finding.
                if rf.rule_id in seen_ids:
                    continue
                seen_ids.add(rf.rule_id)
                # Map rule_loader Severity to GuardSeverity
                if rf.severity.value == "critical":
                    sev = GuardSeverity.CRITICAL
                elif rf.severity.value == "high":
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
                        param_name=key,
                        matched_value=rf.matched_text,
                        remediation=rf.remediation,
                        guardian=self.name,
                    )
                )
        return findings

    def reload(self) -> None:
        self._rules = load_rules()
