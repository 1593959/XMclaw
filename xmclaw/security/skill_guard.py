"""Skill-content static scanner — runs before SKILL.md content lands
in the agent's system prompt.

Hermes' ``tools/skills_guard.py`` does this for community-installed
skills; we adapt the pattern for **agent-generated** SKILL.md files.
xm-auto-evo is autonomous, so in principle it could emit a SKILL.md
that tells the agent to ``rm -rf /`` or to ``curl ... | bash`` or to
hand over the user's API key. We scan defensively before the loader
returns content for prompt injection.

Two layers:

1. **Reuse ``prompt_scanner``** — its 29 patterns already cover
   jailbreak / role_forgery / exfiltration / tool_hijack / indirect
   injection vectors. SKILL.md is technically prose-shaped tool
   output (the LLM reads it), so the same patterns apply.

2. **Add destructive-shell patterns** that are specific to procedure-
   style skills which tell the agent to RUN commands. ``rm -rf /``
   inside a SKILL would route through the agent's bash tool.

Returns a ``SkillScanResult`` with verdict ``safe`` / ``caution`` /
``dangerous`` plus a list of findings. Trust-based policy
(``apply_policy``) decides whether to allow the skill into the
prompt. Auto-generated skills ("agent-created" trust level) get the
loosest policy: caution allowed, dangerous blocked.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from xmclaw.security.prompt_scanner import scan_text, Severity


class Verdict(str, Enum):
    SAFE = "safe"
    CAUTION = "caution"
    DANGEROUS = "dangerous"


class TrustLevel(str, Enum):
    BUILTIN = "builtin"          # Bundled with XMclaw — always trust
    TRUSTED = "trusted"          # User explicitly installed from a known source
    AGENT_CREATED = "agent-created"  # auto_evo wrote it
    COMMUNITY = "community"      # External / unverified


# Trust × Verdict → action.
#   "allow" — load into prompt
#   "block" — drop the skill, agent never sees it
#   "warn"  — load but emit an event so the user sees it in /trace
_POLICY: dict[TrustLevel, dict[Verdict, str]] = {
    TrustLevel.BUILTIN:       {Verdict.SAFE: "allow",  Verdict.CAUTION: "allow",  Verdict.DANGEROUS: "allow"},
    TrustLevel.TRUSTED:       {Verdict.SAFE: "allow",  Verdict.CAUTION: "allow",  Verdict.DANGEROUS: "block"},
    TrustLevel.AGENT_CREATED: {Verdict.SAFE: "allow",  Verdict.CAUTION: "warn",   Verdict.DANGEROUS: "block"},
    TrustLevel.COMMUNITY:     {Verdict.SAFE: "allow",  Verdict.CAUTION: "block",  Verdict.DANGEROUS: "block"},
}


@dataclass(frozen=True, slots=True)
class SkillFinding:
    pattern_id: str
    severity: str        # "critical" | "high" | "medium" | "low"
    category: str        # "destructive" | "exfiltration" | "injection" | ...
    match: str
    description: str = ""


@dataclass(frozen=True, slots=True)
class SkillScanResult:
    verdict: Verdict
    findings: tuple[SkillFinding, ...] = field(default_factory=tuple)
    summary: str = ""

    @property
    def is_safe(self) -> bool:
        return self.verdict == Verdict.SAFE

    @property
    def is_blockable(self) -> bool:
        return self.verdict == Verdict.DANGEROUS


# Destructive-shell patterns specific to SKILL.md procedure bodies.
# These complement prompt_scanner's tool_hijack family — the
# distinction: prompt_scanner catches ATTEMPTS to coerce the agent
# (e.g. "now run curl ... | bash"); these catch the agent literally
# running such a command if the SKILL says to.
_DESTRUCTIVE = [
    # rm -rf — only flag when targeting roots / user dirs.
    (
        r"\brm\s+(?:-[A-Za-z]*[rR][A-Za-z]*[fF][A-Za-z]*|-[A-Za-z]*[fF][A-Za-z]*[rR][A-Za-z]*)\s+"
        r"(?:/|\$HOME|~|/Users/|/home/|C:[\\\/])",
        "rm_rf_root", "critical", "destructive",
        "rm -rf targeting root or user directory",
    ),
    # Format / mkfs
    (
        r"\b(?:mkfs|format)\s+",
        "format_disk", "critical", "destructive",
        "filesystem format command",
    ),
    # Disk-clobbering dd
    (
        r"\bdd\s+if=[^\s]+\s+of=/dev/(?:sd[a-z]|nvme|disk)",
        "dd_disk", "critical", "destructive",
        "dd writing to a raw disk device",
    ),
    # Recursive chmod 777 on big paths
    (
        r"\bchmod\s+-R\s+777\s+(?:/|\$HOME|~|/Users/|/home/)",
        "chmod_777_root", "high", "destructive",
        "recursive chmod 777 on root/home — privilege escalation surface",
    ),
    # Force-push to main / master
    (
        r"\bgit\s+push\s+(?:--force|-f)\s+\S+\s+(?:main|master|HEAD)",
        "force_push_main", "high", "destructive",
        "git force-push to main/master — destroys remote history",
    ),
    # Nuke .git
    (
        r"\brm\s+(?:-[A-Za-z]*[rR][A-Za-z]*[fF][A-Za-z]*|-[A-Za-z]*[fF][A-Za-z]*[rR][A-Za-z]*)\s+\.git",
        "rm_dotgit", "high", "destructive",
        "deletes the .git directory",
    ),
    # Curl piped to interpreter (destructive only when explicit)
    (
        r"\bcurl\s+[^|\n]*\|\s*(?:bash|sh|zsh|python|perl|node|ruby)",
        "curl_pipe_shell", "critical", "destructive",
        "remote-code-execution via curl piped into shell",
    ),
    # Persistence via cron / systemd / authorized_keys
    (
        r"(?:crontab\s+-\s*<|>>\s*~/\.crontab|systemctl\s+enable|"
        r">>\s*~/\.bashrc|>>\s*~/\.zshrc|>>\s*~/\.ssh/authorized_keys)",
        "persistence", "high", "destructive",
        "writes to a persistence vector",
    ),
    # Password-DB dump
    (
        r"(?:cat|less|head|tail)\s+(?:/etc/shadow|/etc/passwd)\b",
        "shadow_dump", "high", "exfiltration",
        "reads /etc/shadow or /etc/passwd",
    ),
]


_DESTRUCTIVE_COMPILED = [
    (re.compile(p, re.IGNORECASE), pid, sev, cat, desc)
    for (p, pid, sev, cat, desc) in _DESTRUCTIVE
]


def _verdict_for(findings: list[SkillFinding]) -> Verdict:
    """Aggregate severity → overall verdict.

    * Any ``critical`` finding → DANGEROUS
    * Any ``high`` (but no critical) → CAUTION
    * Otherwise → SAFE
    """
    if not findings:
        return Verdict.SAFE
    severities = {f.severity for f in findings}
    if "critical" in severities:
        return Verdict.DANGEROUS
    if "high" in severities:
        return Verdict.CAUTION
    return Verdict.SAFE


def scan_skill_content(content: str) -> SkillScanResult:
    """Static-scan SKILL.md body. Returns a verdict + findings.

    Combines:
      - the project's prompt_scanner (29 patterns)
      - this module's destructive-shell patterns
    """
    if not content:
        return SkillScanResult(verdict=Verdict.SAFE)

    findings: list[SkillFinding] = []

    # Layer 1: prompt-injection scanner (already catches role forgery,
    # exfiltration, jailbreak, indirect injection, tool-hijack ssh
    # patterns, etc.).
    pscan = scan_text(content)
    for pf in pscan.findings:
        sev_str = "critical" if pf.severity == Severity.HIGH else (
            "high" if pf.severity == Severity.MEDIUM else "medium"
        )
        # NOTE: prompt_scanner uses HIGH/MEDIUM/LOW; we elevate HIGH→
        # critical because SKILL.md is privileged content the agent
        # WILL act on.
        findings.append(SkillFinding(
            pattern_id=pf.pattern_id,
            severity=sev_str,
            category=pf.category,
            match=pf.match[:120],
            description=f"prompt_scanner: {pf.category}/{pf.pattern_id}",
        ))

    # Layer 2: destructive-shell patterns.
    for rx, pid, sev, cat, desc in _DESTRUCTIVE_COMPILED:
        for m in rx.finditer(content):
            findings.append(SkillFinding(
                pattern_id=pid,
                severity=sev,
                category=cat,
                match=m.group(0)[:120],
                description=desc,
            ))

    verdict = _verdict_for(findings)
    cats = sorted({f.category for f in findings})
    summary = f"{verdict.value}: {len(findings)} findings ({', '.join(cats) or 'none'})"
    return SkillScanResult(
        verdict=verdict,
        findings=tuple(findings),
        summary=summary,
    )


def apply_policy(
    result: SkillScanResult, *, trust: TrustLevel = TrustLevel.AGENT_CREATED,
) -> tuple[str, str]:
    """Map (trust, verdict) → action + reason.

    Returns ``(action, reason)`` where action is one of:
      - ``"allow"`` — let the skill into the prompt
      - ``"warn"``  — let it in, surface a warning (logs / events)
      - ``"block"`` — drop the skill entirely
    """
    action = _POLICY[trust][result.verdict]
    if action == "allow":
        reason = f"trust={trust.value} verdict={result.verdict.value}"
    else:
        # Pick the worst finding to summarise the reason.
        worst = sorted(
            result.findings,
            key=lambda f: {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(f.severity, 9),
        )
        sample = worst[0] if worst else None
        if sample:
            reason = (
                f"{action} (trust={trust.value} verdict={result.verdict.value}; "
                f"{sample.severity}/{sample.category}/{sample.pattern_id}: "
                f"{sample.match[:60]!r})"
            )
        else:
            reason = f"{action} (trust={trust.value} verdict={result.verdict.value})"
    return action, reason
