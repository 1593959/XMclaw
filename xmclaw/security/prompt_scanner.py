"""Prompt-injection scanner for untrusted context (Epic #14).

Scans a string of external content — a tool result, a web-fetched page, a
user-owned file that the agent is about to quote — for known prompt-
injection attacks *before* it lands in the conversation history that feeds
the next LLM request.

Design:

* **Pure function.** ``scan_text(text)`` returns a :class:`ScanResult`; the
  caller (AgentLoop) decides how to react. Makes the scanner trivially
  unit-testable without mocking a bus or a turn.

* **Findings, not verdicts.** Each hit is a :class:`Finding` with a stable
  ``pattern_id``, the matched span, and a severity. The caller maps
  finding severity to its policy (detect / redact / block).

* **Two-axis detection.** Regex patterns catch the documented attack
  phrases ("ignore previous instructions", fake system markers, credential-
  exfiltration asks). A separate unicode pass catches zero-width / bidi
  characters that would render invisibly to a human reviewer but still
  reach the model.

* **Policy enum.** :class:`PolicyMode` is the contract the config +
  AgentLoop speak — the scanner itself is policy-agnostic.

No third-party imports: this runs on every tool result, so the cost must
stay well under a millisecond even for 100-KB outputs.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class PolicyMode(str, Enum):
    """What to do when a finding meets or exceeds the configured threshold.

    * ``DETECT_ONLY`` — emit an event but pass the original text through
      unchanged. Sensible default while we collect baseline noise.
    * ``REDACT`` — replace each finding's matched span with a stable
      placeholder (``[redacted:<pattern_id>]``). The LLM sees the
      redaction, not the attack payload.
    * ``BLOCK`` — short-circuit the turn: AgentLoop records an
      ``ANTI_REQ_VIOLATION`` and returns without continuing the hop.
    """

    DETECT_ONLY = "detect_only"
    REDACT = "redact"
    BLOCK = "block"

    @classmethod
    def parse(cls, raw: str | None, *, default: "PolicyMode" = None) -> "PolicyMode":
        """Lenient parse — accepts ``None`` / unknown values and falls back
        to ``default`` (or ``DETECT_ONLY``). Keeps config parsing here so
        every callsite spells the contract identically."""
        fallback = default or PolicyMode.DETECT_ONLY
        if raw is None:
            return fallback
        s = raw.strip().lower()
        for m in cls:
            if m.value == s:
                return m
        return fallback


class Severity(str, Enum):
    """Finding severity. Threshold comparisons use the enum's index in the
    declared order, so keep high → higher."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


_SEVERITY_ORDER = {s: i for i, s in enumerate(Severity)}


def _sev_ge(a: Severity, b: Severity) -> bool:
    return _SEVERITY_ORDER[a] >= _SEVERITY_ORDER[b]


@dataclass(frozen=True, slots=True)
class Finding:
    """One match. ``span`` is a half-open ``[start, end)`` into the input.

    ``pattern_id`` is the stable machine handle used in events, redaction
    placeholders, and severity filters — the regex source may change for
    tuning without breaking dashboards that key on the id.
    """

    pattern_id: str
    severity: Severity
    span: tuple[int, int]
    match: str
    category: str  # "instruction_override" | "role_forgery" | ... (see below)


@dataclass(frozen=True, slots=True)
class ScanResult:
    """Everything ``scan_text`` returns. Iterate ``findings`` to decide."""

    findings: tuple[Finding, ...] = ()
    invisible_chars: int = 0          # count of stripped zero-width / bidi chars
    scanned_length: int = 0

    @property
    def any_high(self) -> bool:
        return any(f.severity == Severity.HIGH for f in self.findings)

    @property
    def any_findings(self) -> bool:
        return bool(self.findings) or self.invisible_chars > 0

    def categories(self) -> list[str]:
        """Unique categories, preserving first-seen order. Used in event
        payloads so consumers can group without re-parsing findings."""
        seen: list[str] = []
        for f in self.findings:
            if f.category not in seen:
                seen.append(f.category)
        return seen


# ── Regex catalogue ─────────────────────────────────────────────────────
#
# One rule per line so future additions can credit their source and bump
# the catalogue's provenance in a single diff. Patterns are compiled with
# IGNORECASE + DOTALL. Keep them focused — false positives on a friendly
# README would poison the policy's signal.
#
# Provenance: the initial set is adapted from the public Hermes
# ``_CONTEXT_THREAT_PATTERNS`` (MIT-licensed) and extended with a few
# data-exfiltration asks seen in 2025-Q1 red-team reports.


@dataclass(frozen=True, slots=True)
class _PatternSpec:
    pattern_id: str
    regex: re.Pattern[str]
    severity: Severity
    category: str


def _compile(
    pattern_id: str, regex: str, severity: Severity, category: str,
) -> _PatternSpec:
    return _PatternSpec(
        pattern_id=pattern_id,
        regex=re.compile(regex, re.IGNORECASE | re.DOTALL),
        severity=severity,
        category=category,
    )


# "instruction_override" — the classic "forget everything, do X instead"
# ask. High severity: if it hits verbatim in a tool output, something
# hostile is in the loop.
_INSTRUCTION_OVERRIDE = [
    _compile(
        "ignore_previous",
        r"\bignore\s+(?:(?:all|the|any|every)\s+)?(?:previous|prior|above|earlier)\s+"
        r"(?:instruction|message|prompt|direction|rule|constraint)s?\b",
        Severity.HIGH, "instruction_override",
    ),
    _compile(
        "disregard_prior",
        r"\bdisregard\s+(?:all|the|any|everything|your)\s+(?:previous|prior|above|earlier)\s+"
        r"(?:instruction|message|prompt|context|rule)s?\b",
        Severity.HIGH, "instruction_override",
    ),
    _compile(
        "forget_instructions",
        r"\bforget\s+(?:all|your|the|any)\s+(?:previous|prior|above|earlier)?\s*"
        r"(?:instruction|message|prompt|rule|constraint)s?\b",
        Severity.HIGH, "instruction_override",
    ),
    _compile(
        "override_system",
        r"\boverride\s+(?:the\s+)?(?:system|previous)\s+"
        r"(?:prompt|instruction|message|rule)s?\b",
        Severity.HIGH, "instruction_override",
    ),
]

# "role_forgery" — the attacker drops fake chat-template markers to make
# their content look like a system/developer turn. Medium by default
# because documentation *about* these markers is legitimate; combined
# with instruction_override hits it becomes high-risk.
_ROLE_FORGERY = [
    _compile(
        "openai_im_start",
        r"<\|im_start\|>\s*(?:system|developer|assistant)\b",
        Severity.HIGH, "role_forgery",
    ),
    _compile(
        "anthropic_human_tag",
        r"\n\s*(?:\u0001|\x00)?\s*(?:Human|Assistant|System)\s*:\s*[A-Z]",
        Severity.MEDIUM, "role_forgery",
    ),
    _compile(
        "inst_block",
        r"\[INST\][\s\S]{0,400}?\[/INST\]",
        Severity.MEDIUM, "role_forgery",
    ),
    _compile(
        "xml_system",
        r"<\s*system\s*>[\s\S]{0,400}?<\s*/\s*system\s*>",
        Severity.MEDIUM, "role_forgery",
    ),
    _compile(
        "new_instructions_header",
        r"(?:^|\n)\s*(?:###?\s*)?(?:new|updated|revised)\s+"
        r"(?:instruction|system\s+prompt|directive)s?\s*:\s*",
        Severity.MEDIUM, "role_forgery",
    ),
]

# "exfiltration" — asks that try to smuggle secrets out via the agent's
# legitimate tool surface. High severity: a tool output that tells the
# agent to POST an api_key somewhere is always a red flag.
_EXFILTRATION = [
    _compile(
        "reveal_secrets",
        r"\b(?:reveal|show|print|send|email|upload|post|leak|dump|exfiltrate)\s+"
        r"(?:your|the|all)?\s*"
        r"(?:system\s+prompt|api[_\s]*key|password|credential|secret|token|"
        r"private\s+key|.env|env\s+var|environment\s+variable)s?\b",
        Severity.HIGH, "exfiltration",
    ),
    _compile(
        "send_to_url",
        r"\b(?:send|POST|curl|fetch|exfiltrate)\s+(?:this|the|your|everything|"
        r"all\s+data)\s+to\s+(?:https?://|attacker|evil|exfil)",
        Severity.HIGH, "exfiltration",
    ),
]


_ALL_PATTERNS: tuple[_PatternSpec, ...] = tuple(
    _INSTRUCTION_OVERRIDE + _ROLE_FORGERY + _EXFILTRATION,
)


# ── Unicode pass ────────────────────────────────────────────────────────
#
# Zero-width and bidi-override characters render invisibly but still land
# in the model's tokens. Almost never appear in legitimate tool output; a
# single occurrence is already worth flagging.
#
# Deliberately does not include U+FEFF (BOM) because many windows-encoded
# files leak that into their text payload and we'd drown in false
# positives. If it shows up alongside an instruction_override we'll catch
# it on the regex side.

_INVISIBLE_CHARS = re.compile(
    "["
    "\u200b-\u200f"    # zero-width joiners, LTR/RTL marks
    "\u202a-\u202e"    # bidi embedding / override
    "\u2060-\u2064"    # word joiner family
    "\u2066-\u2069"    # isolate family
    "]",
)


# ── Public entry point ──────────────────────────────────────────────────


def scan_text(
    text: str,
    *,
    severity_threshold: Severity = Severity.LOW,
) -> ScanResult:
    """Scan ``text`` for prompt-injection patterns.

    Only findings at or above ``severity_threshold`` are returned. The
    default threshold (LOW) returns everything; callsites that want to
    suppress chatter about documentation of attack phrases can pass
    ``MEDIUM`` or ``HIGH``.
    """
    if not text:
        return ScanResult(scanned_length=0)

    findings: list[Finding] = []
    for spec in _ALL_PATTERNS:
        if not _sev_ge(spec.severity, severity_threshold):
            continue
        for m in spec.regex.finditer(text):
            findings.append(Finding(
                pattern_id=spec.pattern_id,
                severity=spec.severity,
                span=(m.start(), m.end()),
                match=m.group(0),
                category=spec.category,
            ))

    # Sort by span so the redactor can walk them deterministically.
    findings.sort(key=lambda f: f.span)

    invisibles = len(_INVISIBLE_CHARS.findall(text))
    return ScanResult(
        findings=tuple(findings),
        invisible_chars=invisibles,
        scanned_length=len(text),
    )


def redact(text: str, result: ScanResult) -> str:
    """Replace every finding span in ``text`` with a stable placeholder and
    strip invisible characters. Idempotent: scanning redacted output should
    produce zero findings.

    Walks findings right-to-left so earlier spans' indices stay valid while
    we splice.
    """
    if not result.any_findings:
        return text
    out = text
    # Right-to-left so we don't invalidate earlier spans.
    for f in sorted(result.findings, key=lambda x: x.span, reverse=True):
        start, end = f.span
        placeholder = f"[redacted:{f.pattern_id}]"
        out = out[:start] + placeholder + out[end:]
    if result.invisible_chars:
        out = _INVISIBLE_CHARS.sub("", out)
    return out
