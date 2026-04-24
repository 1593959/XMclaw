"""SkillScanner — pre-install safety scan for skill source files.

The GuardedToolProvider stops *tool calls* at runtime; SkillScanner
stops dangerous *skills* at install / promotion time.  Skills land from
two paths that both deserve a second look before they run:

1. Human-authored skills the user drops into ``~/.xmclaw/skills/``.
2. Skills the EvolutionController proposes — LLM-generated, never
   reviewed by a human.

The scanner runs two layers over a skill's Python source:

- **Regex layer** — re-uses the :mod:`xmclaw.security.rule_loader` YAML
  catalogue (same packs the RuleBasedToolGuardian uses) so every
  signature we already curate for tool inputs also applies to skill
  source.
- **AST layer** — catches the handful of smoking-gun Python patterns
  that are near-universally malicious in a skill context: dynamic
  ``eval`` / ``exec`` / ``compile``, ``__import__`` of non-literals,
  ``os.system`` / ``os.popen``, ``subprocess.*`` with
  ``shell=True``, ``pickle.loads`` / ``marshal.loads`` /
  ``shelve.open``, and imports of ``ctypes`` / ``pty`` /
  ``telnetlib``.  These are intentionally narrow — false positives
  here block real work.

Pure library: no daemon state, no bus, no network.  CLI (`xmclaw
security scan`) and SkillForge both call it the same way.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

from xmclaw.security.rule_loader import Finding as _RuleFinding
from xmclaw.security.rule_loader import Severity as _RuleSeverity
from xmclaw.security.rule_loader import scan_with_rules
from xmclaw.security.tool_guard.models import GuardFinding, GuardSeverity


# AST-layer smoking guns.  Each entry is (rule_id, title, severity,
# matcher).  Matcher is called per ast.Call / ast.Import node and
# returns a finding description or None.  Keep this list short —
# false positives here block real skills.

_DANGEROUS_CALLS: dict[str, tuple[str, GuardSeverity, str]] = {
    # call_name -> (rule_id, severity, description)
    "eval":          ("SKILL_AST_EVAL", GuardSeverity.CRITICAL, "Dynamic code execution via eval()"),
    "exec":          ("SKILL_AST_EXEC", GuardSeverity.CRITICAL, "Dynamic code execution via exec()"),
    "compile":       ("SKILL_AST_COMPILE", GuardSeverity.HIGH, "Dynamic compile() — often paired with exec"),
    "__import__":    ("SKILL_AST_DYN_IMPORT", GuardSeverity.HIGH, "__import__() used directly — likely obfuscated import"),
}

# ``os.<name>`` / ``subprocess.<name>`` / etc. — the attribute-call
# shape ``module.name(args)``.  Value of the dict is
# (rule_id, severity, description, shell_true_required).
_DANGEROUS_ATTR_CALLS: dict[tuple[str, str], tuple[str, GuardSeverity, str, bool]] = {
    ("os", "system"):            ("SKILL_AST_OS_SYSTEM", GuardSeverity.CRITICAL, "os.system() runs a shell command", False),
    ("os", "popen"):             ("SKILL_AST_OS_POPEN", GuardSeverity.HIGH, "os.popen() runs a shell command", False),
    ("subprocess", "Popen"):     ("SKILL_AST_SUBPROCESS_SHELL", GuardSeverity.HIGH, "subprocess.Popen(shell=True) enables shell injection", True),
    ("subprocess", "run"):       ("SKILL_AST_SUBPROCESS_SHELL", GuardSeverity.HIGH, "subprocess.run(shell=True) enables shell injection", True),
    ("subprocess", "call"):      ("SKILL_AST_SUBPROCESS_SHELL", GuardSeverity.HIGH, "subprocess.call(shell=True) enables shell injection", True),
    ("subprocess", "check_call"): ("SKILL_AST_SUBPROCESS_SHELL", GuardSeverity.HIGH, "subprocess.check_call(shell=True) enables shell injection", True),
    ("subprocess", "check_output"): ("SKILL_AST_SUBPROCESS_SHELL", GuardSeverity.HIGH, "subprocess.check_output(shell=True) enables shell injection", True),
    ("pickle", "loads"):         ("SKILL_AST_PICKLE_LOADS", GuardSeverity.HIGH, "pickle.loads() deserializes arbitrary objects", False),
    ("pickle", "load"):          ("SKILL_AST_PICKLE_LOADS", GuardSeverity.HIGH, "pickle.load() deserializes arbitrary objects", False),
    ("marshal", "loads"):        ("SKILL_AST_MARSHAL_LOADS", GuardSeverity.HIGH, "marshal.loads() can instantiate malicious code objects", False),
    ("shelve", "open"):          ("SKILL_AST_SHELVE_OPEN", GuardSeverity.MEDIUM, "shelve uses pickle internally", False),
}

_DANGEROUS_IMPORTS: dict[str, tuple[str, GuardSeverity, str]] = {
    "ctypes":    ("SKILL_AST_IMPORT_CTYPES", GuardSeverity.HIGH, "ctypes allows calling arbitrary native code"),
    "pty":       ("SKILL_AST_IMPORT_PTY", GuardSeverity.HIGH, "pty enables interactive shell attachment"),
    "telnetlib": ("SKILL_AST_IMPORT_TELNETLIB", GuardSeverity.MEDIUM, "telnetlib is a classic exfiltration channel"),
}


@dataclass(frozen=True)
class SkillScanResult:
    """Outcome of :func:`scan_source` or :func:`scan_skill`."""
    path: str
    findings: list[GuardFinding] = field(default_factory=list)
    parse_error: str | None = None

    @property
    def is_safe(self) -> bool:
        """No findings above LOW.  Parse errors are not themselves
        findings — they mark the file as unanalyzable, which the caller
        may choose to treat as unsafe."""
        return not self.findings or all(
            f.severity in (GuardSeverity.LOW, GuardSeverity.INFO, GuardSeverity.SAFE)
            for f in self.findings
        )

    @property
    def max_severity(self) -> GuardSeverity:
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


def _rule_severity_to_guard(sev: _RuleSeverity) -> GuardSeverity:
    if sev.value == "critical":
        return GuardSeverity.CRITICAL
    if sev.value == "high":
        return GuardSeverity.HIGH
    if sev.value == "medium":
        return GuardSeverity.MEDIUM
    return GuardSeverity.LOW


def _regex_findings(source: str, filename: str) -> list[GuardFinding]:
    findings: list[GuardFinding] = []
    for rf in scan_with_rules(source):
        findings.append(_wrap_rule(rf, filename))
    return findings


def _wrap_rule(rf: _RuleFinding, filename: str) -> GuardFinding:
    return GuardFinding(
        rule_id=rf.rule_id,
        category=rf.category,
        severity=_rule_severity_to_guard(rf.severity),
        title=rf.rule_id,
        description=rf.description or rf.matched_text,
        tool_name=filename,
        param_name="source",
        matched_value=rf.matched_text,
        remediation=rf.remediation,
        guardian="skill_scanner.regex",
    )


def _has_shell_true_kwarg(node: ast.Call) -> bool:
    for kw in node.keywords:
        if kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
            return True
    return False


def _ast_findings(source: str, filename: str) -> list[GuardFinding]:
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as exc:
        return [
            GuardFinding(
                rule_id="SKILL_AST_SYNTAX_ERROR",
                category="obfuscation",
                severity=GuardSeverity.MEDIUM,
                title="Skill has a syntax error",
                description=f"Parse failed at line {exc.lineno}: {exc.msg}",
                tool_name=filename,
                param_name="source",
                matched_value="",
                remediation="Fix the syntax error; scanner cannot analyze unparseable files",
                guardian="skill_scanner.ast",
            )
        ]

    findings: list[GuardFinding] = []

    for node in ast.walk(tree):
        # Bare name calls: eval, exec, compile, __import__
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            name = node.func.id
            if name in _DANGEROUS_CALLS:
                rule_id, sev, desc = _DANGEROUS_CALLS[name]
                findings.append(
                    GuardFinding(
                        rule_id=rule_id,
                        category="code_execution",
                        severity=sev,
                        title=desc,
                        description=f"{desc} at line {node.lineno}",
                        tool_name=filename,
                        param_name="source",
                        matched_value=name,
                        remediation="Remove dynamic-execution primitives; use explicit imports and calls",
                        guardian="skill_scanner.ast",
                    )
                )

        # Attribute calls: os.system, subprocess.run(shell=True), ...
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            attr = node.func.attr
            if isinstance(node.func.value, ast.Name):
                mod = node.func.value.id
                key = (mod, attr)
                if key in _DANGEROUS_ATTR_CALLS:
                    rule_id, sev, desc, needs_shell_true = _DANGEROUS_ATTR_CALLS[key]
                    if needs_shell_true and not _has_shell_true_kwarg(node):
                        continue
                    findings.append(
                        GuardFinding(
                            rule_id=rule_id,
                            category="code_execution",
                            severity=sev,
                            title=desc,
                            description=f"{desc} at line {node.lineno}",
                            tool_name=filename,
                            param_name="source",
                            matched_value=f"{mod}.{attr}",
                            remediation="Replace with a safer alternative or explicit tool call",
                            guardian="skill_scanner.ast",
                        )
                    )

        # Dangerous imports
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".", 1)[0]
                if top in _DANGEROUS_IMPORTS:
                    rule_id, sev, desc = _DANGEROUS_IMPORTS[top]
                    findings.append(
                        GuardFinding(
                            rule_id=rule_id,
                            category="unauthorized_tool_use",
                            severity=sev,
                            title=desc,
                            description=f"import {alias.name} at line {node.lineno}",
                            tool_name=filename,
                            param_name="source",
                            matched_value=alias.name,
                            remediation="Remove the import or justify in the skill manifest",
                            guardian="skill_scanner.ast",
                        )
                    )
        if isinstance(node, ast.ImportFrom):
            top = (node.module or "").split(".", 1)[0]
            if top in _DANGEROUS_IMPORTS:
                rule_id, sev, desc = _DANGEROUS_IMPORTS[top]
                findings.append(
                    GuardFinding(
                        rule_id=rule_id,
                        category="unauthorized_tool_use",
                        severity=sev,
                        title=desc,
                        description=f"from {node.module} import ... at line {node.lineno}",
                        tool_name=filename,
                        param_name="source",
                        matched_value=node.module or "",
                        remediation="Remove the import or justify in the skill manifest",
                        guardian="skill_scanner.ast",
                    )
                )

    return findings


def scan_source(source: str, filename: str = "<string>") -> SkillScanResult:
    """Scan raw Python source.  Returns findings from both regex + AST
    layers.  Does not raise — parse failure is reported as a
    :class:`GuardFinding` with rule ``SKILL_AST_SYNTAX_ERROR`` so the
    caller can decide whether to fail closed."""
    findings = _regex_findings(source, filename)
    findings.extend(_ast_findings(source, filename))
    return SkillScanResult(path=filename, findings=findings)


def scan_skill(path: str | Path) -> SkillScanResult:
    """Scan a single Python file on disk.

    Missing files and non-text files are reported as findings (not
    raised) so callers iterating over a directory don't have to
    catch OSError everywhere.
    """
    p = Path(path)
    try:
        source = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return SkillScanResult(
            path=str(p),
            findings=[],
            parse_error=f"file not found: {p}",
        )
    except UnicodeDecodeError:
        return SkillScanResult(
            path=str(p),
            findings=[
                GuardFinding(
                    rule_id="SKILL_NOT_UTF8",
                    category="obfuscation",
                    severity=GuardSeverity.MEDIUM,
                    title="Skill source is not valid UTF-8",
                    description=f"{p} cannot be decoded as UTF-8",
                    tool_name=str(p),
                    param_name="source",
                    matched_value="",
                    remediation="Skills must be UTF-8 Python source",
                    guardian="skill_scanner",
                )
            ],
        )
    return scan_source(source, filename=str(p))


def scan_directory(path: str | Path) -> list[SkillScanResult]:
    """Recursively scan every ``*.py`` under *path* (sorted for
    deterministic output).  Empty directories return ``[]``."""
    root = Path(path)
    if not root.exists():
        return []
    results: list[SkillScanResult] = []
    py_files = sorted(root.rglob("*.py")) if root.is_dir() else [root]
    for py in py_files:
        if py.is_file():
            results.append(scan_skill(py))
    return results
