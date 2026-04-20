"""Risk assessment for evolution artifacts.

Phase E7 gate. Runs AFTER validation passes but BEFORE the engine moves
a shadow artifact into the active directory. An artifact deemed HIGH
risk is held in ``needs_approval`` status until a human confirms — the
engine emits ``EVOLUTION_APPROVAL_REQUESTED`` with the reasons so the UI
can render a decision prompt.

Why gate after validation rather than before? Because pre-validation
rejection paths (safety policy, coherence, VFM) are for artifacts that
are *broken*. E7 is for artifacts that are *well-formed but risky*: the
code compiles, the trigger matches something, the VFM score is high
enough — yet we still want a human signoff because of what the artifact
*does*. Running the risk scan last keeps the cheap rejects fast.

Pure functions, no I/O. Each returns a ``(level, reasons)`` tuple:

* ``level`` is one of ``"low"``, ``"medium"``, ``"high"``. Only ``"high"``
  triggers the approval gate. ``"medium"`` is advisory — surfaced in
  journal metadata but not blocking.
* ``reasons`` is a list of short slugs for UI badges and journal
  filtering. Empty for ``"low"``.

Heuristics bias toward false positives. A false-positive approval
prompt is a minor annoyance for the user; a false negative means a
dangerous artifact auto-promotes silently. We would rather over-ask.
"""
from __future__ import annotations

import re
from typing import Any

RiskLevel = str  # one of "low", "medium", "high"

# Substrings in a skill's action_body that indicate code capable of
# mutating the host environment, executing shell, or exfiltrating data.
# Exact-substring match — these are deliberate red flags. The forge's
# LLM almost never emits them in a trivial skill, so hits are signal.
_DANGEROUS_CODE_SUBSTRINGS: tuple[str, ...] = (
    "subprocess",
    "os.system",
    "os.remove",
    "os.unlink",
    "os.rmdir",
    "shutil.rmtree",
    "__import__",
    "eval(",
    "exec(",
    "compile(",
    "pickle.loads",
    "marshal.loads",
    # Network calls. We don't forbid HTTP in skills wholesale (some
    # legitimate skills will need it), but the LLM should say so
    # explicitly — a skill that reaches out without the user knowing
    # is exactly what the gate exists to catch.
    "requests.post",
    "requests.put",
    "requests.delete",
    "urllib.request.urlopen",
    "socket.socket",
    # Raw shell in user-visible tools.
    "rm -rf",
    "rm -r",
    "curl ",
    "wget ",
)

# Keywords in name / description that signal sensitive domain material.
# Case-insensitive substring match.
_SENSITIVE_DOMAIN_KEYWORDS: tuple[str, ...] = (
    "password", "passwd", "credential", "secret", "token", "api_key",
    "apikey", "private_key",
    "payment", "credit_card", "creditcard", "bank",
    "ssn", "social_security",
    "sudo", "root", "chmod", "chown",
    "delete_all", "drop_table", "truncate",
    # Chinese equivalents used by the insight extractor.
    "密码", "密钥", "支付", "银行",
)

# Gene priority ≥ this overrides most sibling genes. Reviewers should
# see a confirmation before such a gene goes live.
_HIGH_PRIORITY_CUTOFF = 8

# Regex patterns so broad they effectively match everything. A gene with
# such a trigger runs on every turn, which is usually not what the
# reflection step intended.
_GREEDY_REGEX_PATTERNS: frozenset[str] = frozenset({
    ".*", ".+", "^$", "^.*$", "^.+$", "", "(?s).*",
})


def _code_hits(action_body: str) -> list[str]:
    if not action_body:
        return []
    body_lower = action_body.lower()
    return [s for s in _DANGEROUS_CODE_SUBSTRINGS if s.lower() in body_lower]


def _sensitive_hits(text: str) -> list[str]:
    if not text:
        return []
    lo = text.lower()
    return [kw for kw in _SENSITIVE_DOMAIN_KEYWORDS if kw in lo]


def assess_skill_risk(
    concept: dict[str, Any],
    action_body: str | None = None,
) -> tuple[RiskLevel, list[str]]:
    """Rate a skill concept + its forged action body.

    The concept carries ``name`` / ``description``; the action_body is the
    raw Python fragment the forge produced. We scan both because a skill
    can be dangerous via intent (description mentions sensitive data) OR
    via implementation (code opens a subprocess).
    """
    reasons: list[str] = []

    code_hits = _code_hits(action_body or "")
    for h in code_hits:
        reasons.append(f"code:{h.strip().replace(' ', '_')}")

    combined_text = " ".join(
        str(concept.get(k) or "") for k in ("name", "description")
    )
    sensitive = _sensitive_hits(combined_text)
    for kw in sensitive:
        reasons.append(f"domain:{kw}")

    if reasons:
        return "high", reasons
    return "low", []


def assess_gene_risk(concept: dict[str, Any]) -> tuple[RiskLevel, list[str]]:
    """Rate a gene concept.

    Genes don't run code, but they DO modify agent behavior on every
    matching turn. The risk signals are therefore structural: an
    overly-greedy regex that matches everything, or an unusually high
    priority that shadows existing genes.
    """
    reasons: list[str] = []

    trigger_type = str(concept.get("trigger_type") or "keyword").lower()
    trigger = str(concept.get("trigger") or "").strip()

    if trigger_type == "regex":
        # Treat both the literal pattern and a compiled form that matches
        # the empty string as over-greedy. re.fullmatch("", "") succeeds
        # for ".*" and friends — that's our test.
        if trigger in _GREEDY_REGEX_PATTERNS:
            reasons.append("regex:greedy_literal")
        else:
            try:
                if re.fullmatch(trigger, "") is not None:
                    reasons.append("regex:matches_empty_string")
            except re.error:
                # Uncompilable regex is already rejected upstream by
                # safety_policy; if one reaches us we err on the safe side.
                reasons.append("regex:uncompilable")

    priority = concept.get("priority", 5)
    try:
        prio_int = int(priority)
    except (TypeError, ValueError):
        prio_int = 5
    if prio_int >= _HIGH_PRIORITY_CUTOFF:
        reasons.append(f"priority:{prio_int}")

    combined_text = " ".join(
        str(concept.get(k) or "") for k in ("name", "description", "action")
    )
    for kw in _sensitive_hits(combined_text):
        reasons.append(f"domain:{kw}")

    if reasons:
        return "high", reasons
    return "low", []
