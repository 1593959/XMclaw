"""YAML security rule loader.

Loads signature rules from ``xmclaw/security/rules/*.yaml`` (ported from
QwenPaw under Apache-2.0) and performs regex matching against text or
tool parameters.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


class Severity(Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Action(Enum):
    DETECT_ONLY = "detect_only"
    REDACT = "redact"
    BLOCK = "block"


@dataclass(frozen=True)
class Rule:
    id: str
    category: str
    severity: Severity
    patterns: list[re.Pattern[str]]
    exclude_patterns: list[re.Pattern[str]] = field(default_factory=list)
    description: str = ""
    remediation: str = ""
    tools: list[str] = field(default_factory=list)
    params: list[str] = field(default_factory=list)
    file_types: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Finding:
    rule_id: str
    category: str
    severity: Severity
    matched_text: str
    description: str
    remediation: str


def _default_rules_dir() -> Path:
    return Path(__file__).resolve().parent / "rules"


def _compile_patterns(raw_patterns: list[str] | None) -> list[re.Pattern[str]]:
    if not raw_patterns:
        return []
    compiled: list[re.Pattern[str]] = []
    for pat in raw_patterns:
        try:
            compiled.append(re.compile(pat))
        except re.error:
            continue
    return compiled


def _parse_severity(raw: str) -> Severity:
    try:
        return Severity(raw.lower())
    except ValueError:
        return Severity.MEDIUM


def load_rules(rules_dir: Path | None = None) -> list[Rule]:
    """Load all YAML rules from *rules_dir* (defaults to built-in ``rules/``)."""
    if yaml is None:
        return []
    directory = rules_dir if rules_dir is not None else _default_rules_dir()
    if not directory.exists():
        return []
    rules: list[Rule] = []
    for path in sorted(directory.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, list):
            continue
        for entry in data:
            if not isinstance(entry, dict):
                continue
            rule_id = entry.get("id", "")
            if not rule_id:
                continue
            severity = _parse_severity(entry.get("severity", "medium"))
            patterns = _compile_patterns(entry.get("patterns"))
            if not patterns:
                continue
            exclude = _compile_patterns(entry.get("exclude_patterns"))
            rules.append(
                Rule(
                    id=rule_id,
                    category=entry.get("category", "unknown"),
                    severity=severity,
                    patterns=patterns,
                    exclude_patterns=exclude,
                    description=entry.get("description", ""),
                    remediation=entry.get("remediation", ""),
                    tools=entry.get("tools", []),
                    params=entry.get("params", []),
                    file_types=entry.get("file_types", []),
                )
            )
    return rules


def scan_with_rules(
    text: str, rules: list[Rule] | None = None
) -> list[Finding]:
    """Scan *text* against loaded rules and return findings."""
    if rules is None:
        rules = load_rules()
    findings: list[Finding] = []
    seen_ids: set[str] = set()
    for rule in rules:
        # skip if already matched by this rule to reduce noise
        if rule.id in seen_ids:
            continue
        # skip binary-only rules when scanning plain text
        if rule.file_types == ["binary"]:
            continue
        excluded = False
        for exc in rule.exclude_patterns:
            if exc.search(text):
                excluded = True
                break
        if excluded:
            continue
        for pat in rule.patterns:
            m = pat.search(text)
            if m:
                findings.append(
                    Finding(
                        rule_id=rule.id,
                        category=rule.category,
                        severity=rule.severity,
                        matched_text=m.group(0),
                        description=rule.description,
                        remediation=rule.remediation,
                    )
                )
                seen_ids.add(rule.id)
                break
    return findings
